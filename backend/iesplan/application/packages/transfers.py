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
from iesplan import model as model_domain
from iesplan import project as project_domain
from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.configuration import (
    get_effective_finance_config as _get_effective_finance_config,
    get_finance_overrides as _get_finance_overrides,
    get_planning_config as _get_planning_config,
)
from iesplan.application.configuration import (
    publish_accepted_finance_state as _publish_accepted_finance_state,
)
from iesplan.application.models import (
    MODEL_MEDIA_TYPE as _MODEL_MEDIA_TYPE,
    save_project_model_in_transaction as _save_project_model_in_transaction,
)
from iesplan.application.projects import (
    ensure_access,
    load_content_object as _load_content_object,
    store_content_object as _store_content_object,
)
from iesplan.core.contracts import (
    PlanningConfig,
    PlanningConfigError,
    ProjectBaseline,
    ProjectBaselineError,
)
from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from iesplan.core.jsonutil import jsonable
from iesplan.core.yamlmini import dump as yaml_dump
from iesplan.core.yamlmini import load as yaml_load
from iesplan.finance import (
    EffectiveFinanceConfig,
    FinanceOverrides,
    FinanceProfile,
    FinanceTripletError,
    merge_effective,
)
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
from iesplan.package import claim_proposal_for_confirm as _claim_proposal_for_confirm
from iesplan.package import create_proposal as _create_proposal
from iesplan.package import get_proposal as _get_proposal
from iesplan.package import list_proposals_for_proposer as _list_proposals_for_proposer
from iesplan.package import (
    media_file_kind as _media_file_kind,
)
from iesplan.package import parse_evidence_content as _parse_evidence_content
from iesplan.package import parse_package as _parse_package
from iesplan.package import set_proposal_review as _set_proposal_review
from iesplan.package.contracts import ImportProposalRecord
from iesplan.planning.contracts import validate_planning_domain
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


def _content_model_refs(content: dict | None) -> dict[str, dict]:
    """内容声明的项目模型精确引用(device_id → 引用; 保存/删除模型时写入)。"""
    refs = (content or {}).get("project_models") or []
    return {str(ref["device_id"]): ref for ref in refs}


def _remap_model_device_types(content: dict, model_map: dict[str, dict[str, Any]]) -> None:
    """内容 model 节设备实例的 device_type: 来源模型 ID → 新项目模型 ID。

    源项目删除过模型(suffix 空号)时新编号与来源不同, 因此一律按导入重建后
    的真实新 ID 改写, 不假设编号不变、不产生双后缀。
    """
    model = content.get("model") or {}
    for device in model.get("devices") or []:
        mapped = model_map.get(str(device.get("device_type")))
        if mapped is not None:
            device["device_type"] = mapped["device_id"]


def _remap_model_refs(content: dict, model_map: dict[str, dict[str, Any]]) -> None:
    """内容 project_models 引用映射到新项目行身份(源行 id 不复用为目标身份)。"""
    for ref in content.get("project_models") or []:
        mapped = model_map.get(str(ref["device_id"]))
        if mapped is not None:
            ref["id"] = mapped["id"]
            ref["device_id"] = mapped["device_id"]
            ref["revision"] = mapped["revision"]


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
                "start_year": project.baseline_start_year,
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
                        "start_year": version.baseline_start_year,
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

        # 项目模型依赖(3.0 files.models): 携带草稿与全部历史版本内容精确
        # 引用到的每个项目模型的已冻结规范文本(当前草稿未引用但历史版本
        # 引用的同样携带, 与 version_configs 同理; 行缺失即 loudly 拒绝,
        # 不静默丢弃、不携带其他项目行)。清单键 device_id 为来源最终 ID,
        # base_device_id 供导入侧重建时按本项目重新分配编号。
        model_refs: dict[str, dict] = _content_model_refs(draft_content)
        for content in version_contents.values():
            model_refs.update(_content_model_refs(content))
        model_rows: list[model_domain.ProjectModelRecord] = []
        for ref in model_refs.values():
            row = model_domain.get_project_model(db, int(ref["id"]))
            if row is None or row.project_id != project.id:
                raise model_domain.ProjectModelNotFoundError(
                    "内容引用的项目模型行缺失",
                    params={"project_id": project.id, "device_id": ref["device_id"]},
                )
            model_rows.append(row)
        models_meta: list[dict[str, str]] = []
        for row in sorted(model_rows, key=lambda item: item.suffix):
            path = f"models/{row.device_id}.json"
            raw = get_object(db, row.model_object_id)
            _add(path, raw, _MODEL_MEDIA_TYPE)
            zf.writestr(path, raw)
            models_meta.append(
                {
                    "device_id": row.device_id,
                    "base_device_id": row.base_device_id,
                    "model_text": path,
                }
            )
        files_meta["models"] = models_meta

        # 数据集版本与溯源(数据本体 + 元数据 + 溯源/许可证/质量报告;
        # 2.0: 每个数据版本独立目录, 元数据不再按数据集共用)
        for item in dataset_list:
            dataset = item["dataset"]
            version = item["version"]
            if dataset is None:
                continue
            base = f"datasets/{dataset.id}/v{version.version_no}"
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
                entry = f"{base}/{f.file_kind}.{ext}"
                _add(entry, content_bytes, obj.get("media_type") or "application/octet-stream")
                zf.writestr(entry, content_bytes)

        # 历史版本精确配置依赖(2.0 files.version_configs): 每个带引用块的
        # 版本快照其冻结 revision 行内容(只带包内版本所需, 不带无关历史/
        # 全库)。行缺失即 loudly 拒绝(指针损坏不静默); 当前工作状态组
        # (files.configs)不受影响, 缺席的依赖不建条目。
        version_configs_meta: dict[str, dict[str, str]] = {}
        for version in versions:
            content = version_contents.get(version.version_no, {})
            eff = content.get("effective_finance") or {}
            plan = content.get("planning_config") or {}
            if not eff and not plan:
                continue
            key = f"{version.version_no:04d}"
            dep_paths: dict[str, str] = {}
            if eff:
                prof_row = configuration_domain.get_profile(db, eff.get("profile_id"))
                ovr_row = configuration_domain.get_overrides_revision(
                    db, project.id, eff.get("overrides_revision")
                )
                eff_row = configuration_domain.get_effective_revision(
                    db, project.id, eff.get("revision")
                )
                if prof_row is None or ovr_row is None or eff_row is None:
                    raise AppError(
                        "版本冻结的财务依赖行缺失",
                        code="PROJ-FIN-003",
                        params={"project_id": project.id, "version_no": version.version_no},
                    )
                for fname, fcontent in (
                    ("finance_profile.yaml", prof_row.content),
                    ("finance_overrides.yaml", ovr_row.content),
                    ("effective_finance.yaml", eff_row.content),
                ):
                    path = f"version_configs/{key}/{fname}"
                    raw = yaml_dump(fcontent).encode("utf-8")
                    _add(path, raw, "application/yaml")
                    zf.writestr(path, raw)
                    dep_paths[fname.removesuffix(".yaml")] = path
            if plan:
                plan_row = configuration_domain.get_planning_revision(
                    db, project.id, plan.get("revision")
                )
                if plan_row is None:
                    raise AppError(
                        "版本冻结的规划配置行缺失",
                        code="PROJ-PLAN-004",
                        params={"project_id": project.id, "version_no": version.version_no},
                    )
                path = f"version_configs/{key}/planning_config.yaml"
                raw = yaml_dump(plan_row.content).encode("utf-8")
                _add(path, raw, "application/yaml")
                zf.writestr(path, raw)
                dep_paths["planning_config"] = path
            version_configs_meta[key] = dep_paths
        files_meta["version_configs"] = version_configs_meta

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
    ensure_access(db, user, project_id, "export_package")
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


