"""序列预备事务式发布用例(0.6.5 事项 3)。

对应 modules/application.md「典型示例:预备项目计算序列」: 输入是项目 ID、
项目 revision 和尚未完成预备的项目模型实例, 用例直接使用项目创建时已固定
的计算基线, 不接收时间轴、重采样、预测算法或算法参数配置:

1. 授权并固定项目计算基线摘要以及项目草稿 revision;
2. 枚举模型实例中的 ``predefined`` interfaces, 分别解析
   ``constant/data_repeat/data_predict`` 来源并调用 sequence_prep 域服务
   完成预备(纯计算, 不落盘);
3. 任一接口预备失败: 返回聚合阻断诊断, 不写对象、不替换任何引用;
4. 全部成功: 原子提交预备对象/回执/训练产物、替换模型实例的预备引用
   (同一事务: 解绑旧引用 + 绑定新引用)并推进项目草稿 revision;
5. 任一写入或引用替换失败: 整体回滚, 模型引用保持上一份已验证引用;
   提供 ``rollback_prepared_sequences`` 显式回滚到上一份已验证状态。

引用权威是对象 owner 引用(存储公开门面, 宪法 10.3); 预备产物以
``sequence_prep:canonical|receipt|artifact:{interface_id}`` 目的挂到模型
实例, 原始输入文件(purpose ``data:{data_ref}``)保留作摘要链追溯。本用例
不跨模块 ORM 直查、不修改 core/timeaxis.py / Dataset 时间戳 / Worker 运行
逻辑, 不实现 Solver Bundle / GeneratorProvider / 运行期插件热加载。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from iesplan.application.projects.model_save import FINAL_OWNER_NAMESPACE
from iesplan.core.contracts import ProjectBaseline
from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.idgen import sha256_hex
from iesplan.devices.contracts2 import DeviceModelDocument
from iesplan.models.project import Project
from iesplan.models.project_model import ProjectModel
from iesplan.sequence_prep import (
    DataRepeatSpec,
    PredictFile,
    PredictSpec,
    PreparedSequence,
    prepare_constant,
    prepare_data_predict,
    prepare_data_repeat,
)
from iesplan.services import project as project_service
from iesplan.storage import (
    ReferenceNotFoundError,
    attach,
    detach,
    find_refs_by_owner,
    get_object,
    object_info,
    put_object,
)

#: 发布被拒诊断码(登记于 core/diagnostics.py NEW_DIAG_CODES)
PROJ_PREP_REJECTED = "PROJ-PREP-001"

#: 预备产物对象媒体类型
PREP_DATA_MEDIA_TYPE: str = "text/csv; charset=utf-8"
PREP_RECEIPT_MEDIA_TYPE: str = "application/json"
PREP_ARTIFACT_MEDIA_TYPE: str = "application/json"

#: 预备引用 purpose 前缀(挂到模型实例; 与 model_save 的 data:{data_ref} 并存)
PREP_CANON_PURPOSE = "sequence_prep:canonical:"
PREP_RECEIPT_PURPOSE = "sequence_prep:receipt:"
PREP_ARTIFACT_PURPOSE = "sequence_prep:artifact:"


class PrepPublishRejectedError(AppError):
    """序列预备失败, 发布被拒绝(HTTP 400; 不生成正式产物、不替换模型引用)。

    诊断明细入 ``params.diagnostics``(结构可定位), 与 ModelCandidateRejectedError
    同构(见 application/projects/model_save.py)。
    """

    code = PROJ_PREP_REJECTED
    severity = "error"
    message_key = "ies.diag.proj.prep_rejected"
    http_status = 400


def _diag(detail: str, *, field: str | None = None) -> Diagnostic:
    location: dict[str, object] = {"object_type": "sequence_prep"}
    if field is not None:
        location["field"] = field
    return make_diag(
        PROJ_PREP_REJECTED,
        severity="error",
        blocking=True,
        params={"detail": detail},
        location=location,
    )


def _reject(diagnostics: list[Diagnostic]) -> None:
    raise PrepPublishRejectedError(
        "",
        params={"diagnostics": [d.to_dict() for d in diagnostics], "count": len(diagnostics)},
        location={"object_type": "sequence_prep"},
    )


def _project_baseline(project: Project) -> ProjectBaseline:
    """从项目行重建不可变基线并核对摘要(摘要不符视为数据损坏)。"""
    baseline = ProjectBaseline(
        resolution=project.baseline_resolution,
        leap_year=project.baseline_leap_year,
        scenario_mode=project.baseline_scenario_mode,
    )
    if baseline.digest() != project.baseline_sha256:
        raise AppError(
            "项目计算基线摘要与规范化算法不一致(数据损坏)",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "project", "object_id": project.id},
        )
    return baseline


def _load_model_document(db: Session, model: ProjectModel) -> DeviceModelDocument:
    """从对象存储读取模型规范字节并重建文档(经公开门面校验完整性)。"""
    from iesplan.devices.parser2 import parse_device_model_v2

    raw = get_object(db, model.model_object_id)
    try:
        mapping = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AppError(
            "项目模型规范对象损坏",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "project_model", "object_id": model.id},
        ) from None
    result = parse_device_model_v2(mapping, file=f"<project_model:{model.id}>")
    if not result.ok or result.document is None:
        _reject(list(result.diagnostics))
    assert result.document is not None
    return result.document


def _resolve_attached_file(
    db: Session, model_id: int, ref_key: str
) -> tuple[bytes, str] | None:
    """按 purpose ``data:{ref_key}`` 取模型实例附着的原始输入文件(字节+摘要)。

    原始文件在模型保存时以 model_save 的 ``data:{data_ref}`` purpose 绑定;
    缺失返回 None(由调用方聚合为阻断诊断)。
    """
    refs = find_refs_by_owner(db, FINAL_OWNER_NAMESPACE, model_id, FINAL_OWNER_NAMESPACE)
    for ref in refs:
        if ref.get("purpose") == f"data:{ref_key}":
            handle = object_info(db, ref["object_id"])
            return get_object(db, ref["object_id"]), str(handle["sha256"])
    return None


def _prepare_model_interfaces(
    db: Session,
    document: DeviceModelDocument,
    baseline: ProjectBaseline,
    model_id: int,
    specs: Mapping[str, Any],
) -> tuple[dict[str, PreparedSequence], list[Diagnostic]]:
    """预备模型实例的全部 predefined 接口。

    ``specs``: {data_ref → 预备规格}; ``constant`` 接口无需规格自动展开,
    ``data_repeat`` 规格为 {"semantics": {列: 语义}}, ``data_predict`` 规格为
    {"feature_columns": [...], "feature_semantics": {...}, "training_input_ref":
    str, "training_target_ref": str, "prediction_input_ref": str}。
    返回 (interface_id → PreparedSequence, diagnostics); 任一失败返回全部
    诊断且不产出部分映射。
    """
    groups: dict[str, list[str]] = {}  # data_ref(或 "constant") → 接口 id 列表
    constant_iids: list[str] = []
    for iid, iface in document.interfaces.items():
        if iface.type != "predefined" or iface.source is None:
            continue
        if iface.source.mode == "constant":
            constant_iids.append(iid)
        elif iface.source.mode in ("data_repeat", "data_predict"):
            data_ref = iface.source.data_ref
            if data_ref is None:
                return {}, [_diag(f"预定义接口 {iid} 缺少 data_ref", field=iid)]
            groups.setdefault(data_ref, []).append(iid)
        else:  # pragma: no cover - devices 契约已限定三种来源
            return {}, [_diag(f"预定义接口 {iid} 来源模式非法: {iface.source.mode!r}", field=iid)]

    per_interface: dict[str, PreparedSequence] = {}
    diagnostics: list[Diagnostic] = []

    if constant_iids:
        outcome = prepare_constant(document, baseline)
        if not outcome.ok or outcome.result is None:
            diagnostics.extend(outcome.diagnostics)
        else:
            per_interface.update({iid: outcome.result for iid in constant_iids})

    for data_ref, iids in groups.items():
        spec = dict(specs.get(data_ref) or {})
        first_iface = document.interfaces[iids[0]]
        mode = first_iface.source.mode if first_iface.source is not None else ""
        if mode == "data_repeat":
            resolved = _resolve_attached_file(db, model_id, data_ref)
            if resolved is None:
                diagnostics.append(
                    _diag(f"缺少模型实例附着的原始输入文件: {data_ref}", field=f"data:{data_ref}")
                )
                continue
            raw_bytes = resolved[0]
            semantics = {str(k): str(v) for k, v in dict(spec.get("semantics") or {}).items()}
            outcome = prepare_data_repeat(
                document, baseline, raw_bytes, data_ref, DataRepeatSpec(semantics=semantics)
            )
        else:  # data_predict: 显式三个输入文件引用(训练输入/训练目标/预测输入)
            required = ("feature_columns", "training_input_ref",
                           "training_target_ref", "prediction_input_ref")
            missing = [key for key in required if not spec.get(key)]
            if missing:
                diagnostics.append(
                    _diag(
                        f"data_predict 引用 {data_ref} 缺少显式输入声明: {missing}",
                        field=data_ref,
                    )
                )
                continue
            predict_spec = PredictSpec(
                data_ref=data_ref,
                training_input_ref=str(spec["training_input_ref"]),
                training_target_ref=str(spec["training_target_ref"]),
                prediction_input_ref=str(spec["prediction_input_ref"]),
                feature_columns=tuple(str(c) for c in spec["feature_columns"]),
                feature_semantics={
                    str(k): str(v) for k, v in dict(spec.get("feature_semantics") or {}).items()
                },
            )
            files: list[tuple[str, PredictFile]] = []
            ok = True
            for ref_key, what in (
                (predict_spec.training_input_ref, "training_input"),
                (predict_spec.training_target_ref, "training_target"),
                (predict_spec.prediction_input_ref, "prediction_input"),
            ):
                resolved = _resolve_attached_file(db, model_id, ref_key)
                if resolved is None:
                    diagnostics.append(
                        _diag(f"缺少 {what} 文件引用: {ref_key}", field=f"data:{ref_key}")
                    )
                    ok = False
                    continue
                files.append((what, PredictFile(data=resolved[0], sha256=resolved[1])))
            if not ok:
                continue
            by_what = {what: pf for what, pf in files}
            outcome = prepare_data_predict(
                document,
                baseline,
                by_what["training_input"],
                by_what["training_target"],
                by_what["prediction_input"],
                predict_spec,
            )
        if not outcome.ok or outcome.result is None:
            diagnostics.extend(outcome.diagnostics)
        else:
            for iid in iids:
                per_interface[iid] = outcome.result

    if diagnostics:
        return {}, diagnostics
    return per_interface, []


def _detach_previous_prepared(db: Session, model_id: int) -> None:
    """解绑该模型实例既有的全部预备引用(同事务; 幂等跳过缺失)。

    解绑后显式 flush: attach 的幂等查询依赖已落盘的删除状态(调用方会话可能
    ``autoflush=False``), 否则同一对象重新 attach 会命中会话内未刷新的旧行。
    """
    refs = find_refs_by_owner(db, FINAL_OWNER_NAMESPACE, model_id, FINAL_OWNER_NAMESPACE)
    for ref in refs:
        purpose = str(ref.get("purpose") or "")
        if purpose.startswith(
            ("sequence_prep:canonical:", "sequence_prep:receipt:", "sequence_prep:artifact:")
        ):
            try:
                detach(db, ref["object_id"], FINAL_OWNER_NAMESPACE, model_id,
                       ref_entity_type=FINAL_OWNER_NAMESPACE)
            except ReferenceNotFoundError:  # pragma: no cover - 幂等跳过
                pass
    db.flush()


def prepare_prepared_sequences(
    db: Session,
    user,
    project_id: int,
    *,
    expected_revision: int,
    prepared_specs: Mapping[str, Mapping[str, Any]],
    model_id: int,
) -> dict[str, Any]:
    """预备指定项目模型实例的序列并原子替换数据引用(事务式发布)。

    任一接口预备失败: 抛 ``PrepPublishRejectedError``(携带全部结构化诊断),
    不写对象、不替换任何引用; 全部成功才在单事务内提交对象 + 替换引用 +
    推进项目草稿 revision。失败整体回滚, 模型引用保持上一份已验证引用
    (也可用 ``rollback_prepared_sequences`` 显式回滚)。

    ``prepared_specs``: {data_ref → 规格}(见 ``_prepare_model_interfaces``;
    ``constant`` 接口自动展开, 无需规格)。
    """
    project_service.ensure_access(db, user, project_id, "edit")
    project = db.get(Project, project_id)
    if project is None or project.status == "deleted":
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    if project.status != "active":
        raise ConflictError(
            "项目已归档, 不能预备序列",
            location={"object_type": "project", "object_id": project_id},
        )
    baseline = _project_baseline(project)
    current_draft = project_service.get_current_draft(db, project)
    if current_draft.revision != expected_revision:
        raise ConflictError(
            "项目草稿已被其他操作更新",
            params={"expected_revision": expected_revision, "current_revision": current_draft.revision},
            location={"object_type": "draft", "object_id": str(current_draft.id)},
        )
    model = db.get(ProjectModel, model_id)
    if model is None or model.project_id != project_id:
        raise NotFoundError(
            "项目模型不存在",
            params={"project_id": project_id, "model_id": model_id},
            location={"object_type": "project_model", "object_id": model_id},
        )
    document = _load_model_document(db, model)

    # 1) 预备(纯计算, 不落盘): 任一失败聚合诊断, 不写对象、不替换引用
    per_interface, diagnostics = _prepare_model_interfaces(
        db, document, baseline, model.id, dict(prepared_specs)
    )
    if diagnostics:
        _reject(diagnostics)

    # 2) 原子提交: 写对象 → 替换引用 → 推进草稿 revision(同一事务, 失败回滚)
    try:
        per_model: dict[str, Any] = {}
        for interface_id, seq in per_interface.items():
            canonical = seq.canonical_bytes
            receipt_bytes = json.dumps(
                dict(seq.receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            canon_handle = put_object(db, canonical, PREP_DATA_MEDIA_TYPE,
                                      source_category="sequence_prep")
            receipt_handle = put_object(db, receipt_bytes, PREP_RECEIPT_MEDIA_TYPE,
                                        source_category="sequence_prep_receipt")
            artifact: dict[str, Any] | None = None
            if seq.training_artifact_bytes is not None:
                artifact_handle = put_object(
                    db, seq.training_artifact_bytes, PREP_ARTIFACT_MEDIA_TYPE,
                    source_category="sequence_prep_artifact",
                )
                artifact = {
                    "object_id": artifact_handle.id,
                    "sha256": sha256_hex(seq.training_artifact_bytes),
                }
            per_model[interface_id] = {
                "object_id": canon_handle.id,
                "content_sha256": seq.canonical_sha256,
                "receipt_object_id": receipt_handle.id,
                "receipt_sha256": seq.receipt_sha256,
                "source_mode": str(seq.receipt.get("source_mode") or ""),
                "training_artifact": artifact,
            }

        # 替换引用(先解绑既有预备引用, 再绑定新引用; 同事务, 回滚即恢复)
        _detach_previous_prepared(db, model.id)
        for interface_id in per_interface:
            info = per_model[interface_id]
            attach(db, info["object_id"], FINAL_OWNER_NAMESPACE, model.id,
                   ref_entity_type=FINAL_OWNER_NAMESPACE,
                   purpose=f"sequence_prep:canonical:{interface_id}")
            attach(db, info["receipt_object_id"], FINAL_OWNER_NAMESPACE, model.id,
                   ref_entity_type=FINAL_OWNER_NAMESPACE,
                   purpose=f"sequence_prep:receipt:{interface_id}")
            if info["training_artifact"] is not None:
                attach(db, info["training_artifact"]["object_id"], FINAL_OWNER_NAMESPACE, model.id,
                       ref_entity_type=FINAL_OWNER_NAMESPACE,
                       purpose=f"sequence_prep:artifact:{interface_id}")

        # 合并进草稿的 prepared_sequences 清单(其他模型的预备产物保留)
        content = project_service.load_draft_content(db, current_draft)
        merged = dict(content.get("prepared_sequences") or {})
        merged[str(model.id)] = per_model
        new_draft = project_service.record_sequence_prep_refs(
            db, user, project_id, expected_revision, merged
        )
        db.commit()
        return {
            "project_revision": new_draft.revision,
            "prepared": per_model,
        }
    except Exception:
        db.rollback()
        raise


def rollback_prepared_sequences(
    db: Session,
    user,
    project_id: int,
    *,
    expected_revision: int,
    model_id: int,
) -> dict[str, Any]:
    """把指定模型实例的预备引用回滚为上一份已验证状态(事务式回滚)。

    策略: 从历史草稿中取最近一份带 ``prepared_sequences`` 的状态(不存在则为
    空), 解绑本模型当前独有的预备引用并重写草稿清单; 对象字节不删(由存储
    运维 safe_cleanup 回收)。幂等: 重复回滚无副作用(解绑跳过缺失引用)。
    """
    project_service.ensure_access(db, user, project_id, "edit")
    project = db.get(Project, project_id)
    if project is None or project.status == "deleted":
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    if project.status != "active":
        raise ConflictError(
            "项目已归档, 不能回滚预备序列",
            location={"object_type": "project", "object_id": project_id},
        )
    current_draft = project_service.get_current_draft(db, project)
    if current_draft.revision != expected_revision:
        raise ConflictError(
            "项目草稿已被其他操作更新",
            params={"expected_revision": expected_revision, "current_revision": current_draft.revision},
            location={"object_type": "draft", "object_id": str(current_draft.id)},
        )
    try:
        model = db.get(ProjectModel, model_id)
        if model is None or model.project_id != project_id:
            raise NotFoundError(
                "项目模型不存在",
                params={"project_id": project_id, "model_id": model_id},
                location={"object_type": "project_model", "object_id": model_id},
            )
        current = project_service.load_draft_content(db, current_draft)
        current_prepared = dict(current.get("prepared_sequences") or {})
        previous_prepared = project_service.load_latest_prepared_sequences(
            db, project, before_revision=expected_revision
        )
        current_this = dict(current_prepared.get(str(model_id)) or {})
        previous_this = dict(previous_prepared.get(str(model_id)) or {})
        # 解绑本模型当前有而上一份没有的预备引用(新增或替换的产物 → orphaned);
        # 与上一份一致的引用保持不变(幂等回滚)
        for interface_id, info in current_this.items():
            prev_info = previous_this.get(interface_id)
            if prev_info is not None and prev_info.get("object_id") == info.get("object_id"):
                continue
            for key in ("object_id", "receipt_object_id"):
                oid = info.get(key)
                if oid is None:
                    continue
                try:
                    detach(db, oid, FINAL_OWNER_NAMESPACE, model.id,
                           ref_entity_type=FINAL_OWNER_NAMESPACE)
                except ReferenceNotFoundError:  # pragma: no cover - 幂等跳过
                    pass
            artifact = info.get("training_artifact")
            if isinstance(artifact, Mapping) and artifact.get("object_id"):
                try:
                    detach(db, artifact["object_id"], FINAL_OWNER_NAMESPACE, model.id,
                           ref_entity_type=FINAL_OWNER_NAMESPACE)
                except ReferenceNotFoundError:  # pragma: no cover - 幂等跳过
                    pass
        # 恢复上一份已验证引用(幂等 attach; 已被后续发布解绑时重新建立)
        for interface_id, prev in previous_this.items():
            oid = prev.get("object_id")
            if oid is not None:
                attach(db, oid, FINAL_OWNER_NAMESPACE, model.id,
                       ref_entity_type=FINAL_OWNER_NAMESPACE,
                       purpose=f"sequence_prep:canonical:{interface_id}")
            rid = prev.get("receipt_object_id")
            if rid is not None:
                attach(db, rid, FINAL_OWNER_NAMESPACE, model.id,
                       ref_entity_type=FINAL_OWNER_NAMESPACE,
                       purpose=f"sequence_prep:receipt:{interface_id}")
            artifact = prev.get("training_artifact")
            if isinstance(artifact, Mapping) and artifact.get("object_id"):
                attach(db, artifact["object_id"], FINAL_OWNER_NAMESPACE, model.id,
                       ref_entity_type=FINAL_OWNER_NAMESPACE,
                       purpose=f"sequence_prep:artifact:{interface_id}")
        db.flush()
        merged = dict(previous_prepared)
        if str(model.id) in merged and not merged[str(model.id)]:
            del merged[str(model.id)]
        new_draft = project_service.record_sequence_prep_refs(
            db, user, project_id, expected_revision, merged
        )
        db.commit()
        return {"project_revision": new_draft.revision, "prepared": merged.get(str(model.id), {})}
    except Exception:
        db.rollback()
        raise


__all__ = ["prepare_prepared_sequences", "rollback_prepared_sequences", "PrepPublishRejectedError"]
