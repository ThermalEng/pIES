"""P2 bound-propagation work counters and small convergence probes."""
from __future__ import annotations

from iesplan.computation.generators.bound_inference import (
    BoundInferenceEngine,
    PropagationStats,
)
from iesplan.computation.generators.compiler_ir import (
    BoundFact,
    CompilerInventory,
)


def _engine_with_bound(symbol: str, lower: float | None, upper: float | None) -> BoundInferenceEngine:
    inventory = CompilerInventory()
    inventory.register_bound_fact(BoundFact(symbol, lower=lower, upper=upper))
    inventory.seal()
    return BoundInferenceEngine(inventory, {})


def _add_step_relation(
    engine: BoundInferenceEngine,
    target: str,
    previous: str,
    *,
    coefficient: float = 1.0,
    increment: float = 1.0,
    relation_id: str,
) -> None:
    # target = coefficient * previous + increment
    engine._add_affine(
        {target: 1.0, previous: -coefficient},
        -increment,
        relation_id=relation_id,
    )


def test_forward_state_chain_records_self_requeues() -> None:
    """The current baseline exposes same-row re-entry on an ordered chain."""
    count = 128
    engine = _engine_with_bound("s_0", 0.0, 0.0)
    for step in range(1, count + 1):
        _add_step_relation(
            engine,
            f"s_{step}",
            f"s_{step - 1}",
            relation_id="state.forward",
        )

    engine._propagate(budget=count)

    assert engine.bounds[f"s_{count}"] == (float(count), float(count))
    stats = engine.propagation_stats
    assert isinstance(stats, PropagationStats)
    assert stats.relation_count == count
    assert stats.steps == 2 * count
    assert stats.bound_updates == count
    assert stats.requeues == count
    assert stats.self_requeues == count
    assert stats.dependent_requeues == 0
    assert stats.max_queue_size == count
    assert stats.budget_exhausted is False


def test_terminal_bound_reports_single_reverse_sweep_work() -> None:
    """A terminal bound may require one reverse pass after the initial scan."""
    count = 64
    engine = _engine_with_bound(f"s_{count}", float(count), float(count))
    for step in range(1, count + 1):
        _add_step_relation(
            engine,
            f"s_{step}",
            f"s_{step - 1}",
            relation_id="state.terminal",
        )

    engine._propagate(budget=2 * count)

    assert engine.bounds["s_0"] == (0.0, 0.0)
    stats = engine.propagation_stats
    assert count < stats.steps <= 3 * count
    assert stats.bound_updates == count
    assert stats.dependent_requeues >= count - 1
    assert stats.self_requeues >= count - 1
    assert stats.budget_exhausted is False


def test_near_identity_cycle_exposes_incomplete_work_without_losing_known_bounds() -> None:
    """A slowly contracting cycle is bounded by the budget and remains observable."""
    engine = _engine_with_bound("x", 0.0, 1.0)
    engine._add_affine(
        {"x": 1.0, "y": -0.999},
        0.0,
        relation_id="cycle.near_identity",
    )
    engine._add_affine(
        {"y": 1.0, "x": -0.999},
        0.0,
        relation_id="cycle.near_identity",
    )

    engine._propagate(budget=32)

    stats = engine.propagation_stats
    assert stats.budget_exhausted is True
    assert stats.steps == stats.step_limit
    assert stats.incomplete_symbols
    assert "x" in stats.incomplete_symbols or "y" in stats.incomplete_symbols
    assert engine.bounds["x"][1] is not None
    assert 0.0 < engine.bounds["x"][1] < 1.0


def test_budget_exhaustion_keeps_established_bounds_and_does_not_invent_limits() -> None:
    """Stopping early preserves known conservative facts and leaves unknowns open."""
    engine = _engine_with_bound("x", 0.0, 10.0)
    engine._add_affine(
        {"x": 1.0, "y": -0.5},
        0.0,
        relation_id="budget.cycle",
    )
    engine._add_affine(
        {"y": 1.0, "x": -0.5},
        0.0,
        relation_id="budget.cycle",
    )
    engine._add_affine(
        {"unknown": 1.0, "free": -1.0},
        0.0,
        relation_id="budget.unbounded",
    )

    engine._propagate(budget=1)

    x_lower, x_upper = engine.bounds["x"]
    assert x_lower == 0.0
    assert x_upper is not None and 0.0 <= x_upper <= 10.0
    assert engine.bounds["unknown"] == (None, None)
    assert engine.propagation_stats.budget_exhausted is True