# ---------------------------------------------------------------------------
# 包内配置解析(财务三件套/规划配置; 归属 application.packages.transfers)
# ---------------------------------------------------------------------------
#
# 强类型财务/规划解析依赖 iesplan.finance 与 iesplan.planning.contracts,
# 不得下沉 package 域(域源码纯度门禁); 本函数行为与原 package 域实现一致。


def parse_config_files(entries: dict[str, bytes], manifest: dict) -> dict:
    """解析包内财务三件套/规划配置 YAML(0.6.5 条目 1-2), 严格校验。

    返回 {"profile": FinanceProfile, "overrides": FinanceOverrides,
          "effective": EffectiveFinanceConfig, "planning": PlanningConfig | None};
    包未携带配置时返回 {}(导入后项目无配置, 不静默默认)。

    校验(任一失败 → ImportValidationError, 拒绝整个导入):
    - files.configs 声明的路径必须存在且为合法安全 YAML(yamlmini 子集);
    - 三件套必须齐全(Profile + Overrides + Effective 一并导入, 缺一拒绝);
    - 内容严格恢复(FinanceProfile/FinanceOverrides/EffectiveFinanceConfig/
      PlanningConfig.from_dict: 拒未知/缺失字段);
    - Overrides 对 Profile 结构校验(profile_ref 匹配、只许既有叶子、
      禁改单位/carrier/direction/tax、禁新增 finance_type/price_id);
    - Effective 的 profile_id 必须等于组内 Profile 稳定 id(来源一致),
      且其内容必须等于精确来源合并结果(公开 merge_effective 确定);
    对象字节完整性由 _parse_package 的对象清单一一对应 + 大小校验承担
    (外部包入口边界); 领域层不做本地内容重算比对(2.6)。
    """
    files_meta = manifest.get("files") or {}
    configs_meta = files_meta.get("configs") or {}
    if not configs_meta:
        return {}
    if not isinstance(configs_meta, dict):
        raise ImportValidationError(["清单 files.configs 结构非法(期望映射)"])
    reasons: list[str] = []

    profile_path = configs_meta.get("finance_profile")
    overrides_path = configs_meta.get("finance_overrides")
    effective_path = configs_meta.get("effective_finance")
    planning_path = configs_meta.get("planning_config")
    required = {
        "finance_profile": profile_path,
        "finance_overrides": overrides_path,
        "effective_finance": effective_path,
    }
    for field, path in required.items():
        if path is None:
            reasons.append(f"清单 files.configs 缺少 {field} 条目")
    if planning_path is not None and profile_path is None:
        reasons.append("包内携带规划配置但缺少财务三件套(规划必须引用已生成的有效财务快照)")
    if reasons:
        raise ImportValidationError(reasons)

    group = _parse_triplet_group(
        entries, profile_path, overrides_path, effective_path, planning_path, label="包内",
    )
    if "effective" in group and "profile" in group:
        if group["effective"].profile_id != group["profile"].profile_id:
            raise ImportValidationError(["包内 Effective 与 Profile 来源不一致"])
        # 外部输入核心财务约束: 包内 Effective 必须等于精确来源合并结果。
        expected = merge_effective(group["profile"], group.get("overrides"))
        if expected.to_dict() != group["effective"].to_dict():
            raise ImportValidationError(["包内 Effective 与来源合并结果不一致"])
    return group


def _parse_triplet_group(
    entries: dict[str, bytes],
    profile_path: str | None,
    overrides_path: str | None,
    effective_path: str | None,
    planning_path: str | None,
    *,
    label: str,
) -> dict:
    """解析一组财务三件套/规划配置(同一正式解析器, 当前组与历史组共用)。

    缺失路径、YAML 解码失败、正式 from_dict/领域校验失败一律记入原因并
    ImportValidationError; 返回 typed 对象映射(缺席键不出现)。
    """
    reasons: list[str] = []

    def _load_yaml(package_field: str, path: str | None) -> dict:
        if not isinstance(path, str) or path not in entries:
            raise ImportValidationError([f"{label} {package_field} 指向的包内文件缺失: {path}"])
        try:
            doc = yaml_load(entries[path].decode("utf-8"))
        except Exception as exc:
            raise ImportValidationError([f"{label} {path} 无法解析为安全 YAML: {exc}"]) from exc
        if not isinstance(doc, dict):
            raise ImportValidationError([f"{label} {path} 结构非法(期望对象)"])
        return doc

    profile: FinanceProfile | None = None
    overrides: FinanceOverrides | None = None
    effective: EffectiveFinanceConfig | None = None
    if profile_path is not None:
        try:
            profile = FinanceProfile.from_dict(_load_yaml("finance_profile", profile_path))
        except FinanceTripletError as exc:
            reasons.append(f"{label} FinanceProfile 非法: {exc}")
    if overrides_path is not None:
        try:
            overrides = FinanceOverrides.from_dict(
                _load_yaml("finance_overrides", overrides_path), profile=profile
            )
        except FinanceTripletError as exc:
            reasons.append(f"{label} FinanceOverrides 非法: {exc}")
    if effective_path is not None:
        try:
            effective = EffectiveFinanceConfig.from_dict(_load_yaml("effective_finance", effective_path))
        except FinanceTripletError as exc:
            reasons.append(f"{label} EffectiveFinanceConfig 非法: {exc}")

    planning: PlanningConfig | None = None
    if planning_path is not None:
        planning_doc = _load_yaml("planning_config", planning_path)
        try:
            planning = PlanningConfig.from_dict(planning_doc)
        except PlanningConfigError as exc:
            reasons.append(f"{label}规划配置非法: {exc}")
        else:
            for d in validate_planning_domain(planning):
                reasons.append(f"{label}规划配置领域校验失败: {d.params.get('detail') or d.code}")
    if reasons:
        raise ImportValidationError(reasons)
    result: dict = {}
    if profile is not None:
        result["profile"] = profile
    if overrides is not None:
        result["overrides"] = overrides
    if effective is not None:
        result["effective"] = effective
    if planning is not None:
        result["planning"] = planning
    return result


