"""项目模型保存用例(切片 dm2-A: application/projects)。

对应 modules/application.md「典型示例:保存项目模型」与 formats/
device-model-yaml.md「进入项目前的候选模型门禁」:

1. 候选模型(直接 YAML 或模板实例化)在项目内完整校验(委托 devices 2.0 契约);
2. 失败: 返回聚合诊断, 不写对象、不登记清单、不分配编号;
3. 成功: 项目作用域内分配只递增、不复用的 ``_N`` 后缀(行锁 + 唯一约束);
4. 用最终 ID 重新完成身份校验, 生成规范文本、内容摘要与校验回执;
5. 原子保存: 模型/回执/配套数据对象 finalize + 清单行 + 审计, 同一事务提交
   (编号分配、文件提交与清单登记属于同一用例的一致性边界);
6. 临时 owner → 幂等 finalize → reconciliation: 失败事务不留半文件、孤立引用
   或对用户不可见的已占编号(对象文件遗留由存储运维 safe_cleanup 回收)。

``data_repeat``/``data_predict`` 配套文件在同一门禁中完成存在性、摘要、临时
归属、data_ref、step、列、单位、采样间隔与数值范围校验。
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.core.diagnostics import (
    SEVERITY_ERROR,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.yamlmini import YamlParseError
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices import (
    DeviceModelDocument,
    canonical_bytes,
    canonical_receipt,
    content_sha256,
    instantiate_template,
    is_valid_id,
    parse_device_model_v2,
    to_dict,
)
from iesplan.devices.datacontract2 import canonicalize_device_data_v2
from iesplan.models.audit import AuditLog
from iesplan.models.project import Project
from iesplan.models.project_model import (
    MODEL_SOURCE_DIRECT,
    MODEL_SOURCE_TEMPLATE,
    ProjectModel,
)
from iesplan.services import project as project_service
from iesplan.storage import (
    ReferenceNotFoundError,
    attach,
    detach,
    find_refs_by_entity_type,
    find_refs_by_owner,
    get_object,
    object_info,
    put_object,
)
from iesplan.application.model_templates import (
    TEMPLATE_OWNER_NAMESPACE,
    resolve_template_revision,
)

#: 临时 owner 命名空间(上传会话隔离区; 保存成功 finalize 后解绑)
TEMP_OWNER_NAMESPACE: str = "project_model_temp"
#: 最终 owner 命名空间(项目模型清单行持有者)
FINAL_OWNER_NAMESPACE: str = "project_model"

#: 临时数据文件默认保留时长(超龄由 reconciliation 解绑, 之后对象进入
#: orphaned, 由存储运维 safe_cleanup/purge 物理回收)
TEMP_FILE_RETENTION: timedelta = timedelta(days=1)

#: 候选模型 YAML 上限(2 MiB, 防御性限制)
MAX_MODEL_YAML_BYTES: int = 2 * 1024 * 1024

#: 数据文件媒体类型(设备配套 CSV)
DATA_MEDIA_TYPE: str = "text/csv; charset=utf-8"
#: 模型/回执对象媒体类型(规范字节为版本化 JSON 文本)
MODEL_MEDIA_TYPE: str = "application/json"

#: 项目模型域诊断码(集中登记于 core/diagnostics.py NEW_DIAG_CODES; 消费者
#: 本地常量与目录同语义, 与 dataset.py 对 DATA-TS-004..007 的处理一致)
PROJ_MDL_DATA_MISSING = "PROJ-MDL-001"  # 临时数据文件不存在或不可用
PROJ_MDL_DATA_DIGEST_MISMATCH = "PROJ-MDL-002"  # 数据文件摘要与声明不一致
PROJ_MDL_DATA_OWNER_MISMATCH = "PROJ-MDL-003"  # 数据文件归属与上传会话不一致
PROJ_MDL_IDENTITY_FAILED = "PROJ-MDL-004"  # 最终设备 ID 身份校验失败
PROJ_MDL_VALIDATION_FAILED = "PROJ-MDL-005"  # 候选模型校验失败(保存拒绝, 包络码)
PROJ_MDL_YAML_PARSE = "PROJ-MDL-006"  # 候选模型 YAML 解析失败


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class ModelCandidateRejectedError(AppError):
    """候选模型校验失败, 保存被拒绝(HTTP 400, 诊断明细入 params.diagnostics)。

    code 与 message_key 见 core/diagnostics.py NEW_DIAG_CODES 集中登记。
    """

    code = PROJ_MDL_VALIDATION_FAILED
    severity = SEVERITY_ERROR
    message_key = "ies.diag.proj.model_validation_failed"
    http_status = 400


class ProjectModelNotFoundError(NotFoundError):
    """项目模型清单行不存在或不属于该项目。"""


# ---------------------------------------------------------------------------
# 值对象与诊断辅助
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataFileRef:
    """配套数据文件引用(本切片校验: 存在 + 摘要 + 临时归属)。"""

    data_ref: str
    upload_id: int
    object_id: int
    sha256: str


@dataclass(slots=True)
class CandidateValidation:
    """候选模型完整校验结果: 要么带最终文档(含规范摘要), 要么带诊断列表。

    ``document`` 为解析后文档(未加 _N 后缀); ``receipt``/``canonical_text``
    在直接 YAML 路径下即为规范化结果, 模板路径下由实例化器产出(均以
    基础 ID 计算, 加后缀后的最终规范由保存步骤重算)。
    """

    ok: bool
    diagnostics: list[Diagnostic] = field(default_factory=list)
    document: DeviceModelDocument | None = None
    canonical_text: str = ""
    content_sha256: str = ""
    receipt: dict[str, Any] | None = None
    template_sha256: str | None = None
    inputs_sha256: str | None = None
    #: 模板溯源(模板稳定 ID / 精确 revision / 摘要 / schema_version; 模板来源时非空)
    template_provenance: dict[str, Any] | None = None
    data_files: tuple[DataFileRef, ...] = ()

    @property
    def blocking_diags(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity in ("error", "blocking")]


def _diag(
    code: str,
    detail: str,
    *,
    field: str | None = None,
    params: Mapping[str, object] | None = None,
) -> Diagnostic:
    """构造项目模型域诊断(带字段路径与 expected/actual 参数)。"""
    location: dict[str, object] = {"object_type": "project_model"}
    if field is not None:
        location["field"] = field
    return make_diag(
        code, severity=SEVERITY_ERROR,
        params=dict(params or {"detail": detail}), location=location,
    )


# ---------------------------------------------------------------------------
# 候选模型解析与校验(纯校验, 不写对象/清单/编号)
# ---------------------------------------------------------------------------


def _parse_candidate_yaml(model_yaml: str) -> tuple[Mapping[str, Any] | None, list[Diagnostic]]:
    """候选 YAML 安全子集解析(重复键/锚点/非法缩进在解析层拒绝)。"""
    diags: list[Diagnostic] = []
    if not model_yaml or not model_yaml.strip():
        diags.append(
            _diag(PROJ_MDL_YAML_PARSE, "候选模型 YAML 不能为空", field="model_yaml",
                  params={"expected": "非空 ies.device-model YAML", "actual": "空"})
        )
        return None, diags
    if len(model_yaml.encode("utf-8")) > MAX_MODEL_YAML_BYTES:
        diags.append(
            _diag(PROJ_MDL_YAML_PARSE, f"候选模型 YAML 超过上限 {MAX_MODEL_YAML_BYTES} 字节",
                  field="model_yaml", params={"expected": f"≤ {MAX_MODEL_YAML_BYTES} 字节",
                                              "actual": len(model_yaml.encode("utf-8"))})
        )
        return None, diags
    try:
        raw = yaml_load(model_yaml)
    except YamlParseError as exc:
        diags.append(
            _diag(PROJ_MDL_YAML_PARSE, str(exc), field="model_yaml",
                  params={"expected": "YAML 1.2 安全子集", "actual": str(exc), "line": exc.line})
        )
        return None, diags
    if not isinstance(raw, Mapping):
        diags.append(
            _diag(PROJ_MDL_YAML_PARSE, "候选模型顶层必须是 mapping",
                  field="<root>", params={"expected": "mapping", "actual": type(raw).__name__})
        )
        return None, diags
    return raw, diags


def _load_template_authoritative_document(
    db: Session,
    user,
    template_id: str,
    template_revision: int,
    template_sha256: str,
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """读取项目模型候选引用的权威模板内容(经对象存储门面)。

    模板来源候选只提交稳定模板 ID、精确 revision 与 content_sha256, 后端
    从模板 revision 的对象引用读取权威规范字节; 候选携带的摘要只作二次
    确认(不一致 → 409, 内容已失效)。返回 (模板原始映射, 模板溯源)。
    """
    ref = resolve_template_revision(db, user, template_id, template_revision, template_sha256)
    raw = get_object(db, ref.yaml_object_id)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AppError(
            "模板 revision 对象损坏",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "model_template", "object_id": str(ref.yaml_object_id)},
        ) from None
    if not isinstance(parsed, Mapping):
        raise AppError(
            "模板 revision 对象形态非法",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "model_template", "object_id": str(ref.yaml_object_id)},
        )
    provenance = {
        "template_id": template_id,
        "template_revision": ref.revision,
        "template_sha256": ref.content_sha256,
        "template_schema_version": ref.schema_version,
    }
    return parsed, provenance


def _parse_candidate_document(
    raw: Mapping[str, Any], source: str, template_inputs: Mapping[str, Any] | None, file: str
) -> tuple[
    DeviceModelDocument | None,
    list[Diagnostic],
    str,
    str,
    dict[str, Any] | None,
    str | None,
    str | None,
]:
    """按来源(直接 YAML / 模板实例化)产出基础文档与规范摘要。

    返回 (document, diagnostics, canonical_text, content_sha256, receipt,
    template_sha256, inputs_sha256)。失败时 document 为 None。
    """
    if source == MODEL_SOURCE_TEMPLATE:
        result, diags = instantiate_template(raw, dict(template_inputs or {}), file=file)
        if result is None:
            return None, diags, "", "", None, None, None
        return (
            result.document, [], result.canonical_text, result.content_sha256,
            result.receipt, result.template_sha256, result.inputs_sha256,
        )
    parse_result = parse_device_model_v2(raw, file=file)
    if not parse_result.ok:
        return None, parse_result.diagnostics, "", "", None, None, None
    doc = parse_result.document
    assert doc is not None
    text = canonical_bytes(doc).decode("utf-8")
    return doc, [], text, content_sha256(doc), canonical_receipt(doc), None, None

def _validate_data_files(
    db: Session,
    data_files: tuple[DataFileRef, ...],
    document: DeviceModelDocument,
) -> list[Diagnostic]:
    """配套文件完整门禁：引用、临时归属与 devices 数据内容契约。"""
    diags: list[Diagnostic] = []
    expected_refs = {
        iface.source.data_ref
        for iface in document.interfaces.values()
        if iface.type == "predefined"
        and iface.source is not None
        and iface.source.mode in ("data_repeat", "data_predict")
        and iface.source.data_ref is not None
    }
    submitted_refs = [ref.data_ref for ref in data_files]
    for data_ref in sorted(expected_refs - set(submitted_refs)):
        diags.append(_diag(
            PROJ_MDL_DATA_MISSING,
            f"缺少模型声明的配套数据文件: {data_ref}",
            field=f"data_files.{data_ref}",
            params={"data_ref": data_ref, "expected": "已上传的配套文件", "actual": "missing"},
        ))
    for data_ref in sorted(set(submitted_refs) - expected_refs):
        diags.append(_diag(
            PROJ_MDL_DATA_MISSING,
            f"配套数据引用未在模型中声明: {data_ref}",
            field=f"data_files.{data_ref}",
            params={"data_ref": data_ref, "expected": sorted(expected_refs), "actual": data_ref},
        ))
    duplicates = {data_ref for data_ref in submitted_refs if submitted_refs.count(data_ref) > 1}
    for data_ref in sorted(duplicates):
        diags.append(_diag(
            PROJ_MDL_DATA_OWNER_MISMATCH,
            f"同一 data_ref 重复提交: {data_ref}",
            field=f"data_files.{data_ref}",
            params={"data_ref": data_ref, "expected": "每个 data_ref 一个文件", "actual": "duplicate"},
        ))
    for ref in data_files:
        fld = f"data_files.{ref.data_ref}"
        try:
            handle = object_info(db, ref.object_id)
        except NotFoundError:
            diags.append(
                _diag(PROJ_MDL_DATA_MISSING, f"数据文件不存在: {ref.data_ref}", field=fld,
                      params={"data_ref": ref.data_ref, "object_id": ref.object_id,
                              "expected": "已上传且可用的临时对象", "actual": "missing"})
            )
            continue
        if handle["status"] != "stored":
            diags.append(
                _diag(PROJ_MDL_DATA_MISSING, f"数据文件状态不可用: {ref.data_ref}", field=fld,
                      params={"data_ref": ref.data_ref, "object_id": ref.object_id,
                              "expected": "stored", "actual": handle["status"]})
            )
            continue
        if str(handle["sha256"]) != ref.sha256:
            diags.append(
                _diag(PROJ_MDL_DATA_DIGEST_MISMATCH, f"数据文件摘要不一致: {ref.data_ref}", field=fld,
                      params={"data_ref": ref.data_ref, "object_id": ref.object_id,
                              "expected_sha256": ref.sha256, "actual_sha256": handle["sha256"]})
            )
            continue
        refs = find_refs_by_owner(
            db, TEMP_OWNER_NAMESPACE, ref.upload_id, ref_entity_type=TEMP_OWNER_NAMESPACE
        )
        if not any(r["object_id"] == ref.object_id for r in refs):
            diags.append(
                _diag(PROJ_MDL_DATA_OWNER_MISMATCH, f"数据文件归属不一致: {ref.data_ref}", field=fld,
                      params={"data_ref": ref.data_ref, "object_id": ref.object_id,
                              "upload_id": ref.upload_id, "expected": f"属于 upload_id={ref.upload_id}",
                              "actual": "不属于该上传会话"})
            )
            continue
        result = canonicalize_device_data_v2(
            get_object(db, ref.object_id),
            document,
            expected_data_ref=ref.data_ref,
        )
        diags.extend(diag for diag in result.diagnostics if diag.blocking)
    return diags


def validate_candidate(
    db: Session,
    user,
    project_id: int,
    *,
    model_yaml: str,
    source: str = MODEL_SOURCE_DIRECT,
    template_id: str | None = None,
    template_revision: int | None = None,
    template_sha256: str | None = None,
    template_inputs: Mapping[str, Any] | None = None,
    data_files: tuple[DataFileRef, ...] = (),
) -> CandidateValidation:
    """候选模型完整校验(门禁, 不保存)。

    依次执行: 项目 view 授权 → YAML 安全解析 → devices 2.0 完整校验(或模板
    实例化) → 配套数据文件存在/摘要/归属校验。任何失败聚合诊断返回, 不写
    对象、不登记清单、不分配编号。

    ``source=template`` 时若携带模板稳定 ID/精确 revision/内容摘要, 后端
    从模板 revision 读取权威内容并实例化(候选提交的 model_yaml 被覆盖为
    权威规范字节, 不信任客户端自带的模板字节); 未携带模板引用时为旧契约
    路径(model_yaml 即模板 YAML, 校验用, 正式保存仍以权威内容为准)。
    """
    project_service.ensure_access(db, user, project_id, "view")
    if source not in (MODEL_SOURCE_DIRECT, MODEL_SOURCE_TEMPLATE):
        return CandidateValidation(
            ok=False,
            diagnostics=[_diag(PROJ_MDL_YAML_PARSE, f"未知来源: {source!r}", field="source",
                               params={"expected": "direct_yaml|template", "actual": source})],
        )
    raw: Mapping[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    if source == MODEL_SOURCE_TEMPLATE and template_id and template_revision and template_sha256:
        try:
            template_raw, provenance = _load_template_authoritative_document(
                db, user, template_id, template_revision, template_sha256
            )
        except (AppError, ConflictError, NotFoundError) as exc:
            return CandidateValidation(
                ok=False,
                diagnostics=[_diag(
                    exc.code, exc.message_key or str(exc), field="template_id",
                    params={"template_id": template_id, "template_revision": template_revision,
                            "detail": str(exc)},
                )],
            )
        # 权威模板字节为规范 JSON: 直接 json.loads(跳过 yamlmini, 避免 JSON
        # 与 YAML 安全子集解析差异)
        raw = template_raw
    if raw is None:
        raw, diags = _parse_candidate_yaml(model_yaml)
        if raw is None:
            return CandidateValidation(ok=False, diagnostics=diags)
    else:
        diags = []
    doc, parse_diags, canonical_text, sha256, receipt, tpl_sha, inputs_sha = _parse_candidate_document(
        raw, source, template_inputs, file="<candidate>"
    )
    diags.extend(parse_diags)
    data_diags = _validate_data_files(db, data_files, doc) if doc is not None else []
    diags.extend(data_diags)
    if diags:
        return CandidateValidation(ok=False, diagnostics=diags)
    return CandidateValidation(
        ok=True,
        document=doc,
        canonical_text=canonical_text,
        content_sha256=sha256,
        receipt=receipt,
        template_sha256=tpl_sha,
        inputs_sha256=inputs_sha,
        template_provenance=provenance,
        data_files=data_files,
    )


# ---------------------------------------------------------------------------
# 编号分配(项目作用域、递增、删除不复用、并发唯一)
# ---------------------------------------------------------------------------


def _allocate_suffix(db: Session, project_id: int) -> int:
    """项目内分配下一个 _N 编号(原子 UPDATE..RETURNING + 唯一约束兜底)。

    只递增、删除不复用: 编号来自 per-project 计数器行, 删除清单行不回落
    计数器; 并发安全: ``UPDATE ... SET next_suffix = next_suffix + 1 ...
    RETURNING next_suffix - 1`` 在数据库内原子完成(PostgreSQL 行锁 +
    SQLite 写锁串行化), 并发请求不可能读到同一编号; 计数器行缺失时以
    savepoint 插入(next_suffix=2)并重查(与存储 put_object 的唯一键竞争
    处理同模式)。
    """
    for _attempt in range(3):
        row = db.execute(
            sa.text(
                "UPDATE project_model_sequences SET next_suffix = next_suffix + 1 "
                "WHERE project_id = :pid RETURNING next_suffix - 1"
            ),
            {"pid": project_id},
        ).first()
        if row is not None:
            return int(row[0])
        try:
            with db.begin_nested():  # 只回滚嵌套 savepoint, 不触调用方外层事务
                db.execute(
                    sa.text(
                        "INSERT INTO project_model_sequences (project_id, next_suffix) "
                        "VALUES (:pid, 2)"
                    ),
                    {"pid": project_id},
                )
                db.flush()
            return 1
        except IntegrityError:
            continue  # 并发竞争者已插入计数器行: 下一轮 UPDATE 原子重试
    raise ConflictError(
        "项目模型编号分配失败(并发冲突), 请重试",
        location={"object_type": "project_model", "project_id": str(project_id)},
    )


# ---------------------------------------------------------------------------
# 保存用例(候选校验 → 编号分配 → 规范化 → 原子保存)
# ---------------------------------------------------------------------------


def _rebuild_with_final_id(
    document: DeviceModelDocument, final_id: str
) -> tuple[DeviceModelDocument | None, list[Diagnostic], str, str, dict[str, Any]]:
    """用最终 ID 重建文档: 替换 device.id → 完整重新校验 → 规范摘要与回执。"""
    raw = to_dict(document)
    raw.setdefault("device", {})
    raw["device"]["id"] = final_id
    if not is_valid_id(final_id):
        return None, [
            _diag(PROJ_MDL_IDENTITY_FAILED, f"最终设备 ID 非法: {final_id!r}",
                  field="device.id", params={"base_device_id": document.device.id if document.device else "",
                                              "final_id": final_id, "expected": "小写命名空间 ID",
                                              "actual": final_id})
        ], "", "", {}
    result = parse_device_model_v2(raw, file="<candidate>")
    if not result.ok:
        return None, result.diagnostics, "", "", {}
    doc = result.document
    assert doc is not None
    text = canonical_bytes(doc).decode("utf-8")
    return doc, [], text, content_sha256(doc), canonical_receipt(doc)


def _find_idempotent_model(db: Session, project_id: int, idempotency_key: str) -> ProjectModel | None:
    return db.execute(
        sa.select(ProjectModel).where(
            ProjectModel.project_id == project_id,
            ProjectModel.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()


def _receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _load_stored_receipt(db: Session, model: ProjectModel) -> dict[str, Any]:
    """读取清单行关联的校验回执(读取时经存储门面校验完整性)。"""
    raw = get_object(db, model.receipt_object_id)
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise AppError(
            "项目模型回执对象损坏",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "project_model", "object_id": str(model.id)},
        )
    return parsed


def _finalize_data_files(
    db: Session,
    data_files: tuple[DataFileRef, ...],
    base_doc: DeviceModelDocument,
    final_doc: DeviceModelDocument,
) -> dict[str, Any]:
    """把配套数据文件按最终 _N 模型锁定并落盘(内容锁)。

    返回 ``{data_ref: ObjectHandle}``: 数据区(列/单位/step/数值范围)以
    基础文档校验(原始文件头按基础 ID 声明), 落盘字节的元数据改写为最终
    ``device_id`` 与最终 ``device_content_sha256`` —— 编号分配后的模型与其
    配套数据形成可复核的内容锁。失败抛 AppError, 由调用方整体回滚。
    """
    from iesplan.devices.datacontract2 import (
        DeviceData2Meta,
        canonicalize_device_data_v2,
        serialize_metadata_v2,
    )

    handles: dict[str, Any] = {}
    for ref in data_files:
        raw = get_object(db, ref.object_id)
        # 数据区校验: 与原始文件头(基础 ID)一致; 最终模型不允许改变预定义
        # 接口列/单位/step/数值范围(只允许 device.id 与内容摘要变化)
        result = canonicalize_device_data_v2(raw, base_doc, expected_data_ref=ref.data_ref)
        blocking = [d for d in result.diagnostics if d.blocking]
        if blocking:
            raise AppError(
                "配套数据文件与最终模型内容锁不一致",
                code="SYS-STORE-001",
                message_key="ies.diag.store.corrupt",
                params={"data_ref": ref.data_ref,
                        "diagnostics": [d.to_dict() for d in blocking]},
                location={"object_type": "project_model", "data_ref": ref.data_ref},
            )
        meta = result.meta
        assert final_doc.device is not None
        # 最终文件元数据: 绑定最终 _N 模型的 device_id 与内容摘要(内容锁)
        locked_meta = DeviceData2Meta(
            schema_id=meta.schema_id, schema_version=meta.schema_version,
            dataset_id=meta.dataset_id,
            device_id=final_doc.device.id,
            device_content_sha256=content_sha256(final_doc),
            source_mode=meta.source_mode, resolution=meta.resolution,
            period=meta.period,
            project_baseline_sha256=meta.project_baseline_sha256,
            point_count=meta.point_count, prepared=meta.prepared,
            units=meta.units, notes=meta.notes,
            declared_columns=meta.declared_columns,
        )
        text = result.canonical_csv_bytes().decode("utf-8")
        lines = text.split("\n")
        header_idx = next(
            (i for i, line in enumerate(lines) if line.strip() and not line.strip().startswith("#")),
            len(lines),
        )
        locked_text = serialize_metadata_v2(locked_meta, column_order=meta.declared_columns or None)
        locked_bytes = (locked_text + "\n".join(lines[header_idx:])).encode("utf-8")
        # 内容锁闭合: 最终字节必须能按最终文档重新完整校验(无诊断)
        verify = canonicalize_device_data_v2(locked_bytes, final_doc, expected_data_ref=ref.data_ref)
        if any(d.blocking for d in verify.diagnostics):
            raise AppError(
                "配套数据文件最终字节无法通过内容锁校验",
                code="SYS-STORE-001",
                message_key="ies.diag.store.corrupt",
                params={"data_ref": ref.data_ref,
                        "diagnostics": [d.to_dict() for d in verify.diagnostics]},
                location={"object_type": "project_model", "data_ref": ref.data_ref},
            )
        handle = put_object(db, locked_bytes, DATA_MEDIA_TYPE,
                            source_category="project_model_data")
        handles[ref.data_ref] = handle
    return handles


def _project_model_draft_refs(db: Session, project_id: int) -> list[dict[str, object]]:
    """项目草稿只保存模型清单引用，不复制模型正文。"""
    rows = db.execute(
        sa.select(ProjectModel)
        .where(ProjectModel.project_id == project_id)
        .order_by(ProjectModel.suffix)
    ).scalars()
    return [
        {
            "id": str(model.id),
            "device_id": model.device_id,
            "revision": model.revision,
            "content_sha256": model.content_sha256,
        }
        for model in rows
    ]


def _save_project_model(
    db: Session,
    user,
    project_id: int,
    *,
    model_yaml: str,
    source: str = MODEL_SOURCE_DIRECT,
    template_id: str | None = None,
    template_revision: int | None = None,
    template_sha256: str | None = None,
    template_inputs: Mapping[str, Any] | None = None,
    data_files: tuple[DataFileRef, ...] = (),
    idempotency_key: str | None = None,
    expected_revision: int,
) -> dict[str, Any]:
    """正式保存项目模型(候选校验 → 编号分配 → 规范化 → 原子保存)。

    模板来源候选提交模板稳定 ID、精确 revision、content_sha256 与 inputs,
    后端读取权威模板内容并实例化(``model_yaml`` 为权威模板的规范字节);
    直接 YAML 来源提交完整 ``model_yaml``。两条路径汇入同一候选校验与
    保存用例。返回 ``{project_model, receipt, project_revision, duplicate}``;
    公共 application 用例拥有提交/回滚边界。
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
            "项目已归档, 不能保存模型",
            location={"object_type": "project", "object_id": project_id},
        )
    if idempotency_key:
        existing = _find_idempotent_model(db, project_id, idempotency_key)
        if existing is not None:
            # 幂等重试: 返回同一逻辑结果, 不重复副作用(不占新编号)
            return {
                "project_model": project_model_to_dict(existing),
                "receipt": _load_stored_receipt(db, existing),
                "project_revision": existing.project_revision,
                "duplicate": True,
            }
    current_draft = project_service.get_current_draft(db, project)
    if current_draft.revision != expected_revision:
        raise ConflictError(
            "项目草稿已被其他操作更新",
            params={"expected_revision": expected_revision, "current_revision": current_draft.revision},
            location={"object_type": "draft", "object_id": str(current_draft.id)},
        )

    validation = validate_candidate(
        db, user, project_id,
        model_yaml=model_yaml, source=source,
        template_id=template_id,
        template_revision=template_revision,
        template_sha256=template_sha256,
        template_inputs=template_inputs, data_files=data_files,
    )
    if not validation.ok or validation.document is None:
        raise ModelCandidateRejectedError(
            "",
            params={
                "diagnostics": [d.to_dict() for d in validation.diagnostics],
                "count": len(validation.diagnostics),
            },
            location={"object_type": "project_model", "project_id": project_id},
        )
    template_provenance = validation.template_provenance
    document = validation.document
    base_device_id = document.device.id if document.device is not None else ""

    # 编号分配与文件/清单/审计同事务: 失败整体回滚, 编号不占号
    suffix = _allocate_suffix(db, project_id)
    final_id = f"{base_device_id}_{suffix}"
    final_doc, identity_diags, canonical_text, final_sha256, final_receipt = _rebuild_with_final_id(
        document, final_id
    )
    if final_doc is None:
        raise ModelCandidateRejectedError(
            "",
            params={"diagnostics": [d.to_dict() for d in identity_diags], "count": len(identity_diags)},
            location={"object_type": "project_model", "project_id": project_id},
        )

    # 最终回执: 完整保留模板溯源(模板 ID/精确 revision/模板摘要/inputs 摘要/
    # 实例化器算法标识/候选模型摘要)与最终模型摘要
    final_receipt = dict(final_receipt)
    if template_provenance is not None:
        final_receipt["instantiator"] = "ies.device-model.instantiator@1.0.0"
        final_receipt.update(template_provenance)
        if validation.inputs_sha256 is not None:
            final_receipt["inputs_sha256"] = validation.inputs_sha256
        if validation.content_sha256:
            final_receipt["candidate_content_sha256"] = validation.content_sha256

    # 对象写入(内容寻址; 先写字节与回执, 清单行建立后统一 attach 最终 owner)
    model_handle = put_object(
        db, canonical_text.encode("utf-8"), MODEL_MEDIA_TYPE,
        source_category="project_model",
    )
    receipt_handle = put_object(
        db, _receipt_bytes(final_receipt), MODEL_MEDIA_TYPE,
        source_category="project_model_receipt",
    )

    # 内容锁: 配套数据文件必须与最终 _N 模型一致(device_id / device_content_sha256 /
    # 列 / 单位 / step 连续 / 有效区间)。数据区按基础文档校验(存在/摘要/归属/
    # 内容), 落盘字节绑定最终 _N 模型(失败整体拒绝, 已占编号随事务回滚)。
    lock_diags = _validate_data_files(db, data_files, document)
    if lock_diags:
        raise ModelCandidateRejectedError(
            "",
            params={"diagnostics": [d.to_dict() for d in lock_diags],
                    "count": len(lock_diags)},
            location={"object_type": "project_model", "project_id": project_id},
        )
    data_handles = _finalize_data_files(db, data_files, document, final_doc)

    model = ProjectModel(
        project_id=project_id,
        suffix=suffix,
        base_device_id=base_device_id,
        device_id=final_id,
        revision=1,
        project_revision=expected_revision + 1,
        content_sha256=final_sha256,
        model_object_id=model_handle.id,
        receipt_object_id=receipt_handle.id,
        source=source,
        template_id=template_id,
        template_revision=template_revision,
        template_sha256=(
            template_provenance["template_sha256"]
            if template_provenance is not None
            else None
        ),
        inputs_sha256=validation.inputs_sha256,
        idempotency_key=idempotency_key,
        created_by=user.id,
    )
    db.add(model)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "项目模型保存冲突(编号或最终 ID 唯一性), 请重试",
            params={"project_id": project_id, "device_id": final_id},
            location={"object_type": "project_model", "project_id": project_id},
        ) from exc

    # finalize: 模型/回执/配套数据对象从临时隔离区转为最终 owner 引用
    attach(db, model_handle.id, FINAL_OWNER_NAMESPACE, model.id,
           ref_entity_type=FINAL_OWNER_NAMESPACE, purpose="model_yaml")
    attach(db, receipt_handle.id, FINAL_OWNER_NAMESPACE, model.id,
           ref_entity_type=FINAL_OWNER_NAMESPACE, purpose="receipt")
    for ref in validation.data_files:
        handle = data_handles.get(ref.data_ref)
        if handle is not None:
            attach(db, handle.id, FINAL_OWNER_NAMESPACE, model.id,
                   ref_entity_type=FINAL_OWNER_NAMESPACE, purpose=f"data:{ref.data_ref}")
        # 原始临时对象不再属于本项目模型(引用解除, 进入孤儿生命周期)
        try:
            detach(db, ref.object_id, TEMP_OWNER_NAMESPACE, ref.upload_id,
                   ref_entity_type=TEMP_OWNER_NAMESPACE)
        except ReferenceNotFoundError:  # pragma: no cover - 校验时已确认归属, 防御性跳过
            pass

    db.add(
        AuditLog(
            entity_type="project_model",
            entity_id=model.id,
            action="project_model.created",
            actor_id=user.id,
            actor_type="user",
            after={
                "project_id": project.id,
                "device_id": final_id,
                "suffix": suffix,
                "content_sha256": final_sha256,
                "source": source,
                "template_id": template_id,
                "template_revision": template_revision,
                "data_refs": [r.data_ref for r in validation.data_files],
            },
        )
    )
    db.flush()
    new_draft = project_service.replace_project_model_refs(
        db,
        user,
        project_id,
        expected_revision,
        _project_model_draft_refs(db, project_id),
    )
    model.project_revision = new_draft.revision
    db.flush()
    return {
        "project_model": project_model_to_dict(model),
        "receipt": final_receipt,
        "project_revision": new_draft.revision,
        "duplicate": False,
    }


