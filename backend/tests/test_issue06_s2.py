"""ISSUE-06 S2：纯标量小 LP 的维护仿射、税务身份、界与容量选择。

没有时间步变量或整段小时求解；真实 HiGHS 仅解有限个标量列。
公开提交→Worker→Adapter→封存复用既有现金流链测试的求解次数。
"""

from __future__ import annotations

import subprocess
from decimal import Decimal
from types import SimpleNamespace

import pytest

from iesplan.computation import HIGHS_ADAPTER_PATH
from iesplan.computation.generators.linear import LoweringUnsupportedError
from iesplan.computation.generators.mps import write_mps
from iesplan.computation.generators.project import _ProjectBuilder
from iesplan.computation.result_adapters.highs import _mps_column_names, _parse_solution
from iesplan.finance import FinanceProfile, merge_effective
from iesplan.tasks.contracts import parse_evaluation_input


def _scale(inst: str, value: str) -> dict:
    return {
        "kind": "scale_unit_cost", "instance_id": inst, "property": "cap_kw",
        "unit_cost": {"value": value, "unit": "CNY/kW"},
    }


def _share(inst: str, ratio: float) -> dict:
    return {
        "kind": "cost_component_share", "instance_id": inst,
        "component": "upfront_capex", "ratio": ratio,
    }


def _expense(amount: str, *terms: dict) -> dict:
    return {"year": 1, "amount": amount, "currency": "CNY", "terms": list(terms)}


def _builder(
    expenses: list[dict], *, taxes: bool = False, second_planned: bool = False,
    same_category: bool = True, upper: str | None = "10", fixed_capacity: bool = False,
    currency: str = "CNY",
    finance_patch=None, evaluation_patch=None,
) -> _ProjectBuilder:
    """S1 已冻结的形态直接进入生产 builder：设备仅含标量 property。"""
    vat_rule = {
        "kind": "vat", "label_ref": "vat_tag", "settlement": "monthly",
        "in_tax": [], "out_tax": [], "om_input_tax": [],
        "maintenance_input_tax": [
            {"year": 1, "position": pos, "rate": {"value": "0.13", "unit": "1"},
             "deductible_ratio": {"value": ratio, "unit": "1"}}
            for pos, ratio in ((1, "1"), (2, "0"))
        ],
        "credit_pool": "pool", "confirmation": "same_period",
    }
    income_rule = {
        "kind": "income_tax", "label_ref": "income_tag",
        "rate": {"value": "0.25", "unit": "1"},
        "revenue": [], "energy_cost": [], "annual_om": [], "variable_om": [],
        "deductible_maintenance": {"mode": "explicit_items", "items": [
            {"year": 1, "position": 1, "deductible": True},
            {"year": 1, "position": 2, "deductible": False},
        ]},
        "depreciation": [], "loss_carryforward_years": 5, "opening_losses": [],
    }
    payload = {
        "schema": "ies.finance-profile", "schema_version": "6.0.0",
        "profile": {
            "id": "maintenance-tiny", "region": "CN-North", "currency": currency,
            "base_year": 2025, "price_basis": "tax_inclusive",
            "cost_method": "fixed_plus_linear",
        },
        "finance_types": {
            name: {"upfront_capex": {"linear": {
                "cap": {"unit_cost": {"value": cost, "unit": f"{currency}/kW"}},
            }}}
            for name, cost in (("same", "200"), ("alternative", "220"))
        },
        # 财务画像契约要求至少一个合法价格；本测试仅研究维护费用，
        # 故用未被 tariff_bindings 引用的价格，不引入任何能源现金流。
        "energy_prices": {
            "unused_purchase": {
                "carrier": "electricity", "direction": "purchase", "kind": "constant",
                "value": {"value": "0.1", "unit": f"{currency}/kWh"},
            },
        },
        "demand_charges": {},
        "taxes": ({
            "vat_tag": {"display_name": "维护增值税", "tax_type": "vat", "applies_to": "same.upfront_capex"},
            "income_tag": {"display_name": "维护所得税", "tax_type": "income_tax", "applies_to": "same.upfront_capex"},
        } if taxes else {}),
        "credit_pools": ({
            "pool": {"opening_credit": {"value": "0", "unit": "CNY"},
                     "receivable_policy": "end_receivable", "refunds": []},
        } if taxes else {}),
        "tax_rules": ({"maint_vat": vat_rule, "maint_income": income_rule} if taxes else {}),
        "net_cashflows": {},
    }
    if finance_patch is not None:
        finance_patch(payload)
    finance = merge_effective(FinanceProfile.from_dict(payload), None)
    planning = {
        "cap_a": {"device_ref": "a", "parameter": "cap_kw",
                  "lower_bound": "10" if fixed_capacity else "0", "unit": "kW"},
    }
    if upper is not None:
        planning["cap_a"]["upper_bound"] = upper
    constraints = {}
    if second_planned:
        planning["cap_b"] = {
            "device_ref": "b", "parameter": "cap_kw", "lower_bound": "0",
            "upper_bound": "10", "unit": "kW",
        }
        constraints = {
            "min_total": {"expression": "cap_a + cap_b >= 10 kW"},
            "max_total": {"expression": "cap_a + cap_b <= 10 kW"},
        }
    doc = {
        "project_baseline": {"resolution": "1h", "start_year": 2025},
        "devices": {
            "a": {"definition": {"id": "ies.device.synthetic_capacity"},
                  "asset_origin": "new", "properties": {"cap_kw": {"value": 60, "unit": "kW"}}},
            "b": {"definition": {"id": "ies.device.synthetic_capacity"},
                  "asset_origin": "new", "properties": {"cap_kw": {"value": 100, "unit": "kW"}}},
            "old": {"definition": {"id": "ies.device.synthetic_capacity"},
                    "asset_origin": "existing", "properties": {"cap_kw": {"value": 20, "unit": "kW"}}},
        },
        "finance_binding": {"instances": {
            name: {"type": "costed", "finance_type": (
                "same" if same_category or name != "b" else "alternative"
            ), "drivers": {"cap": {"property": "cap_kw"}}}
            for name in ("a", "b", "old")
        }},
        "planning": {
            "objective": {"sense": "maximize", "expression": "system.after_tax_npv"},
            "variables": planning, "constraints": constraints,
        },
        "tariff_bindings": {}, "demand_bindings": {}, "connections": {},
    }
    scalar = SimpleNamespace(kind="property", value=1.0, unit="kW")
    contribution = SimpleNamespace(
        device_id="ies.device.synthetic_capacity",
        variables={"cap_kw": scalar}, interfaces={}, relations=(), states=(),
    )
    evaluation_data = {
        "evaluation_years": 2, "construction_period": 0,
        "operation_years": 2, "discount_rate": 0.0,
        "construction_payments": [{"t": 0, "ratio": 1}],
        "maintenance_expenses": expenses,
    }
    if evaluation_patch is not None:
        evaluation_patch(evaluation_data)
    evaluation = parse_evaluation_input(evaluation_data)
    return _ProjectBuilder(
        doc=doc, devices=doc["devices"], models={name: contribution for name in doc["devices"]},
        series=(), finance=finance, evaluation=evaluation, point_count=0, step_s=3600.0,
    )


