"""Compact analysis preserves the binder, exact bounds and source identities."""
from __future__ import annotations

import gc
from array import array

import pytest

from iesplan.computation.generators.bound_inference import BoundInferenceEngine
from iesplan.computation.generators.compiler_ir import (
    BoundFact,
    CompilerAnalysis,
    CompilerInventory,
    IndexDomain,
    RelationFamily,
    SourceLocation,
)
from iesplan.computation.generators.linear import LinearVariable
from iesplan.computation.generators.relation_storage import numeric_bytes
from iesplan.computation.generators.templates import bind_technical, compile_technical
from iesplan.modeling import BinaryNode, MathRelation, RefNode, RelationAst


def _chain(
    count: int, gains: tuple[float, ...] | None = None,
) -> tuple[CompilerInventory, RelationFamily]:
    inv = CompilerInventory()
    source = SourceLocation("models.cell.equations.state", instance_id="cell", relation_id="state")
    inv.step_columns[("cell", "s")] = tuple(f"state_{i}" for i in range(count))
    inv.register_bound_fact(BoundFact(
        "cell.s", lower=0.0, upper=float(count * 2), lower_source=source, upper_source=source,
    ))
    previous = RefNode("s", offset=-1)
    roles = {"s": "var", "delta": "known"}
    series = {"delta": tuple(1.25 if i % 2 else 0.5 for i in range(count))}
    if gains is not None:
        previous = BinaryNode("*", RefNode("gain"), previous)
        roles["gain"] = "known"
        series["gain"] = gains
    ast = RelationAst(
        "state", None, RefNode("s"),
        BinaryNode("+", previous, RefNode("delta")), (), (),
    )
    relation = MathRelation("state", None, ast)
    family = RelationFamily("cell.state", relation, IndexDomain("step", count), (), None, (), source)
    inv.relations.append(family)
    inv.technical_contexts[family.identity] = (
        compile_technical(relation, roles),
        {}, {"s": 0.0}, series,
        {"s": ("var", 0.0, float(count * 2))},
    )
    inv.seal()
    return inv, family


def test_cached_rows_are_exact_binder_rows_with_actual_coefficients_and_lags():
    gains = tuple((0.0, -0.5, 1.0, 0.5)[i % 4] for i in range(260))
    inv, family = _chain(260, gains)
    analysis = CompilerAnalysis(inv, {})
    template, _, initial, series, symbols = inv.technical_contexts[family.identity]
    expected_values = []
    previous = 0.0
    for gain, delta in zip(gains, series["delta"], strict=False):
        previous = gain * previous + delta
        expected_values.append(previous)
    for step in (0, 1, 254, 255, 256, 259):
        expected = bind_technical(
            template, instance="cell", step=step,
            known={name: values[step] for name, values in series.items()},
            initial=initial, bound={"s": symbols["s"][1:]},
            affine_column=lambda key, step=step: (inv.step_columns[("cell", "s")][step + key[1]], 0),
            affine_row_name=f"cell__state__t{step}",
        ).constraints
        assert analysis.technical_rows(family.identity, step) == expected
        expected_value = expected_values[step]
        assert analysis.variable_bounds(f"state_{step}") == (expected_value, expected_value)
        fact = analysis.bound_fact(f"state_{step}")
        assert fact.lower_source == fact.upper_source == family.source


@pytest.mark.parametrize("constant", [0.0, 1.0])
def test_projected_alias_cancellation_retains_raw_final_row_and_conflict(constant):
    inv = CompilerInventory()
    inv.property_bindings[("cell", "x")] = "q"
    inv.property_bindings[("cell", "y")] = "q"
    inv.register_bound_fact(BoundFact("q", lower=0.0, upper=10.0))
    ast = RelationAst(
        "alias", None, RefNode("x"), BinaryNode("+", RefNode("y"), RefNode("fixed")), (), (),
    )
    relation = MathRelation("alias", None, ast)
    family = RelationFamily(
        "cell.alias", relation, IndexDomain("step", 2), (), None, (), SourceLocation("alias"), "cell",
    )
    inv.relations.append(family)
    template = compile_technical(relation, {"x": "var", "y": "var", "fixed": "known"})
    inv.technical_contexts[family.identity] = (
        template, {"fixed": constant}, {}, {}, {"x": ("var", 0.0, 10.0), "y": ("var", 0.0, 10.0)},
    )
    inv.seal()
    engine = BoundInferenceEngine(inv, {})
    expected = bind_technical(
        template, instance="cell", step=1, known={"fixed": constant}, initial={},
        bound={"x": (0.0, 10.0), "y": (0.0, 10.0)},
        affine_column=lambda key: ("q", 0), affine_row_name="cell__alias__t1",
    ).constraints
    assert engine.technical_constraints(family.identity, 1) == expected
    assert bool(engine.conflicts) == bool(constant)
    assert len(engine.relations) == (2 if constant else 0)
    assert engine.bounds["q"] == (0.0, 10.0)
    if constant:
        assert engine.conflict_error().model_source["index"] == 0