def save_project_model(
    db: Session,
    user,
    project_id: int,
    *,
    model_yaml: str,
    expected_revision: int,
    source: str = MODEL_SOURCE_DIRECT,
    template_id: str | None = None,
    template_revision: int | None = None,
    template_sha256: str | None = None,
    template_inputs: Mapping[str, Any] | None = None,
    data_files: tuple[DataFileRef, ...] = (),
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """事务型保存命令；application 层统一提交或回滚。"""
    try:
        result = _save_project_model(
            db,
            user,
            project_id,
            model_yaml=model_yaml,
            expected_revision=expected_revision,
            source=source,
            template_id=template_id,
            template_revision=template_revision,
            template_sha256=template_sha256,
            template_inputs=template_inputs,
            data_files=data_files,
            idempotency_key=idempotency_key,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 临时数据文件上传(临时 owner 隔离区)
# ---------------------------------------------------------------------------


def _upload_temp_data_file(
    db: Session, user, project_id: int, *, content: bytes, data_ref: str, upload_id: int
) -> dict[str, Any]:
    """把配套数据文件写入临时隔离区(临时 owner 引用)。

    返回 ``{temp_file, upload_id}``; 对象内容寻址去重(同内容复用记录, 引用
    单独建立)。保存成功时由 finalize 转为最终 owner, 超龄未保存的临时引用
    由 reconciliation 解绑, 对象随后进入孤儿回收生命周期。
    """
    project_service.ensure_access(db, user, project_id, "edit")
    handle = put_object(
        db, content, DATA_MEDIA_TYPE,
        source_category="project_model_temp",
        ref_type=TEMP_OWNER_NAMESPACE,
        ref_id=upload_id,
        ref_entity_type=TEMP_OWNER_NAMESPACE,
        purpose=f"data:{data_ref}",
    )
    return {
        "temp_file": {
            "object_id": handle.id,
            "oid": handle.oid,
            "sha256": handle.sha256,
            "size_bytes": handle.size_bytes,
            "media_type": handle.media_type,
            "status": handle.status,
        },
        "upload_id": upload_id,
    }


def upload_temp_data_file(
    db: Session, user, project_id: int, *, content: bytes, data_ref: str, upload_id: int
) -> dict[str, Any]:
    """事务型临时文件上传命令。"""
    try:
        result = _upload_temp_data_file(
            db, user, project_id, content=content, data_ref=data_ref, upload_id=upload_id
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def new_temp_upload_id() -> int:
    """生成临时上传会话标识(63 位正整数, 对外以不透明十进制字符串传输)。"""
    return secrets.randbelow(2**63 - 1) + 1


# ---------------------------------------------------------------------------
# 删除(编号不复用: 只删清单行与 owner 引用, 计数器不回落)
# ---------------------------------------------------------------------------


def _get_project_model(db: Session, project_id: int, model_id: int) -> ProjectModel:
    model = db.get(ProjectModel, model_id)
    if model is None or model.project_id != project_id:
        raise ProjectModelNotFoundError(
            "项目模型不存在",
            params={"project_id": project_id, "model_id": model_id},
            location={"object_type": "project_model", "object_id": model_id},
        )
    return model


def _delete_project_model(
    db: Session,
    user,
    project_id: int,
    model_id: int,
    *,
    expected_revision: int,
) -> int:
    """删除项目模型(硬删除清单行 + 解绑最终 owner 引用)。

    - 对象解除引用后进入 orphaned, 由存储运维 safe_cleanup/purge 物理回收;
    - 编号计数器不回落: 之后保存的新模型取得更大的 _N, 已删除编号不复用。
    """
    project_service.ensure_access(db, user, project_id, "edit")
    model = _get_project_model(db, project_id, model_id)
    refs = find_refs_by_owner(db, FINAL_OWNER_NAMESPACE, model.id, FINAL_OWNER_NAMESPACE)
    for ref in refs:
        detach(db, ref["object_id"], FINAL_OWNER_NAMESPACE, model.id,
               ref_entity_type=FINAL_OWNER_NAMESPACE)
    db.add(
        AuditLog(
            entity_type="project_model",
            entity_id=model.id,
            action="project_model.deleted",
            actor_id=user.id,
            actor_type="user",
            after={
                "project_id": project_id,
                "device_id": model.device_id,
                "suffix": model.suffix,
                "content_sha256": model.content_sha256,
            },
        )
    )
    db.delete(model)
    db.flush()
    new_draft = project_service.replace_project_model_refs(
        db,
        user,
        project_id,
        expected_revision,
        _project_model_draft_refs(db, project_id),
    )
    return new_draft.revision


def delete_project_model(
    db: Session,
    user,
    project_id: int,
    model_id: int,
    *,
    expected_revision: int,
) -> int:
    """事务型删除命令；编号计数器不回退。"""
    try:
        revision = _delete_project_model(
            db,
            user,
            project_id,
            model_id,
            expected_revision=expected_revision,
        )
        db.commit()
        return revision
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 读取与 reconciliation
# ---------------------------------------------------------------------------


def get_project_models(db: Session, user, project_id: int) -> list[dict]:
    """项目模型清单(最新在前; 编号对用户可见, 不存在"不可见已占编号")。"""
    project_service.ensure_access(db, user, project_id, "view")
    rows = db.execute(
        sa.select(ProjectModel)
        .where(ProjectModel.project_id == project_id)
        .order_by(ProjectModel.suffix.desc(), ProjectModel.id.desc())
    ).scalars()
    return [project_model_to_dict(m) for m in rows]


def project_model_to_dict(model: ProjectModel) -> dict[str, Any]:
    """清单行 → 公开视图。"""
    return {
        "id": str(model.id),
        "project_id": str(model.project_id),
        "device_id": model.device_id,
        "base_device_id": model.base_device_id,
        "suffix": model.suffix,
        "revision": model.revision,
        "project_revision": model.project_revision,
        "content_sha256": model.content_sha256,
        "model_object_id": str(model.model_object_id),
        "receipt_object_id": str(model.receipt_object_id),
        "source": model.source,
        "template_id": model.template_id,
        "template_revision": model.template_revision,
        "template_sha256": model.template_sha256,
        "inputs_sha256": model.inputs_sha256,
        "created_by": str(model.created_by),
        "created_at": model.created_at.isoformat() if model.created_at else None,
    }


def reconcile_stale_temp_files(
    db: Session, *, older_than: timedelta = TEMP_FILE_RETENTION, dry_run: bool = True
) -> dict[str, Any]:
    """临时数据文件 reconciliation(幂等): 解绑超龄临时 owner 引用。

    - 只处理 ``TEMP_OWNER_NAMESPACE`` 引用且超龄者; 保留期内不动;
    - 解绑后对象 ref_count 归零进入 orphaned, 物理回收由存储运维
      (safe_cleanup/purge_expired)负责, 本用例不直接删字节;
    - dry_run=True 只报告; 幂等: 重复执行无副作用。
    """
    now = datetime.now(UTC)
    refs = find_refs_by_entity_type(db, TEMP_OWNER_NAMESPACE)
    stale: list[dict[str, Any]] = []
    kept = 0
    for ref in refs:
        created = ref.get("created_at")
        if not created:
            stale.append(ref)
            continue
        try:
            created_dt = datetime.fromisoformat(created)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=UTC)
        except ValueError:
            stale.append(ref)
            continue
        if now - created_dt > older_than:
            stale.append(ref)
        else:
            kept += 1
    if not dry_run:
        for ref in stale:
            try:
                owner_id = int(ref["ref_entity_id"])
            except (TypeError, ValueError):
                continue  # 非法 owner 标识不属本用例(防御): 幂等跳过
            try:
                detach(
                    db, ref["object_id"],
                    TEMP_OWNER_NAMESPACE, owner_id,
                    ref_entity_type=TEMP_OWNER_NAMESPACE,
                )
            except ReferenceNotFoundError:
                continue  # 引用已被其他路径解绑: 幂等跳过
        db.flush()
    return {
        "dry_run": dry_run,
        "stale_count": len(stale),
        "kept_count": kept,
        "stale": stale,
    }
