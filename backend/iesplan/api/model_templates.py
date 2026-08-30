"""用户自定义模型模板 API(切片 dm2: 完整生命周期 + 项目模板目录)。

路由清单(prefix /api/model-templates, 当前用户作用域):
- POST   /api/model-templates                    创建模板草稿(模板 ID = YAML 的 device.id);
- GET    /api/model-templates                    当前用户模板列表(全部状态);
- GET    /api/model-templates/{template_id}      模板详情(草稿内容 + 聚合诊断);
- PUT    /api/model-templates/{template_id}      保存草稿(expected_revision 乐观锁);
- POST   /api/model-templates/{template_id}/validate   模板完整校验(不落盘);
- POST   /api/model-templates/{template_id}/publish    发布不可变 revision(幂等);
- POST   /api/model-templates/{template_id}/disable    停用(只影响后续选择);
- POST   /api/model-templates/{template_id}/enable     重新启用;
- DELETE /api/model-templates/{template_id}      删除尚未发布的草稿;
- GET    /api/model-templates/{template_id}/revisions/{revision}  精确发布 revision 详情;
- GET    /api/model-templates/catalog            当前用户可用模板目录(已发布且启用)。

语义:
- 校验失败: validate 返回 200 + {valid: false, diagnostics}(完整聚合诊断);
  保存/发布返回 400 + 标准 8 字段信封(TPL-MDL-002), 诊断明细入
  params.diagnostics; 不落盘、不产生 revision;
- 草稿乐观锁: expected_revision 与当前 draft_revision 不一致 → 409 标准信封
  (TPL-MDL-003, 并发编辑不得静默覆盖);
- 发布: 相同规范内容幂等返回同一 revision(duplicate: true); 幂等键重放
  返回同一逻辑结果;
- 删除: 已发布模板 → 409/400 标准信封(TPL-MDL-007); 未发布草稿整行删除;
- 所有权: 全部端点只作用于当前用户自己的模板(他人模板 → 404, 不泄露存在性);
- 所有公开 ID(模板主行 id / revision id / 对象 id)以不透明十进制字符串传输。

权限: 全部端点要求登录(401 标准信封); 无管理员专用端点。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.application.model_templates import (
    create_template_draft,
    delete_template_draft,
    get_draft_revision,
    get_template_detail,
    get_template_revision,
    list_available_templates,
    list_draft_revisions,
    list_my_templates,
    migrate_draft_to_new_stable_id,
    migrate_published_template,
    publish_template,
    save_template_draft,
    set_template_status,
    validate_template_revision,
    validate_template_yaml,
)
from iesplan.db import get_db

router = APIRouter(prefix="/api/model-templates", tags=["model-templates"])

DbSession = Annotated[Session, Depends(get_db)]

_TEMPLATE_ID_PATTERN = r"^[a-z0-9]+([._-][a-z0-9]+)*$"


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class TemplateCreateRequest(BaseModel):
    """创建模板草稿(客户端提交 slug，后端组合 user.<namespace>.device.<slug>)。"""

    slug: str = Field(min_length=1, max_length=64, description="模板 slug（小写字母/数字，点/下划线/连字符分段）")
    model_yaml: str = Field(min_length=1, max_length=2_000_000, description="模板 YAML 文本(含顶层 inputs)")
    description: str | None = Field(default=None, max_length=500, description="简短说明")
    # 客户端不得提交 public_namespace（服务端分配）；若提交则拒绝
    public_namespace: str | None = Field(default=None, description="禁止客户端提交")

    model_config = {"extra": "forbid"}


class TemplateValidateRequest(BaseModel):
    """模板完整校验请求(validate 端点, 不落盘)。

    支持两种形态:
    - ``model_yaml``: 直接提交候选 YAML 文本(在线编辑即时校验);
    - ``template_id`` + ``template_revision`` + ``template_sha256``:
      对已发布的精确 revision 重新校验(不读取当前草稿)。
    """

    model_yaml: str | None = Field(
        default=None,
        max_length=2_000_000,
        description="候选模板 YAML 文本(在线编辑)",
    )
    template_id: str | None = Field(default=None, description="已发布模板稳定 ID")
    template_revision: int | None = Field(default=None, ge=1, description="精确发布 revision")
    template_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", description="精确 revision 内容摘要",
    )


class TemplateDraftUpdateRequest(BaseModel):
    """保存模板草稿(完整替换; expected_revision 乐观锁)。"""

    model_yaml: str = Field(min_length=1, max_length=2_000_000, description="模板 YAML 文本(含顶层 inputs)")
    description: str | None = Field(default=None, max_length=500, description="简短说明")
    expected_revision: int = Field(ge=0, description="预期草稿修订(乐观锁)")


class TemplatePublishRequest(BaseModel):
    """发布模板草稿为不可变 revision(可重试写操作携带幂等键)。"""

    expected_revision: int = Field(ge=0, description="预期草稿修订(乐观锁)")
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9._:-]{1,128}$", description="模板内幂等键",
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post("", status_code=201, summary="创建模板草稿")
def create_template_endpoint(
    payload: TemplateCreateRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """创建模板草稿(校验失败 400 聚合诊断; 同用户模板 ID 重复 409)。"""
    from iesplan.core.errors import AppError
    if payload.public_namespace is not None:
        raise AppError(
            "客户端不得提交 public_namespace",
            code="TPL-NS-003",
            message_key="ies.diag.tpl.namespace_forbidden",
            params={"field": "public_namespace"},
            location={"object_type": "model_template", "field": "public_namespace"},
        )
    result = create_template_draft(
        db, user, slug=payload.slug, model_yaml=payload.model_yaml, description=payload.description
    )
    return {"template": result}


@router.get("", summary="当前用户模板列表")
def list_templates_endpoint(
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """当前用户模板列表(全部状态, 最新在前)。"""
    return {"templates": list_my_templates(db, user)}


@router.get("/catalog", summary="可用模板目录(项目模板选择器)")
def list_available_templates_endpoint(
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """当前用户已发布且启用的模板目录(供「新建模型」页面选择)。"""
    return {"items": list_available_templates(db, user)}


@router.get("/{template_id}", summary="模板详情(草稿内容 + 聚合诊断)")
def get_template_endpoint(
    template_id: str,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """模板详情: 草稿规范 YAML + 聚合诊断(已发布模板同时附 revision 视图)。"""
    return get_template_detail(db, user, template_id)


@router.put("/{template_id}", summary="保存模板草稿(乐观锁)")
def save_template_draft_endpoint(
    template_id: str,
    payload: TemplateDraftUpdateRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """保存草稿: 完整校验 → 通过落盘(草稿修订 +1); 失败 400 聚合诊断。"""
    result = save_template_draft(
        db, user, template_id,
        model_yaml=payload.model_yaml,
        expected_revision=payload.expected_revision,
        description=payload.description,
    )
    return {"template": result}


@router.post("/{template_id}/validate", summary="模板完整校验(不落盘)")
def validate_template_endpoint(
    template_id: str,
    payload: TemplateValidateRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """模板完整校验: 返回聚合诊断与 valid 标志, 不落盘、不产生 revision。

    ``model_yaml`` 直接提交候选 YAML(在线编辑即时校验); 或携带模板
    稳定引用对已发布精确 revision 重新校验。
    """
    if payload.model_yaml is not None and payload.model_yaml.strip():
        validation = validate_template_yaml(payload.model_yaml)
    else:
        validation = validate_template_revision(
            db, user, template_id, payload.template_revision or 0,
            payload.template_sha256 or "",
        )
    return {
        "valid": validation.ok,
        "diagnostics": [d.to_dict() for d in validation.diagnostics],
    }


@router.post("/{template_id}/publish", status_code=201, summary="发布不可变 revision")
def publish_template_endpoint(
    template_id: str,
    payload: TemplatePublishRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """发布草稿为不可变 revision(相同内容幂等; 幂等键重放返回同一结果)。"""
    result = publish_template(
        db, user, template_id,
        expected_revision=payload.expected_revision,
        idempotency_key=payload.idempotency_key,
    )
    return result


@router.post("/{template_id}/disable", summary="停用模板(只影响后续选择)")
def disable_template_endpoint(
    template_id: str,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """停用已发布模板(已保存项目模型与历史证据不受影响)。"""
    result = set_template_status(db, user, template_id, enabled=False)
    return {"template": result}


@router.post("/{template_id}/enable", summary="重新启用模板")
def enable_template_endpoint(
    template_id: str,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """重新启用已停用模板(回到可被项目选择状态)。"""
    result = set_template_status(db, user, template_id, enabled=True)
    return {"template": result}


@router.delete("/{template_id}", summary="删除尚未发布的草稿")
def delete_template_endpoint(
    template_id: str,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """删除未发布模板草稿(整行); 已发布模板返回 400/409 标准信封。"""
    return delete_template_draft(db, user, template_id)


@router.get("/{template_id}/revisions/{revision}", summary="精确发布 revision 详情")
def get_template_revision_endpoint(
    template_id: str,
    revision: int,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """精确发布 revision: 规范 YAML + 校验回执 + 结构摘要 + 聚合诊断。"""
    return get_template_revision(db, user, template_id, revision)


@router.get("/{template_id}/draft-revisions", summary="草稿不可变历史")
def list_draft_revisions_endpoint(
    template_id: str,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """列出模板的不可变草稿 revision 历史（旧 revision 可读）。"""
    return {"draft_revisions": list_draft_revisions(db, user, template_id)}


@router.get("/{template_id}/draft-revisions/{revision}", summary="精确草稿 revision")
def get_draft_revision_endpoint(
    template_id: str,
    revision: int,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """读取精确草稿 revision（不可变）。"""
    return get_draft_revision(db, user, template_id, revision)


class MigrateDraftRequest(BaseModel):
    new_slug: str = Field(min_length=1, max_length=64, description="新 slug")


@router.post("/{template_id}/migrate-draft", summary="未发布草稿显式迁移")
def migrate_draft_endpoint(
    template_id: str,
    payload: MigrateDraftRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """未发布草稿显式迁移：生成新稳定 ID、摘要与迁移回执。"""
    return migrate_draft_to_new_stable_id(db, user, template_id, payload.new_slug)


@router.post("/{template_id}/migrate-published", summary="已发布模板离线迁移")
def migrate_published_endpoint(
    template_id: str,
    payload: MigrateDraftRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """已发布模板离线迁移：旧 ID → 新 ID，原子更新全部引用。"""
    return migrate_published_template(db, user, template_id, payload.new_slug)