def _collect_only(builder: _ProjectBuilder) -> _ProjectBuilder:
    """Stop exactly at the S2 collection/analysis barrier; allocate no rows."""
    buses = builder._compute_buses()
    bus_flows = builder._bus_flow_index(buses)
    builder._declare_global_symbols()
    bound = {
        inst: builder._bind_instance_symbols(
            inst, bus_flows.get(inst, frozenset())
        )
        for inst in sorted(builder.devices)
    }
    builder._collect_semantic_relations(buses, bound)
    assert builder.inventory.sealed
    assert not builder.rows
    return builder


def test_pre_lowering_amount_sources_bind_cost_and_maintenance_to_income() -> None:
    """Capacity -> amount -> depreciation -> income is one inspectable graph."""

    def patch(profile: dict) -> None:
        profile["tax_rules"]["maint_income"]["depreciation"] = [
            {
                "base_from": {
                    "kind": "upfront_capex_net", "finance_type": "same",
                },
                "method": "straight_line", "life_years": 4,
                "residual": {"value": "0", "unit": "1"}, "first_year": 1,
            },
            {
                "base_from": {
                    "kind": "maintenance_expense_net", "year": 1, "position": 1,
                },
                "method": "straight_line", "life_years": 2,
                "residual": {"value": "0", "unit": "1"}, "first_year": 1,
            },
        ]

    builder = _collect_only(_builder(
        [_expense("11", _scale("a", "2"), _share("a", 0.1))],
        taxes=True, finance_patch=patch,
    ))
    analysis = builder.analysis

    cost_a = analysis.operation("cost.a__upfront_capex")
    assert cost_a.input_symbols == ("cap_a",)
    assert cost_a.identity in analysis.uses["cap_a"]
    assert cost_a.payload.terms[0].symbol == "cap_a"

    upfront = analysis.operation("depreciation.maint_income.0")
    assert upfront.input_symbols == ("a__upfront_capex", "b__upfront_capex")
    assert upfront.depends_on == (
        "cost.a__upfront_capex", "cost.b__upfront_capex",
    )
    assert "('upfront_capex_net', 'same')" not in upfront.input_symbols

    maintenance = analysis.operation("maintenance.y1.p1")
    assert "cap_a" in maintenance.input_symbols
    maint_dep = analysis.operation("depreciation.maint_income.1")
    assert maint_dep.input_symbols == ("maintenance.y1.p1",)
    assert maint_dep.depends_on == ("maintenance.y1.p1",)

    year1 = analysis.operation("income.maint_income.y1")
    assert upfront.identity in year1.depends_on
    assert maint_dep.identity in year1.depends_on
    assert {term.source_relation for term in year1.payload.amount_terms} >= {
        upfront.identity, maint_dep.identity, maintenance.identity,
    }
    assert year1.identity in analysis.dependencies["income.maint_income"]
    assert not any(name.startswith("inc__") for name in builder.variables)


