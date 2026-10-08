"""0.12-F1 定向测试: 评价确认提交闸门 + 快照 3.0.0 冻结 + 全期覆盖核对。

- 提交边界类型化解析(``parse_evaluation_input``): 缺失/形态非法 → 400
  TASK-REQ-015(无默认值); 多年 → 400 TASK-REQ-016(不静默按单年运行)。
- 快照冻结: ``calc_config_snapshot.evaluation`` 含年限/折现率/确认人/
  确认时间, 不进 ``task_params``; 复用身份包含年限/折现率(折现率不同 → 新快照)。
- 全期覆盖(``check_tariff_horizon_coverage``): 期望点数由起始年 + 已确认
  年限 + 分辨率推导, 不一致 → 400 TASK-REQ-017。
"""

from __future__ import annotations

import pytest
from test_task_config_revision import (  # noqa: E402,F401,F811
    client,
    db,
    engine,
    factory,
)

from iesplan import tasks as tasks_domain
from iesplan.tasks.contracts import (
    InvalidRequestError,
    check_tariff_horizon_coverage,
    parse_evaluation_input,
)


# ---------------------------------------------------------------------------
# 提交边界类型化解析
# ---------------------------------------------------------------------------


def test_evaluation_missing_or_malformed_blocked_015() -> None:
    for config in (
        None,
        {},
        {"config_revision": 1},
        {"evaluation": None},
        {"evaluation": []},
        {"evaluation": {"evaluation_years": 1}},
        {"evaluation": {"discount_rate": 0.08}},
        {"evaluation": {"evaluation_years": 0, "discount_rate": 0.08}},
        {"evaluation": {"evaluation_years": -1, "discount_rate": 0.08}},
        {"evaluation": {"evaluation_years": True, "discount_rate": 0.08}},
        {"evaluation": {"evaluation_years": "1", "discount_rate": 0.08}},
        {"evaluation": {"evaluation_years": 1, "discount_rate": -0.01}},
        {"evaluation": {"evaluation_years": 1, "discount_rate": float("inf")}},
        {"evaluation": {"evaluation_years": 1, "discount_rate": float("nan")}},
        {"evaluation": {"evaluation_years": 1, "discount_rate": True}},
        {"evaluation": {"evaluation_years": 1, "discount_rate": 0.08, "extra": 1}},
    ):
        with pytest.raises(InvalidRequestError) as exc_info:
            parse_evaluation_input(config)
        assert exc_info.value.code == "TASK-REQ-015", config


def test_evaluation_multi_year_explicitly_blocked_016() -> None:
    for years in (2, 3, 25):
        with pytest.raises(InvalidRequestError) as exc_info:
            parse_evaluation_input(
                {"evaluation": {"evaluation_years": years, "discount_rate": 0.08}}
            )
        assert exc_info.value.code == "TASK-REQ-016", years


def test_evaluation_single_year_parses() -> None:
    assert parse_evaluation_input(
        {"evaluation": {"evaluation_years": 1, "discount_rate": 0.08}}
    ) == {"evaluation_years": 1, "discount_rate": 0.08}
    assert parse_evaluation_input(
        {"evaluation": {"evaluation_years": 1, "discount_rate": 0}}
    ) == {"evaluation_years": 1, "discount_rate": 0.0}


# ---------------------------------------------------------------------------
# 全期覆盖核对
# ---------------------------------------------------------------------------


def test_horizon_coverage_passes_for_matching_series() -> None:
    check_tariff_horizon_coverage(
        baseline_resolution="1h",
        baseline_start_year=2024,
        evaluation_years=1,
        tariff_bindings={"m1": {"price": "tou", "instance": "d", "interface": "p"}},
        price_series={"tou": {"kind": "time_series", "point_count": 8784}},
    )


def test_horizon_coverage_blocks_mismatch_017() -> None:
    with pytest.raises(InvalidRequestError) as exc_info:
        check_tariff_horizon_coverage(
            baseline_resolution="1h",
            baseline_start_year=2024,
            evaluation_years=1,
            tariff_bindings={"m1": {"price": "tou", "instance": "d", "interface": "p"}},
            price_series={"tou": {"kind": "time_series", "point_count": 8760}},
        )
    assert exc_info.value.code == "TASK-REQ-017"
    assert exc_info.value.params["expected_point_count"] == 8784


