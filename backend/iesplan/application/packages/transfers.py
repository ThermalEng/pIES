"""项目包传输编排用例(application/packages): 导出/导入/Excel 报告。

跨域编排（project/dataset/tasks/results/configuration/storage/audit）归属本层；
package 域仅保留包格式解析纯函数、下载授权签名、常量与行持久化。
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy.orm import Session

from iesplan import __version__
from iesplan import audit as audit_domain
from iesplan import configuration as configuration_domain
from iesplan import dataset as dataset_domain
from iesplan import project as project_domain
from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.configuration.revisions import (
    get_effective_finance_config as _get_effective_finance_config,
    get_finance_overrides as _get_finance_overrides,
    get_planning_config as _get_planning_config,
    register_finance_profile as _register_finance_profile,
    save_finance_overrides as _save_finance_overrides,
    save_planning_config as _save_planning_config,
    set_project_finance_profile as _set_project_finance_profile,
)
from iesplan.application.projects.content_objects import (
    load_content_object as _load_content_object,
    store_content_object as _store_content_object,
)
from iesplan.core.contracts import (
    ProjectBaseline,
    ProjectBaselineError,
)
from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from iesplan.core.jsonutil import jsonable
from iesplan.core.yamlmini import dump as yaml_dump
from iesplan.finance import FinanceOverrides, FinanceProfile
from iesplan.identity.contracts import UserRecord
from iesplan.package import (
    DOWNLOAD_TOKEN_TTL_SECONDS,
    PACKAGE_FORMAT_VERSION,
    PACKAGE_MEDIA_TYPE,
    ImportValidationError,
    PackageExport,
    create_download_token,
)
from iesplan.package import (
    bound_dataset_ids as _bound_dataset_ids,
)
from iesplan.package import create_proposal as _create_proposal
from iesplan.package import get_proposal as _get_proposal
from iesplan.package import list_proposals_for_proposer as _list_proposals_for_proposer
from iesplan.package import (
    media_file_kind as _media_file_kind,
)
from iesplan.package import parse_config_files as _parse_config_files
from iesplan.package import parse_evidence_content as _parse_evidence_content
from iesplan.package import parse_package as _parse_package
from iesplan.package import set_proposal_review as _set_proposal_review
from iesplan.package.contracts import ImportProposalRecord
from iesplan.project.contracts import DraftRecord, ProjectRecord, ProjectVersionRecord
from iesplan.storage import add_ref, get_object, object_info, put_object

#: 审计动作常量唯一所有者为 ``iesplan.audit`` 域门面(下文经 ``audit_domain``
#: 引用, 本层不复制)。


def _audit_entry(
    db: Session,
    actor_id: int | None,
    action: str,
    entity_type: str,
    entity_id: int,
    *,
    revision: int | None = None,
    result: dict | None = None,
    extra: dict | None = None,
) -> None:
    """经 audit 域门面追加不可变审计行(与业务写入同事务,只 INSERT)。"""
    audit_domain.append_entry(
        db,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        revision=revision,
        result=result,
        extra=extra,
    )


def _collect_datasets(db: Session, dataset_version_ids: list[int]) -> list[dict]:
    """收集数据集版本(元数据 + 文件对象内容), 供打包使用。

    返回 [{"dataset": DatasetRecord, "version": DatasetVersionRecord,
            "files": [{"file": DatasetFileRecord, "obj": dict(元数据), "content": bytes}]}],
    经 dataset 域公开门面读取, 不直接访问数据集表。
    """
    out: list[dict] = []
    for dvid in dict.fromkeys(int(i) for i in dataset_version_ids):
        version = dataset_domain.get_version(db, dvid)
        if version is None:
            continue
        dataset = dataset_domain.get_dataset(db, version.dataset_id)
        files: list[dict] = []
        for f in dataset_domain.list_files(db, version.id):
            try:
                obj = object_info(db, f.object_id)
            except NotFoundError:
                continue
            files.append({"file": f, "obj": obj, "content": get_object(db, obj["id"])})
        out.append({"dataset": dataset, "version": version, "files": files})
    return out


def _collect_evidence(db: Session, project_id: int) -> list[dict]:
    """收集项目历史结果证据与评估引用(经任务归属项目，domain-model §快照、任务和结果)。

    任务与快照经 tasks 域公开门面读取, 不直接访问任务表。
    """
    packages = results_domain.list_evidence_for_tasks(db, tasks_domain.list_task_ids(db, project_id))
    out: list[dict] = []
    for pkg in packages:
        task = tasks_domain.get_task(db, pkg.task_id)
        snapshot = tasks_domain.get_snapshot(db, pkg.calc_snapshot_id) if pkg.calc_snapshot_id else None
        assessments = results_domain.list_package_assessments(db, pkg.id)
        index = results_domain.list_package_index(db, pkg.id)
        content: bytes | None = None
        obj: dict | None = None
        try:
            obj = object_info(db, pkg.object_id)
            content = get_object(db, obj["id"])
        except NotFoundError:
            pass
        out.append(
            {
                "package": pkg,
                "task": task,
                "snapshot": snapshot,
                "assessments": assessments,
                "index": index,
                "object": obj,
                "content": content,
            }
        )
    return out


def _build_package_zip(
    db: Session,
    project: ProjectRecord,
    draft: DraftRecord,
    draft_content: dict,
) -> tuple[bytes, dict]:
    """组装项目包 zip 字节与清单(流式写入; 不含账号/权限/会话/密钥，见 domain-model §对象生命周期)。

    版本经 project 域公开门面按 version_no 升序读取, 不直接访问版本表。
    """
    versions = sorted(project_domain.list_versions(db, project.id), key=lambda v: v.version_no)

    # 数据集版本 id 全集: 当前草稿绑定 + 全部版本绑定 + 证据快照绑定
    dataset_ids: list[int] = list(_bound_dataset_ids(draft_content))
    version_contents: dict[int, dict] = {}
    for version in versions:
        content = _load_content_object(db, version.content_object_id)
        version_contents[version.version_no] = content
        dataset_ids.extend(_bound_dataset_ids(content))
    evidence_list = _collect_evidence(db, project.id)
    for item in evidence_list:
        snapshot = item["snapshot"]
        if snapshot is not None:
            dataset_ids.extend(int(i) for i in (snapshot.dataset_version_ids or []))
    dataset_list = _collect_datasets(db, dataset_ids)

    objects_manifest: list[dict[str, Any]] = []
    files_meta: dict[str, Any] = {
        "draft": {"revision": draft.revision},
        "versions": [],
        "datasets": [],
        "evidence": [],
        "configs": {},
    }

    def _add(path: str, data: bytes, media_type: str) -> None:
        """写入 zip 条目并登记对象清单(路径/大小/类型, 不登记内容摘要)。"""
        objects_manifest.append({"path": path, "size_bytes": len(data), "media_type": media_type})

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 项目元数据(不导出所有者/创建者等账号信息)
        project_meta = {
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "status": project.status,
            "currency": project.currency,
            "project_baseline": {
                "resolution": project.baseline_resolution,
                "leap_year": project.baseline_leap_year,
                "scenario_mode": project.baseline_scenario_mode,
            },
            "schema_version": project.schema_version,
            "created_at": project.created_at,
        }
        project_json = json.dumps(jsonable(project_meta), ensure_ascii=False, indent=2).encode()
        _add("project.json", project_json, "application/json")
        zf.writestr("project.json", project_json)

        # 当前草稿(领域内容, 命令簿记不外泄)
        content = {k: v for k, v in draft_content.items() if k != "applied_commands"}
        draft_json = json.dumps(
            {"revision": draft.revision, "content": content},
            ensure_ascii=False,
            indent=2,
        ).encode()
        _add("draft.json", draft_json, "application/json")
        zf.writestr("draft.json", draft_json)

        # 当前生效财务三件套/规划配置(0.6.5 条目 1-2): 仅导出当前生效
        # 快照为四个 YAML(finance_profile.yaml / finance_overrides.yaml /
        # effective_finance.yaml / planning_config.yaml; revision 追加历史属
        # 服务端状态, 不入包)。经配置服务按精确指针读取(2.6: 不做本地内容
        # 重算比对)。Profile/Overrides 为 authoring 输入, Effective 为合并器
        # 产物, 导入时从精确来源重新合并(finance-yaml.md)。
        configs_meta: dict[str, str] = {}
        if project.finance_profile_id is not None:
            profile_row = configuration_domain.get_profile_row(db, project.finance_profile_id)
            if profile_row is None:
                raise AppError(
                    "项目 FinanceProfile 指针损坏",
                    code="PROJ-FIN-003",
                    params={"project_id": project.id},
                )
            # 精确恢复(禁止 latest 猜测漂移)
            profile = FinanceProfile.from_dict(profile_row.content)
            profile_raw = yaml_dump(profile.to_dict()).encode("utf-8")
            _add("finance_profile.yaml", profile_raw, "application/yaml")
            zf.writestr("finance_profile.yaml", profile_raw)
            configs_meta["finance_profile"] = "finance_profile.yaml"
            # Overrides: 无覆盖时导出显式空覆盖文档
            overrides, _ = _get_finance_overrides(db, project.id)
            if overrides is None:
                empty = FinanceOverrides.empty_for_profile(profile)
                overrides = empty
            overrides_raw = yaml_dump(overrides.to_dict()).encode("utf-8")
            _add("finance_overrides.yaml", overrides_raw, "application/yaml")
            zf.writestr("finance_overrides.yaml", overrides_raw)
            configs_meta["finance_overrides"] = "finance_overrides.yaml"
            effective, _, _ = _get_effective_finance_config(db, project.id)
            effective_raw = yaml_dump(effective.to_dict()).encode("utf-8")
            _add("effective_finance.yaml", effective_raw, "application/yaml")
            zf.writestr("effective_finance.yaml", effective_raw)
            configs_meta["effective_finance"] = "effective_finance.yaml"
        if project.planning_revision is not None:
            planning, _, _ = _get_planning_config(db, project.id)
            planning_raw = yaml_dump(planning.to_dict()).encode("utf-8")
            _add("planning_config.yaml", planning_raw, "application/yaml")
            zf.writestr("planning_config.yaml", planning_raw)
            configs_meta["planning_config"] = "planning_config.yaml"
        if configs_meta:
            files_meta["configs"] = configs_meta

        # 项目版本(不可变, 版本内容 + 版本元数据; 不含创建者账号)
        for version in versions:
            path = f"versions/{version.version_no:04d}.json"
            version_doc = {
                "version": {
                    "version_no": version.version_no,
                    "name": version.name,
                    "description": version.description,
                    "reason": version.reason,
                    "project_baseline": {
                        "resolution": version.baseline_resolution,
                        "leap_year": version.baseline_leap_year,
                        "scenario_mode": version.baseline_scenario_mode,
                    },
                    "currency": version.currency,
                    "schema_version": version.schema_version,
                    "created_at": version.created_at,
                    "source_draft_revision": version.source_draft_revision,
                },
                "content": version_contents.get(version.version_no, {}),
            }
            raw = json.dumps(jsonable(version_doc), ensure_ascii=False, indent=2).encode()
            _add(path, raw, "application/json")
            zf.writestr(path, raw)
            files_meta["versions"].append(path)

        # 数据集版本与溯源(数据本体 + 元数据 + 溯源/许可证/质量报告)
        for item in dataset_list:
            dataset = item["dataset"]
            version = item["version"]
            if dataset is None:
                continue
            base = f"datasets/{dataset.id}"
            meta_doc = {
                "dataset_id": dataset.id,
                "dataset_version_id": version.id,
                "dataset": {
                    "name": dataset.name,
                    "description": dataset.description,
                    "status": dataset.status,
                    "default_license": dataset.default_license,
                },
                "version": {
                    "version_no": version.version_no,
                    "timeline": version.timeline,
                    "resolution": version.resolution,
                    "fixed_utc_offset_minutes": version.fixed_utc_offset_minutes,
                    "fields": version.fields,
                    "units": version.units,
                    "quality_report": version.quality_report,
                    "provenance": version.provenance,
                    "license": version.license,
                    "created_at": version.created_at,
                    "created_reason": version.created_reason,
                },
                "files": [
                    {
                        "file_kind": item["file"].file_kind,
                        "format": item["file"].format,
                        "row_count": item["file"].row_count,
                        "size_bytes": item["file"].size_bytes,
                        "media_type": item["obj"]["media_type"],
                    }
                    for item in item["files"]
                ],
            }
            meta_raw = json.dumps(jsonable(meta_doc), ensure_ascii=False, indent=2).encode()
            meta_path = f"{base}/dataset.json"
            _add(meta_path, meta_raw, "application/json")
            zf.writestr(meta_path, meta_raw)
            files_meta["datasets"].append(meta_path)
            for file_item in item["files"]:
                f, obj, content_bytes = file_item["file"], file_item["obj"], file_item["content"]
                ext = _media_file_kind(obj.get("media_type"))
                entry = f"{base}/v{version.version_no}-{f.file_kind}.{ext}"
                _add(entry, content_bytes, obj.get("media_type") or "application/octet-stream")
                zf.writestr(entry, content_bytes)

        # 历史结果证据与评估引用(证据数据 + 评估四维结论; 不重新求解)
        for item in evidence_list:
            pkg = item["package"]
            task = item["task"]
            snapshot = item["snapshot"]
            base = f"evidence/{pkg.id}"
            evidence_doc = {
                "package": {
                    "id": pkg.id,
                    "status": pkg.status,
                    "created_at": pkg.created_at,
                },
                "task": {
                    "id": task.id if task else None,
                    "type": task.type if task else None,
                    "status": task.status if task else None,
                    "business_outcome": task.business_outcome if task else None,
                },
                "snapshot": {
                    "id": snapshot.id if snapshot else None,
                    "program_version": snapshot.program_version if snapshot else None,
                    "random_seed": snapshot.random_seed if snapshot else None,
                    "dataset_version_ids": (snapshot.dataset_version_ids if snapshot else []),
                    "canonical_assembly_text": (snapshot.canonical_assembly_text if snapshot else None),
                    "assembly_receipt": snapshot.assembly_receipt if snapshot else None,
                },
                "assessments": [
                    {
                        "id": a.id,
                        "assessor": a.assessor,
                        "dimension_physical": a.dimension_physical,
                        "dimension_optimality": a.dimension_optimality,
                        "dimension_financial": a.dimension_financial,
                        "dimension_reliability": a.dimension_reliability,
                        "overall_score": float(a.overall_score) if a.overall_score is not None else None,
                        "comment": a.comment,
                        "detail": a.detail,
                        "created_at": a.created_at,
                    }
                    for a in item["assessments"]
                ],
                "result_index": [
                    {
                        "id": r.id,
                        "assessment_id": r.assessment_id,
                        "is_latest": r.is_latest,
                        "created_at": r.created_at,
                    }
                    for r in item["index"]
                ],
            }
            evidence_raw = json.dumps(jsonable(evidence_doc), ensure_ascii=False, indent=2).encode()
            evidence_path = f"{base}.json"
            _add(evidence_path, evidence_raw, "application/json")
            zf.writestr(evidence_path, evidence_raw)
            files_meta["evidence"].append(evidence_path)
            if item["content"] is not None:
                media = (
                    item["object"].get("media_type") if item["object"] else None
                ) or "application/octet-stream"
                ext = "json" if "json" in media else "bin"
                entry = f"{base}/result.{ext}"
                _add(entry, item["content"], media)
                zf.writestr(entry, item["content"])

        # 清单最后写入(版本化清单: 格式版本/文件清单/对象清单)
        objects_manifest.sort(key=lambda e: e["path"])
        manifest: dict[str, Any] = {
            "format_version": PACKAGE_FORMAT_VERSION,
            "package_type": "project",
            "generated_at": datetime.now(UTC).isoformat(),
            "exporter": "iesplan",
            "exporter_version": __version__,
            "project": {k: v for k, v in project_meta.items() if k != "id"},
            "files": files_meta,
            "objects": objects_manifest,
        }
        manifest_raw = json.dumps(jsonable(manifest), ensure_ascii=False, indent=2).encode()
        zf.writestr("manifest.json", manifest_raw)
    return buf.getvalue(), manifest


def export_package(db: Session, user: UserRecord, project_id: int) -> PackageExport:
    """导出完整项目包(仅所有者，架构宪法 §12/domain-model §对象生命周期)。

    流程: 权限校验 → 组装 zip(模型/配置/草稿/版本/数据集版本与溯源/历史结果
    证据与评估引用) → 对象存储对象登记 →
    业务引用 + 审计 → 短期单对象下载授权。

    包内不含: 账号/权限与查看者名单/会话/全局系统配置/部署环境密钥。
    """
    project_domain.ensure_access(db, user, project_id, "export_package")
    project = project_domain.require_project(db, project_id)
    draft = project_domain.require_current_draft(db, project)
    draft_content = _load_content_object(db, draft.content_object_id)
    zip_bytes, manifest = _build_package_zip(db, project, draft, draft_content)

    obj = put_object(
        db,
        zip_bytes,
        PACKAGE_MEDIA_TYPE,
        source_category="project_package",
    )
    add_ref(
        db,
        obj.id,
        "export_package",
        project.id,
        ref_entity_type="projects",
        purpose="项目包导出对象(23.2 保留)",
    )
    # ObjectHandle → 元数据 dict(公开门面统一形状)
    obj_info = object_info(db, obj.id)
    # 不再写 data_dir/packages 非托管副本(无引用/配额/校验/清理协议，架构宪法 §10);
    # 对象存储是包的唯一事实源, 下载经短期授权 token 走公开读取门面。

    _audit_entry(
        db,
        user.id,
        audit_domain.AUDIT_PROJECT_EXPORTED,
        "project",
        project.id,
        revision=draft.revision,
        result={"kind": "package", "package_object_id": obj.id, "size_bytes": len(zip_bytes)},
        extra={"file_name": f"project-package-{project.id}.zip"},
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=DOWNLOAD_TOKEN_TTL_SECONDS)
    token = create_download_token(obj.id, "package", project_id=project.id, user_id=user.id)
    return PackageExport(
        object_id=obj.id,
        oid=obj_info["oid"],
        size_bytes=obj_info["size_bytes"],
        media_type=obj_info["media_type"],
        file_name=f"project-package-{project.id}.zip",
        manifest=manifest,
        token=token,
        expires_at=expires_at,
    )


def _unique_project_name(db: Session, base: str) -> str:
    """项目名称去重: 已存在同名项目时追加 " (导入 n)" 后缀(不静默覆盖)。

    经 project 域公开门面判定重名, 不直接访问项目表。
    """
    candidate = base
    index = 2
    while project_domain.project_name_exists(db, candidate):
        candidate = f"{base} (导入 {index})"
        index += 1
    return candidate


def import_proposal(
    db: Session,
    user: UserRecord,
    file_bytes: bytes,
    idempotency_key: str | None = None,
) -> ImportProposalRecord:
    """创建导入提案: 校验 → 暂存对象 → 拟创建项目快照 → 校验报告(domain-model §对象生命周期)。

    - 校验失败(格式/兼容性/清单/完整性)抛 ImportValidationError, 不创建任何记录;
    - 幂等: 调用方传入 idempotency_key 时, 同一提议人已有相同键的未确认提案
      直接返回(不重复暂存); 无键时每次调用新建提案(对象存储无内容去重,
      源包字节相同也不复用对象行);
    - 暂存: 包内全部对象按对象 id 落盘;
    - 拟创建项目快照: 同事务创建新项目身份(导入者即所有者, 原授权不迁移),
      校验报告与分区提交内容写入 review_summary(review_errors 空);
    - 确认导入见 confirm_import。
    """
    manifest, entries = _parse_package(file_bytes)
    # 幂等: 同一提议人 + 同一幂等键的未确认提案 → 直接返回(校验已通过, 不重复暂存)
    if idempotency_key:
        for cand in _list_proposals_for_proposer(db, user.id):
            if (
                cand.status == "proposed"
                and (cand.review_summary or {}).get("idempotency_key") == idempotency_key
            ):
                return cand
    # 源包落盘为新对象(每次提案新建对象行)
    source_obj = put_object(
        db,
        file_bytes,
        PACKAGE_MEDIA_TYPE,
        source_category="project_package",
    )

    # 1) 暂存对象: 包内全部对象按对象 id 落盘(每次写入新对象, 无内容去重)
    staged: dict[str, dict] = {}  # path → 元数据 dict(公开门面)
    for entry in manifest.get("objects", []):
        path = entry["path"]
        staged[path] = put_object(
            db,
            entries[path],
            entry.get("media_type") or "application/octet-stream",
            source_category="project_package_import",
        )
    source_obj = put_object(
        db,
        file_bytes,
        PACKAGE_MEDIA_TYPE,
        source_category="import_package_source",
    )

    # 2) 拟创建项目快照: 新项目身份(每次导入新身份, 导入者成为所有者)
    project_meta = manifest.get("project") or {}
    name = _unique_project_name(db, str(project_meta.get("name") or "导入项目"))
    currency = project_meta.get("currency") or "CNY"
    if currency not in ("CNY", "USD"):
        raise ImportValidationError([f"包内币种非法: {currency}"])
    # 项目计算基线(0.6.5 事项 1): 包必须携带完整基线, 缺失/非法一律拒绝导入
    # (不静默默认; 默认基线只用于数据库迁移对存量项目的回填)。
    baseline_errors = ProjectBaseline.validate(project_meta.get("project_baseline"))
    if baseline_errors:
        raise ImportValidationError(
            [f"包内项目计算基线非法: {d.params.get('detail') or d.code}" for d in baseline_errors]
        )
    try:
        # 基线为用户输入口径(不含内部派生摘要); 契约校验失败即拒绝导入。
        baseline = ProjectBaseline.from_dict(project_meta.get("project_baseline"))
    except ProjectBaselineError as exc:
        raise ImportValidationError([f"包内项目计算基线非法: {exc}"]) from exc
    # 规划/财务配置 revision(0.6.5 事项 3): 包内配置严格校验(缺失 = 导入后
    # 无配置, 不静默默认; 非法 → 拒绝整个导入)。
    package_configs = _parse_config_files(entries, manifest)
    project = project_domain.create_project(
        db,
        name=name,
        owner_id=user.id,
        created_by=user.id,
        description=project_meta.get("description"),
        currency=currency,
        baseline_resolution=baseline.resolution,
        baseline_leap_year=baseline.leap_year,
        baseline_scenario_mode=baseline.scenario_mode,
        schema_version=int(project_meta.get("schema_version", 1) or 1),
    )

    # 3) 导入提案(校验报告 + 分区提交内容, 01 §10.4)
    # 不再写 source_path(该列可空且无任何消费点) —— storage_path 属
    # §11 内部路径, 不得进入审计记录; 可追溯性由 source_object_id
    # (对象存储对象外键)承担。提案行经 package 域 repository 创建,
    # 不直接访问提案表。
    proposal = _create_proposal(
        db,
        project_id=project.id,
        proposer_id=user.id,
        source_type="json",
        source_object_id=source_obj.id,
    )
    proposal = _set_proposal_review(
        db,
        proposal.id,
        status="proposed",
        review_summary={
            "package": {
                "format_version": manifest.get("format_version"),
                "generated_at": manifest.get("generated_at"),
                "exporter": manifest.get("exporter"),
                "exporter_version": manifest.get("exporter_version"),
            },
            "project_snapshot": {
                "name": name,
                "currency": currency,
                "project_baseline": baseline.to_dict(),
                "schema_version": project.schema_version,
            },
            "configs": {
                "finance_triplet": {
                    "present": "effective" in package_configs,
                    "profile_id": package_configs["profile"].profile_id
                    if "profile" in package_configs
                    else None,
                },
                # revision 追加历史属服务端状态、不入包(导出侧同), 提案摘要
                # 只陈述包内容有无; 导入确认时服务层生成 revision(测试头注)。
                "planning": {
                    "present": "planning" in package_configs,
                },
            },
            "staging": {
                "object_count": len(staged),
                "objects": [{"path": path, "object_id": obj.id} for path, obj in sorted(staged.items())],
                "source_object_id": source_obj.id,
            },
            "checks": {
                "zip_ok": True,
                "manifest_ok": True,
                "integrity_ok": True,
                "integrity_verified_objects": len(manifest.get("objects", [])),
            },
            "idempotency_key": idempotency_key,
        },
        review_errors={},
    )
    _audit_entry(
        db,
        user.id,
        audit_domain.AUDIT_PROJECT_IMPORT_PROPOSED,
        "import_proposals",
        proposal.id,
        result={"project_id": project.id, "source_object_id": source_obj.id, "staged_objects": len(staged)},
    )
    return proposal


def _create_draft_row(db: Session, project_id: int, content: dict, user: UserRecord):
    """新项目身份创建初始草稿(revision=1, 经 project 域 repository)。"""
    content_object_id = _store_content_object(db, content)
    return project_domain.create_draft(
        db,
        project_id=project_id,
        content_object_id=content_object_id,
        updated_by=user.id,
    )


def _staged_object_id(staged_by_path: dict[str, int], path: str) -> int:
    """按包内路径取暂存对象 id(缺失 → 数据损坏, 明确报错不返回占位)。"""
    try:
        return staged_by_path[path]
    except KeyError as exc:
        raise AppError(
            "导入暂存对象缺失(数据损坏)",
            code="PKG-IMP-002",
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            params={"path": path},
        ) from exc


def confirm_import(db: Session, user: UserRecord, proposal_id: int) -> ProjectRecord:
    """提交导入(U14, domain-model §对象生命周期): 创建新项目身份, 导入者成为所有者。

    分区提交内容(单事务):
    - 数据集: 重建 Dataset/DatasetVersion/DatasetFile(引用暂存对象, 原标识重映射);
    - 草稿: revision=1 领域内容(数据集绑定重映射, 证据来源登记);
    - 版本: 按包内版本顺序重建 ProjectVersion(新身份版本号, 不倒写原版本);
    - 配置: 包内规划/财务配置 revision 重建为 revision=1 行(与提案同源
      重校验, 失败 → 整个事务回滚, 不落任何行; 0.6.5 事项 3);
    - 证据: 历史结果作为证据来源保留 — 登记对象引用(imported_evidence)与
      评估摘要, 不创建本地任务(不伪造本地任务, domain-model §快照、任务和结果)。

    导入约束: 不得静默覆盖(名称去重 + 新项目身份); 账号/权限/会话不随包导入;
    导入者成为新项目所有者; 原授权关系不迁移。
    """
    proposal = _get_proposal(db, proposal_id)
    if proposal is None:
        raise NotFoundError(
            "导入提案不存在",
            params={"proposal_id": proposal_id},
            location={"object_type": "import_proposals", "object_id": proposal_id},
        )
    if proposal.proposer_id != user.id:
        raise ForbiddenError(
            "仅提案人可确认导入",
            params={"proposal_id": proposal_id},
            location={"object_type": "import_proposals", "object_id": proposal_id},
        )
    project = project_domain.get_project(db, proposal.project_id)
    if project is None:
        raise NotFoundError("导入提案关联项目缺失", params={"proposal_id": proposal_id})
    if proposal.status == "applied":
        return project  # 幂等重放: 已导入则返回导入结果项目
    if proposal.status == "rejected":
        raise ConflictError(
            "导入提案已被拒绝", params={"proposal_id": proposal_id, "status": proposal.status}
        )
    if proposal.status not in ("proposed", "validated", "approved"):
        raise ConflictError(
            "导入提案状态不允许确认", params={"proposal_id": proposal_id, "status": proposal.status}
        )

    # 复核源包(完整性), 取分区提交内容
    summary = proposal.review_summary or {}
    source_object_id = (summary.get("staging") or {}).get("source_object_id")
    file_bytes = get_object(db, int(source_object_id))
    manifest, entries = _parse_package(file_bytes)
    # 提案阶段暂存的对象 id 按包内路径索引(确认阶段不再按摘要查找对象)
    staged_by_path = {
        str(item.get("path")): int(item.get("object_id"))
        for item in (summary.get("staging") or {}).get("objects", [])
        if isinstance(item, dict) and item.get("path") is not None and item.get("object_id") is not None
    }

    # 1) 数据集(先建, 供绑定重映射; 原数据集版本标识 → 新标识)
    dataset_id_map: dict[int, int] = {}
    for meta_path in manifest.get("files", {}).get("datasets", []):
        if meta_path not in entries:
            continue
        meta = json.loads(entries[meta_path].decode("utf-8"))
        ds_meta = meta.get("dataset") or {}
        ver_meta = meta.get("version") or {}
        # 数据集重建经 dataset 域公开门面(状态发布紧随创建, 与旧直写终态一致)。
        dataset = dataset_domain.create_dataset(
            db,
            name=str(ds_meta.get("name") or "导入数据集"),
            created_by=user.id,
            project_id=project.id,
            description=ds_meta.get("description"),
            default_license=ds_meta.get("default_license"),
        )
        dataset = dataset_domain.set_dataset_status(db, dataset.id, "published")
        version = dataset_domain.create_version(
            db,
            dataset_id=dataset.id,
            timeline=str(ver_meta.get("timeline") or "hourly"),
            fixed_utc_offset_minutes=int(ver_meta.get("fixed_utc_offset_minutes", 480)),
            fields=ver_meta.get("fields") or {},
            units=ver_meta.get("units") or {},
            created_by=user.id,
            resolution=ver_meta.get("resolution"),
            quality_report=ver_meta.get("quality_report"),
            provenance=ver_meta.get("provenance"),
            license=ver_meta.get("license"),
            created_reason="imported",
        )
        dataset_id_map[int(meta.get("dataset_version_id") or 0)] = version.id
        base = meta_path.removesuffix("/dataset.json")
        for file_meta in meta.get("files", []):
            kind = str(file_meta.get("file_kind") or "data")
            prefix = f"{base}/v{int(ver_meta.get('version_no', 1))}-{kind}."
            matches = [p for p in staged_by_path if p.startswith(prefix)]
            if len(matches) != 1:
                raise AppError(
                    "导入暂存对象缺失(数据损坏)",
                    code="PKG-IMP-002",
                    severity=SEVERITY_ERROR,
                    message_key="ies.diag.store.corrupt",
                    params={"path": prefix},
                )
            dataset_domain.add_file(
                db,
                dataset_version_id=version.id,
                object_id=staged_by_path[matches[0]],
                file_kind=kind,
                format=str(file_meta.get("format") or "csv"),
                row_count=int(file_meta.get("row_count", 0)),
                size_bytes=int(file_meta.get("size_bytes", 0)),
            )

    def _remap(content: dict) -> dict:
        """重映射数据集绑定到新项目的数据集版本标识(原授权/标识不迁移)。"""
        bindings = content.get("dataset_bindings") or []
        for binding in bindings:
            original = binding.get("dataset_version_id")
            if original in dataset_id_map:
                binding["dataset_version_id"] = dataset_id_map[original]
                binding.pop("dataset_id", None)
        content.pop("applied_commands", None)  # 命令簿记不外泄
        return content

    # 2) 证据来源: 历史结果作为证据来源保留(不伪造本地任务, domain-model §快照、任务和结果)
    imported_evidence: list[dict[str, Any]] = []
    for evidence_path in manifest.get("files", {}).get("evidence", []):
        if evidence_path not in entries:
            continue
        doc = json.loads(entries[evidence_path].decode("utf-8"))
        pkg_meta = doc.get("package") or {}
        task_meta = doc.get("task") or {}
        snapshot_meta = doc.get("snapshot") or {}
        assessments = doc.get("assessments") or []
        base = evidence_path.removesuffix(".json")
        ref_objects = [
            entry
            for entry in manifest.get("objects", [])
            if entry.get("path") == evidence_path or entry.get("path", "").startswith(f"{base}/")
        ]
        object_ids: list[int] = []
        for entry in ref_objects:
            object_id = _staged_object_id(staged_by_path, str(entry.get("path")))
            object_ids.append(object_id)
            add_ref(
                db,
                object_id,
                "imported_evidence",
                project.id,
                ref_entity_type="projects",
                purpose="导入的历史结果证据来源(不伪造本地任务)",
            )
        imported_evidence.append(
            {
                "package_id": pkg_meta.get("id"),
                "task": {k: task_meta.get(k) for k in ("type", "status", "business_outcome")},
                "snapshot_id": snapshot_meta.get("id"),
                "assessments": [
                    {
                        k: a.get(k)
                        for k in (
                            "id",
                            "assessor",
                            "dimension_physical",
                            "dimension_optimality",
                            "dimension_financial",
                            "dimension_reliability",
                            "overall_score",
                            "comment",
                        )
                    }
                    for a in assessments
                ],
                "objects": [
                    {"path": e.get("path"), "object_id": oid}
                    for e, oid in zip(ref_objects, object_ids, strict=False)
                ],
            }
        )

    # 3) 草稿(revision=1): 领域内容 + 证据来源登记
    draft_doc = json.loads(entries["draft.json"].decode("utf-8"))
    draft_content = _remap(dict(draft_doc.get("content") or {}))
    draft_content["imported_evidence"] = imported_evidence
    _create_draft_row(db, project.id, draft_content, user)

    # 4) 版本: 按包内版本顺序重建(新身份版本号, parent 链按导入顺序)
    # 经 project 域 create_version 一次完成版本行 + 内容引用 + current 指针
    # 移动(基线/币种/schema 与项目一致, 由包内基线校验保证, 与旧直写等价;
    # 新身份下版本号自 1 起按包内顺序重新分配)。
    prev_version: ProjectVersionRecord | None = None
    version_paths = sorted(manifest.get("files", {}).get("versions", []))
    for version_path in version_paths:
        if version_path not in entries:
            continue
        doc = json.loads(entries[version_path].decode("utf-8"))
        ver_meta = doc.get("version") or {}
        # 版本基线: 包内版本必须携带与项目一致的基线(缺失/不一致 → 拒绝,
        # 版本内容自包含, 不以"当前项目"解释历史版本)。
        version_baseline_errors = ProjectBaseline.validate(ver_meta.get("project_baseline"))
        if version_baseline_errors:
            raise ImportValidationError(
                [
                    f"包内版本 {ver_meta.get('version_no', '?')} 项目计算基线非法: "
                    f"{d.params.get('detail') or d.code}"
                    for d in version_baseline_errors
                ]
            )
        try:
            version_baseline = ProjectBaseline.from_dict(ver_meta.get("project_baseline"))
        except ProjectBaselineError as exc:
            raise ImportValidationError(
                [f"包内版本 {ver_meta.get('version_no', '?')} 项目计算基线非法: {exc}"]
            ) from exc
        if (
            version_baseline.resolution != project.baseline_resolution
            or version_baseline.leap_year != project.baseline_leap_year
            or version_baseline.scenario_mode != project.baseline_scenario_mode
        ):
            raise ImportValidationError(
                [
                    f"包内版本 {ver_meta.get('version_no', '?')} 项目计算基线"
                    "与项目不一致(版本必须与项目共享同一基线)"
                ]
            )
        version_content = _remap(dict(doc.get("content") or {}))
        content_object_id = _store_content_object(db, version_content)
        version = project_domain.create_version(
            db,
            project_id=project.id,
            name=str(ver_meta.get("name") or f"导入版本 {ver_meta.get('version_no', 1)}"),
            reason="imported",
            created_by=user.id,
            content_object_id=content_object_id,
            parent_version_id=prev_version.id if prev_version is not None else None,
            description=ver_meta.get("description"),
        )
        prev_version = version

    # 5) 财务三件套/规划配置(0.6.5 条目 1-2): 从暂存源包重解析并重建
    # revision=1 行(与提案同源校验, 确认阶段为强制点; 失败 → 整个事务回滚,
    # 不落任何行)。重建顺序: 登记 Profile → 保存 Overrides(重合并生成
    # Effective) → 保存规划(强制与当前 Effective content 一致)。
    package_configs = _parse_config_files(entries, manifest)
    if "effective" in package_configs:
        try:
            row, _ = _register_finance_profile(db, package_configs["profile"].to_dict(), user.id)
            # 按稳定 profile_id 绑定注册行(注册表按 id 唯一, 同 id 复用既有行)
            # set_project_finance_profile 内部按稳定 profile_id 定位
            _set_project_finance_profile(db, project.id, row.profile_id, user.id)
            _save_finance_overrides(db, project.id, package_configs["overrides"].to_dict(), 1, user.id)
            if "planning" in package_configs:
                _save_planning_config(db, project.id, package_configs["planning"].to_dict(), None, user.id)
        except AppError as exc:
            raise ImportValidationError(
                [f"包内配置在确认阶段校验失败: {getattr(exc, 'code', 'PKG-IMP-001')} {exc}"]
            ) from exc

    # 6) 提案收尾 + 审计(提案状态经 package 域状态机推进)
    _set_proposal_review(
        db,
        proposal.id,
        status="applied",
        decided_by=user.id,
    )
    _audit_entry(
        db,
        user.id,
        audit_domain.AUDIT_PROJECT_IMPORTED,
        "project",
        project.id,
        revision=1,
        result={
            "source_object_id": proposal.source_object_id,
            "imported_versions": len(version_paths),
            "imported_datasets": len(dataset_id_map),
            "evidence_objects": len(imported_evidence),
            "imported_configs": len(package_configs),
        },
    )
    # 重读项目行: 版本/草稿创建已移动 current 指针, 入口快照已过期
    return project_domain.require_project(db, project.id)


def _first_section(content: dict | None, *keys: str) -> Any:
    """按键名顺序取证据内容中的摘要节(未命中返回 None)。"""
    if not content:
        return None
    for key in keys:
        if key in content:
            return content[key]
    return None


def _section_rows(section: Any) -> list[tuple[str, Any]]:
    """摘要节 → (名称, 值) 行(兼容 dict 与 [{name,value,unit}] 两种形态)。"""
    rows: list[tuple[str, Any]] = []
    if isinstance(section, dict):
        rows = [(str(k), v) for k, v in section.items()]
    elif isinstance(section, list):
        for item in section:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("key") or item.get("label")
            if name is None:
                continue
            value = item.get("value")
            unit = item.get("unit")
            rows.append((str(name), f"{value} {unit}" if unit is not None else value))
    return rows


def _excel_safe(value: Any) -> Any:
    """Excel 公式注入防护(M-09): 用户可控字符串以 = + - @ 开头时前置单引号。

    所有写入 Excel 单元格的用户可控值都必须经过本函数, 防止恶意项目名/设备名/
    评论等在打开报表时被 Excel/LibreOffice 当作公式执行(客户端文件风险)。
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _set_sheet_title(ws, title_zh: str, title_en: str) -> None:
    """写入双语节标题(加粗)。"""
    cell = ws.cell(row=1, column=1, value=_excel_safe(f"{title_zh} / {title_en}"))
    cell.font = Font(bold=True)


