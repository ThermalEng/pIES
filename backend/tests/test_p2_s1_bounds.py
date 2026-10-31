"""P2-S1: effective domain intersection, unified bounds query, and provenance.

Requirements verified:
1. Intersect model valid domain and project planning boundary, preserving provenance.
2. Planning takeover does not lose model domain and does not substitute template reference value.
3. Range referencing planning variable is preserved as an association relation.
4. Correct handling of zero bounds, single-sided bounds, unknown sides, and conflicts.
5. Consumers query via unified CompilerAnalysis entrypoint.
"""
from __future__ import annotations

from dataclasses import replace
import json
import pytest

from iesplan.computation import build_registry
from iesplan.computation.generators.compiler_ir import (
    BoundFact,
    BoundRequest,
    CompilerAnalysis,
    CompilerInventory,
    IndexDomain,
    SemanticRelation,
    SourceLocation,
    VatPoolOperation,
)
from iesplan.computation.generators.linear import (
    LinearVariable,
    LoweringUnsupportedError,
)
from iesplan.computation.generators.project import (
    _AffineAcc,
    build_project_model,
)

from test_project_model import (
    _gen_inputs,
    _island_doc,
    _island_inputs,
    _finance_payload,
)


def test_effective_domain_intersection_and_sources() -> None:
    """模型域 [0,10]kW 与项目规划界 [3,20]kW 取交集得 [3,10]kW，保留来源。"""
    artifact, models, materialized, finance, config, evaluation = _gen_inputs()
    doc = json.loads(artifact.canonical_text)
    # Acme gen model: cap_kw valid_range is [0, 1000] kW in GEN_YAML.
    # Set model property cap_kw valid_range to [0, 10] kW.
    models["gen1"].variables["cap_kw"] = replace(
        models["gen1"].variables["cap_kw"], valid_range=(0.0, 10.0)
    )
    # Project planning boundary: [3, 20] kW.
    doc["planning"]["variables"]["gen_cap"]["lower_bound"] = "3"
    doc["planning"]["variables"]["gen_cap"]["upper_bound"] = "20"
    new_art = replace(artifact, canonical_text=json.dumps(doc))
    from iesplan.computation.generators.project import _new_builder
    builder = _new_builder(new_art, models, materialized, finance, evaluation)
    model, _, _ = builder.build()

    # 1. 求解变量界：下界取 max(0, 3)=3 kW (3000 W)，上界取 min(10, 20)=10 kW (10000 W)
    by_var = {var.name: var for var in model.variables}
    gen_cap_var = by_var["gen_cap"]
    assert gen_cap_var.lower == pytest.approx(3000.0)
    assert gen_cap_var.upper == pytest.approx(10000.0)

    # 2. 统一分析入口查询 BoundFact
    fact = builder.analysis.bound_fact("gen_cap")
    assert fact is not None
    assert fact.symbol == "gen_cap"
    assert fact.lower == pytest.approx(3000.0)
    assert fact.upper == pytest.approx(10000.0)
    assert fact.model_domain == (pytest.approx(0.0), pytest.approx(10000.0))
    assert fact.project_boundary == (pytest.approx(3000.0), pytest.approx(20000.0))
    # 来源：下界来自项目规划，上界来自模型有效域
    assert fact.lower_source is not None
    assert fact.lower_source.field_path == "planning.variables.gen_cap"
    assert fact.upper_source is not None
    assert fact.upper_source.field_path == "properties.cap_kw.valid_range"

    # 通过 (instance, property) 别名查询也得到同一 BoundFact
    alias_fact = builder.analysis.bound_fact("gen1.cap_kw")
    assert alias_fact is fact


