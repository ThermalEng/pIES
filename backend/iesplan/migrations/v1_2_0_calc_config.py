"""1.1.5 -> 1.2.0 CalculationConfig 意图保留迁移与在途任务排空门禁。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from iesplan.computation import validate_calculation_config

MIGRATION_VERSION = "20261126_120_calc_config"
NON_TERMINAL_TASK_STATUSES: tuple[str, ...] = ("queued", "running", "cancelling")


class UpgradeDrainError(RuntimeError):
    """升级前仍有未终结任务（queued/running/cancelling）时显式阻断迁移。"""

    def __init__(self, inflight: list[dict[str, Any]]) -> None:
        self.inflight = inflight
        summary = ", ".join(f"#{t['id']}({t['type']}:{t['status']})" for t in inflight[:10])
        suffix = f" 等共 {len(inflight)} 项" if len(inflight) > 10 else f" (共 {len(inflight)} 项)"
        super().__init__(
            f"升级 1.2.0 前须先排空全部未终结任务（queued/running/cancelling）: {summary}{suffix}"
        )


def list_inflight_tasks(conn: Connection) -> list[dict[str, Any]]:
    """查询全部未终结任务（queued / running / cancelling）。"""
    rows = conn.execute(
        text(
            "SELECT id, project_id, type, status FROM tasks "
            "WHERE status IN ('queued', 'running', 'cancelling') ORDER BY id"
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def assert_no_inflight_tasks(conn: Connection) -> None:
    """排空门禁：存在任何未终结任务即抛出 ``UpgradeDrainError``。"""
    inflight = list_inflight_tasks(conn)
    if inflight:
        raise UpgradeDrainError(inflight)


def convert_calc_config_115_to_120(content: Mapping[str, Any]) -> dict[str, Any]:
    """按原业务意图将 1.1.5 14 字段 CalculationConfig 转换为 1.2.0 13 字段格式。

    - 若已为 1.2.0 13 字段格式则校验后原样返回（幂等）；
    - 移除 ``convergence`` 与 ``evaluator``；
    - 显式指定 ``ies.result-adapter.highs@1.0.0`` 或 1.1.5 分步模式默认绑定且请求
      ``solution`` 输出的配置映射为 ``evaluators: [{"evaluator": "ies.evaluator.finance@1.0.0",
      "parameters": {}}]``；其余非空 ``evaluator`` 原样转入 ``evaluators``；其余
      ``evaluator: null`` 保留无评价意图映射为 ``evaluators: []``；
    - 原扁平 ``options`` 按模式分别归入 ``options.generator`` 或 ``options.algorithm``。
    """
    if "evaluators" in content and "evaluator" not in content and "convergence" not in content:
        return validate_calculation_config(content).to_dict()

    algorithm = content.get("algorithm")
    evaluator = content.get("evaluator")
    outputs = list(content.get("outputs") or ())
    raw_options = dict(content.get("options") or {})

    if evaluator == "ies.result-adapter.highs@1.0.0" or (
        evaluator is None and algorithm is None and "solution" in outputs
    ):
        evaluators: list[dict[str, Any]] = [
            {"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}}
        ]
    elif isinstance(evaluator, str) and evaluator:
        evaluators = [{"evaluator": evaluator, "parameters": {}}]
    else:
        evaluators = []

    if algorithm is not None:
        slotted_options: dict[str, Any] = {"algorithm": raw_options} if raw_options else {}
    else:
        slotted_options = {"generator": raw_options} if raw_options else {}

    converted = {
        "schema": content["schema"],
        "schema_version": content["schema_version"],
        "mode": content["mode"],
        "predictions": list(content.get("predictions") or ()),
        "generator": content.get("generator"),
        "solver": content.get("solver"),
        "algorithm": algorithm,
        "evaluators": evaluators,
        "precision": content["precision"],
        "time_limit_seconds": content["time_limit_seconds"],
        "options": slotted_options,
        "seed": content.get("seed"),
        "outputs": outputs,
    }
    return validate_calculation_config(converted).to_dict()


def upgrade_calc_configs(conn: Connection) -> int:
    """一次性迁移可编辑 ``calc_configs`` 修订；严禁触碰 ``calc_snapshots`` 与 ``evidence_packages``。"""
    assert_no_inflight_tasks(conn)

    rows = conn.execute(text("SELECT id, content FROM calc_configs ORDER BY id")).fetchall()
    updates: list[tuple[int, dict[str, Any]]] = []
    for row_id, raw_content in rows:
        parsed = json.loads(raw_content) if isinstance(raw_content, str) else dict(raw_content)
        converted = convert_calc_config_115_to_120(parsed)
        if parsed != converted:
            updates.append((int(row_id), converted))

    if not updates:
        return 0

    is_pg = conn.dialect.name == "postgresql"
    if is_pg:
        conn.execute(text("ALTER TABLE calc_configs DISABLE TRIGGER tg_calc_configs_no_update"))
    try:
        for row_id, converted in updates:
            serialized = json.dumps(converted, ensure_ascii=False, separators=(",", ":"))
            if is_pg:
                conn.execute(
                    text("UPDATE calc_configs SET content = CAST(:content AS jsonb) WHERE id = :id"),
                    {"id": row_id, "content": serialized},
                )
            else:
                conn.execute(
                    text("UPDATE calc_configs SET content = :content WHERE id = :id"),
                    {"id": row_id, "content": serialized},
                )
    finally:
        if is_pg:
            conn.execute(text("ALTER TABLE calc_configs ENABLE TRIGGER tg_calc_configs_no_update"))
    return len(updates)


__all__ = [
    "MIGRATION_VERSION",
    "NON_TERMINAL_TASK_STATUSES",
    "UpgradeDrainError",
    "assert_no_inflight_tasks",
    "convert_calc_config_115_to_120",
    "list_inflight_tasks",
    "upgrade_calc_configs",
]
