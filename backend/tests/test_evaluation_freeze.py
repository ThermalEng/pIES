"""0.12-F1 定向测试: 评价确认提交闸门 + 快照 3.0.0 冻结 + 全期覆盖核对。

- 提交边界类型化解析(``parse_evaluation_input``): 顶层 ``evaluation`` 块
  必填, 缺失/形态非法 → 400 TASK-REQ-015(无默认值); 年限接受 ``>= 1``
  连续全期(与全期连续时域目标一致, 不再以 TASK-REQ-016 阻断多年);
  折现率须为 ``0..1`` 区间内有限数(宪法 §7.3 比例口径)。
- 快照冻结: 顶层 ``evaluation`` 块含年限/折现率/确认人/确认时间, 不进
  ``calc_config`` 正文与 ``task_params``; 复用身份包含年限/折现率/确认人
  (确认时间只存档), 折现率或确认人不同 → 新快照。
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
    EvaluationInput,
    InvalidRequestError,
    check_tariff_horizon_coverage,
    parse_evaluation_input,
)


# ---------------------------------------------------------------------------
# 提交边界类型化解析
# ---------------------------------------------------------------------------


def test_evaluation_missing_or_malformed_blocked_015() -> None:
    for raw in (
        None,
        {},
        {"evaluation_years": 1},
        {"discount_rate": 0.08},
        {"evaluation_years": 0, "discount_rate": 0.08},
        {"evaluation_years": -1, "discount_rate": 0.08},
        {"evaluation_years": True, "discount_rate": 0.08},
        {"evaluation_years": "1", "discount_rate": 0.08},
        {"evaluation_years": 1, "discount_rate": -0.01},
        {"evaluation_years": 1, "discount_rate": 1.01},
        {"evaluation_years": 1, "discount_rate": 2},
        {"evaluation_years": 1, "discount_rate": float("inf")},
        {"evaluation_years": 1, "discount_rate": float("nan")},
        {"evaluation_years": 1, "discount_rate": True},
        {"evaluation_years": 1, "discount_rate": "0.08"},
        {"evaluation_years": 1, "discount_rate": 0.08, "extra": 1},
    ):
        with pytest.raises(InvalidRequestError) as exc_info:
            parse_evaluation_input(raw)
        assert exc_info.value.code == "TASK-REQ-015", raw


def test_evaluation_multi_year_accepted_as_continuous_horizon() -> None:
    """多年不再阻断: 年限 >= 1 均为合法连续全期输入(全周期求解由后续切片贯通)。"""
    for years in (1, 2, 3, 25):
        parsed = parse_evaluation_input(
            {"evaluation_years": years, "discount_rate": 0.08}
        )
        assert parsed == EvaluationInput(evaluation_years=years, discount_rate=0.08)


def test_evaluation_single_year_parses() -> None:
    assert parse_evaluation_input(
        {"evaluation_years": 1, "discount_rate": 0.08}
    ) == EvaluationInput(evaluation_years=1, discount_rate=0.08)
    assert parse_evaluation_input(
        {"evaluation_years": 1, "discount_rate": 0}
    ) == EvaluationInput(evaluation_years=1, discount_rate=0.0)
    assert parse_evaluation_input(
        {"evaluation_years": 1, "discount_rate": 1}
    ) == EvaluationInput(evaluation_years=1, discount_rate=1.0)


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


def test_horizon_coverage_multi_year_scales_with_calendar() -> None:
    """多年期望点数逐年按公历推导(2024 闰年 8784 + 2025 平年 8760)。"""
    check_tariff_horizon_coverage(
        baseline_resolution="1h",
        baseline_start_year=2024,
        evaluation_years=2,
        tariff_bindings={"m1": {"price": "tou", "instance": "d", "interface": "p"}},
        price_series={"tou": {"kind": "time_series", "point_count": 8784 + 8760}},
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


def _post(client, pid, user, config, key, evaluation="default"):  # noqa: F811
    from test_task_config_revision import _h  # noqa: E402

    body: dict = {"task_type": "calc", "config": config, "idempotency_key": key}
    if evaluation == "default":
        evaluation = {"evaluation_years": 1, "discount_rate": 0.08}
    if evaluation is not None:
        body["evaluation"] = evaluation
    return client.post(
        f"/api/projects/{pid}/tasks",
        json=body,
        headers=_h(client, user),
    )


def test_submit_without_evaluation_400_015(client, db) -> None:  # noqa: F811
    from auth_helpers import make_user
    from test_task_config_revision import _prepare_project, _save_config

    owner = make_user(db, "owner_eval_015")
    pid = _prepare_project(client, db, owner, name="评价门禁015项目")
    rev = _save_config(client, pid, owner)
    resp = _post(client, pid, owner, {"config_revision": rev}, "eval-015", evaluation=None)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "TASK-REQ-015"


def test_submit_evaluation_in_config_ignored_015(client, db) -> None:  # noqa: F811
    """评价确认只走顶层任务输入: 塞进 config 的旧形态不被读取, 仍 400。"""
    from auth_helpers import make_user
    from test_task_config_revision import _prepare_project, _save_config

    owner = make_user(db, "owner_eval_cfg")
    pid = _prepare_project(client, db, owner, name="评价顶层项目")
    rev = _save_config(client, pid, owner)
    resp = _post(
        client, pid, owner,
        {"config_revision": rev,
         "evaluation": {"evaluation_years": 1, "discount_rate": 0.08}},
        "eval-cfg-1", evaluation=None,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "TASK-REQ-015"


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
            {"config_revision": rev},
            key,
            evaluation={"evaluation_years": 1, "discount_rate": rate},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["task"]

    first = _submit_eval(0.08, "eval-snap-1")
    snap = tasks_domain.get_snapshot(factory(), first["calc_snapshot_id"])
    assert snap is not None
    assert snap.schema_version == "3.0.0"
    frozen = jsonable(dict(snap.calc_config_snapshot))
    assert "evaluation" not in frozen
    assert "evaluation" not in frozen.get("task_params", {})
    evaluation = jsonable(dict(snap.evaluation))
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

    # 多年提交直达快照冻结(年限/折现率/确认人/确认时间存于快照顶层,
    # 不进配置正文; 全周期求解由后续 F 切片贯通, 本切片只验提交与冻结)
    resp = _post(
        client, pid, owner,
        {"config_revision": rev},
        "eval-snap-multi",
        evaluation={"evaluation_years": 2, "discount_rate": 0.08},
    )
    assert resp.status_code == 201, resp.text
    multi = resp.json()["task"]
    multi_snap = tasks_domain.get_snapshot(factory(), multi["calc_snapshot_id"])
    assert multi_snap is not None
    multi_config = jsonable(dict(multi_snap.calc_config_snapshot))
    assert "evaluation" not in multi_config
    multi_eval = jsonable(dict(multi_snap.evaluation))
    assert multi_eval["evaluation_years"] == 2
    assert multi_eval["discount_rate"] == 0.08
    assert multi_eval["confirmed_by"] == owner.id
    assert multi_eval["confirmed_at"]


def test_snapshot_reuse_identity_includes_confirmer() -> None:
    """复用身份含确认人(防错归)、不含确认时间(否则复用永不命中)。"""
    from iesplan.application.tasks.submissions import _evaluation_identity

    base = {
        "evaluation_years": 1, "discount_rate": 0.08,
        "confirmed_by": 7, "confirmed_at": "2026-10-09T00:00:00+00:00",
    }
    same_user = dict(base, confirmed_at="2026-10-10T00:00:00+00:00")
    other_user = dict(base, confirmed_by=9)
    assert _evaluation_identity(same_user) == _evaluation_identity(base)
    assert _evaluation_identity(other_user) != _evaluation_identity(base)