def test_horizon_coverage_ignores_constant_and_blocks_unknown_price() -> None:
    check_tariff_horizon_coverage(
        baseline_resolution="1h",
        baseline_start_year=2026,
        evaluation_years=1,
        tariff_bindings={"m1": {"price": "flat", "instance": "d", "interface": "p"}},
        price_series={"flat": {"kind": "constant"}},
    )
    with pytest.raises(InvalidRequestError) as exc_info:
        check_tariff_horizon_coverage(
            baseline_resolution="1h",
            baseline_start_year=2026,
            evaluation_years=1,
            tariff_bindings={"m1": {"price": "ghost", "instance": "d", "interface": "p"}},
            price_series={},
        )
    assert exc_info.value.code == "TASK-REQ-017"


# ---------------------------------------------------------------------------
# API: 闸门 + 快照冻结 + 复用身份
# ---------------------------------------------------------------------------


def _post(client, pid, user, config, key):  # noqa: F811
    from test_task_config_revision import _h  # noqa: E402

    return client.post(
        f"/api/projects/{pid}/tasks",
        json={"task_type": "calc", "config": config, "idempotency_key": key},
        headers=_h(client, user),
    )


def test_submit_without_evaluation_400_015(client, db) -> None:  # noqa: F811
    from auth_helpers import make_user
    from test_task_config_revision import _prepare_project, _save_config

    owner = make_user(db, "owner_eval_015")
    pid = _prepare_project(client, db, owner, name="评价门禁015项目")
    rev = _save_config(client, pid, owner)
    resp = _post(client, pid, owner, {"config_revision": rev}, "eval-015")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "TASK-REQ-015"


def test_submit_multi_year_400_016(client, db) -> None:  # noqa: F811
    from auth_helpers import make_user
    from test_task_config_revision import _prepare_project, _save_config

    owner = make_user(db, "owner_eval_016")
    pid = _prepare_project(client, db, owner, name="评价门禁016项目")
    rev = _save_config(client, pid, owner)
    resp = _post(
        client, pid, owner,
        {"config_revision": rev,
         "evaluation": {"evaluation_years": 2, "discount_rate": 0.08}},
        "eval-016",
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "TASK-REQ-016"


def test_snapshot_freezes_evaluation_and_identity(  # noqa: F811
    client, db, factory
) -> None:
    """快照 3.0.0: evaluation 块冻结确认人/时间; 折现率不同 → 新快照。"""
    from auth_helpers import make_user
    from test_task_config_revision import _prepare_project, _save_config

    from iesplan.core.jsonutil import jsonable

    owner = make_user(db, "owner_eval_snap")
    pid = _prepare_project(client, db, owner, name="评价快照项目")
    rev = _save_config(client, pid, owner)

    def _submit_eval(rate, key):
        resp = _post(
            client, pid, owner,
            {"config_revision": rev,
             "evaluation": {"evaluation_years": 1, "discount_rate": rate}},
            key,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["task"]

    first = _submit_eval(0.08, "eval-snap-1")
    snap = tasks_domain.get_snapshot(factory(), first["calc_snapshot_id"])
    assert snap is not None
    assert snap.schema_version == "3.0.0"
    frozen = jsonable(dict(snap.calc_config_snapshot))
    assert "evaluation" not in frozen.get("task_params", {})
    evaluation = frozen["evaluation"]
    assert evaluation["evaluation_years"] == 1
    assert evaluation["discount_rate"] == 0.08
    assert evaluation["confirmed_by"] == owner.id
    assert evaluation["confirmed_at"]

    # 同输入复用快照
    replay = _submit_eval(0.08, "eval-snap-2")
    assert replay["calc_snapshot_id"] == first["calc_snapshot_id"]
    # 折现率不同 → 复用身份不同 → 新快照
    other = _submit_eval(0.05, "eval-snap-3")
    assert other["calc_snapshot_id"] != first["calc_snapshot_id"]