def _amount(node: dict, **values: float) -> float:
    return float(node["constant"]) + sum(float(coeff) * values[name]
                                        for name, coeff in node["terms"].items())


def _tiny_solve(builder: _ProjectBuilder, tmp_path, *, with_model=False):
    """一个真正的 HiGHS 主解；MPS 至多为标量与月税务列，无时间步。"""
    model, _, _ = builder.build()
    raw, _ = write_mps(model)
    work = tmp_path / "input"
    work.mkdir(parents=True)
    (tmp_path / "output").mkdir()
    (work / "problem.mps").write_text(raw, encoding="utf-8")
    proc = subprocess.run(
        [HIGHS_ADAPTER_PATH, "input/problem.mps", "--solution_file", "output/solution.sol",
         "--time_limit", "30", "--random_seed", "0"],
        capture_output=True, text=True, cwd=tmp_path, timeout=40, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    solution = _parse_solution((tmp_path / "output" / "solution.sol").read_bytes())
    assert solution["model_status"] == "HighsModelStatus.kOptimal"
    columns = _mps_column_names(raw.encode("utf-8"))
    assert len(columns) == len(solution["primals"])
    solved = dict(zip(columns, solution["primals"], strict=True))
    facts = builder.financial_facts()
    return (solved, facts, model) if with_model else (solved, facts)


def test_affine_amount_instance_scope_zero_and_full_cash() -> None:
    """同财务类别实例不串成本；旧设备有规模现金而无沉没建设额。"""
    builder = _builder([
        _expense("11", _scale("a", "2"), _share("a", 0.1)),
        _expense("17", _scale("b", "3"), _share("b", 0.02), _scale("old", "4")),
    ])
    builder.build()
    first = builder._maintenance_items[(1, 1)]
    second = builder._maintenance_items[(1, 2)]
    assert first.constant == pytest.approx(11)
    assert first.terms == pytest.approx({"cap_a": 0.022})
    assert second.constant == pytest.approx(17 + 3 * 100 + 0.02 * 200 * 100 + 4 * 20)
    assert second.terms == {}
    facts = builder.financial_facts()
    maintenance = facts["annual_operation"][0]["maintenance"]
    assert isinstance(maintenance, dict) and maintenance["constant"] == pytest.approx(808)
    assert _amount(maintenance, cap_a=0) == pytest.approx(808)  # 规模零时仍有确定项
    assert _amount(maintenance, cap_a=10000) == pytest.approx(1028)
    # 建设额：只有本课题 a/b，旧设备 upfront 沉没不进入 CF[0]。
    assert _amount(facts["construction_total"], cap_a=10000) == pytest.approx(22000)
    assert _amount(facts["annual_net_flows"][0], cap_a=10000) == pytest.approx(-22000)
    assert _amount(facts["annual_net_flows"][1], cap_a=10000) == pytest.approx(-1028)
    assert _amount(facts["annual_net_flows"][2], cap_a=10000) == pytest.approx(0)


def test_usd_unit_cost_preserves_currency_without_fx() -> None:
    """合法 USD/kW 单价只将规模换成 SI，既不拒绝也不推测美元汇率。"""
    builder = _builder([{
        "year": 1, "amount": "5", "currency": "USD", "terms": [
            {**_scale("a", "2"), "unit_cost": {"value": "2", "unit": "USD/kW"}},
            _share("a", 0.1),
        ],
    }], currency="USD")
    builder.build()
    amount = builder.financial_facts()["annual_operation"][0]["maintenance"]
    assert _amount(amount, cap_a=0) == pytest.approx(5)
    assert _amount(amount, cap_a=10000) == pytest.approx(225)


def test_vat_income_losses_and_same_payment_identity_on_tiny_solve(tmp_path) -> None:
    """税则只引用(年,原序号)；不抵扣不影响现金，负所得进入 B0。"""
    builder = _builder([
        _expense("11", _scale("a", "2"), _share("a", 0.1)),
        _expense("17", _scale("b", "3"), _share("b", 0.02), _scale("old", "4")),
    ], taxes=True, fixed_capacity=True)
    solved, facts = _tiny_solve(builder, tmp_path)
    paid1 = Decimal("231")   # 11 + 2×10 + 0.1×(200×10)
    paid2 = Decimal("797")   # 17 + 3×100 + 0.02×(200×100) + 4×20
    credit = paid1 * Decimal("0.13") / Decimal("1.13")
    deduction = paid1 - credit
    assert _amount(facts["annual_operation"][0]["maintenance"], **solved) == pytest.approx(float(paid1 + paid2))
    assert _amount(facts["annual_net_flows"][1], **solved) == pytest.approx(-1028)
    vat = {(row["pool"], row["month"]): row for row in facts["tax_cash"]["vat"]}
    assert _amount(vat[("pool", 12)]["paid"], **solved) == pytest.approx(0)
    assert _amount(vat[("pool", 12)]["carry"], **solved) == pytest.approx(float(credit))
    assert _amount(vat[("pool", 24)]["carry"], **solved) == pytest.approx(float(credit))
    income = {row["year"]: row for row in facts["tax_cash"]["income"]}
    assert _amount(income[1]["taxable_base"], **solved) == pytest.approx(-float(deduction))
    assert _amount(income[1]["loss_generated"], **solved) == pytest.approx(float(deduction))
    assert _amount(income[1]["tax"], **solved) == pytest.approx(0)
    assert _amount(income[2]["taxable_base"], **solved) == pytest.approx(0)
    assert _amount(income[2]["loss_used"], **solved) == pytest.approx(0)
    assert _amount(income[2]["tax"], **solved) == pytest.approx(0)
    assert _amount(facts["annual_net_flows"][0], **solved) == pytest.approx(-22000)
    assert sum(_amount(row, **solved) for row in facts["annual_net_flows"]) == pytest.approx(-23028)


def test_maintenance_coefficient_switches_optimal_plan_without_hourly_solve(tmp_path) -> None:
    """同一建设需求选 a/b：无维护选低建设单价 a，维护比例升高选 b。"""
    no_maintenance = _builder([], second_planned=True, same_category=False)
    values0, facts0 = _tiny_solve(no_maintenance, tmp_path / "base")
    with_maintenance = _builder([
        _expense("7", _share("a", 0.2)),
    ], second_planned=True, same_category=False)
    values1, facts1 = _tiny_solve(with_maintenance, tmp_path / "maint")
    assert (values0["cap_a"], values0["cap_b"]) == pytest.approx((10000, 0))
    assert (values1["cap_a"], values1["cap_b"]) == pytest.approx((0, 10000))
    assert _amount(facts0["construction_total"], **values0) == pytest.approx(2000)
    assert _amount(facts1["construction_total"], **values1) == pytest.approx(2200)
    assert _amount(facts1["annual_operation"][0]["maintenance"], **values1) == pytest.approx(7)
    assert _amount(facts1["annual_net_flows"][1], **values1) == pytest.approx(-7)


def test_finite_bound_only_required_when_tax_expression_needs_it() -> None:
    items = [_expense("0", _scale("a", "2")), _expense("0")]
    # 无税链时不对纯 NPV 仿射虚构最小建设量或上界。
    _builder(items, upper=None).build()
    with pytest.raises(LoweringUnsupportedError, match="VAT 池.*缺有限界.*cap_a"):
        _builder(items, upper=None, taxes=True).build()
