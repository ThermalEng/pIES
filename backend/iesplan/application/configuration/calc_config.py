"""计算配置用例(application/configuration, 1.2.0 13 字段单一契约)。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan import configuration as configuration_domain
from iesplan import project as project_domain
from iesplan.application.projects import ensure_access
from iesplan.computation import CalculationConfig, validate_calculation_config
from iesplan.computation.config import build_stage_plugin_map
from iesplan.computation.contracts.compatibility import validate_stage_compatibility
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
    """逐预测条目解析精确引用并调用算法自身的参数规则（保存输入边界唯一位置）。"""
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


def _config_validation_error(
    message: str,
    *,
    message_key: str,
    params: dict[str, Any],
    field: str | None = None,
    project_id: int | None = None,
) -> AppError:
    location = (
        {"object_type": "calculation_config", "object_id": project_id, "field": field}
        if field is not None and project_id is not None
        else None
    )
    err = AppError(
        message,
        code="CONFIG-VAL-001",
        blocking=True,
        message_key=message_key,
        params=params,
        location=location,
    )
    err.http_status = 422
    return err


def _map_unavailable_field(exc: ComputationUnavailableError, *, ref_field: str) -> str:
    msg = str(exc)
    if exc.reason == "incompatible-mode":
        return "mode"
    if exc.reason == "incompatible-precision":
        return "precision"
    if exc.reason == "incompatible-solver":
        return "solver"
    if exc.reason == "incompatible-evaluator":
        return "evaluators"
    if exc.reason == "incompatible-outputs":
        return "outputs"
    if "评价器" in msg or "evaluator" in msg:
        return "evaluators"
    if "求解器" in msg or "solver" in msg:
        return "solver"
    if "预测" in msg or "predictor" in msg:
        return "predictions"
    return ref_field


def _map_value_error_field(detail: str) -> str:
    if "[predictions/" in detail or "prediction" in detail:
        return "predictions"
    if "[evaluators/" in detail:
        return "evaluators"
    return "options"


def save_calculation_config_case(
    db: Session,
    user: UserRecord,
    project_id: int,
    calculation_config: dict[str, Any],
    expected_revision: int | None,
    predictor_registry: Any | None = None,
    generator_registry: Any | None = None,
    solver_registry: Any | None = None,
    evaluator_registry: Any | None = None,
    algorithm_registry: Any | None = None,
) -> dict[str, Any]:
    """保存 13 字段计算配置（用户输入入口唯一校验；成功追加不可变 revision）。"""
    ensure_access(db, user, project_id, "edit")
    project_domain.require_project(db, project_id)
    try:
        validated = validate_calculation_config(calculation_config)
        _check_predictor_parameters(validated, predictor_registry, project_id)
    except (ValueError, TypeError) as exc:
        raise _config_validation_error(
            "计算配置校验失败",
            message_key="ies.error.data_validation_failed",
            params={"detail": str(exc)},
        ) from exc

    is_unified = validated.algorithm is not None
    active_registry = algorithm_registry if is_unified else generator_registry
    selected_ref = validated.algorithm if is_unified else validated.generator
    ref_field = "algorithm" if is_unified else "generator"
    if active_registry is None:
        missing_msg = (
            "一体化算法目录未装配，计算配置不可保存"
            if is_unified
            else "生成器目录未装配，计算配置不可保存"
        )
        raise AppError(missing_msg, params={"project_id": project_id})
    if not is_unified and solver_registry is None:
        raise AppError("求解器目录未装配，计算配置不可保存", params={"project_id": project_id})
    if validated.evaluators and evaluator_registry is None:
        raise AppError("评价器目录未装配，计算配置不可保存", params={"project_id": project_id})

    try:
        active_registry.resolve(validated)
        plugin_specs = build_stage_plugin_map(
            predictor_registry=predictor_registry,
            generator_registry=generator_registry,
            solver_registry=solver_registry,
            evaluator_registry=evaluator_registry,
            algorithm_registry=algorithm_registry,
        )
        validate_stage_compatibility(validated, plugin_specs)
    except ComputationUnavailableError as exc:
        field = _map_unavailable_field(exc, ref_field=ref_field)
        supported: list[str] = []
        if field == ref_field:
            supported = [item.ref for item in active_registry.snapshot()]
        elif field == "solver" and solver_registry is not None:
            supported = [item.ref for item in solver_registry.snapshot()]
        elif field == "evaluators" and evaluator_registry is not None:
            supported = [item.ref for item in evaluator_registry.snapshot()]
        elif field in ("mode", "precision", "outputs") and selected_ref is not None:
            descriptor = active_registry.get(selected_ref).descriptor
            raw_supported = getattr(
                descriptor,
                {"mode": "modes", "precision": "precisions", "outputs": "outputs"}[field],
                None,
            )
            supported = list(raw_supported or ())
        if field == "evaluators":
            display_val: Any = ", ".join(str(e["evaluator"]) for e in validated.evaluators)
        elif field == "outputs":
            display_val = ", ".join(validated.outputs)
        elif field == "predictions":
            display_val = ", ".join(str(p["algorithm"]) for p in validated.predictions)
        else:
            display_val = getattr(validated, field, selected_ref)
        raise _config_validation_error(
            str(exc),
            message_key=f"ies.config.unsupported_{field}",
            params={"value": display_val, "supported": ", ".join(supported)},
            field=field,
            project_id=project_id,
        ) from exc
    except ValueError as exc:
        detail = str(exc)
        field = _map_value_error_field(detail)
        raise _config_validation_error(
            "计算选项校验失败",
            message_key="ies.config.invalid_options",
            params={"detail": detail},
            field=field,
            project_id=project_id,
        ) from exc

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
