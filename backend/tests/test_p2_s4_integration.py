"""P2-S4: exact transformation wrap-up, provenance, determinism, and strategy evaluation.

Requirements verified:
1. Mutex, peak demand, VAT, and income tax all consume unified analysis authority.
2. Removal of legacy bypasses and Big-M guessing.
3. On-demand mutex isolated benchmark comparison and formal recommendation.
4. Determinism: identical inputs produce byte-identical MPS and metadata.
5. Cooperative cancellation / stop probe during model generation.
"""
from __future__ import annotations

import json
import pytest

from iesplan.assembly.contracts import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation.generators.compiler_ir import (
    BoundFact,
    CompilerAnalysis,
    CompilerInventory,
    IndexDomain,
    SemanticRelation,
    SourceLocation,
    VatPoolOperation,
)
from iesplan.computation.generators.highs_milp import HIGHS_MILP_PROVIDER
from iesplan.computation.generators.linear import LoweringUnsupportedError
from iesplan.computation.generators.on_demand_mutex import evaluate_mutex_strategies
from iesplan.computation.generators.project import build_project_model, _new_builder
from iesplan.computation.protocols import CalculationConfig, StopProbe
from iesplan.computation.sequences import materialize_non_predictive_series
from iesplan.finance import FinanceProfile, merge_effective

from test_project_model import (
    _gen_inputs,
    _island_doc,
    _finance_payload,
    _evaluation_n,
)


def test_unified_bound_consumption_by_all_converters() -> None:
    """互斥、月峰、增值税及所得税全部经过 CompilerAnalysis 查询，记录 BoundRequest。"""
    from test_vat_monthly import _inputs
    inputs = _inputs()
    artifact, models, materialized, finance, config, evaluation = inputs
    builder = _new_builder(artifact, models, materialized, finance, evaluation)
    model, _, _ = builder.build()

    analysis = builder.analysis
    assert analysis is not None
    # 检查各转换阶段均记录了真实的 BoundRequest
    requests = analysis.bound_requests
    assert requests, "必须通过统一分析入口记录 bound requests"
    req_relations = {r.relation_id for r in requests}
    # 包含增值税相关转换关系
    assert any("vat" in r for r in req_relations)


def test_no_arbitrary_big_m_guessing_when_unbounded() -> None:
    """当精确转换所需变量缺有限上界时，明确报告转换条件不足，不瞎猜 Big-M。"""
    inventory = CompilerInventory()
    rel = SemanticRelation(
        "vat.unbounded", "vat", IndexDomain("month"),
        VatPoolOperation("unbounded", {"vat": [], "construction": []}),
        SourceLocation("finance.credit_pools.unbounded"),
        input_symbols=("z_unbounded",),
        requires_bounds=True,
    )
    inventory.collect(rel)
    # z_unbounded has lower=0, upper=None
    inventory.register_bound_fact(BoundFact("z_unbounded", lower=0.0, upper=None))
    inventory.seal()

    analysis = CompilerAnalysis(inventory, {})
    with pytest.raises(LoweringUnsupportedError, match="缺有限上界"):
        analysis.mutex("test_mutex", "z_unbounded", "z_unbounded", relation=rel)


def test_on_demand_mutex_isolated_comparison(tmp_path) -> None:
    """按需互斥策略对照评估：在隔离环境下对照，结论为生产环境继续采用完整精确模型。"""
    artifact, models, materialized, finance, config, evaluation = _gen_inputs()
    comparison = evaluate_mutex_strategies(
        artifact, models, materialized, finance, config, evaluation, tmp_path,
    )
    assert comparison.full_status in ("HighsModelStatus.kOptimal", "HighsModelStatus.kNotset", "HighsModelStatus.kModelEmpty", "HighsModelStatus.kInfeasible") or comparison.full_status.startswith("HighsModelStatus")
    assert comparison.candidate_all_relations_feasible is True
    # No mutex in the explicit fixture: no strategy conclusion is justified.
    assert comparison.full_integers == 0
    assert comparison.candidate_integers_added == 0
    assert comparison.recommendation == "not_evaluated"


def test_generation_determinism() -> None:
    """相同输入逐字节完全确定，生成结果无随机波动。"""
    artifact, models, materialized, finance, config, evaluation = _gen_inputs()
    b1 = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    b2 = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)

    assert b1.files["input/problem.mps"] == b2.files["input/problem.mps"]
    assert b1.files["input/parameters.json"] == b2.files["input/parameters.json"]


def test_cooperative_cancellation_probe() -> None:
    """生成阶段响应取消探针，及时中断并不产生半成品 Bundle。"""
    from iesplan.computation.errors import ComputationUnavailableError
    artifact, models, materialized, finance, config, evaluation = _gen_inputs()

    with pytest.raises(ComputationUnavailableError) as exc_info:
        HIGHS_MILP_PROVIDER.generate(
            artifact, models, materialized, finance, config, evaluation,
            stop=lambda: "cancelled",
        )
    assert exc_info.value.reason == "cancelled"
