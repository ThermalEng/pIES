"""计算配置端点用例(application/configuration/config_cases)。

``api/config.py`` 每个 HTTP 业务动作只转交本模块的一个完整用例。
用例接收已认证主体、业务参数和事务会话，在内部完成授权、
业务步骤与事务，返回与 HTTP 无关的应用结果；API 只做 DTO、
一次调用和错误/响应映射。

职责（与重构前路由内联顺序一致）：
- 读取/校验/默认：鉴权(view)→加载工作图→校验或默认生成→元数据拼装；
- 保存：鉴权(edit)→加载工作图→校验→诊断判断→保存→元数据拼装；
- 注册表：无认证无事务的公开读取。

事务：读用例不提交；保存经 ``calc_config.save_config`` 拥有提交/回滚。
本模块不新增校验/哈希/防御分支。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from iesplan.application.configuration import calc_config
from iesplan.application.projects import ensure_access
from iesplan.identity.contracts import UserRecord

__all__ = [
    "SaveConfigResult",
    "default_config_case",
    "get_config_case",
    "list_algorithms_case",
    "save_config_case",
    "validate_config_case",
]


@dataclass(frozen=True)
class SaveConfigResult:
    """保存用例结果（与 HTTP 无关）：saved 为 False 即校验未通过。"""

    saved: bool
    config: dict[str, Any] | None
    meta: dict[str, Any] | None
    version: int | None
    status: str | None
    diagnostics: list[dict[str, Any]]
    count: int


def _serialize_diagnostics(diags: list) -> list[dict[str, Any]]:
    """诊断对象列表序列化为 dict 列表(04 §5.4 JSON 结构)。"""
    return [d.to_dict() for d in diags]


def _has_errors(diags: list) -> bool:
    """是否存在阻断保存的诊断(error/blocking 任一即阻断)。"""
    return any(d.severity in ("error", "blocking") or d.blocking for d in diags)


def get_config_case(db: Session, user: UserRecord, project_id: int) -> dict[str, Any]:
    """读取当前计算配置（未保存过返回生成的默认配置，version=None）。"""
    ensure_access(db, user, project_id, "view")
    return calc_config.get_config(db, project_id)


def save_config_case(
    db: Session,
    user: UserRecord,
    project_id: int,
    config: dict[str, Any],
    expected_revision: int,
) -> SaveConfigResult:
    """保存计算配置（与草稿修订绑定）；校验不通过时 saved=False，不落库。"""
    ensure_access(db, user, project_id, "edit")
    graph = calc_config.load_work_graph(db, project_id)
    diags = calc_config.validate_config(config, graph)
    serialized = _serialize_diagnostics(diags)
    if _has_errors(diags):
        return SaveConfigResult(
            saved=False,
            config=None,
            meta=None,
            version=None,
            status=None,
            diagnostics=serialized,
            count=len(serialized),
        )
    row = calc_config.save_config(
        db, project_id, config, expected_revision, user_id=user.id
    )
    return SaveConfigResult(
        saved=True,
        config=calc_config.row_to_config(row),
        meta=calc_config.parameter_metadata(graph),
        version=row.version,
        status=row.status,
        diagnostics=[],
        count=0,
    )


def validate_config_case(
    db: Session, user: UserRecord, project_id: int, config: dict[str, Any]
) -> dict[str, Any]:
    """只校验不保存；始终返回 diagnostics（前端实时校验用）。"""
    ensure_access(db, user, project_id, "view")
    graph = calc_config.load_work_graph(db, project_id)
    diags = calc_config.validate_config(config, graph)
    serialized = _serialize_diagnostics(diags)
    return {"diagnostics": serialized, "count": len(serialized)}


def default_config_case(
    db: Session, user: UserRecord, project_id: int
) -> dict[str, Any]:
    """基于系统模型设备清单重新生成默认配置（不保存）。"""
    ensure_access(db, user, project_id, "view")
    graph = calc_config.load_work_graph(db, project_id)
    return {
        "config": calc_config.get_default_config(db, project_id),
        "meta": calc_config.parameter_metadata(graph),
    }


def list_algorithms_case() -> dict[str, Any]:
    """算法列表 + 能力清单 + 参数规格（公开，无认证）。"""
    return {"algorithms": calc_config.list_algorithms_meta()}
