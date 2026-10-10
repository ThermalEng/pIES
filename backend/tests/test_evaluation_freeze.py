"""0.12-T1 定向测试: 评价确认提交闸门 + 快照 4.0.0 冻结 + 运营期覆盖核对。

- 提交边界类型化解析(``parse_evaluation_input``): 顶层 ``evaluation`` 块
  必填六字段, 缺失/形态非法 → 400 TASK-REQ-015(无默认值); 评价年限含
  建设期(``evaluation_years == construction_period + operation_years``,
  旧实现不是权威, 无旧语义兼容); 建设期显式非负整数年(K=0 须明写,
  不缺省); 付款比例按十进制语义求和精确为 1; 维护发生年/金额/币种显式。
  折现率须为 ``0..1`` 区间内有限数(宪法 §7.3 比例口径)。
- 快照冻结: 顶层 ``evaluation`` 块含年限/建设期/运营期/折现率/付款/维护/
  确认人/确认时间, 不进 ``calc_config`` 正文与 ``task_params``; 复用身份
  包含新冻结字段(确认时间只存档); 任一不同 → 新快照。
- 运营期覆盖(``check_tariff_horizon_coverage``): 期望点数由运行起始年 +
  已确认运营年限 + 分辨率推导, 不一致 → 400 TASK-REQ-017。
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
    ConstructionPayment,
    EvaluationInput,
    InvalidRequestError,
    MaintenanceExpense,
    check_tariff_horizon_coverage,
    parse_evaluation_input,
)


def _eval(**overrides):
    """合法评价输入基线(K=1、N=2、E=3，付款 0.6/0.4，一项维护)。"""
    base = {
        "evaluation_years": 3,
        "construction_period": 1,
        "operation_years": 2,
        "discount_rate": 0.08,
        "construction_payments": [{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}],
        "maintenance_expenses": [{"year": 3, "amount": "1000.00", "currency": "CNY"}],
    }
    base.update(overrides)
    return base


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
    """多年不再阻断: 运营年限 >= 1 均为合法连续运营输入。"""
    for years in (1, 2, 3, 25):
        parsed = parse_evaluation_input(
            _eval(
                evaluation_years=years,
                construction_period=0,
                operation_years=years,
                construction_payments=[{"t": 0, "ratio": 1}],
                maintenance_expenses=[],
            )
        )
        assert parsed == EvaluationInput(
            evaluation_years=years,
            construction_period=0,
            operation_years=years,
            discount_rate=0.08,
            construction_payments=(ConstructionPayment(t=0, ratio="1"),),
            maintenance_expenses=(),
        )


def test_evaluation_single_year_parses() -> None:
    assert parse_evaluation_input(
        _eval(
            evaluation_years=1,
            construction_period=0,
            operation_years=1,
            construction_payments=[{"t": 0, "ratio": 1}],
            maintenance_expenses=[],
        )
    ) == EvaluationInput(
        evaluation_years=1,
        construction_period=0,
        operation_years=1,
        discount_rate=0.08,
        construction_payments=(ConstructionPayment(t=0, ratio="1"),),
        maintenance_expenses=(),
    )
    assert parse_evaluation_input(_eval(discount_rate=0)).discount_rate == 0.0
    assert parse_evaluation_input(_eval(discount_rate=1)).discount_rate == 1.0


def test_evaluation_explicit_zero_construction_period() -> None:
    """K=0 须明写且合法：不缺省为零，不拒绝显式零。"""
    parsed = parse_evaluation_input(
        _eval(
            evaluation_years=2,
            construction_period=0,
            operation_years=2,
            construction_payments=[{"t": 0, "ratio": 1}],
            maintenance_expenses=[],
        )
    )
    assert (parsed.evaluation_years, parsed.construction_period) == (2, 0)


@pytest.mark.parametrize("period", [-1, True, 1.0, "1", None])
def test_evaluation_bad_construction_period_blocked_015(period) -> None:
    with pytest.raises(InvalidRequestError) as exc_info:
        parse_evaluation_input(_eval(construction_period=period))
    assert exc_info.value.code == "TASK-REQ-015"


@pytest.mark.parametrize("operation", [0, -1, True, "2"])
def test_evaluation_bad_operation_years_blocked_015(operation) -> None:
    with pytest.raises(InvalidRequestError) as exc_info:
        parse_evaluation_input(_eval(evaluation_years=3, operation_years=operation))
    assert exc_info.value.code == "TASK-REQ-015"


@pytest.mark.parametrize(
    "years,period,operation", [(2, 1, 2), (4, 1, 2), (3, 0, 2), (3, 2, 2)]
)
def test_evaluation_relation_mismatch_blocked_015(years, period, operation) -> None:
    """E != K+N 即阻断：旧实现不是权威，无旧语义兼容。"""
    with pytest.raises(InvalidRequestError) as exc_info:
        parse_evaluation_input(
            _eval(
                evaluation_years=years,
                construction_period=period,
                operation_years=operation,
            )
        )
    assert exc_info.value.code == "TASK-REQ-015"


def test_evaluation_missing_required_key_blocked_015() -> None:
    raw = _eval()
    del raw["construction_payments"]
    with pytest.raises(InvalidRequestError) as exc_info:
        parse_evaluation_input(raw)
    assert exc_info.value.code == "TASK-REQ-015"


@pytest.mark.parametrize(
    "payments",
    [
        "x",
        [{"t": 0}],
        [{"ratio": 0.6}],
        [{"t": 0, "ratio": 0.6, "extra": 1}],
        [{"t": -1, "ratio": 1}],
        [{"t": 2, "ratio": 1}],
        [{"t": True, "ratio": 1}],
        [{"t": 0, "ratio": -0.1}],
        [{"t": 0, "ratio": 1.1}],
        [{"t": 0, "ratio": True}],
        [{"t": 0, "ratio": "0.6"}],
        [{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.3}],
        [{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.5}],
        [],
    ],
)
def test_evaluation_bad_construction_payments_blocked_015(payments) -> None:
    with pytest.raises(InvalidRequestError) as exc_info:
        parse_evaluation_input(_eval(construction_payments=payments))
    assert exc_info.value.code == "TASK-REQ-015"


def test_evaluation_decimal_ratio_sum_exactly_one() -> None:
    """0.6+0.4 按十进制语义精确为 1（二进制浮点不断言）。"""
    parsed = parse_evaluation_input(_eval())
    assert parsed.construction_payments == (
        ConstructionPayment(t=0, ratio="0.6"),
        ConstructionPayment(t=1, ratio="0.4"),
    )


def test_evaluation_zero_construction_single_payment() -> None:
    parsed = parse_evaluation_input(
        _eval(
            evaluation_years=2,
            construction_period=0,
            operation_years=2,
            construction_payments=[{"t": 0, "ratio": 1}],
            maintenance_expenses=[],
        )
    )
    assert parsed.construction_payments == (ConstructionPayment(t=0, ratio="1"),)


@pytest.mark.parametrize(
    "expenses",
    [
        "x",
        [{"year": 3, "amount": "1000.00"}],
        [{"year": 3, "currency": "CNY"}],
        [{"amount": "1000.00", "currency": "CNY"}],
        [{"year": 3, "amount": "1000.00", "currency": "CNY", "extra": 1}],
        [{"year": 0, "amount": "1000.00", "currency": "CNY"}],
        [{"year": 1, "amount": "1000.00", "currency": "CNY"}],
        [{"year": 4, "amount": "1000.00", "currency": "CNY"}],
        [{"year": True, "amount": "1000.00", "currency": "CNY"}],
        [{"year": 3, "amount": "-1", "currency": "CNY"}],
        [{"year": 3, "amount": "nan", "currency": "CNY"}],
        [{"year": 3, "amount": True, "currency": "CNY"}],
        [{"year": 3, "amount": "1000.00", "currency": ""}],
        [{"year": 3, "amount": "1000.00", "currency": None}],
    ],
)
def test_evaluation_bad_maintenance_expenses_blocked_015(expenses) -> None:
    with pytest.raises(InvalidRequestError) as exc_info:
        parse_evaluation_input(_eval(maintenance_expenses=expenses))
    assert exc_info.value.code == "TASK-REQ-015"


def test_evaluation_empty_maintenance_means_none() -> None:
    assert parse_evaluation_input(_eval(maintenance_expenses=[])).maintenance_expenses == ()


def test_evaluation_maintenance_entry_normalized() -> None:
    assert parse_evaluation_input(_eval()).maintenance_expenses == (
        MaintenanceExpense(year=3, amount="1000", currency="CNY"),
    )
    int_amount = _eval(
        maintenance_expenses=[{"year": 2, "amount": 500, "currency": "CNY"}]
    )
    assert parse_evaluation_input(int_amount).maintenance_expenses == (
        MaintenanceExpense(year=2, amount="500", currency="CNY"),
    )


# ---------------------------------------------------------------------------
# 运营期覆盖核对
# ---------------------------------------------------------------------------


def test_horizon_coverage_passes_for_matching_series() -> None:
    check_tariff_horizon_coverage(
        baseline_resolution="1h",
        baseline_start_year=2024,
        operation_years=1,
        tariff_bindings={"m1": {"price": "tou", "instance": "d", "interface": "p"}},
        price_series={"tou": {"kind": "time_series", "point_count": 8784}},
    )


def test_horizon_coverage_multi_year_scales_with_calendar() -> None:
    """多年期望点数逐年按公历推导(2024 闰年 8784 + 2025 平年 8760)。"""
    check_tariff_horizon_coverage(
        baseline_resolution="1h",
        baseline_start_year=2024,
        operation_years=2,
        tariff_bindings={"m1": {"price": "tou", "instance": "d", "interface": "p"}},
        price_series={"tou": {"kind": "time_series", "point_count": 8784 + 8760}},
    )


def test_horizon_coverage_blocks_mismatch_017() -> None:
    with pytest.raises(InvalidRequestError) as exc_info:
        check_tariff_horizon_coverage(
            baseline_resolution="1h",
            baseline_start_year=2024,
            operation_years=1,
            tariff_bindings={"m1": {"price": "tou", "instance": "d", "interface": "p"}},
            price_series={"tou": {"kind": "time_series", "point_count": 8760}},
        )
    assert exc_info.value.code == "TASK-REQ-017"
    assert exc_info.value.params["expected_point_count"] == 8784


def test_horizon_coverage_ignores_constant_and_blocks_unknown_price() -> None:
    check_tariff_horizon_coverage(
        baseline_resolution="1h",
        baseline_start_year=2026,
        operation_years=1,
        tariff_bindings={"m1": {"price": "flat", "instance": "d", "interface": "p"}},
        price_series={"flat": {"kind": "constant"}},
    )
    with pytest.raises(InvalidRequestError) as exc_info:
        check_tariff_horizon_coverage(
            baseline_resolution="1h",
            baseline_start_year=2026,
            operation_years=1,
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
        evaluation = {
            "evaluation_years": 1,
            "construction_period": 0,
            "operation_years": 1,
            "discount_rate": 0.08,
            "construction_payments": [{"t": 0, "ratio": 1}],
            "maintenance_expenses": [],
        }
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


def _k1n2(rate=0.08):
    """K=1、N=2、E=3 提交输入（付款 0.6/0.4，一项 t=3 维护）。"""
    return {
        "evaluation_years": 3,
        "construction_period": 1,
        "operation_years": 2,
        "discount_rate": rate,
        "construction_payments": [{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}],
        "maintenance_expenses": [{"year": 3, "amount": "1000.00", "currency": "CNY"}],
    }


def test_snapshot_freezes_evaluation_and_identity(  # noqa: F811
    client, db, factory
) -> None:
    """快照 4.0.0: evaluation 块冻结时轴/付款/维护/确认人/时间; 任一不同 → 新快照。"""
    from auth_helpers import make_user
    from test_task_config_revision import _prepare_project, _save_config

    from iesplan.core.jsonutil import jsonable

    owner = make_user(db, "owner_eval_snap")
    pid = _prepare_project(client, db, owner, name="评价快照项目")
    rev = _save_config(client, pid, owner)

    def _submit_eval(payload, key):
        resp = _post(
            client, pid, owner,
            {"config_revision": rev},
            key,
            evaluation=payload,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["task"]

    first = _submit_eval(_k1n2(), "eval-snap-1")
    snap = tasks_domain.get_snapshot(factory(), first["calc_snapshot_id"])
    assert snap is not None
    assert snap.schema_version == "4.0.0"
    frozen = jsonable(dict(snap.calc_config_snapshot))
    assert "evaluation" not in frozen
    assert "evaluation" not in frozen.get("task_params", {})
    evaluation = jsonable(dict(snap.evaluation))
    assert evaluation["evaluation_years"] == 3
    assert evaluation["construction_period"] == 1
    assert evaluation["operation_years"] == 2
    assert evaluation["discount_rate"] == 0.08
    assert evaluation["construction_payments"] == [
        {"t": 0, "ratio": "0.6"}, {"t": 1, "ratio": "0.4"},
    ]
    assert evaluation["maintenance_expenses"] == [
        {"year": 3, "amount": "1000", "currency": "CNY"},
    ]
    assert evaluation["confirmed_by"] == owner.id
    assert evaluation["confirmed_at"]

    # 同输入复用快照
    replay = _submit_eval(_k1n2(), "eval-snap-2")
    assert replay["calc_snapshot_id"] == first["calc_snapshot_id"]
    # 折现率不同 → 复用身份不同 → 新快照
    other = _submit_eval(_k1n2(rate=0.05), "eval-snap-3")
    assert other["calc_snapshot_id"] != first["calc_snapshot_id"]
    # 付款不同 → 复用身份不同 → 新快照(财务含义不同)
    other_pay = _k1n2()
    other_pay["construction_payments"] = [{"t": 0, "ratio": 0.5}, {"t": 1, "ratio": 0.5}]
    branched = _submit_eval(other_pay, "eval-snap-4")
    assert branched["calc_snapshot_id"] != first["calc_snapshot_id"]
    # 维护不同 → 复用身份不同 → 新快照
    other_maint = _k1n2()
    other_maint["maintenance_expenses"] = []
    branched_maint = _submit_eval(other_maint, "eval-snap-5")
    assert branched_maint["calc_snapshot_id"] != first["calc_snapshot_id"]

    # 多年提交直达快照冻结(时轴/付款/维护/确认人/确认时间存于快照顶层,
    # 不进配置正文; 实际现金流计算由后续切片消费, 本切片只验提交与冻结)
    resp = _post(
        client, pid, owner,
        {"config_revision": rev},
        "eval-snap-multi",
        evaluation={
            "evaluation_years": 2,
            "construction_period": 0,
            "operation_years": 2,
            "discount_rate": 0.08,
            "construction_payments": [{"t": 0, "ratio": 1}],
            "maintenance_expenses": [],
        },
    )
    assert resp.status_code == 201, resp.text
    multi = resp.json()["task"]
    multi_snap = tasks_domain.get_snapshot(factory(), multi["calc_snapshot_id"])
    assert multi_snap is not None
    multi_config = jsonable(dict(multi_snap.calc_config_snapshot))
    assert "evaluation" not in multi_config
    multi_eval = jsonable(dict(multi_snap.evaluation))
    assert multi_eval["evaluation_years"] == 2
    assert multi_eval["construction_period"] == 0
    assert multi_eval["operation_years"] == 2
    assert multi_eval["discount_rate"] == 0.08
    assert multi_eval["confirmed_by"] == owner.id
    assert multi_eval["confirmed_at"]


def test_snapshot_maintenance_currency_mismatch_blocked_015(  # noqa: F811
    client, db,
) -> None:
    """维护币种与有效财务配置不一致 → 提交边界阻断(持本次闸门快照核对)。"""
    from auth_helpers import make_user
    from test_task_config_revision import _prepare_project, _save_config

    owner = make_user(db, "owner_eval_ccy")
    pid = _prepare_project(client, db, owner, name="维护币种项目")
    rev = _save_config(client, pid, owner)
    payload = _k1n2()
    payload["maintenance_expenses"] = [
        {"year": 3, "amount": "1000.00", "currency": "USD"}
    ]
    resp = _post(client, pid, owner, {"config_revision": rev}, "eval-ccy-1", evaluation=payload)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "TASK-REQ-015"


def test_snapshot_reuse_identity_includes_confirmer() -> None:
    """复用身份含确认人(防错归)、含新冻结字段，不含确认时间(否则复用永不命中)。"""
    from iesplan.application.tasks.submissions import _evaluation_identity

    base = {
        "evaluation_years": 3, "construction_period": 1, "operation_years": 2,
        "discount_rate": 0.08,
        "construction_payments": [{"t": 0, "ratio": "0.6"}],
        "maintenance_expenses": [{"year": 3, "amount": "1000", "currency": "CNY"}],
        "confirmed_by": 7, "confirmed_at": "2026-10-09T00:00:00+00:00",
    }
    same_user = dict(base, confirmed_at="2026-10-10T00:00:00+00:00")
    other_user = dict(base, confirmed_by=9)
    other_period = dict(base, construction_period=0)
    other_pay = dict(
        base, construction_payments=[{"t": 0, "ratio": "1"}]
    )
    assert _evaluation_identity(same_user) == _evaluation_identity(base)
    assert _evaluation_identity(other_user) != _evaluation_identity(base)
    assert _evaluation_identity(other_period) != _evaluation_identity(base)
    assert _evaluation_identity(other_pay) != _evaluation_identity(base)
