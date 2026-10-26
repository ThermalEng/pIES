"""计算配置 API(U06, 0.12-A1 起收敛为 CalculationConfig revision)。

- GET    /api/projects/{id}/config          最新计算配置 revision(未保存 404)
- PUT    /api/projects/{id}/config          保存 {calculation_config, expected_revision};
  输入入口唯一校验，不通过返回 422 + 标准错误信封；revision 失配 409

注意: 本模块导出 config_router, 由集成阶段在 main.py 通过 include_router
挂载(请求会话依赖见 iesplan/api/deps)。

认证与权限: 读要求项目 view, 保存要求项目 edit(403); 未认证 401。

传输适配说明: 每个业务动作只转交 application.configuration.calc_config
中的一个完整用例(授权、业务步骤与事务均在用例内); 本模块只做 DTO、
一次调用和错误/响应映射。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.api.deps import get_request_db
from iesplan.application import configuration as config_cases

#: FastAPI 依赖注入的数据库会话
DbSession = Annotated[Session, Depends(get_request_db)]

#: 配置域路由: 挂载前缀 /api/projects/{project_id}/config
config_router = APIRouter(prefix="/api/projects/{project_id}/config", tags=["config"])


class CalculationConfigSaveRequest(BaseModel):
    """保存请求: 12 字段 CalculationConfig 全文 + 自有链期望 revision。"""

    calculation_config: dict[str, Any] = Field(
        description="计算配置(12 字段 CalculationConfig 契约)"
    )
    expected_revision: int | None = Field(
        default=None, ge=1, description="期望的当前 revision; 首次保存为 null"
    )


@config_router.get("", summary="读取最新计算配置 revision")
def get_config_endpoint(project_id: int, db: DbSession, user: CurrentUser) -> dict:
    """最新 CalculationConfig revision + revision 号; 未保存过 404。"""
    return config_cases.get_calculation_config_case(db, user, project_id)


@config_router.put("", summary="保存计算配置(新 revision)")
def save_config_endpoint(
    project_id: int,
    body: CalculationConfigSaveRequest,
    db: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict:
    """保存 CalculationConfig: 输入校验 422, revision 失配 409。

    预测算法目录取组合根已发布实例(经 app.state 透传, 不自行发现/组装);
    含预测目标而目录缺席时由用例可见报告不可用, 不猜测、不绕过。
    """
    bootstrap = getattr(request.app.state, "bootstrap_context", None)
    predictor_registry = (
        getattr(bootstrap, "predictor_registry", None) if bootstrap is not None else None
    )
    generator_registry = (
        getattr(bootstrap, "generator_registry", None) if bootstrap is not None else None
    )
    return config_cases.save_calculation_config_case(
        db, user, project_id, body.calculation_config, body.expected_revision,
        predictor_registry=predictor_registry,
        generator_registry=generator_registry,
    )