def _package_content_docs(entries: dict[str, bytes], manifest: dict) -> list[tuple[str, dict]]:
    """包内草稿与全部历史版本的内容文档(边界校验消费; 形状/解码非法即拒绝导入)。"""
    files_meta = manifest["files"]
    docs: list[tuple[str, dict]] = []

    def _content_of(raw: bytes, label: str) -> dict:
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ImportValidationError([f"{label}无法解析"]) from exc
        if not isinstance(doc, dict):
            raise ImportValidationError([f"{label}结构非法(期望对象)"])
        content = doc.get("content")
        if content is None:
            return {}
        if not isinstance(content, dict):
            raise ImportValidationError([f"{label}内容结构非法(期望对象)"])
        return content

    if "draft.json" in entries:
        label = "包内草稿文件"
        docs.append((label, _content_of(entries["draft.json"], label)))
    for version_path in files_meta.get("versions") or []:
        if version_path not in entries:
            continue
        label = f"包内版本文件 {version_path}"
        docs.append((label, _content_of(entries[version_path], label)))
    return docs


def _content_model_ref_ids(content: dict, label: str, reasons: list[str]) -> set[str]:
    """内容声明的项目模型引用键(device_id); 形状非法即记原因, 不静默归一为空集。

    字段缺席与显式 ``null`` 区分: 缺席按无引用处理; 字段存在时严格要求
    列表/对象/非空 ``device_id`` 字符串。
    """
    if "project_models" not in content:
        return set()
    refs = content["project_models"]
    if not isinstance(refs, list):
        reasons.append(f"{label}的 project_models 结构非法(期望列表)")
        return set()
    ids: set[str] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            reasons.append(f"{label}的 project_models 条目结构非法(期望对象)")
            continue
        device_id = ref.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            reasons.append(f"{label}的 project_models 条目缺 device_id")
            continue
        ids.add(device_id)
    return ids


def _is_carried_model_document(raw: bytes) -> bool:
    """携带模型文本可消费的最小结构: 可解码 JSON 对象且带 ``device`` 节。

    确认导入只按来源 ``base_device_id`` 改写 ``device.id``; schema/属性/
    接口/方程等正文校验由该权威保存路径完成, 此处不重复校验语义。
    """
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(doc, dict) and isinstance(doc.get("device"), dict)


def _check_content_structures(entries: dict[str, bytes], manifest: dict) -> set[str]:
    """内容文档消费结构校验 + 引用集合收集(上传入口, 先于其他内容消费者)。

    返回内容声明的 ``project_models`` 引用集合; 以下结构被后续校验与导入
    重映射直接消费, 形状非法即在此给可定位诊断(不落 500、不静默归一):

    - ``project_models`` 引用: 列表、条目对象、``device_id`` 非空字符串;
    - ``model`` 节: 对象、``devices``/``connections`` 列表、条目对象(设备类型
      重映射、证据/审计与设备/连接命令消费)。

    ``model`` 节按平台内容骨架(``initial_content``)要求存在——消费端按存在
    直接读取(``content["model"]["devices"]`` 等), 缺席与显式 ``null`` 同等
    拒绝。内容文档本身的解码与 ``content`` 对象形状由 ``_package_content_docs``
    保证(失败即 ``ImportValidationError``)。
    """
    reasons: list[str] = []
    referenced: set[str] = set()
    for label, content in _package_content_docs(entries, manifest):
        referenced |= _content_model_ref_ids(content, label, reasons)
        _check_model_section(content, label, reasons)
    if reasons:
        raise ImportValidationError(reasons)
    return referenced


def _check_model_section(content: dict, label: str, reasons: list[str]) -> None:
    """内容 ``model`` 节形状(设备/连接名键表): 消费端按存在直接读取。

    与平台内容骨架(``initial_content``)一致: ``model`` 对象、``devices``/
    ``connections`` 列表、条目对象; 字段缺席与显式 ``null`` 同等拒绝。
    """
    if "model" not in content:
        reasons.append(f"{label}内容缺 model 节")
        return
    model = content["model"]
    if not isinstance(model, dict):
        reasons.append(f"{label}的 model 节结构非法(期望对象)")
        return
    for key in ("devices", "connections"):
        if key not in model:
            reasons.append(f"{label}的 model 节缺 {key}")
        elif not isinstance(model[key], list):
            reasons.append(f"{label}的 model.{key} 结构非法(期望列表)")
        else:
            for entry in model[key]:
                if not isinstance(entry, dict):
                    reasons.append(f"{label}的 model.{key} 条目结构非法(期望对象)")