def test_planning_takeover_retains_model_domain_when_unspecified() -> None:
    """规划未给上界时保留模型域，不代入模板参考值（参考值 60kW 不参与）。"""
    artifact, models, materialized, finance, config, evaluation = _gen_inputs()
    doc = json.loads(artifact.canonical_text)
    # cap_kw model valid_range: [0, 10] kW; template reference value is 60 kW
    models["gen1"].variables["cap_kw"] = replace(
        models["gen1"].variables["cap_kw"], valid_range=(0.0, 10.0)
    )
    assert models["gen1"].variables["cap_kw"].value == 60.0
    # Planning specifies lower_bound 2 kW, but omits upper_bound
    doc["planning"]["variables"]["gen_cap"]["lower_bound"] = "2"
    doc["planning"]["variables"]["gen_cap"]["upper_bound"] = None
    new_art = replace(artifact, canonical_text=json.dumps(doc))
    from iesplan.computation.generators.project import _new_builder
    builder = _new_builder(new_art, models, materialized, finance, evaluation)
    model, _, _ = builder.build()

    by_var = {var.name: var for var in model.variables}
    gen_cap_var = by_var["gen_cap"]
    # Model domain upper 10 kW (10000 W) is retained, NOT None and NOT 60 kW!
    assert gen_cap_var.lower == pytest.approx(2000.0)
    assert gen_cap_var.upper == pytest.approx(10000.0)

    fact = builder.analysis.bound_fact("gen_cap")
    assert fact is not None
    assert fact.upper == pytest.approx(10000.0)
    assert fact.upper_source.field_path == "properties.cap_kw.valid_range"


def test_reference_to_planning_var_retained_as_relation() -> None:
    """引用规划量的范围保留为关联关系（容量约束行），不以局部大数或独立区间代替。"""
    artifact, models, materialized, finance, config, evaluation = _gen_inputs()
    doc = json.loads(artifact.canonical_text)
    new_art = replace(artifact, canonical_text=json.dumps(doc))
    from iesplan.computation.generators.project import _new_builder
    builder = _new_builder(new_art, models, materialized, finance, evaluation)
    model, _, _ = builder.build()

    # power_out 引用 gen_cap（cap_kw）作为上界，生成了 capacity 约束行
    cap_relations = [
        op for op in builder.inventory.operations
        if op.kind == "capacity" and op.payload.local == "power_out"
    ]
    assert len(cap_relations) == 2  # plan_le (max) and plan_ge (min)
    le_rel = next(r for r in cap_relations if r.payload.sense == "le")
    assert le_rel.payload.planning_symbol == "gen_cap"


def test_zero_bound_and_single_sided_and_unbounded() -> None:
    """零界、单侧界、未知侧均合法，正确处理不报错。"""
    inventory = CompilerInventory()
    inventory.seal()
    analysis = CompilerAnalysis(inventory, {
        "zero_var": LinearVariable("zero_var", lower=0.0, upper=0.0),
        "single_lo": LinearVariable("single_lo", lower=0.0, upper=None),
        "single_hi": LinearVariable("single_hi", lower=None, upper=10.0),
        "unbounded": LinearVariable("unbounded", lower=None, upper=None),
    })

    assert analysis.variable_bounds("zero_var") == (0.0, 0.0)
    assert analysis.variable_bounds("single_lo") == (0.0, None)
    assert analysis.variable_bounds("single_hi") == (None, 10.0)
    assert analysis.variable_bounds("unbounded") == (None, None)

    # 零界 BoundFact
    fact_zero = analysis.bound_fact("zero_var")
    assert fact_zero.lower == 0.0
    assert fact_zero.upper == 0.0


def test_disjoint_domain_conflict_reports_locatable_error() -> None:
    """有效域交集为空（模型域 [0,10]kW 与项目规划界 [15,20]kW）可定位并报错。"""
    artifact, models, materialized, finance, config, evaluation = _gen_inputs()
    doc = json.loads(artifact.canonical_text)
    models["gen1"].variables["cap_kw"] = replace(
        models["gen1"].variables["cap_kw"], valid_range=(0.0, 10.0)
    )
    # Project boundary lower is 15 kW, which is > model domain max 10 kW
    doc["planning"]["variables"]["gen_cap"]["lower_bound"] = "15"
    doc["planning"]["variables"]["gen_cap"]["upper_bound"] = "20"
    new_art = replace(artifact, canonical_text=json.dumps(doc))
    from iesplan.computation.generators.project import _new_builder
    builder = _new_builder(new_art, models, materialized, finance, evaluation)
    with pytest.raises(LoweringUnsupportedError, match="有效域冲突") as exc_info:
        builder.build()
    err = exc_info.value
    assert "gen_cap" in str(err)
    assert err.model_source is not None
    assert err.model_source["role"] == "planning_bound_conflict"
    assert err.model_source["location"]["planning_variable"] == "gen_cap"