def _write_kv(ws, rows: list[tuple[str, Any]], start_row: int = 3) -> int:
    """写键值行(两列), 返回下一可用行号(全部值经 _excel_safe 防公式注入)。"""
    row = start_row
    for key, value in rows:
        ws.cell(row=row, column=1, value=_excel_safe(str(key)))
        if value is None:
            ws.cell(row=row, column=2, value="—")
        elif isinstance(value, (dict, list)):
            ws.cell(row=row, column=2, value=_excel_safe(json.dumps(value, ensure_ascii=False)[:500]))
        else:
            ws.cell(row=row, column=2, value=_excel_safe(value))
        row += 1
    return row


def export_excel(
    db: Session,
    user: UserRecord,
    project_id: int,
    evidence_package_id: int,
    assessment_id: int,
    lang: str = "zh",
) -> bytes:
    """导出固定模板 Excel 报告(查看者可导出,
    domain-model §快照、任务和结果 / contracts §公共文件契约 / REQ-EXPORT-001)。

    - 固定引用给定证据包与结果评估, 导出时不重新求解(11.2);
    - 标题中英双语(默认简体中文); 内容: 项目版本/计算快照/数据版本/计算配置/
      算法/结果状态/四维结论/主要指标表/设备配置/财务摘要/环境摘要/工程摘要/
      适用范围与限制;
    - 注明适用单位与数据来源(数据集版本/溯源/许可证/内容校验值)。
    """
    project_domain.ensure_access(db, user, project_id, "export_excel")
    project = project_domain.require_project(db, project_id)
    evidence = results_domain.get_evidence(db, evidence_package_id)
    if evidence is None:
        raise NotFoundError(
            "证据包不存在",
            params={"evidence_package_id": evidence_package_id},
            location={"object_type": "evidence_packages", "object_id": evidence_package_id},
        )
    task = tasks_domain.get_task(db, evidence.task_id)
    if task is None or task.project_id != project_id:
        raise NotFoundError(
            "证据包不属于该项目",
            params={"evidence_package_id": evidence_package_id},
        )
    assessment = results_domain.get_assessment(db, assessment_id)
    if assessment is None or assessment.evidence_package_id != evidence.id:
        raise NotFoundError(
            "结果评估不存在或与证据包不匹配",
            params={"assessment_id": assessment_id, "evidence_package_id": evidence.id},
            location={"object_type": "result_assessments", "object_id": assessment_id},
        )
    snapshot = tasks_domain.get_snapshot(db, evidence.calc_snapshot_id) if evidence.calc_snapshot_id else None
    version = (
        project_domain.get_version(db, project_id, project.current_version_id)
        if project.current_version_id
        else None
    )
    version_content: dict = {}
    if version is not None:
        version_content = _load_content_object(db, version.content_object_id)
    evidence_content = _parse_evidence_content(get_object(db, evidence.object_id))

    # 数据版本(计算快照绑定的数据集版本 + 溯源/许可证)
    dataset_rows: list[dict[str, Any]] = []
    dvid_list = (snapshot.dataset_version_ids if snapshot else None) or []
    for dvid in dvid_list:
        dver = dataset_domain.get_version(db, int(dvid))
        if dver is None:
            continue
        dset = dataset_domain.get_dataset(db, dver.dataset_id)
        dataset_rows.append(
            {
                "dataset": dset.name if dset else dver.dataset_id,
                "version_no": dver.version_no,
                "resolution": dver.resolution,
                "provenance": dver.provenance,
                "license": dver.license,
            }
        )

    wb = Workbook()
    ws = wb.active
    ws.title = "报告总览"
    # 固定标题: 中英双语(默认简体中文在前)
    title = ws.cell(row=1, column=1, value="pIES 项目结果报告 / pIES Project Result Report")
    title.font = Font(bold=True, size=14)
    lang_label = "简体中文" if lang != "en" else "English"
    ws.cell(row=2, column=1, value=f"生成语言: {lang_label} / Language: {lang}")

    overview: list[tuple[str, Any]] = []
    overview.append(("项目名称 / Project name", project.name))
    overview.append(("项目状态 / Project status", project.status))
    overview.append(("币种 / Currency", project.currency))
    if version is not None:
        overview.append(("项目版本 / Project version", f"#{version.version_no} {version.name}"))
        overview.append(("版本说明 / Version reason", version.reason))
        overview.append(("版本内容对象 / Version content object", f"#{version.content_object_id}"))
    else:
        overview.append(("项目版本 / Project version", "—"))
    if snapshot is not None:
        overview.append(("计算快照 / Calc snapshot", f"#{snapshot.id}"))
        overview.append(("程序版本 / Program version", snapshot.program_version))
        overview.append(("随机种子 / Random seed", snapshot.random_seed))
    else:
        overview.append(("计算快照 / Calc snapshot", "—"))
    overview.append(("数据版本 / Data versions", len(dataset_rows)))
    calc_config = snapshot.calc_config_snapshot if snapshot else {}
    overview.append(("计算配置 / Calc config", f"{len(calc_config.get('params') or {})} 参数 / parameters"))
    algorithm = calc_config.get("algorithm") or calc_config.get("solver")
    overview.append(("算法 / Algorithm", algorithm or "—"))
    overview.append(("结果状态 / Result status", task.status))
    overview.append(("业务结局 / Business outcome", task.business_outcome or "—"))
    overview.append(("证据包状态 / Evidence status", evidence.status))
    overview.append(("关联标识 / Reference", f"evidence_package={evidence.id}, assessment={assessment.id}"))
    overview.append(
        (
            "四维结论 / Four-dimension conclusion",
            f"物理 {assessment.dimension_physical} / 最优 {assessment.dimension_optimality} / "
            f"财务 {assessment.dimension_financial} / 可靠 {assessment.dimension_reliability}"
            + (
                f" | 综合评分 {float(assessment.overall_score)}"
                if assessment.overall_score is not None
                else ""
            ),
        )
    )
    overview.append(("评估意见 / Assessment comment", assessment.comment or "—"))
    _write_kv(ws, overview)
    row = 3 + len(overview) + 1
    # 适用范围与限制(固定引用证据与评估, 不重新求解)
    applicability = _first_section(evidence_content, "applicability", "scope", "适用")
    ws.cell(row=row, column=1, value="适用范围与限制 / Applicability and limitations").font = Font(bold=True)
    _write_kv(ws, _section_rows(applicability) or [("适用范围", "—")], start_row=row + 1)
    ws.cell(
        row=row + 6,
        column=1,
        value="本报告固定引用证据包与结果评估, 导出时不重新求解 / "
        "This report references a fixed evidence package and assessment; no re-solve on export.",
    ).font = Font(italic=True)
    ws.cell(
        row=row + 7,
        column=1,
        value="适用单位 / Units: 指标单位以各摘要表注记为准(如 kWh、MW、°C、元/kWh、kgCO₂/kWh)。",
    )
    ws.cell(
        row=row + 8,
        column=1,
        value="数据来源 / Data sources: "
        + (
            "; ".join(
                f"{d['dataset']} v{d['version_no']}({d['resolution']}, 许可证 {d['license'] or '—'})"
                for d in dataset_rows
            )
            or "无绑定数据版本"
        ),
    )

    # 主要指标表
    kpis = _first_section(evidence_content, "kpis", "key_metrics", "metrics")
    kp_sheet = wb.create_sheet("主要指标")
    _set_sheet_title(kp_sheet, "主要指标表", "Key Metrics Table")
    kp_rows = kpis if isinstance(kpis, list) else _section_rows(kpis)
    r = 3
    if kp_rows:
        kp_sheet.cell(row=2, column=1, value="指标 / Metric")
        kp_sheet.cell(row=2, column=2, value="值 / Value")
        kp_sheet.cell(row=2, column=3, value="单位 / Unit")
        for item in kp_rows:
            if isinstance(item, dict):
                kp_sheet.cell(
                    row=r,
                    column=1,
                    value=_excel_safe(str(item.get("name") or item.get("key") or "")),
                )
                kp_sheet.cell(row=r, column=2, value=_excel_safe(item.get("value")))
                kp_sheet.cell(row=r, column=3, value=_excel_safe(item.get("unit") or ""))
            elif isinstance(item, tuple) and len(item) >= 2:
                kp_sheet.cell(row=r, column=1, value=_excel_safe(str(item[0])))
                kp_sheet.cell(row=r, column=2, value=_excel_safe(item[1]))
            r += 1
    else:
        kp_sheet.cell(row=3, column=1, value="无指标数据 / No metric data")

    # 设备配置(来自版本内容模型, 只展示不重算, REQ-RESULT-002)
    dev_sheet = wb.create_sheet("设备配置")
    _set_sheet_title(dev_sheet, "设备配置", "Equipment Configuration")
    devices = version_content.get("model", {}).get("devices", [])
    if devices:
        dev_sheet.cell(row=2, column=1, value="名称 / Name")
        dev_sheet.cell(row=2, column=2, value="类型 / Type")
        dev_sheet.cell(row=2, column=3, value="参数 / Parameters")
        r = 3
        for dev in devices:
            params = dev.get("params") or {}
            type_name = params.get("type_detail") or dev.get("device_type") or "—"
            dev_sheet.cell(row=r, column=1, value=_excel_safe(str(dev.get("name") or "")))
            dev_sheet.cell(row=r, column=2, value=_excel_safe(str(type_name)))
            dev_sheet.cell(
                row=r,
                column=3,
                value=_excel_safe(json.dumps(params, ensure_ascii=False)[:300]),
            )
            r += 1
    else:
        dev_sheet.cell(row=3, column=1, value="无设备配置 / No equipment")

    # 财务/环境/工程摘要(证据包内容, 固定引用, 不重新求解)
    for sheet_name, title_zh, title_en, section in (
        (
            "财务摘要",
            "财务摘要",
            "Financial Summary",
            _first_section(evidence_content, "financial", "finance"),
        ),
        (
            "环境摘要",
            "环境摘要",
            "Environmental Summary",
            _first_section(evidence_content, "environmental", "environment", "emissions"),
        ),
        (
            "工程摘要",
            "工程摘要",
            "Engineering Summary",
            _first_section(evidence_content, "engineering", "engineering_summary"),
        ),
    ):
        ws_s = wb.create_sheet(sheet_name)
        _set_sheet_title(ws_s, title_zh, title_en)
        rows = _section_rows(section)
        _write_kv(ws_s, rows if rows else [("无摘要数据 / No summary data", "—")])

    buf = io.BytesIO()
    wb.save(buf)
    _audit_entry(
        db,
        user.id,
        audit_domain.AUDIT_PROJECT_EXPORTED,
        "project",
        project.id,
        result={"kind": "excel", "evidence_package_id": evidence.id, "assessment_id": assessment.id},
        extra={"lang": lang},
    )
    return buf.getvalue()