def _check_model_dependencies(
    entries: dict[str, bytes], manifest: dict, referenced: set[str]
) -> None:
    """3.0 项目模型依赖关系边界校验(上传入口一次校验, 任一失败 → 拒绝整个导入)。

    前置: ``_check_content_structures`` 已校验内容形状并给出引用集合; 本函数
    只校验携带侧与集合相等:

    - ``files.models`` 条目结构合法、``device_id`` 唯一、``model_text`` 文件在包内;
    - 包内携带的模型集合必须等于草稿与全部历史版本内容引用的集合:
      内容引用了未携带的模型 → 拒绝(不复用源行身份、不回填、不静默丢弃);
      携带了内容未引用的模型 → 拒绝(不把包内多余资产带进新项目)。

    模型正文的正式校验由确认导入的权威保存路径一次完成(提案阶段无目标项目)。
    """
    files_meta = manifest["files"]
    models_meta = files_meta.get("models")
    reasons: list[str] = []
    if models_meta is None:
        models_meta = []
    elif not isinstance(models_meta, list):
        reasons.append("清单 files.models 结构非法(期望列表)")
        models_meta = []
    carried: set[str] = set()
    for item in models_meta:
        if not isinstance(item, dict):
            reasons.append("清单 files.models 条目结构非法(期望对象)")
            continue
        device_id = item.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            reasons.append("清单 files.models 条目缺 device_id")
            continue
        if device_id in carried:
            reasons.append(f"清单 files.models 重复携带模型: {device_id}")
        if not isinstance(item.get("base_device_id"), str) or not item["base_device_id"]:
            reasons.append(f"模型 {device_id} 缺 base_device_id")
        path = item.get("model_text")
        raw = entries.get(path) if isinstance(path, str) else None
        if raw is None:
            reasons.append(f"模型 {device_id} 的 model_text 文件缺失: {path}")
        elif not _is_carried_model_document(raw):
            reasons.append(
                f"模型 {device_id} 的 model_text 不是模型文本(需 JSON 对象且带 device 节)"
            )
        carried.add(device_id)
    missing = sorted(referenced - carried)
    if missing:
        reasons.append("内容引用的项目模型未随包携带: " + ", ".join(missing))
    extra = sorted(carried - referenced)
    if extra:
        reasons.append("包内携带了内容未引用的项目模型: " + ", ".join(extra))
    if reasons:
        raise ImportValidationError(reasons)


