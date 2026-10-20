"""计算配置用例(application/configuration)。

``api/config.py`` 经本模块读写 CalculationConfig revision；依赖方向
``api → application.configuration.calc_config → {configuration, project}
域公开门面 + computation 公开入口``；不导入 ORM、私有模块，不调用
``services.*``。

- 保存：鉴权(edit) → computation 公开入口唯一校验 → 追加不可变 revision
  → 本层拥有事务提交/回滚；``expected_revision`` 为自有链乐观锁
  （None 表首次保存；失配 409）；
- 读取：鉴权(view) → 最新 revision；未保存过 404；
- 读路径与持久化层不重复校验（校验只发生在用户输入入口）。

本模块不新增校验/哈希/回退。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan import configuration as configuration_domain
from iesplan import project as project_domain
from iesplan.application.projects import ensure_access
from iesplan.computation import CalculationConfig, validate_calculation_config
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.identity.contracts import UserRecord

__all__ = [
    "get_calculation_config_case",
    "save_calculation_config_case",
]


def _check_predictor_parameters(
    validated: CalculationConfig,
    predictor_registry: Any | None,
    project_id: int,
) -> None:
    """逐预测条目解析精确引用并调用算法自身的参数规则（保存输入边界唯一位置）。

    无预测条目时直接通过，不要求无关能力。目录缺席或算法未知时保存拒绝：
    未知算法不能冒充已校验配置落库（提交边界仍负责绑定与实际可用性语义，
    此处不替代）。算法规则失败（``ValueError`` 指明字段）即保存拒绝，
    统一走 CONFIG-VAL-001 信封。
    """
    entries = tuple(validated.predictions or ())
    if not entries:
        return
    if predictor_registry is None:
        raise AppError(
            "预测算法目录未装配，含预测目标的计算配置不可保存",
            params={"project_id": project_id},
            location={
                "object_type": "calculation_config",
                "object_id": project_id,
                "field": "predictions",
            },
        )
    for entry in entries:
        target = entry["target"]
        try:
            predictor = predictor_registry.get(str(entry["algorithm"]))
        except ComputationUnavailableError as exc:
            raise ValueError(
                "预测算法未注册，参数规则无法确认 "
                f"(algorithm={entry['algorithm']!r}, "
                f"target={target['instance']!r}x{target['interface']!r})"
            ) from exc
        try:
            predictor.validate_parameters(dict(entry.get("parameters") or {}))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "预测算法参数不满足该算法输入规则 "
                f"(algorithm={entry['algorithm']!r}): {exc}"
            ) from exc


def save_calculation_config_case(
    db: Session,
    user: UserRecord,
    project_id: int,
    calculation_config: dict[str, Any],
    expected_revision: int | None,
    predictor_registry: Any | None = None,
) -> dict[str, Any]:
    """保存计算配置（用户输入入口唯一校验；成功追加不可变 revision）。

    校验不通过 → 422 CONFIG-VAL-001 标准信封，不落库；revision 失配 → 409。
    预测条目的算法精确引用在此解析并调用其参数规则；未知算法不能冒充
    已校验配置落库（含预测目标而目录缺席同样不可用）。无预测条目时不
    要求无关能力。
    """
    ensure_access(db, user, project_id, "edit")
    project_domain.require_project(db, project_id)
    try:
        validated = validate_calculation_config(calculation_config)
        _check_predictor_parameters(validated, predictor_registry, project_id)
    except (ValueError, TypeError) as exc:
        err = AppError(
            "计算配置校验失败",
            code="CONFIG-VAL-001",
            blocking=True,
            message_key="ies.error.data_validation_failed",
            params={"detail": str(exc)},
        )
        err.http_status = 422
        raise err from exc
    latest = configuration_domain.get_latest_calculation_config(db, project_id)
    current = latest.version if latest is not None else None
    if current != expected_revision:
        raise ConflictError(
            "计算配置已在其他会话中修改, 请基于最新 revision 重试",
            params={"expected_revision": expected_revision, "current": current},
        )
    try:
        row = configuration_domain.create_calculation_config(
            db,
            project_id=project_id,
            content=validated.to_dict(),
            created_by=user.id,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"calculation_config": validated.to_dict(), "revision": row.version}


def get_calculation_config_case(
    db: Session, user: UserRecord, project_id: int
) -> dict[str, Any]:
    """读取项目最新计算配置 revision；未保存过 → 404。读路径不重复校验。"""
    ensure_access(db, user, project_id, "view")
    project_domain.require_project(db, project_id)
    latest = configuration_domain.get_latest_calculation_config(db, project_id)
    if latest is None:
        raise NotFoundError(
            "项目尚未保存计算配置",
            params={"project_id": project_id},
            location={"object_type": "calculation_config", "object_id": project_id},
        )
    return {"calculation_config": latest.content, "revision": latest.version}
