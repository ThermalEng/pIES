"""项目模型 API(切片 dm2-A: 候选校验 / 临时数据文件 / 正式保存 / 清单 / 删除)。

路由清单(prefix /api/projects/{project_id}/models):
- POST   /api/projects/{project_id}/models/validate   候选模型门禁(不保存);

- GET    /api/projects/{project_id}/models             项目模型清单(编号可见);
- POST   /api/projects/{project_id}/models             正式保存(校验→编号→规范化→原子保存);
- DELETE /api/projects/{project_id}/models/{model_id}  删除(编号不复用)。

语义(format 标准「进入项目前的候选模型门禁」):
- 候选校验失败: validate 返回 200 + {valid: false, diagnostics}(完整聚合诊断);
  正式保存返回 400 + 标准 8 字段信封(PROJ-MDL-005), 诊断明细入
  params.diagnostics(每条含 code/message_key/location.field/expected/actual),
  不写项目模型目录、不登记清单、不分配编号;
- 请求体结构/语义不可处理(Pydantic 校验失败)返回 422 标准信封(main.py);
- 成功保存 201 + {project_model, receipt}(必要时含 duplicate 幂等重放标志)。

权限: 读/校验要求项目 view, 保存/删除要求项目 edit(403 标准信封)。

2.0: 设备模型参数或接口中的包内相对 CSV 路径为唯一引用，不再接受 data_files。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.api.limits import QuotaError  # 2.0: temp-files 已退役，仅保留 QuotaError 占位
from iesplan.application.projects import (
    delete_project_model,
    get_project_models,
    save_project_model,
    validate_candidate,
)
from iesplan.core.errors import http_error
from iesplan.db import get_db

router = APIRouter(prefix="/api/projects/{project_id}/models", tags=["project-models"])

DbSession = Annotated[Session, Depends(get_db)]


class ModelCandidateRequest(BaseModel):
    """候选模型请求体(校验与保存共用)。

    来源判别:
    - ``source=direct_yaml``: ``model_yaml`` 为完整候选模型 YAML(必填);
    - ``source=template``: ``template_id`` + ``template_revision``
      固定精确模板 revision(后端读取权威内容实例化,
      不信任客户端自带的模板字节); ``template_inputs`` 为用户 inputs。
      ``model_yaml`` 可省略(校验端点允许内联模板 YAML 形态)。文本文件只校验字头。
    """

    model_yaml: str = Field(default="", max_length=2_000_000, description="候选模型 YAML 文本(直接来源必填)或模板来源的权威模板字节(可省略)")
    source: Literal["direct_yaml", "template"] = "direct_yaml"
    template_id: str | None = Field(
        default=None, description="模板来源: 稳定模板 ID(不透明字符串)",
    )
    template_revision: int | None = Field(
        default=None, ge=1, description="模板来源: 精确发布 revision(固定不可变)",
    )
    template_inputs: dict[str, Any] | None = Field(
        default=None, description="模板来源时用户提交的 inputs(未声明字段拒绝)"
    )

    @model_validator(mode="after")
    def _source_requires_fields(self) -> ModelCandidateRequest:
        if self.source == "direct_yaml" and not self.model_yaml.strip():
            raise ValueError("source=direct_yaml 必须提供 model_yaml")
        if self.source == "template":
            if not self.template_id or not self.template_revision:
                raise ValueError(
                    "source=template 必须携带 template_id 与 template_revision"
                )
            if self.template_inputs is None:
                raise ValueError("source=template 必须携带 template_inputs")
        return self


class ModelSaveRequest(ModelCandidateRequest):
    """正式保存请求体(可重试写操作携带幂等键; 重放返回同一逻辑结果)。"""

    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9._:-]{1,128}$", description="项目内幂等键",
    )
    expected_revision: int = Field(ge=1, description="预期项目草稿 revision(乐观锁)")


class ModelDeleteRequest(BaseModel):
    """删除项目模型同样推进项目草稿 revision。"""

    expected_revision: int = Field(ge=1)



# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post("/validate", summary="候选模型门禁(不保存)")
def validate_project_model_candidate(
    project_id: int,
    payload: ModelCandidateRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """候选模型完整校验: 返回聚合诊断与 valid 标志, 不写对象/清单/编号。

    诊断含已登记诊断码、message_key、字段路径(location.field)与
    expected/actual(params); 合法候选返回空诊断列表。
    """
    validation = validate_candidate(
        db, user, project_id,
        model_yaml=payload.model_yaml,
        source=payload.source,
        template_id=payload.template_id,
        template_revision=payload.template_revision,
        template_inputs=payload.template_inputs,
    )
    return {
        "valid": validation.ok,
        "diagnostics": [d.to_dict() for d in validation.diagnostics],
    }


@router.get("", summary="项目模型清单")
def list_project_models_endpoint(
    project_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """项目模型清单(最新在前; 编号对用户可见)。"""
    return {"project_models": get_project_models(db, user, project_id)}


@router.post("", status_code=201, summary="正式保存项目模型")
def save_project_model_endpoint(
    project_id: int,
    payload: ModelSaveRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """正式保存: 完整校验 → 分配 _N 编号 → 规范化/摘要/回执 → 原子保存。

    校验失败 400 + PROJ-MDL-005 信封(聚合诊断); 幂等键重放返回同一逻辑结果
    (duplicate: true), 不重复占号。事务由本端点统一提交(application 拥有
    commit/rollback 边界, 宪法 §5.4)。
    """
    result = save_project_model(
        db, user, project_id,
        model_yaml=payload.model_yaml,
        source=payload.source,
        template_id=payload.template_id,
        template_revision=payload.template_revision,
        template_inputs=payload.template_inputs,
        idempotency_key=payload.idempotency_key,
        expected_revision=payload.expected_revision,
    )
    return result


@router.delete("/{model_id}", summary="删除项目模型")
def delete_project_model_endpoint(
    project_id: int,
    model_id: int,
    payload: ModelDeleteRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """删除项目模型(硬删除清单行 + 解绑对象引用; 编号不复用)。"""
    revision = delete_project_model(
        db, user, project_id, model_id, expected_revision=payload.expected_revision
    )
    return {"ok": True, "deleted": str(model_id), "project_revision": revision}