def test_unit_equivalent_domain_intersection() -> None:
    """等价单位（模型 kW，项目 MW）正确换算后取交集。"""
    artifact, models, materialized, finance, config, evaluation = _gen_inputs()
    doc = json.loads(artifact.canonical_text)
    # Model: [0, 1000] kW = [0, 1e6] W
    models["gen1"].variables["cap_kw"] = replace(
        models["gen1"].variables["cap_kw"], valid_range=(0.0, 1000.0)
    )
    # Planning: [0.2, 2.0] MW = [2e5, 2e6] W
    doc["planning"]["variables"]["gen_cap"]["lower_bound"] = "0.2"
    doc["planning"]["variables"]["gen_cap"]["upper_bound"] = "2.0"
    doc["planning"]["variables"]["gen_cap"]["unit"] = "MW"
    new_art = replace(artifact, canonical_text=json.dumps(doc))
    from iesplan.computation.generators.project import _new_builder
    builder = _new_builder(new_art, models, materialized, finance, evaluation)
    model, _, _ = builder.build()

    by_var = {var.name: var for var in model.variables}
    gen_cap_var = by_var["gen_cap"]
    # Intersection: max(0, 0.2 MW) = 200,000 W; min(1000 kW, 2.0 MW) = 1,000,000 W
    assert gen_cap_var.lower == pytest.approx(200000.0)
    assert gen_cap_var.upper == pytest.approx(1000000.0)


def test_consumers_query_via_unified_analysis() -> None:
    """现有消费者（月峰需量、增值税、所得税、互斥）均通过统一 CompilerAnalysis 入口查询。"""
    inventory = CompilerInventory()
    origin = SourceLocation("finance.tax.demo", binding_id="tax_demo")
    vat = SemanticRelation(
        "vat.t", "vat", IndexDomain("month", 1),
        VatPoolOperation("demo", {"vat": [], "construction": []}),
        origin, input_symbols=("a", "b"), requires_bounds=True,
    )
    inventory.collect(vat)
    fact_a = BoundFact("a", lower=0.0, upper=50.0)
    inventory.register_bound_fact(fact_a)
    inventory.seal()

    analysis = CompilerAnalysis(inventory, {
        "a": LinearVariable("a", lower=0.0, upper=50.0),
        "b": LinearVariable("b", lower=10.0, upper=30.0),
    })

    # 1. 变量界与请求记录
    lo, hi = analysis.variable_bounds("a", relation=vat, period=1)
    assert lo == 0.0 and hi == 50.0
    requests = [r for r in analysis.bound_requests if r.relation_id == "vat.t"]
    assert len(requests) == 2
    assert {r.side for r in requests} == {"min", "max"}

    # 2. 仿射表达式界
    affine = _AffineAcc()
    affine.add_term("a", 2.0)
    affine.add_term("b", -1.0)
    affine.add_const(5.0)
    # min: 5 + 2*0 - 1*30 = -25
    # max: 5 + 2*50 - 1*10 = 95
    assert analysis.affine_side(affine, side="min", label="test", relation=vat, period=1) == -25.0
    assert analysis.affine_side(affine, side="max", label="test", relation=vat, period=1) == 95.0

    # 3. 互斥降解
    mutex = analysis.mutex("test_mutex", "a", "b", relation=vat, period=1)
    assert len(mutex.variables) == 1
    assert mutex.variables[0].kind == "binary"