def _check_version_dependencies(
    entries: dict[str, bytes], manifest: dict, baseline: ProjectBaseline
) -> None:
    """2.0 历史依赖关系边界校验(上传入口一次校验, 任一失败 → 拒绝整个导入)。

    - 各历史版本基线合法且与项目基线一致(确认阶段不再重复校验);
    - 版本元数据按数据版本唯一: 同一数据集不同版本不得共用元数据条目;
    - 带引用块的版本必须有 version_configs 对应条目, 每组依赖经同一正式
      解析器(字头/结构/领域)校验, 版本冻结引用与组内来源一致(Effective 的
      profile_id 等于组内 Profile 稳定 id, 内容等于精确来源合并结果);
    - 数据集元数据的每个文件条目必须在同目录有唯一匹配的暂存文件。
    """
    files_meta = manifest.get("files") or {}
    vc_meta = files_meta.get("version_configs") or {}
    if not isinstance(vc_meta, dict):
        raise ImportValidationError(["清单 files.version_configs 结构非法(期望映射)"])
    version_paths = files_meta.get("versions") or []
    for key in vc_meta:
        if f"versions/{key}.json" not in version_paths:
            raise ImportValidationError([f"version_configs 引用了不存在的版本: {key}"])
    for version_path in version_paths:
        if version_path not in entries:
            continue
        try:
            doc = json.loads(entries[version_path].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ImportValidationError([f"包内版本文件无法解析: {version_path}"]) from exc
        if not isinstance(doc, dict):
            raise ImportValidationError([f"包内版本文件结构非法: {version_path}"])
        ver_meta = doc.get("version") or {}
        version_no = ver_meta.get("version_no", "?")
        version_baseline_errors = ProjectBaseline.validate(ver_meta.get("project_baseline"))
        if version_baseline_errors:
            raise ImportValidationError(
                [
                    f"包内版本 {version_no} 项目计算基线非法: "
                    f"{d.params.get('detail') or d.code}"
                    for d in version_baseline_errors
                ]
            )
        try:
            version_baseline = ProjectBaseline.from_dict(ver_meta.get("project_baseline"))
        except ProjectBaselineError as exc:
            raise ImportValidationError(
                [f"包内版本 {version_no} 项目计算基线非法: {exc}"]
            ) from exc
        if (
            version_baseline.resolution != baseline.resolution
            or version_baseline.start_year != baseline.start_year
            or version_baseline.scenario_mode != baseline.scenario_mode
        ):
            raise ImportValidationError(
                [f"包内版本 {version_no} 项目计算基线与项目不一致(版本必须与项目共享同一基线)"]
            )
        key = version_path.removeprefix("versions/").removesuffix(".json")
        content = doc.get("content") or {}
        eff = content.get("effective_finance") or {}
        plan = content.get("planning_config") or {}
        if not eff and not plan:
            continue
        entry = vc_meta.get(key)
        if not isinstance(entry, dict):
            raise ImportValidationError([f"版本 {key} 带配置引用但缺 version_configs 条目"])
        label = f"版本 {key} 的历史依赖"
        group = _parse_triplet_group(
            entries,
            entry.get("finance_profile") if eff else None,
            entry.get("finance_overrides") if eff else None,
            entry.get("effective_finance") if eff else None,
            entry.get("planning_config") if plan else None,
            label=label,
        )
        if eff:
            for need in ("profile", "overrides", "effective"):
                if need not in group:
                    raise ImportValidationError([f"版本 {key} 的财务依赖文件缺失"])
            if group["effective"].profile_id != group["profile"].profile_id:
                raise ImportValidationError([f"版本 {key} 的 Effective 与 Profile 来源不一致"])
            if group["overrides"].profile_ref["id"] != group["profile"].profile_id:
                raise ImportValidationError([f"版本 {key} 的 Overrides 与 Profile 来源不一致"])
            if eff.get("profile_id") != group["profile"].profile_id:
                raise ImportValidationError(
                    [f"版本 {key} 的冻结 Effective 引用与历史依赖 Profile 来源不一致"]
                )
            # 外部输入核心财务约束: 历史组同样从精确来源合并确定结果。
            expected = merge_effective(group["profile"], group["overrides"])
            if expected.to_dict() != group["effective"].to_dict():
                raise ImportValidationError([f"版本 {key} 的 Effective 与来源合并结果不一致"])
        if plan and "planning" not in group:
            raise ImportValidationError([f"版本 {key} 的规划依赖文件缺失"])
    for meta_path in files_meta.get("datasets") or []:
        if meta_path not in entries:
            continue
        try:
            meta = json.loads(entries[meta_path].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ImportValidationError([f"包内数据集元数据无法解析: {meta_path}"]) from exc
        if not isinstance(meta, dict) or not isinstance(meta.get("dataset_version_id"), int):
            raise ImportValidationError([f"包内数据集元数据缺 dataset_version_id: {meta_path}"])
        base = meta_path.removesuffix("/dataset.json")
        for file_meta in meta.get("files") or []:
            kind = (file_meta or {}).get("file_kind")
            matches = [
                p for p in entries
                if p.startswith(base + "/") and p != meta_path
                and p.rsplit("/", 1)[-1].split(".", 1)[0] == kind
            ]
            if len(matches) != 1:
                raise ImportValidationError([f"数据集版本数据文件缺失或歧义: {meta_path} {kind}"])


def import_proposal(
    db: Session,
    user: UserRecord,
    file_bytes: bytes,
    idempotency_key: str | None = None,
) -> ImportProposalRecord:
    """创建导入提案: 校验 → 暂存对象 → 提案记录 → 校验报告(domain-model §对象生命周期)。

    - 校验失败(格式/兼容性/清单/完整性/历史依赖关系)抛 ImportValidationError,
      不创建任何记录;
    - 幂等: 调用方传入 idempotency_key 时, 同一提议人已有相同键的未确认提案
      直接返回(不重复暂存); 无键时每次调用新建提案(对象存储无内容去重,
      源包字节相同也不复用对象行);
    - 暂存: 包内全部对象按对象 id 落盘, 源包落盘为新对象;
    - 提案阶段不创建普通项目(无目标项目身份); 拟创建项目快照(名称/币种/
      基线)与校验报告写入 review_summary(review_errors 空);
    - 暂存对象与源包由提案登记存储引用, 提案存续期间不被当孤儿;
    - 确认导入见 confirm_import(确认时才创建新项目身份, 导入者成为所有者)。
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
    # 源包落盘为新对象(每次提案新建对象行; 仅此一次, 结果记入提案)
    source_obj = put_object(
        db,
        file_bytes,
        PACKAGE_MEDIA_TYPE,
        source_category="import_package_source",
    )

    # 2) 拟创建项目快照: 只做命名/币种/基线的输入校验与快照记录, 不创建
    # 普通项目(确认时才创建新项目身份, 导入者成为所有者, 原授权不迁移)。
    project_meta = manifest.get("project") or {}
    name = str(project_meta.get("name") or "导入项目")
    currency = project_meta.get("currency") or "CNY"
    if currency not in ("CNY", "USD"):
        raise ImportValidationError([f"包内币种非法: {currency}"])
    # 项目计算基线(0.6.5 事项 1): 包必须携带完整基线, 缺失/非法一律拒绝导入
    # (不静默默认; 导入内容必须携带完整项目基线)。
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
    # 1b) 内容文档消费结构 + 引用集合(先于历史配置依赖消费内容)
    referenced = _check_content_structures(entries, manifest)
    # 1c) 历史依赖关系边界校验(提案行创建之前; 失败不建提案行)
    _check_version_dependencies(entries, manifest, baseline)
    # 1d) 项目模型依赖关系边界校验(3.0; 与历史配置依赖同一上传入口)
    _check_model_dependencies(entries, manifest, referenced)
    # 规划/财务配置 revision(0.6.5 事项 3): 包内配置严格校验(缺失 = 导入后
    # 无配置, 不静默默认; 非法 → 拒绝整个导入)。
    package_configs = parse_config_files(entries, manifest)
    schema_version = int(project_meta.get("schema_version", 1) or 1)

    # 3) 导入提案(校验报告 + 分区提交内容, 01 §10.4)
    # 不再写 source_path(该列可空且无任何消费点) —— storage_path 属
    # §11 内部路径, 不得进入审计记录; 可追溯性由 source_object_id
    # (对象存储对象外键)承担。提案行经 package 域 repository 创建,
    # 不直接访问提案表。提案阶段无目标项目(project_id 缺席)。
    proposal = _create_proposal(
        db,
        proposer_id=user.id,
        source_type="json",
        source_object_id=source_obj.id,
    )
    # 提案持有暂存对象引用: 源包与包内暂存对象在提案存续期间不被当孤儿;
    # 与提案同事务提交/回滚, 不新增校验。
    add_ref(
        db,
        source_obj.id,
        "import_proposal",
        proposal.id,
        ref_entity_type="import_proposals",
        purpose="proposal_source",
    )
    for _path, obj in sorted(staged.items()):
        add_ref(
            db,
            obj.id,
            "import_proposal",
            proposal.id,
            ref_entity_type="import_proposals",
            purpose="proposal_staging",
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
                "description": project_meta.get("description"),
                "project_baseline": baseline.to_dict(),
                "schema_version": schema_version,
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
        result={"source_object_id": source_obj.id, "staged_objects": len(staged)},
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

    分区提交内容(同一数据库事务: 新项目身份/所有权/草稿/历史版本/当前配置/
    对象引用/提案应用状态一并发布; 失败全回滚, 不留半成品, 提案可重试):
    - 新项目: 按提案快照创建项目身份(名称去重, 导入者即所有者);
    - 数据集: 重建 Dataset/DatasetVersion/DatasetFile(引用暂存对象, 原标识重映射);
    - 项目模型: 按包内携带的已冻结规范模型文本经权威保存路径重建本项目模型资产
      (新编号/新行 id, 源行身份不复用), 草稿与历史版本的引用与设备类型串
      映射到新身份;
    - 草稿: revision=1 领域内容(数据集绑定重映射, 证据来源登记);
    - 版本: 按包内版本顺序重建 ProjectVersion(新身份版本号, 不倒写原版本);
    - 配置: 消费提案已接受的包内规划/财务配置发布 revision 行(上传边界
      是唯一正式校验, 确认只消费及保存; 失败 → 整个事务回滚, 不落任何行);
    - 证据: 历史结果作为证据来源保留 — 登记对象引用(imported_evidence)与
      评估摘要, 不创建本地任务(不伪造本地任务, domain-model §快照、任务和结果)。

    导入约束: 不得静默覆盖(名称去重 + 新项目身份); 账号/权限/会话不随包导入;
    导入者成为新项目所有者; 原授权关系不迁移。确认重放返回同一已应用项目。
    同一提案的并发确认经提案行锁串行化, 等候者返回同一项目。
    """
    # 确认领取(同一提案串行化): 原子抢占行锁, 持有到本事务提交/回滚。
    # 未抢到说明行已被他方确认, 重读最新状态并按状态机处理。
    proposal = _claim_proposal_for_confirm(db, proposal_id)
    if proposal is None:
        # 丢弃本事务内可能已读的旧快照, 等候者必须看到最新 applied, 不得
        # 沿用身份缓存中的旧 proposed。
        db.expire_all()
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
    if proposal.status == "applied":
        project = project_domain.get_project(db, proposal.project_id)
        if project is None:
            raise NotFoundError("导入提案关联项目缺失", params={"proposal_id": proposal_id})
        return project  # 幂等重放: 已导入则返回同一导入结果项目
    if proposal.status == "rejected":
        raise ConflictError(
            "导入提案已被拒绝", params={"proposal_id": proposal_id, "status": proposal.status}
        )
    if proposal.status not in ("proposed", "validated", "approved"):
        raise ConflictError(
            "导入提案状态不允许确认", params={"proposal_id": proposal_id, "status": proposal.status}
        )
    # 取分区提交内容: 消费提案阶段已接受的源包与暂存(解码读取, 不重新
    # 跑上传边界的完整性/语义校验; 结构缺失则 loudly 拒绝)。快照各必备键
    # 由上传边界写入, 此处直接读取, 不做静默默认、不重新 from_dict 校验。
    summary = proposal.review_summary or {}
    source_object_id = (summary.get("staging") or {}).get("source_object_id")
    file_bytes = get_object(db, int(source_object_id))
    manifest, entries = _read_staged_package(file_bytes)
    # 确认时创建新项目身份(每次导入新身份, 导入者成为所有者; 名称按快照去重)。
    snapshot = summary["project_snapshot"]
    snap_baseline = snapshot["project_baseline"]
    project = project_domain.create_project(
        db,
        name=_unique_project_name(db, snapshot["name"]),
        owner_id=user.id,
        created_by=user.id,
        description=snapshot.get("description"),
        currency=snapshot["currency"],
        baseline_resolution=snap_baseline["resolution"],
        baseline_start_year=snap_baseline["start_year"],
        baseline_scenario_mode=snap_baseline["scenario_mode"],
        schema_version=snapshot["schema_version"],
    )
    # 提案阶段暂存的对象 id 按包内路径索引(确认阶段不再按摘要查找对象)
    staged_by_path = {
        str(item.get("path")): int(item.get("object_id"))
        for item in (summary.get("staging") or {}).get("objects", [])
        if isinstance(item, dict) and item.get("path") is not None and item.get("object_id") is not None
    }

    # 1) 数据集(同源 dataset 只建一次, 各版本分别重建; 原数据集版本标识 →
    # 新标识; 数据文件按元数据同目录唯一匹配)
    dataset_id_map: dict[int, int] = {}
    new_dataset_by_source: dict[Any, int] = {}
    for meta_path in manifest.get("files", {}).get("datasets", []):
        if meta_path not in entries:
            continue
        meta = json.loads(entries[meta_path].decode("utf-8"))
        ds_meta = meta.get("dataset") or {}
        ver_meta = meta.get("version") or {}
        group_key = meta.get("dataset_id", meta_path)
        if group_key in new_dataset_by_source:
            dataset_id = new_dataset_by_source[group_key]
        else:
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
            dataset_id = dataset.id
            new_dataset_by_source[group_key] = dataset_id
        version = dataset_domain.create_version(
            db,
            dataset_id=dataset_id,
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
            # 边界已保证同目录同 kind 唯一; 此处只消费, 暂存若被篡改则读取错误自然可见。
            match = next(
                p for p in staged_by_path
                if p.startswith(base + "/") and p != meta_path
                and p.rsplit("/", 1)[-1].split(".", 1)[0] == kind
            )
            dataset_domain.add_file(
                db,
                dataset_version_id=version.id,
                object_id=staged_by_path[match],
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

    def _ensure_profile(entry_bytes: bytes, label: str) -> configuration_domain.FinanceProfileRecord:
        """登记包内 Profile 来源并返回注册行(同 id 复用既有行)。

        上传边界已做唯一正式解析; 此处只解码消费, 不跑 from_dict。
        既有行内容与包内来源不一致即身份冲突, 明确拒绝, 不偷偷沿用。
        """
        doc = yaml_load(entry_bytes.decode("utf-8"))
        # 已接受内容的解码消费: 字段由上传边界保证, 此处直接取值, 不做形状设防。
        stable_id = doc["profile"]["id"]
        existing = configuration_domain.get_profile(db, stable_id)
        if existing is not None:
            if existing.content != doc:
                raise ImportValidationError(
                    [f"{label} 的 Profile 身份冲突: 稳定 id 在目标库已有不同内容, 拒绝导入"]
                )
            return existing
        obj = put_object(
            db,
            entry_bytes,
            "application/yaml",
            source_category="finance_profile",
        )
        row = configuration_domain.register_profile(
            db,
            profile_id=stable_id,
            region=doc["profile"]["region"],
            content=doc,
            object_id=obj.id,
            created_by=user.id,
        )
        add_ref(
            db,
            obj.id,
            "finance_profile",
            row.id,
            ref_entity_type="finance_profiles",
            purpose="finance_profile_yaml",
        )
        db.flush()
        return row

    # 2) 历史配置依赖重建(来源 revision → 新 revision 映射; 新编号, 不复用
    # 来源数字; 同一来源只建一次。Profile 按 Effective 记录的稳定 id 解析,
    # 缺席即拒绝, 绝不用当前导出 Profile 冒充历史)
    config_rev_map: dict[tuple[str, Any], int] = {}
    vc_meta = manifest.get("files", {}).get("version_configs") or {}
    for version_path in sorted(manifest.get("files", {}).get("versions", [])):
        if version_path not in entries:
            continue
        doc = json.loads(entries[version_path].decode("utf-8"))
        key = version_path.removeprefix("versions/").removesuffix(".json")
        content = (doc.get("content") or {}) if isinstance(doc, dict) else {}
        eff = content.get("effective_finance") or {}
        plan = content.get("planning_config") or {}
        if not eff and not plan:
            continue
        # 上传边界已保证带引用块的版本必有 version_configs 字典条目; 此处直接消费。
        vc_entry = vc_meta[key]
        if eff:
            # 包内来源已在上传边界过正式解析; 此处只解码消费(普通读取)。
            # Profile 自包含: 注册包内来源(同 id 复用既有行), 内容不一致
            # 即身份冲突, 明确拒绝, 不偷偷沿用目标库内容。
            p_path = vc_entry.get("finance_profile")
            o_path = vc_entry.get("finance_overrides")
            e_path = vc_entry.get("effective_finance")
            o_doc = yaml_load(entries[o_path].decode("utf-8"))
            e_doc = yaml_load(entries[e_path].decode("utf-8"))
            _ensure_profile(entries[p_path], f"版本 {key}")
            src_ovr_rev = eff.get("overrides_revision")
            if ("overrides", src_ovr_rev) not in config_rev_map:
                new_ovr = configuration_domain.next_overrides_revision(db, project.id)
                configuration_domain.append_overrides(
                    db,
                    project_id=project.id,
                    revision=new_ovr,
                    content=o_doc,
                    profile_id=(o_doc.get("profile_ref") or {}).get("id"),
                    created_by=user.id,
                )
                config_rev_map[("overrides", src_ovr_rev)] = new_ovr
            src_eff_rev = eff.get("revision")
            if ("effective", src_eff_rev) not in config_rev_map:
                obj = put_object(
                    db, entries[e_path], "application/yaml",
                    source_category="project_package_history",
                )
                new_eff = configuration_domain.next_effective_revision(db, project.id)
                eff_row = configuration_domain.append_effective(
                    db,
                    project_id=project.id,
                    revision=new_eff,
                    content=e_doc,
                    profile_id=e_doc.get("profile_id"),
                    created_by=user.id,
                    content_object_id=obj.id,
                )
                # 历史 Effective 内容对象同样登记引用(与确认同事务回滚)。
                add_ref(
                    db,
                    obj.id,
                    "effective_finance_revision",
                    eff_row.id,
                    ref_entity_type="effective_finance_revisions",
                    purpose="effective_finance_content",
                )
                config_rev_map[("effective", src_eff_rev)] = new_eff
        if plan:
            p_doc = yaml_load(entries[vc_entry.get("planning_config")].decode("utf-8"))
            src_plan_rev = plan.get("revision")
            if ("planning", src_plan_rev) not in config_rev_map:
                new_plan = configuration_domain.next_planning_revision(db, project.id)
                configuration_domain.append_planning(
                    db,
                    project_id=project.id,
                    revision=new_plan,
                    content=p_doc,
                    created_by=user.id,
                )
                config_rev_map[("planning", src_plan_rev)] = new_plan

    # 3) 证据来源: 历史结果作为证据来源保留(不伪造本地任务, domain-model §快照、任务和结果)
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

    # 4) 草稿(revision=1): 领域内容 + 证据来源登记
    draft_doc = json.loads(entries["draft.json"].decode("utf-8"))
    draft_content = _remap(dict(draft_doc.get("content") or {}))
    draft_content["imported_evidence"] = imported_evidence
    _create_draft_row(db, project.id, draft_content, user)

    # 4b) 项目模型依赖重建(3.0 files.models): 按包内来源经权威保存路径重建
    # 本项目模型资产(新 suffix/新行 id, 不复用源身份; 包内携带模型规范文本
    # [紧凑 JSON], 其 device.id 是来源最终 ID, 按来源 base_device_id 提交后
    # 由保存路径分配新 suffix), 再把草稿与每个历史版本内容中的引用与设备
    # 类型串映射到新 ID。
    model_map: dict[str, dict[str, Any]] = {}
    for item in manifest.get("files", {}).get("models") or []:
        source_device_id = str(item["device_id"])
        doc = json.loads(entries[str(item["model_text"])].decode("utf-8"))
        doc["device"]["id"] = str(item["base_device_id"])
        saved = _save_project_model_in_transaction(
            db,
            user,
            project.id,
            model_yaml=yaml_dump(doc),
            expected_revision=project_domain.require_current_draft(db, project).revision,
            source="direct_yaml",
        )["project_model"]
        model_map[source_device_id] = {
            "id": str(saved["id"]),
            "device_id": saved["device_id"],
            "revision": saved["revision"],
        }
    if model_map:
        # 保存路径已按本项目清单重建草稿 project_models 引用; 这里改写草稿
        # model 节设备实例的 device_type 并推进草稿(源引用不残留)。
        current = project_domain.require_current_draft(db, project)
        content = _load_content_object(db, current.content_object_id)
        _remap_model_device_types(content, model_map)
        project_domain.create_draft(
            db,
            project_id=project.id,
            content_object_id=_store_content_object(db, content),
            updated_by=user.id,
        )

    # 5) 版本: 按包内版本顺序重建(新身份版本号, parent 链按导入顺序)
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
        # 基线已在上传边界校验(提案已接受); 此处只消费, 不重复语义校验。
        version_content = _remap(dict((doc.get("content") or {}) if isinstance(doc, dict) else {}))
        # 配置引用改写: 来源 revision → 新项目映射后 revision(映射与重建在
        # 同一遍构造, 缺席的块保持缺席; 暂存若被篡改则读取错误自然可见)。
        eff = version_content.get("effective_finance")
        if eff is not None:
            for kind, field in (("overrides", "overrides_revision"), ("effective", "revision")):
                eff[field] = config_rev_map[(kind, eff.get(field))]
        plan = version_content.get("planning_config")
        if plan is not None:
            plan["revision"] = config_rev_map[("planning", plan.get("revision"))]
        # 项目模型引用/设备类型串改写: 来源身份 → 本项目重建身份(与草稿同一映射)。
        _remap_model_refs(version_content, model_map)
        _remap_model_device_types(version_content, model_map)
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

    # 6) 当前工作状态配置: 上传边界已做唯一正式校验, 此处只解码消费,
    # 经 configuration 拥有者的公开 flush 能力一次发布(登记 Profile →
    # 追加 Overrides/Effective[/Planning] 行 + 移动项目指针), 与确认事务
    # 同提交, 不重跑作者输入校验/合并器; 无规划则当前规划保持缺席。
    # 失败 → 整个事务回滚, 不落任何行。
    configs_meta = manifest.get("files", {}).get("configs") or {}
    if all(configs_meta.get(k) for k in ("finance_profile", "finance_overrides", "effective_finance")):
        try:
            profile_row = _ensure_profile(entries[configs_meta["finance_profile"]], "当前工作状态")
            overrides_doc = yaml_load(entries[configs_meta["finance_overrides"]].decode("utf-8"))
            effective_doc = yaml_load(entries[configs_meta["effective_finance"]].decode("utf-8"))
            planning_doc = (
                yaml_load(entries[configs_meta["planning_config"]].decode("utf-8"))
                if configs_meta.get("planning_config") else None
            )
            _publish_accepted_finance_state(
                db,
                project.id,
                profile_row=profile_row,
                overrides_doc=overrides_doc,
                effective_doc=effective_doc,
                effective_bytes=entries[configs_meta["effective_finance"]],
                planning_doc=planning_doc,
                user_id=user.id,
            )
        except AppError as exc:
            raise ImportValidationError(
                [f"包内配置在确认阶段发布失败: {getattr(exc, 'code', 'PKG-IMP-001')} {exc}"]
            ) from exc

    # 7) 提案收尾 + 审计(提案状态经 package 域状态机推进, 写入新项目关联)
    _set_proposal_review(
        db,
        proposal.id,
        status="applied",
        decided_by=user.id,
        project_id=project.id,
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
            "imported_project_models": len(model_map),
            "evidence_objects": len(imported_evidence),
            "imported_configs": len([v for v in configs_meta.values() if v]),
            "mapped_config_revisions": len(config_rev_map),
        },
    )
    # 重读项目行: 版本/草稿创建已移动 current 指针, 入口快照已过期
    return project_domain.require_project(db, project.id)


def _read_staged_package(file_bytes: bytes) -> tuple[dict, dict[str, bytes]]:
    """读取已接受提案的源包(普通 zip/JSON 解码, 不是重新校验)。

    上传边界已做完整校验; 此处只做读取, 暂存若被篡改则读取错误自然可见。
    """
    zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    with zf:
        entries = {
            info.filename: zf.read(info) for info in zf.infolist() if not info.is_dir()
        }
    manifest = json.loads(entries["manifest.json"].decode("utf-8"))
    return manifest, entries


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


def _is_sealed_evaluation(section: Any) -> bool:
    """是否为封存评价块(分发表单, 非兼容回退)。

    仅当节是 ``status == "evaluated"`` 的封存字典时走专用展平; 旧
    ``financial`` 等真实业务摘要仍走通用节渲染(原样保留, 不删除)。
    """
    return isinstance(section, dict) and section.get("status") == "evaluated"


def _sealed_evaluation_rows(fin: dict[str, Any]) -> list[tuple[str, Any]]:
    """已封存评价块 → 展示行(纯展示格式化, 不重算、不读最新配置)。

    必需字段直接消费, 缺件自然失败, 不跳行、不回退; 金额均为封存字符
    串原样展示, 不转浮点、不舍入。仅 IRR/回收期数值为领域允许 null
    (状态字段仍必需)。
    """
    evaluation = fin["evaluation"]
    ccy = fin["currency"]
    rows: list[tuple[str, Any]] = [
        ("评价年限 E / Evaluation years", fin["evaluation_years"]),
        ("建设期 K(财务时点 t=0..K) / Construction period", fin["construction_period"]),
        ("运营年限 N(第 j 年落 t=K+j) / Operation years", fin["operation_years"]),
        ("币种 / Currency", ccy),
        ("价格口径 / Price basis", fin["price_basis"]),
        ("基年 / Base year", fin["base_year"]),
        ("折现率(冻结) / Discount rate", fin["discount_rate"]),
        (
            "建设总额 C / Construction total",
            f"{fin['construction_total']} {fin['construction_total_unit']}",
        ),
    ]
    for item in fin["payments_applied"]:
        rows.append(
            (
                f"建设付款 t={item['t']} / Payment t={item['t']}",
                f"{item['ratio']} × C = {item['amount']} {ccy}",
            )
        )
    for index, item in enumerate(fin["annual_operation"]):
        rows.append(
            (
                f"运营年第 {index + 1} 年(t={item['financial_year']}) / Year {index + 1}",
                f"能源 {item['energy_net']} − 可变 {item['variable_om']} "
                f"− 固定 {item['fixed_om']} − 维护 {item['maintenance']} "
                f"= 净额 {item['net']} {ccy}",
            )
        )
    for item in evaluation["rows"]:
        rows.append(
            (
                f"CF[t={item['year']}] 净额 / Net",
                f"{item['net']} {ccy}",
            )
        )
        rows.append(
            (
                f"CF[t={item['year']}] 贴现/累计/贴现累计",
                f"{item['discounted']} / {item['cumulative']} / "
                f"{item['cumulative_discounted']} {ccy}",
            )
        )
    rows.append(("净现值 NPV / NPV", f"{evaluation['npv']} {evaluation['npv_unit']}"))
    irr = evaluation["irr"]
    irr_text = (
        f"{irr} / {evaluation['irr_status']}"
        if irr is not None
        else f"— / {evaluation['irr_status']}"
    )
    rows.append(("内部收益率 IRR / IRR", irr_text))
    for key, label in (("static", "静态回收期"), ("dynamic", "动态回收期")):
        value = evaluation[f"{key}_payback"]
        if value is not None:
            rows.append(
                (
                    f"{label} / {key} payback",
                    f"{value} {evaluation[f'{key}_payback_unit']} / {evaluation[f'{key}_status']}",
                )
            )
        else:
            rows.append((f"{label} / {key} payback", f"— / {evaluation[f'{key}_status']}"))
    return rows


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
    ensure_access(db, user, project_id, "export_excel")
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
            type_name = dev.get("device_type") or "—"
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
            _first_section(evidence_content, "financial", "finance", "financial_evaluation"),
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
        # 财务摘要表: 封存评价块走专用展平, 其他真实业务摘要(如旧
        # financial 节)仍走通用节渲染; 非财务表一律通用渲染。
        if title_zh == "财务摘要" and _is_sealed_evaluation(section):
            rows = _sealed_evaluation_rows(section)
        else:
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
