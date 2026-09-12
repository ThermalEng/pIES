"""用户自定义模型模板用例(切片 dm2: 完整生命周期 + 项目目录)。

对应 customization-center.md「编写设备模型」与格式标准「模板(未实例化阶段)」:

1. 创建模板草稿(模板 ID = 模板 YAML 声明的 ``device.id``, 同一用户内唯一);
2. 查询当前用户模板列表 / 草稿详情(含聚合诊断) / 精确发布 revision;
3. ``expected_revision`` 乐观锁更新草稿 YAML(并发编辑拒绝静默覆盖);
4. 执行安全 YAML、schema、inputs、properties、interfaces、equations 校验,
   一次返回聚合诊断(校验失败不落盘, 不产生 revision);
5. 发布经过校验的草稿为不可变 revision(幂等以幂等键与 (template_id, revision) 保证);
6. 停用 / 重新启用模板(只影响后续选择, 不改变已保存项目模型与历史证据);
7. 删除尚未发布的草稿(已发布模板禁止删除; 被项目模型引用的 revision 保留);
8. 所有公开 ID 以不透明十进制字符串传输; 写操作统一由本层管理事务、
   审计、乐观锁与幂等边界; 模板 YAML、校验回执与结构摘要经对象存储门面保存。

项目目录接口(供「新建模型」页面): 可用模板列表 = 当前用户已发布且启用的
模板, 模板详情固定精确 revision。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import model as model_domain
from iesplan.core.diagnostics import SEVERITY_ERROR, Diagnostic, make_diag
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.namespace import (
    build_stable_id,
    is_valid_slug,
    is_valid_stable_id,
)
from iesplan.core.yamlmini import YamlParseError
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    DeviceModelDocument,
    canonical_bytes,
    canonical_receipt,
    parse_device_model_v2,
)
from iesplan.model import (
    TEMPLATE_STATUS_DISABLED,
    TEMPLATE_STATUS_DRAFT,
    TEMPLATE_STATUS_PUBLISHED,
    ModelConflictError,
    ModelTemplateRecord,
    ModelTemplateRevisionRecord,
)
from iesplan.storage import (
    ReferenceNotFoundError,
    attach,
    detach,
    find_refs_by_owner,
    get_object,
    put_object,
)

#: 模板域 owner 命名空间(模板主表行与 revision 行的对象引用持有者)
TEMPLATE_OWNER_NAMESPACE: str = "model_template"

#: 候选模板 YAML 上限(2 MiB, 与项目模型候选一致)
MAX_TEMPLATE_YAML_BYTES: int = 2 * 1024 * 1024

#: 模板 YAML / 回执 / 摘要对象媒体类型(规范字节为 JSON 文本)
TEMPLATE_MEDIA_TYPE: str = "application/json"

#: 模板域诊断码(集中登记于 core/diagnostics.py NEW_DIAG_CODES)
TPL_MDL_YAML_PARSE = "TPL-MDL-001"  # 模板 YAML 解析失败
TPL_MDL_VALIDATION_FAILED = "TPL-MDL-002"  # 模板校验失败(保存/发布拒绝, 包络码)
TPL_MDL_REVISION_CONFLICT = "TPL-MDL-003"  # 草稿乐观锁冲突
TPL_MDL_NOT_FOUND = "TPL-MDL-004"  # 模板或 revision 不存在
TPL_MDL_STATUS_INVALID = "TPL-MDL-005"  # 生命周期状态不允许该操作
TPL_MDL_REVISION_REQUIRED = "TPL-MDL-006"  # 尚未发布, 需要先发布
TPL_MDL_ALREADY_PUBLISHED = "TPL-MDL-007"  # 已发布模板禁止删除
TPL_NS_SLUG_INVALID = "API-VAL-002"  # slug 格式非法
TPL_NS_ID_MISMATCH = "API-VAL-003"  # device.id 与期望不一致
TPL_NS_FORBIDDEN = "API-REQ-002"  # 客户端伪造 namespace
TPL_NS_STABLE_ID_INVALID = "API-VAL-004"  # 稳定 ID 格式非法
TPL_NS_SLUG_CONFLICT = "API-CONFLICT-001"  # slug 已存在


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class TemplateNotFoundError(NotFoundError):
    """模板不存在或不属于当前用户(404; 不泄露他人模板存在性)。"""

    code = TPL_MDL_NOT_FOUND
    message_key = "ies.diag.tpl.not_found"
    http_status = 404


class TemplateValidationError(AppError):
    """模板校验失败(HTTP 400, 诊断明细入 params.diagnostics)。"""

    code = TPL_MDL_VALIDATION_FAILED
    severity = SEVERITY_ERROR
    message_key = "ies.diag.tpl.validation_failed"
    http_status = 400


class TemplateConflictError(ConflictError):
    """模板状态/乐观锁冲突(HTTP 409, TPL-MDL-003)。"""

    code = TPL_MDL_REVISION_CONFLICT
    message_key = "ies.diag.tpl.revision_conflict"


def _status_error(template_id: str, status: str) -> AppError:
    """生命周期状态非法(HTTP 400, TPL-MDL-005)。"""
    err = AppError(
        "模板当前状态不允许该生命周期操作",
        code=TPL_MDL_STATUS_INVALID,
        message_key="ies.diag.tpl.status_invalid",
        params={"template_id": template_id, "status": status},
        location={"object_type": "model_template", "template_id": template_id},
    )
    err.http_status = 400
    return err


def _published_delete_error(template_id: str, published_revision: int) -> AppError:
    """已发布模板禁止删除(HTTP 400, TPL-MDL-007)。"""
    err = AppError(
        "已发布模板禁止删除(发布 revision 与内容证据必须保留)",
        code=TPL_MDL_ALREADY_PUBLISHED,
        message_key="ies.diag.tpl.already_published",
        params={"template_id": template_id, "published_revision": published_revision},
        location={"object_type": "model_template", "template_id": template_id},
    )
    err.http_status = 400
    return err


# ---------------------------------------------------------------------------
# 值对象与诊断辅助
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TemplateValidation:
    """模板完整校验结果: 要么带最终文档, 要么带聚合诊断。文本文件只校验字头。"""

    ok: bool
    diagnostics: list[Diagnostic] = field(default_factory=list)
    document: DeviceModelDocument | None = None
    canonical_text: str = ""
    receipt: dict[str, Any] | None = None
    input_count: int = 0
    has_inputs: bool = False


@dataclass(frozen=True, slots=True)
class TemplateRevisionRef:
    """项目模型候选引用的精确模板 revision(权威内容经对象存储读取)。文本只校验字头。"""

    template_id: int
    revision: int
    yaml_object_id: int
    schema_version: str


def _diag(code: str, detail: str, *, field: str | None = None,
          params: Mapping[str, object] | None = None) -> Diagnostic:
    """构造模板域诊断(带字段路径与 expected/actual 参数)。"""
    location: dict[str, object] = {"object_type": "model_template"}
    if field is not None:
        location["field"] = field
    return make_diag(
        code, severity=SEVERITY_ERROR,
        params=dict(params or {"detail": detail}), location=location,
    )


def _ensure_namespace(db: Session, user) -> str:
    """确保用户已分配 public_namespace（首次需要时分配，终身不变）。"""
    from iesplan.application.namespace import ensure_public_namespace
    return ensure_public_namespace(db, user)


def _validate_slug(slug: str) -> None:
    """校验 slug 格式与长度，不合法抛 400。"""
    if not is_valid_slug(slug):
        err = AppError(
            f"slug 格式非法: {slug!r}",
            code=TPL_NS_SLUG_INVALID,
            message_key="ies.diag.tpl.api.slug_invalid",
            params={"slug": slug, "expected": "小写字母/数字，点/下划线/连字符分段，1-64 字符"},
            location={"object_type": "model_template", "field": "slug"},
        )
        err.http_status = 400
        raise err


def _validate_device_id_match(yaml_device_id: str, expected_id: str) -> None:
    """校验 YAML 中的 device.id 与后端计算结果一致，不一致返回阻断诊断。"""
    if yaml_device_id != expected_id:
        err = AppError(
            "device.id 与后端计算结果不一致",
            code=TPL_NS_ID_MISMATCH,
            message_key="ies.diag.tpl.api.device_id_mismatch",
            params={"expected": expected_id, "actual": yaml_device_id,
                    "field": "device.id"},
            location={"object_type": "model_template", "field": "device.id"},
        )
        err.http_status = 400
        raise err


def _put_json(db: Session, value: Any, category: str):
    """经对象存储门面保存 JSON 字节, 返回 ObjectHandle。

    支持 dict 与 list(diagnostic 列表等聚合结构)。
    """
    payload = json.dumps(dict(value) if isinstance(value, Mapping) else value,
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return put_object(db, payload, TEMPLATE_MEDIA_TYPE, source_category=category)


# ---------------------------------------------------------------------------
# 模板解析与完整校验(纯校验, 不写对象/不产生 revision)
# ---------------------------------------------------------------------------


def validate_template_raw(raw: Mapping[str, Any]) -> TemplateValidation:
    """对已解析的模板原始映射执行完整 2.0.0 校验(含顶层 inputs 强制)。

    草稿保存(来自 YAML 文本)与发布(来自规范字节 JSON)共用同一门禁。
    """
    diags: list[Diagnostic] = []
    result = parse_device_model_v2(raw, file="<model-template>")
    if not result.ok:
        return TemplateValidation(ok=False, diagnostics=list(result.diagnostics))
    doc = result.document
    assert doc is not None
    if doc.inputs is None:
        diags.append(_diag(TPL_MDL_YAML_PARSE, "模板必须声明顶层 inputs", field="inputs",
                           params={"expected": "顶层 inputs(未实例化模型)",
                                   "actual": "missing"}))
        return TemplateValidation(ok=False, diagnostics=diags)
    from iesplan.devices.parser2 import parse_template_inputs
    try:
        inputs = parse_template_inputs(doc.inputs, file="<model-template>")
    except Exception as exc:  # noqa: BLE001 - parser2.ParseError 等解析异常统一转为诊断
        diags.append(_diag(TPL_MDL_YAML_PARSE, str(exc), field="inputs",
                           params={"expected": "合法 inputs 树", "actual": str(exc)}))
        return TemplateValidation(ok=False, diagnostics=diags)
    text = canonical_bytes(doc).decode("utf-8")
    return TemplateValidation(
        ok=True,
        document=doc,
        canonical_text=text,
        receipt=canonical_receipt(doc),
        input_count=len(inputs.leaves),
        has_inputs=True,
    )


def validate_template_yaml(model_yaml: str) -> TemplateValidation:
    """模板 YAML 完整校验: 安全解析 → 2.0.0 完整校验(含顶层 inputs)。

    模板必须声明顶层 ``inputs``(未实例化模型特例); 校验失败返回聚合诊断。
    本函数是草稿保存的权威门禁(发布以规范字节重新完整校验)。
    """
    diags: list[Diagnostic] = []
    if not model_yaml or not model_yaml.strip():
        diags.append(_diag(TPL_MDL_YAML_PARSE, "模板 YAML 不能为空", field="model_yaml",
                           params={"expected": "非空 ies.device-model YAML(含顶层 inputs)",
                                   "actual": "空"}))
        return TemplateValidation(ok=False, diagnostics=diags)
    if len(model_yaml.encode("utf-8")) > MAX_TEMPLATE_YAML_BYTES:
        diags.append(_diag(
            TPL_MDL_YAML_PARSE, f"模板 YAML 超过上限 {MAX_TEMPLATE_YAML_BYTES} 字节",
            field="model_yaml",
            params={"expected": f"≤ {MAX_TEMPLATE_YAML_BYTES} 字节",
                    "actual": len(model_yaml.encode("utf-8"))},
        ))
        return TemplateValidation(ok=False, diagnostics=diags)
    try:
        raw = yaml_load(model_yaml)
    except YamlParseError as exc:
        diags.append(_diag(TPL_MDL_YAML_PARSE, str(exc), field="model_yaml",
                           params={"expected": "YAML 1.2 安全子集", "actual": str(exc),
                                   "line": exc.line}))
        return TemplateValidation(ok=False, diagnostics=diags)
    if not isinstance(raw, Mapping):
        diags.append(_diag(TPL_MDL_YAML_PARSE, "模板顶层必须是 mapping", field="<root>",
                           params={"expected": "mapping", "actual": type(raw).__name__}))
        return TemplateValidation(ok=False, diagnostics=diags)
    return validate_template_raw(raw)


def _build_summary(document: DeviceModelDocument) -> dict[str, Any]:
    """模板结构摘要(properties/interfaces/equations 计数, 供列表与详情展示)。"""
    return {
        "property_count": len(document.properties),
        "interface_count": len(document.interfaces),
        "equation_count": len(document.equations.relations) if document.equations else 0,
    }


# ---------------------------------------------------------------------------
# 领域读取辅助
# ---------------------------------------------------------------------------


def _get_owned_template(db: Session, user, template_id: str) -> ModelTemplateRecord:
    """按模板 ID 读取当前用户的模板(不存在或不属于 → 404)。"""
    record = model_domain.get_owned_template(db, user.id, template_id)
    if record is None:
        raise TemplateNotFoundError(
            "模板不存在",
            params={"template_id": template_id},
            location={"object_type": "model_template", "template_id": template_id},
        )
    return record


def _template_to_dict(template: ModelTemplateRecord) -> dict[str, Any]:
    """模板主表行 → 公开视图。"""
    return {
        "id": str(template.id),
        "template_id": template.template_id,
        "slug": template.slug,
        "public_namespace": template.public_namespace,
        "status": template.status,
        "description": template.description,
        "draft_revision": template.draft_revision,
        "draft_has_inputs": template.draft_has_inputs,
        "published_revision": template.published_revision,
        "published_at": template.published_at,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


def _revision_to_dict(revision: ModelTemplateRevisionRecord) -> dict[str, Any]:
    """发布 revision 行 → 公开视图(精确 revision)。文本文件只校验字头。"""
    return {
        "id": str(revision.id),
        "revision": revision.revision,
        "schema_version": revision.schema_version,
        "input_count": revision.input_count,
        "yaml_object_id": str(revision.yaml_object_id),
        "receipt_object_id": str(revision.receipt_object_id),
        "summary_object_id": str(revision.summary_object_id),
        "published_by": str(revision.published_by),
        "published_at": revision.published_at,
    }


def _read_template_document(db: Session, object_id: int) -> str:
    """经对象存储门面读取模板规范 YAML 文本(解码失败抛模板对象损坏错误)。"""
    raw = get_object(db, object_id)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AppError(
            "模板对象损坏",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "model_template", "object_id": str(object_id)},
        ) from None
    return text


def _read_template_document_mapping(db: Session, object_id: int) -> Mapping[str, Any] | None:
    """读取模板规范文档为嵌套 JSON 对象(API 详情返回的契约形态)。

    规范字节为紧凑 JSON 文本(``canonical_bytes`` 产出); 前端契约
    (features/customization 与 features/modeling)要求 ``document`` 为
    已解析的对象而非文本, 在此门面处解析, 内部仍以文本形式存储。
    """
    text = _read_template_document(db, object_id)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise AppError(
            "模板对象损坏",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "model_template", "object_id": str(object_id)},
        ) from None
    if not isinstance(parsed, Mapping):
        raise AppError(
            "模板对象形态非法",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "model_template", "object_id": str(object_id)},
        ) from None
    return parsed


def _load_diagnostics(db: Session, object_id: int | None) -> list[dict[str, Any]]:
    """读取草稿/revision 关联的聚合诊断 JSON(无诊断对象时返回空列表)。"""
    if object_id is None:
        return []
    parsed = _read_json_object(db, object_id, expected_type=list)
    if not all(isinstance(item, dict) for item in parsed):
        raise _corrupt_template_object(object_id, "诊断对象包含非法条目")
    return list(parsed)


def _corrupt_template_object(object_id: int, message: str) -> AppError:
    """构造统一的模板对象损坏错误；读取失败不得静默降级为空数据。"""
    return AppError(
        message,
        code="SYS-STORE-001",
        message_key="ies.diag.store.corrupt",
        location={"object_type": "model_template", "object_id": str(object_id)},
    )


def _read_json_object(
    db: Session,
    object_id: int,
    *,
    expected_type: type[list] | type[Mapping],
) -> Any:
    """读取 JSON 对象并严格核对形态，损坏时返回标准系统错误。"""
    raw = get_object(db, object_id)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _corrupt_template_object(object_id, "模板关联对象损坏") from None
    if not isinstance(parsed, expected_type):
        raise _corrupt_template_object(object_id, "模板关联对象形态非法")
    return parsed


# ---------------------------------------------------------------------------
# 草稿保存(乐观锁; 校验失败不落盘但保留诊断)
# ---------------------------------------------------------------------------


def _save_draft(
    db: Session,
    user,
    template_id: str,
    *,
    model_yaml: str,
    expected_revision: int | None,
    description: str | None = None,
) -> dict[str, Any]:
    """保存模板草稿(完整校验 → 校验通过落盘; 失败仅保留诊断)。

    乐观锁: ``expected_revision`` 与当前 ``draft_revision`` 不一致时 409;
    校验失败返回 400 包络码(诊断入 params.diagnostics), 草稿内容不变。
    """
    template = _get_owned_template(db, user, template_id)
    # 未完成显式迁移的旧发布内容不得产生新的 publication（任务书 §三）
    if template.template_id and not is_valid_stable_id(template.template_id):
        raise AppError(
            "旧发布内容需先完成显式迁移才能产生新的 publication",
            code="API-VAL-004",
            message_key="ies.diag.tpl.api.stable_id_invalid",
            params={"template_id": template.template_id, "expected": "user.<namespace>.device.<slug>"},
            location={"object_type": "model_template", "template_id": template_id},
        )
    if expected_revision is not None and template.draft_revision != expected_revision:
        raise TemplateConflictError(
            "模板草稿已被其他操作更新",
            params={"expected_revision": expected_revision,
                    "current_revision": template.draft_revision},
            location={"object_type": "model_template", "template_id": template_id},
        )
    validation = validate_template_yaml(model_yaml)
    if not validation.ok or validation.document is None:
        # 校验失败不落盘、不推进 revision、不留下部分引用（任务书 §四）
        # 诊断通过错误信封的 params.diagnostics 返回，前端在编辑会话内保留
        raise TemplateValidationError(
            "",
            params={"diagnostics": [d.to_dict() for d in validation.diagnostics],
                    "count": len(validation.diagnostics)},
            location={"object_type": "model_template", "template_id": template_id},
        )

    # 校验稳定 ID 与后端计算结果一致（不静默覆盖）
    if validation.document is not None and validation.document.device is not None:
        expected_stable = template.template_id
        _validate_device_id_match(validation.document.device.id, expected_stable)

    # 校验通过: 规范 YAML 落盘, 形成新的不可变草稿 revision
    handle = put_object(db, validation.canonical_text.encode("utf-8"), TEMPLATE_MEDIA_TYPE,
                        source_category="model_template_draft")
    old_diags = template.draft_diagnostics_object_id
    new_revision = template.draft_revision + 1
    fields: dict[str, Any] = {
        "draft_yaml_object_id": handle.id,
        "draft_has_inputs": validation.has_inputs,
        "draft_revision": new_revision,
        "draft_updated_at": datetime.now(UTC),
        "draft_diagnostics_object_id": None,
    }
    if description is not None:
        fields["description"] = description
    template = model_domain.update_template(db, template.id, **fields)
    attach(db, handle.id, TEMPLATE_OWNER_NAMESPACE, template.id,
           ref_entity_type=TEMPLATE_OWNER_NAMESPACE, purpose="draft_yaml")
    # 旧 revision 不覆盖、不 detach（不可变草稿历史，任务书 §四）
    # 仅当需要清理时由存储运维处理，不在保存时 detach
    # 新增不可变草稿 revision 行
    diag_handle2 = _put_json(db, [], "model_template_draft_diagnostics")
    draft_rev = model_domain.create_draft_revision(
        db,
        entry_id=template.id,
        revision=new_revision,
        yaml_object_id=handle.id,
        source="yaml_editor",
        created_by=user.id,
        diagnostics_object_id=diag_handle2.id,
    )
    attach(db, diag_handle2.id, TEMPLATE_OWNER_NAMESPACE, template.id,
           ref_entity_type=TEMPLATE_OWNER_NAMESPACE, purpose="draft_revision")
    template = model_domain.update_template(
        db, template.id, current_draft_revision_id=draft_rev.id
    )
    if old_diags is not None:
        try:
            detach(db, old_diags, TEMPLATE_OWNER_NAMESPACE, template.id,
                   ref_entity_type=TEMPLATE_OWNER_NAMESPACE)
        except ReferenceNotFoundError:
            pass
    audit_domain.append_entry(
        db,
        entity_type="model_template",
        entity_id=template.id,
        action="model_template.draft_saved",
        actor_id=user.id,
        actor_type="user",
        extra={
            "template_id": template.template_id,
            "draft_revision": template.draft_revision,
        },
    )
    return _template_to_dict(template)


def save_template_draft(
    db: Session,
    user,
    template_id: str,
    *,
    model_yaml: str,
    expected_revision: int | None,
    description: str | None = None,
) -> dict[str, Any]:
    """事务型草稿保存命令(application 统一 commit/rollback 边界)。"""
    try:
        result = _save_draft(db, user, template_id, model_yaml=model_yaml,
                             expected_revision=expected_revision, description=description)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 创建草稿 / 列表 / 详情
# ---------------------------------------------------------------------------


def create_template_draft(
    db: Session,
    user,
    *,
    model_yaml: str,
    slug: str,
    description: str | None = None,
) -> dict[str, Any]:
    """创建模板草稿(客户端提交 slug，后端组合稳定 ID)。

    流程：校验 slug → 分配 public_namespace → 组合 device.id →
    校验 YAML → 比对 device.id 一致性 → 保存。

    模板稳定 ID 创建后不可变更；需要不同 slug 时创建新条目。
    """
    _validate_slug(slug)
    namespace = _ensure_namespace(db, user)
    expected_id = build_stable_id(namespace, slug)
    validation = validate_template_yaml(model_yaml)
    if not validation.ok or validation.document is None or validation.document.device is None:
        raise TemplateValidationError(
            "",
            params={"diagnostics": [d.to_dict() for d in validation.diagnostics],
                    "count": len(validation.diagnostics)},
            location={"object_type": "model_template"},
        )
    yaml_device_id = validation.document.device.id
    _validate_device_id_match(yaml_device_id, expected_id)
    template_id = expected_id
    handle = put_object(db, validation.canonical_text.encode("utf-8"), TEMPLATE_MEDIA_TYPE,
                        source_category="model_template_draft")
    try:
        template = model_domain.create_template(
            db,
            template_id=template_id,
            slug=slug,
            public_namespace=namespace,
            owner_id=user.id,
            status=TEMPLATE_STATUS_DRAFT,
            description=description,
            draft_yaml_object_id=handle.id,
            draft_has_inputs=validation.has_inputs,
            draft_revision=1,
            draft_updated_at=datetime.now(UTC),
        )
    except ModelConflictError as exc:
        # 事务整体回滚; 已写盘对象未建立 owner 引用, 进入孤儿生命周期
        # 由存储运维 safe_cleanup/purge 回收(与 model_save 冲突路径同模式)
        db.rollback()
        raise ConflictError(
            "模板已存在(同一用户模板 ID 唯一)",
            params={"template_id": template_id},
            location={"object_type": "model_template", "template_id": template_id},
        ) from exc
    attach(db, handle.id, TEMPLATE_OWNER_NAMESPACE, template.id,
           ref_entity_type=TEMPLATE_OWNER_NAMESPACE, purpose="draft_yaml")
    # 初始不可变草稿 revision
    _diag_h = _put_json(db, [], "model_template_draft_diagnostics")
    _dr = model_domain.create_draft_revision(
        db,
        entry_id=template.id,
        revision=1,
        yaml_object_id=handle.id,
        source="yaml_editor",
        created_by=user.id,
        diagnostics_object_id=_diag_h.id,
    )
    attach(db, _diag_h.id, TEMPLATE_OWNER_NAMESPACE, template.id,
           ref_entity_type=TEMPLATE_OWNER_NAMESPACE, purpose="draft_revision")
    template = model_domain.update_template(
        db, template.id, current_draft_revision_id=_dr.id
    )
    audit_domain.append_entry(
        db,
        entity_type="model_template",
        entity_id=template.id,
        action="model_template.created",
        actor_id=user.id,
        actor_type="user",
        extra={"template_id": template_id, "draft_revision": 1},
    )
    db.commit()
    return _template_to_dict(template)


def list_my_templates(db: Session, user) -> list[dict[str, Any]]:
    """当前用户模板列表(全部状态, 最新在前)。"""
    return [_template_to_dict(t) for t in model_domain.list_owned_templates(db, user.id)]


def get_template_detail(db: Session, user, template_id: str) -> dict[str, Any]:
    """模板详情(草稿内容 + 聚合诊断; 已发布时附精确 revision 视图)。

    ``template`` 与目录项同构: 已发布时携带 ``revision`` 字段(最新发布
    revision 的精确视图), 供「新建模型」表单提交时引用固定 revision。
    """
    template = _get_owned_template(db, user, template_id)
    document = None
    if template.draft_yaml_object_id is not None:
        document = _read_template_document_mapping(db, template.draft_yaml_object_id)
    item = _template_to_dict(template)
    if template.published_revision > 0:
        rev = model_domain.get_published_revision(db, template.id, template.published_revision)
        if rev is not None:
            item["revision"] = _revision_to_dict(rev)
    return {
        "template": item,
        "document": document,
        "diagnostics": _load_diagnostics(db, template.draft_diagnostics_object_id),
    }


def get_template_revision(db: Session, user, template_id: str, revision: int) -> dict[str, Any]:
    """精确发布 revision 详情(规范 YAML + 校验回执 + 结构摘要)。

    草稿内容与发布 revision 分开: 详情返回发布时固定的规范字节,
    不读取当前草稿(模板更新不改变历史 revision)。
    """
    template = _get_owned_template(db, user, template_id)
    row = model_domain.get_published_revision(db, template.id, revision)
    if row is None:
        raise TemplateNotFoundError(
            "模板 revision 不存在",
            params={"template_id": template_id, "revision": revision},
            location={"object_type": "model_template", "template_id": template_id,
                      "revision": revision},
        )
    return {
        "template": _template_to_dict(template),
        "revision": _revision_to_dict(row),
        "document": _read_template_document_mapping(db, row.yaml_object_id),
        "receipt": _read_json_object(db, row.receipt_object_id, expected_type=Mapping),
        "summary": _read_json_object(db, row.summary_object_id, expected_type=Mapping),
        "diagnostics": _load_diagnostics(db, row.diagnostics_object_id),
    }


def validate_template_revision(
    db: Session,
    user,
    template_id: str,
    revision: int,
) -> TemplateValidation:
    """重新校验一个精确发布 revision；引用与存储错误保持标准错误信封。

    API 层不得直接读取对象存储。文本文件只校验字头（schema/schema_version）。
    """
    ref = resolve_template_revision(
        db, user, template_id, revision,
    )
    document = _read_template_document_mapping(db, ref.yaml_object_id)
    if document is None:  # 防御式分支；当前读取门面只会返回 Mapping 或抛错
        raise AppError(
            "模板 revision 对象损坏",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "model_template", "object_id": str(ref.yaml_object_id)},
        )
    return validate_template_raw(document)


# ---------------------------------------------------------------------------
# 发布(不可变 revision; 幂等键/(template_id, revision) 幂等)
# ---------------------------------------------------------------------------


def _publish(
    db: Session,
    user,
    template_id: str,
    *,
    expected_revision: int | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """发布草稿为不可变 revision。

    - 校验失败 → 400 聚合诊断(不产生 revision);
    - 幂等键重放返回同一逻辑结果(不新增 revision)，否则以 (template_id, revision) 幂等；
    - 发布成功后模板 status → published, published_revision 推进。文本文件只校验字头。
    """
    template = _get_owned_template(db, user, template_id)
    # 未完成显式迁移的旧发布内容不得产生新的 publication（任务书 §三）
    if template.template_id and not is_valid_stable_id(template.template_id):
        raise AppError(
            "旧发布内容需先完成显式迁移才能产生新的 publication",
            code="API-VAL-004",
            message_key="ies.diag.tpl.api.stable_id_invalid",
            params={"template_id": template.template_id, "expected": "user.<namespace>.device.<slug>"},
            location={"object_type": "model_template", "template_id": template_id},
        )
    if expected_revision is not None and template.draft_revision != expected_revision:
        raise TemplateConflictError(
            "模板草稿已被其他操作更新",
            params={"expected_revision": expected_revision,
                    "current_revision": template.draft_revision},
            location={"object_type": "model_template", "template_id": template_id},
        )
    if template.draft_yaml_object_id is None:
        raise AppError(
            "模板没有可发布的草稿内容",
            code=TPL_MDL_REVISION_REQUIRED,
            message_key="ies.diag.tpl.revision_required",
            params={"template_id": template_id},
            location={"object_type": "model_template", "template_id": template_id},
        )
    # 发布必须核对 expected_revision（任务书 §四）

    # 幂等键重放: 返回同一逻辑结果, 不新增 revision
    if idempotency_key:
        existing = model_domain.find_revision_by_idempotency(db, template.id, idempotency_key)
        if existing is not None:
            return {"revision": _revision_to_dict(existing), "duplicate": True}

    # 以草稿规范字节为权威内容(草稿保存时已完整校验); 规范字节为 JSON 文本
    doc_text = _read_template_document(db, template.draft_yaml_object_id)
    try:
        parsed_raw = json.loads(doc_text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AppError(
            "模板草稿对象损坏",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "model_template",
                      "object_id": str(template.draft_yaml_object_id)},
        ) from None
    if not isinstance(parsed_raw, Mapping):
        raise AppError(
            "模板草稿对象形态非法",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "model_template",
                      "object_id": str(template.draft_yaml_object_id)},
        ) from None
    validation = validate_template_raw(parsed_raw)
    if not validation.ok or validation.document is None:
        raise TemplateValidationError(
            "",
            params={"diagnostics": [d.to_dict() for d in validation.diagnostics],
                    "count": len(validation.diagnostics)},
            location={"object_type": "model_template", "template_id": template_id},
        )
    document = validation.document
    if document.device is None:
        raise TemplateValidationError(
            "", params={"diagnostics": [], "count": 0},
            location={"object_type": "model_template", "template_id": template_id},
        )

    # 发布内容对象: 规范 YAML + 校验回执 + 结构摘要(全部经对象存储门面)
    yaml_handle = put_object(db, validation.canonical_text.encode("utf-8"),
                             TEMPLATE_MEDIA_TYPE, source_category="model_template_revision")
    receipt = validation.receipt or canonical_receipt(document)
    receipt = {**dict(receipt), "template_id": template.template_id,
               "revision": template.published_revision + 1,
               "schema": SCHEMA_ID, "schema_version": SCHEMA_VERSION,
               "input_count": validation.input_count}
    receipt_handle = _put_json(db, receipt, "model_template_receipt")
    summary = _build_summary(document)
    summary_handle = _put_json(db, summary, "model_template_summary")
    diag_handle = _put_json(db, [], "model_template_diagnostics")

    try:
        row = model_domain.create_published_revision(
            db,
            template_row_id=template.id,
            revision=template.published_revision + 1,
            schema_version=SCHEMA_VERSION,
            input_count=validation.input_count,
            yaml_object_id=yaml_handle.id,
            receipt_object_id=receipt_handle.id,
            summary_object_id=summary_handle.id,
            diagnostics_object_id=diag_handle.id,
            idempotency_key=idempotency_key,
            published_by=user.id,
        )
    except ModelConflictError as exc:
        db.rollback()
        # 并发幂等: 以 (template_id, revision) 或 idempotency_key 幂等，文本只校验字头
        if idempotency_key:
            same = model_domain.find_revision_by_idempotency(db, template.id, idempotency_key)
            if same is not None:
                return {"revision": _revision_to_dict(same), "duplicate": True}
        else:
            # 无幂等键时，按 revision 幂等（同一 revision 重放）
            same = model_domain.get_published_revision(
                db, template.id, template.published_revision + 1
            )
            if same is not None:
                return {"revision": _revision_to_dict(same), "duplicate": True}
        raise ConflictError(
            "模板发布冲突(并发), 请重试",
            params={"template_id": template_id},
            location={"object_type": "model_template", "template_id": template_id},
        ) from exc

    for handle in (yaml_handle, receipt_handle, summary_handle, diag_handle):
        attach(db, handle.id, TEMPLATE_OWNER_NAMESPACE, template.id,
               ref_entity_type=TEMPLATE_OWNER_NAMESPACE, purpose="revision")
    template = model_domain.update_template(
        db,
        template.id,
        published_revision=row.revision,
        current_published_revision_id=row.id,
        published_at=datetime.now(UTC),
        status=TEMPLATE_STATUS_PUBLISHED,
    )
    audit_domain.append_entry(
        db,
        entity_type="model_template",
        entity_id=template.id,
        action="model_template.published",
        actor_id=user.id,
        actor_type="user",
        extra={"template_id": template.template_id, "revision": row.revision},
    )
    return {"revision": _revision_to_dict(row), "duplicate": False}


def publish_template(
    db: Session,
    user,
    template_id: str,
    *,
    expected_revision: int | None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """事务型发布命令。"""
    try:
        result = _publish(db, user, template_id,
                          expected_revision=expected_revision,
                          idempotency_key=idempotency_key)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 停用 / 启用 / 删除草稿
# ---------------------------------------------------------------------------


def set_template_status(
    db: Session,
    user,
    template_id: str,
    *,
    enabled: bool,
) -> dict[str, Any]:
    """停用 / 重新启用模板(只影响后续选择; 已保存项目模型不受影响)。

    停用要求模板已发布(published → disabled); 启用要求已发布且当前停用
    (disabled → published)。未发布模板不允许停用/启用(其生命周期由草稿
    保存与发布管理)。
    """
    template = _get_owned_template(db, user, template_id)
    if template.status not in (TEMPLATE_STATUS_PUBLISHED, TEMPLATE_STATUS_DISABLED):
        raise _status_error(template_id, template.status)
    if enabled:
        if template.status == TEMPLATE_STATUS_PUBLISHED:
            return _template_to_dict(template)  # 已启用: 幂等返回
        template = model_domain.update_template(
            db, template.id, status=TEMPLATE_STATUS_PUBLISHED
        )
    else:
        if template.status == TEMPLATE_STATUS_DISABLED:
            return _template_to_dict(template)  # 已停用: 幂等返回
        template = model_domain.update_template(
            db, template.id, status=TEMPLATE_STATUS_DISABLED
        )
    audit_domain.append_entry(
        db,
        entity_type="model_template",
        entity_id=template.id,
        action="model_template.enabled" if enabled else "model_template.disabled",
        actor_id=user.id,
        actor_type="user",
        extra={"template_id": template.template_id, "status": template.status},
    )
    return _template_to_dict(template)


def delete_template_draft(
    db: Session,
    user,
    template_id: str,
) -> dict[str, Any]:
    """删除尚未发布的模板草稿(整行硬删除)。

    - 已发布模板(含停用)禁止删除: revision 与内容证据必须保留;
    - 未发布草稿: 删除主表行并解绑草稿对象引用(对象进入孤儿生命周期);
    - 被项目模型引用的已发布 revision 永不删除(本函数只在未发布时允许)。
    """
    template = _get_owned_template(db, user, template_id)
    if template.published_revision > 0:
        raise _published_delete_error(template_id, template.published_revision)
    refs = find_refs_by_owner(db, TEMPLATE_OWNER_NAMESPACE, template.id,
                              TEMPLATE_OWNER_NAMESPACE)
    for ref in refs:
        try:
            detach(db, ref["object_id"], TEMPLATE_OWNER_NAMESPACE, template.id,
                   ref_entity_type=TEMPLATE_OWNER_NAMESPACE)
        except ReferenceNotFoundError:
            continue
    audit_domain.append_entry(
        db,
        entity_type="model_template",
        entity_id=template.id,
        action="model_template.deleted",
        actor_id=user.id,
        actor_type="user",
        extra={"template_id": template.template_id},
    )
    model_domain.delete_template(db, template.id)
    return {"deleted": template_id, "ok": True}


# ---------------------------------------------------------------------------
# 项目模板目录(供「新建模型」页面选择)
# ---------------------------------------------------------------------------


def list_available_templates(db: Session, user) -> list[dict[str, Any]]:
    """当前用户已发布且启用的模板目录(项目模板选择器)。

    列表项携带最新发布 revision 与结构摘要对象引用; 未发布的草稿与停用模板
    不出现在目录中(停用只影响后续选择)。
    未完成显式迁移的旧 ID 不进入新的选择结果（任务书 §三）。
    """
    rows = model_domain.list_owned_templates_by_status(db, user.id, TEMPLATE_STATUS_PUBLISHED)
    out: list[dict[str, Any]] = []
    for t in rows:
        # 旧 ID 未迁移的不进入选择结果
        if t.template_id and not is_valid_stable_id(t.template_id):
            continue
        item = _template_to_dict(t)
        if t.published_revision > 0:
            rev = model_domain.get_published_revision(db, t.id, t.published_revision)
            if rev is not None:
                item["revision"] = _revision_to_dict(rev)
        out.append(item)
    return out


def resolve_template_revision(
    db: Session, user, template_id: str, revision: int
) -> TemplateRevisionRef:
    """解析项目模型候选引用的精确模板 revision(权威内容)。

    校验: 模板存在且属于当前用户、已发布、未停用, revision 匹配。文本文件只校验字头。
    """
    template = _get_owned_template(db, user, template_id)
    if template.status != TEMPLATE_STATUS_PUBLISHED:
        raise ConflictError(
            "模板未启用(停用只影响后续选择)",
            params={"template_id": template_id, "status": template.status},
            location={"object_type": "model_template", "template_id": template_id},
        )
    row = model_domain.get_published_revision(db, template.id, revision)
    if row is None:
        raise TemplateNotFoundError(
            "模板 revision 不存在",
            params={"template_id": template_id, "revision": revision},
            location={"object_type": "model_template", "template_id": template_id,
                      "revision": revision},
        )
    return TemplateRevisionRef(
        template_id=template.id,
        revision=row.revision,
        yaml_object_id=row.yaml_object_id,
        schema_version=row.schema_version,
    )


def list_draft_revisions(db: Session, user, template_id: str) -> list[dict[str, Any]]:
    """列出模板的不可变草稿 revision 历史（旧 revision 可读，对象引用保留）。"""
    template = _get_owned_template(db, user, template_id)
    rows = model_domain.list_draft_revisions(db, template.id)
    return [
        {
            "id": str(r.id),
            "revision": r.revision,
            "yaml_object_id": str(r.yaml_object_id),
            "source": r.source,
            "created_by": str(r.created_by),
            "created_at": r.created_at,
        }
        for r in rows
    ]


def get_draft_revision(db: Session, user, template_id: str, revision: int) -> dict[str, Any]:
    """读取精确草稿 revision（不可变，对象引用保留）。"""
    template = _get_owned_template(db, user, template_id)
    row = model_domain.get_draft_revision(db, template.id, revision)
    if row is None:
        raise TemplateNotFoundError("草稿 revision 不存在",
                                    params={"template_id": template_id, "revision": revision})
    return {
        "id": str(row.id),
        "revision": row.revision,
        "yaml_object_id": str(row.yaml_object_id),
        "source": row.source,
        "created_by": str(row.created_by),
        "created_at": row.created_at,
        "document": _read_template_document_mapping(db, row.yaml_object_id),
    }