def test_large_analysis_retains_numeric_payload_not_per_slot_gc_graph():
    # This asserts representation/size, not a machine-specific timing limit.
    # Shared names and sources are intentionally outside the numeric metric.
    gc.collect()
    before = len(gc.get_objects())
    count = 30000
    inv, family = _chain(count)
    engine = BoundInferenceEngine(inv, {})
    gc.collect()
    assert len(gc.get_objects()) - before < 150
    assert isinstance(engine.relations, array)
    assert isinstance(engine.equality_rows, array)
    assert len(engine.rows.origins) == 1
    assert len(engine.bounds.sources) == 2
    assert len(engine.rows.coefficients) == 2 * count - 1
    assert len(engine.technical_rows[family.identity].rows) == count
    storage_bytes = numeric_bytes((
        engine.bounds.lower, engine.bounds.upper,
        engine.bounds.lower_sources, engine.bounds.upper_sources,
        engine.bounds.has_sources,
        engine.rows.offsets, engine.rows.columns, engine.rows.coefficients,
        engine.rows.constants, engine.rows.senses, engine.rows.origin_slots, engine.rows.indices,
        engine.relations, engine.equality_rows, engine.inequalities,
        engine._inequalities_by_symbol.offsets, engine._inequalities_by_symbol.entries,
        engine.technical_rows[family.identity].rows,
    ))
    assert storage_bytes < 128 * count + 4096
    assert engine.propagation_stats.steps == count
    assert engine.propagation_stats.requeues == 0
    assert engine.bounds[f"state_{count - 1}"] == (26250.0, 26250.0)


def test_added_relation_reindexes_exact_expression_queries_and_unknown_slots():
    inv = CompilerInventory()
    inv.seal()
    engine = BoundInferenceEngine(inv, {})
    engine._add_affine({"x": 1.0, "y": -1.0}, -3.0, "le")
    assert engine.bound_affine({"x": 1.0, "y": -1.0}, 0.0, side="max") == 3.0
    assert engine.bound_affine({"x": 1.0, "y": -1.0}, 0.0, side="min") is None
    engine._add_affine({"x": 1.0, "y": -1.0}, -2.0)
    assert engine.bound_affine({"x": 1.0, "y": -1.0}, 0.0, side="min") == 2.0
    assert engine.bounds["x"] == engine.bounds["y"] == (None, None)


def test_missing_provenance_keeps_declared_alias_source_and_conflict_rollback():
    inv = CompilerInventory()
    origin = SourceLocation("planning.variables.q")
    inv.register_bound_fact(BoundFact("cell.capacity", 0.0, 10.0, origin, origin))
    inv.property_bindings[("cell", "capacity")] = "q"
    inv.seal()
    analysis = CompilerAnalysis(inv, {"q": LinearVariable("q", 0.0, 10.0)})
    assert "q" not in analysis.inference.provenance
    assert analysis.bound_fact("cell.capacity").upper_source == origin
    baseline = analysis.inference.bounds.snapshot()
    analysis.inference.provenance["q"] = (origin, origin)
    analysis.inference.bounds["q"] = (1.0, 1.0)
    analysis.inference.bounds.restore(baseline)
    assert "q" not in analysis.inference.provenance
    assert analysis.inference.bounds["q"] == (0.0, 10.0)


def test_numeric_fifo_compaction_preserves_reverse_chain_order():
    count = 66000  # Cross the consumed-prefix compaction boundary.
    inv = CompilerInventory()
    inv.register_bound_fact(BoundFact(f"s_{count}", lower=0.0, upper=0.0))
    inv.seal()
    engine = BoundInferenceEngine(inv, {})
    for step in range(count):
        engine._add_affine({f"s_{step}": 1.0, f"s_{step + 1}": -1.0}, 0.0, relation_id="chain")
    engine._propagate(budget=2 * count)
    assert engine.bounds["s_0"] == (0.0, 0.0)
    assert engine.propagation_stats.bound_updates == count
    assert engine.propagation_stats.requeues == count - 1
    assert engine.propagation_stats.steps == 2 * count - 1
    assert not engine.budget_exhausted
