"""P1-S3: compiled technical/step templates, live coefficients and block continuity.

The frozen S2 reference is test-only and independently validates the public
and production S3 binding kernel. All application CI uses Docker.
"""
from __future__ import annotations

import gc
import json
import logging
import time
import tracemalloc

import pytest

from iesplan.computation.generators.compiler_ir import CompilerAnalysis
from iesplan.computation.generators.linear import (
    LinearConstraint, LinearExpression, LinearModel, LoweringUnsupportedError,
    lower_expression, lower_relation,
)
from iesplan.computation.generators.mps import write_mps
from iesplan.computation.generators.project import _ProjectBuilder
from iesplan.computation.generators.templates import compile_technical, bind_technical
from s2_reference_lowerer import lower_relation_s2

from test_battery_planning import battery, _rows
from test_linear_lowering import (
    _relation, HEATER_YAML, SOC_YAML, MUTEX_YAML, BILINEAR_YAML,
)
from test_project_model import _contribution
from test_p1_compiler_s1 import measured_generate


def _compiled(relation, symbols, *, step, initial=None):
    modes = {
        name: ("known" if entry[0] == "const" else "var")
        for name, entry in symbols.items()
    }
    template = compile_technical(relation, modes)
    return template, bind_technical(
        template, instance="synthetic", step=step,
        known={k: v[1] for k, v in symbols.items() if v[0] == "const"},
        initial=initial or {},
        bound={k: (v[1], v[2]) for k, v in symbols.items() if v[0] == "var"},
    )


@pytest.mark.parametrize("efficiency", [0.0, 0.5, 0.9, -1.25])
def test_affine_dynamic_coefficient_matches_legacy_at_each_step(efficiency):
    relation = _relation(HEATER_YAML, "conversion")
    symbols = {
        "efficiency": ("const", efficiency),
        "electricity_in": ("var", 0.0, None),
        "heat_out": ("var", 0.0, None),
    }
    template, actual = _compiled(relation, symbols, step=3)
    expected = lower_relation_s2(relation, symbols, step=3)
    assert template.fast_lhs is not None and template.fast_rhs is not None
    assert actual == expected
    assert lower_relation(relation, symbols, step=3) == actual


def test_same_template_live_step_coefficient_and_late_division_by_zero():
    # Same source structure compiled ONCE; values vary across steps.
    relation = _relation(
        HEATER_YAML.replace("electricity_in[t] * efficiency",
                            "electricity_in[t] / efficiency"),
        "conversion",
    )
    roles = {"heat_out": "var", "electricity_in": "var", "efficiency": "known"}
    template = compile_technical(relation, roles)
    bounds = {"heat_out": (0.0, None), "electricity_in": (0.0, None)}
    for step, value in enumerate((2.0, -4.0, 0.0)):
        if step == 2:
            with pytest.raises(LoweringUnsupportedError, match="除零.*|step 2.*除零"):
                bind_technical(
                    template, instance="x", step=step, known={"efficiency": value},
                    initial={}, bound=bounds,
                )
        else:
            symbols = {"heat_out": ("var", 0.0, None),
                       "electricity_in": ("var", 0.0, None),
                       "efficiency": ("const", value)}
            actual = bind_technical(
                template, instance="x", step=step,
                known={"efficiency": value}, initial={}, bound=bounds,
            )
            assert actual == lower_relation_s2(relation, symbols, step=step)
            assert lower_relation(relation, symbols, step=step) == actual


@pytest.mark.parametrize(
    ("body", "failed_symbol"),
    [
        ("(soc[t-1] / flow[t])^0", "flow"),
        ("(flow[t] / step_c)^0", "step_c"),
        ("((soc[t-1] / flow[t]) * 0)^0", "flow"),
    ],
)
def test_zero_exponent_must_evaluate_divisor_at_later_step(body, failed_symbol):
    # Both examples are parsed and built through the actual modeling
    # publication pipeline, not a hand-built AST. Previously the fast plan
    # discarded the base of x^0 and accepted a later zero denominator.
    yaml_doc = SOC_YAML.replace(
        "flow: {type: in", "flow: {type: predefined",
    ).replace(
        "soc[t] = soc[t-1] + flow[t] * step_c",
        f"soc[t] = {body}",
    )
    relation = _relation(yaml_doc, "soc_step")
    symbols = {
        "soc": ("var", 0.0, 1.0),
        "flow": ("const", 1.0),
        "step_c": ("const", 2.0),
    }
    initial = {"soc": 0.5}
    roles = {name: ("known" if entry[0] == "const" else "var")
             for name, entry in symbols.items()}
    template = compile_technical(relation, roles)
    # At t=0 both independent S2 and public/shared S3 produce the same row.
    first = bind_technical(
        template, instance="synthetic", step=0,
        known={"flow": 1.0, "step_c": 2.0}, initial=initial,
        bound={"soc": (0.0, 1.0)},
    )
    assert first == lower_relation_s2(relation, symbols, step=0, initial=initial)
    assert first == lower_relation(relation, symbols, step=0, initial=initial)
    failing = dict(symbols)
    failing[failed_symbol] = ("const", 0.0)
    with pytest.raises(LoweringUnsupportedError, match="除零"):
        lower_relation_s2(relation, failing, step=1, initial=initial)
    with pytest.raises(LoweringUnsupportedError, match="除零"):
        lower_relation(relation, failing, step=1, initial=initial)
    with pytest.raises(LoweringUnsupportedError, match="除零"):
        lower_expression(relation.ast.rhs_root, failing, step=1, initial=initial)
    with pytest.raises(LoweringUnsupportedError, match="除零") as raised:
        bind_technical(
            template, instance="device_a", step=1,
            known={"flow": 0.0 if failed_symbol == "flow" else 1.0,
                   "step_c": 0.0 if failed_symbol == "step_c" else 2.0},
            initial=initial, bound={"soc": (0.0, 1.0)},
        )
    message = str(raised.value)
    assert "soc_step" in message and "device_a" in message and "step 1" in message


def test_affine_initial_and_cross_step_offset_are_preserved():
    relation = _relation(SOC_YAML, "soc_step")
    symbols = {
        "soc": ("var", 0.0, 1.0),
        "flow": ("var", 0.0, None),
        "step_c": ("const", 2.5),
    }
    initial = {"soc": LinearExpression(
        terms=((("planned_capacity", 0), 0.4),), constant=0.1,
    )}
    for step in (0, 1, 255, 256, 8759, 8760):
        template, actual = _compiled(relation, symbols, step=step, initial=initial)
        expected = lower_relation_s2(relation, symbols, step=step, initial=initial)
        assert template.fast_lhs is not None
        assert actual == expected
        if step == 0:
            assert ("planned_capacity", 0) in dict(actual.constraints[0].expression.terms)
        else:
            assert ("soc", -1) in dict(actual.constraints[0].expression.terms)


def test_mutex_finite_zero_and_nonaffine_protection():
    relation = _relation(MUTEX_YAML, "exclusivity")
    for max_x in (0.0, 100.0):
        symbols = {
            "charge": ("var", 0.0, max_x),
            "discharge": ("var", 0.0, 200.0),
        }
        template, actual = _compiled(relation, symbols, step=1)
        assert template.lhs_mutex is not None
        assert actual == lower_relation_s2(relation, symbols, step=1)
    bad = _relation(BILINEAR_YAML, "bilin")
    symbols = {name: ("var", 0.0, None) for name in ("x", "y", "z")}
    template = compile_technical(bad, {name: "var" for name in symbols})
    with pytest.raises(LoweringUnsupportedError, match="变量×变量"):
        bind_technical(template, instance="b", step=0, known={},
                       initial={}, bound={k: (0.0, None) for k in symbols})


def _battery_model(builder):
    return LinearModel(
        variables=tuple(builder.variables.values()),
        constraints=tuple(builder.rows),
        objective=LinearExpression(), sense="minimize",
    )


def test_battery_block_sizes_preserve_full_model_and_real_columns(battery):
    models = [
        _rows(battery, power=6, block_size=size)
        for size in (1, 2, 256)
    ]
    a = _battery_model(models[0])
    for result in models:
        assert _battery_model(result) == a
        assert result.template_stats["technical_compiles"] == len(battery.relations)
        assert result.template_stats["technical_binds"] == (
            result.point_count * len(battery.relations)
        )
        assert len(result._step_columns[("cell", "stored_energy")]) == 2
    assert write_mps(_battery_model(models[0])) == write_mps(_battery_model(models[2]))


def test_two_instances_share_structure_not_values_and_cross_calendar_year():
    tank = _contribution(SOC_YAML)
    steps = 17520  # two complete 8760-hour operating years, 2026 and 2027
    doc = {"planning": {"variables": {},
                        "objective": {"sense": "minimize", "expression": "0"}},
           "connections": {}, "finance_binding": {"instances": {}},
           "tariff_bindings": {}, "demand_bindings": {}}
    devices = {
        inst: {"definition": {"id": tank.device_id}, "asset_origin": "existing",
               "properties": {
                   "soc0": {"value": initial, "unit": "1"},
                   "step_c": {"value": factor, "unit": "1"},
               }}
        for inst, initial, factor in (("a", 0.2, 1.0), ("b", 0.7, 0.5))
    }
    builder = _ProjectBuilder(
        doc=doc, devices=devices, models={"a": tank, "b": tank},
        series=(), finance=None, evaluation=None,
        point_count=steps, step_s=3600,
    )
    builder.template_block_size = 127
    builder._declare_global_symbols()
    bound = {inst: builder._bind_instance_symbols(inst) for inst in sorted(devices)}
    builder.inventory.seal()
    builder.analysis = CompilerAnalysis(builder.inventory, builder.variables)
    builder._compile_templates(bound)
    for inst in sorted(devices):
        builder._expand_instance(inst, bound[inst])
        builder._build_relations(inst, bound[inst])
    assert builder.template_stats["technical_compiles"] == len(tank.relations)
    assert builder.template_stats["technical_binds"] == 2 * steps * len(tank.relations)
    for inst, initial, factor in (("a", 0.2, 1.0), ("b", 0.7, 0.5)):
        first = next(r for r in builder.rows if r.name == f"{inst}__soc_step__t0")
        assert first.expression.constant == pytest.approx(-initial)
        last = next(r for r in builder.rows
                    if r.name == f"{inst}__soc_step__t8760")
        assert dict(last.expression.terms)[(f"{inst}__soc__t8759", 0)] == -1.0
        assert dict(last.expression.terms)[(f"{inst}__flow__t8760", 0)] == -factor
    assert len(builder._step_columns[("a", "soc")]) == steps
    assert len(builder._step_columns[("b", "soc")]) == steps
    assert builder._step_columns[("a", "soc")][8759] != (
        builder._step_columns[("b", "soc")][8759]
    )


def _reference_technical_benchmark(builder):
    """Same-run S2 AST loop vs S3 template BIND ONLY, no duplicate full model.

    Measures isolated *binding* kernels with the same actual campus input,
    Docker process, tracemalloc methodology and step count. Both exclude final
    row allocation / MPS. Never mislabel this as an S2 full-build benchmark.
    """
    from s2_reference_lowerer import lower_relation_s2
    from iesplan.core.contracts.baseline import TIMELINE_STEP_DURATION_VAR

    inputs = []
    for inst in sorted(builder.models):
        model = builder.models[inst]
        if not model.relations:
            continue
        static = {TIMELINE_STEP_DURATION_VAR: ("const", builder.step_s)}
        known = {TIMELINE_STEP_DURATION_VAR: builder.step_s}
        for name, var in model.variables.items():
            if var.kind == "property":
                if (inst, name) in builder.bound_prop:
                    key = builder.bound_prop[(inst, name)]
                    col = builder.variables[key]
                    static[name] = ("var", col.lower, col.upper)
                else:
                    value = builder._prop_const(inst, name)
                    static[name] = ("const", value)
                    known[name] = value
            else:
                colname = builder._step_columns[(inst, name)][0]
                col = builder.variables[colname]
                static[name] = ("var", col.lower, col.upper)
        series = {}
        for iface, item in model.interfaces.items():
            if item.type == "predefined":
                series[iface] = builder._series_si(inst, iface, model)
            elif (inst, iface) in builder._step_columns:
                col = builder.variables[builder._step_columns[(inst, iface)][0]]
                static[iface] = ("var", col.lower, col.upper)
        initial = {
            op.payload.state: op.payload.value
            for op in builder.analysis.relations("initial")
            if op.payload.instance == inst
        }
        bounds = {k: (v[1], v[2]) for k, v in static.items() if v[0] == "var"}
        inputs.append((inst, model, static, known, series, initial, bounds))

    def sample(kind):
        tracemalloc.start()
        try:
            t0 = time.perf_counter()
            rows = 0
            aux = 0
            for inst, model, static, known, series, initial, bounds in inputs:
                for step in range(builder.point_count):
                    if kind == "legacy":
                        values = dict(static)
                        for name, sequence in series.items():
                            values[name] = ("const", sequence[step])
                        values = builder.analysis.modeling_symbols(inst, step, values)
                    else:
                        for name, sequence in series.items():
                            known[name] = sequence[step]
                    for family in builder._instance_relations[inst]:
                        if kind == "legacy":
                            lowered = lower_relation_s2(
                                family.relation, values, step=step, initial=initial,
                            )
                        else:
                            lowered = bind_technical(
                                builder._technical_templates[family.identity],
                                instance=inst, step=step, known=known,
                                initial=initial, bound=bounds,
                            )
                        rows += len(lowered.constraints)
                        aux += len(lowered.variables)
            wall = round((time.perf_counter() - t0) * 1000, 3)
            _current, peak = tracemalloc.get_traced_memory()
            return {"bind_wall_ms": wall, "traced_peak_bytes": peak,
                    "lowered_rows": rows, "auxiliary_variables": aux}
        finally:
            tracemalloc.stop()
    old = sample("legacy")
    new = sample("template")
    assert old["lowered_rows"] == new["lowered_rows"]
    assert old["auxiliary_variables"] == new["auxiliary_variables"]
    return {"s2_ast_kernel": old, "s3_template_kernel": new}



class _S2ReferenceBuilder(_ProjectBuilder):
    """Test-only frozen S2 per-step technical/step paths, never production fallback."""

    def _compile_templates(self, bound_instances):
        # All other builder stages stay IDENTICAL within this Docker process.
        pass

    def _build_relations(self, inst, statics):
        model = self.models[inst]
        symbols = statics["symbols"]
        initial = {
            op.payload.state: op.payload.value
            for op in self.analysis.relations("initial")
            if op.payload.instance == inst
        }
        predefined = {
            name for name, flow in model.interfaces.items()
            if flow.type == "predefined"
        }
        for step in range(self.point_count):
            self._checkpoint()
            current = dict(symbols)
            for name in predefined:
                current[name] = ("const", self._series_si(inst, name, model)[step])
            current = self.analysis.modeling_symbols(inst, step, current)
            for family in self._instance_relations[inst]:
                lowered = lower_relation_s2(
                    family.relation, current, step=step, initial=initial,
                )
                self._emit_lowered(inst, family.relation.id, step, lowered)

    def _build_step_relations(self):
        from iesplan.computation.generators.project import _AffineAcc
        for operation in self.analysis.relations("planning_step"):
            spec = operation.payload
            template = spec.template
            references = spec.references
            for step in range(self.point_count):
                self._checkpoint()
                acc = _AffineAcc()
                acc.add_const(template.expression.constant)
                for (name, offset), coefficient in template.expression.terms:
                    if offset:
                        raise LoweringUnsupportedError(
                            f"step relation {spec.name!r} has an unsupported offset"
                        )
                    if name in references:
                        target = references[name]
                        key = (target["instance"], target["interface"])
                        column = self._step_columns[key][step]
                    elif name in self.plan_vars:
                        column = name
                    else:
                        raise LoweringUnsupportedError(
                            f"step relation {spec.name!r}: unknown symbol {name!r}"
                        )
                    acc.add_term(column, coefficient)
                self.rows.append(
                    LinearConstraint(
                        name=f"step__{spec.name}__t{step}",
                        expression=acc.freeze(), sense=template.sense,
                        rhs=template.rhs,
                    )
                )


def test_same_docker_s2_s3_full_campus_build_and_exact_mps(tmp_path):
    """One public 8760-point input, same Docker Python, same builder+tracer.

    S2 is a test-only reference preserving its AST and step-row operations;
    no old builder/fallback reaches production. Both builds carry all other
    S2 semantic/tax paths. This measures full model generation (not MPS
    writing or worker execution) with independent tracemalloc peaks.
    """
    from test_highs_milp_provider import _campus_inputs

    artifact, models, materialized, finance, config, evaluation = _campus_inputs()
    from iesplan.computation.generators import project
    def build_once(builder_type):
        gc.collect()
        # Use the public builder factory without changing inputs or horizon.
        # The class is selected only here in the reference test harness.
        from unittest.mock import patch
        with patch.object(project, "_ProjectBuilder", builder_type):
            tracemalloc.start()
            try:
                start = time.perf_counter()
                builder = project._new_builder(
                    artifact, models, materialized, finance, evaluation,
                )
                model, units, objective_unit = builder.build()
                wall_ms = round((time.perf_counter() - start) * 1000, 3)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        metrics = {
            "build_wall_ms": wall_ms,
            "traced_peak_bytes": peak,
            "columns": len(model.variables),
            "rows": len(model.constraints),
            "integer_columns": sum(v.kind == "binary" for v in model.variables),
            "row_nonzeros": sum(len(row.expression.terms) for row in model.constraints),
            "objective_nonzeros": len(model.objective.terms),
        }
        return model, units, objective_unit, builder, metrics

    old, old_units, old_unit, old_builder, old_metrics = build_once(
        _S2ReferenceBuilder,
    )
    old_mps, old_offset = write_mps(old)
    old_blob = (tmp_path / "s2-reference.mps")
    old_blob.write_text(old_mps, encoding="utf-8")
    old_facts = old_builder.financial_facts()
    old_components = old_builder.finance_components()
    # Do not retain the S2 full-series/column maps in the Python heap when
    # measuring S3's tracemalloc peak: serialize the exact comparison facts.
    (tmp_path / "s2-units.json").write_text(
        json.dumps(dict(old_units), sort_keys=True), encoding="utf-8",
    )
    (tmp_path / "s2-series.json").write_text(
        json.dumps(old_builder.step_series(), sort_keys=True), encoding="utf-8",
    )
    del old, old_builder, old_mps, old_units
    gc.collect()

    current, units, objective_unit, builder, current_metrics = build_once(
        _ProjectBuilder,
    )
    current_mps, current_offset = write_mps(current)
    assert current_metrics["columns"] == old_metrics["columns"]
    assert current_metrics["rows"] == old_metrics["rows"]
    assert current_metrics["integer_columns"] == old_metrics["integer_columns"]
    assert current_metrics["row_nonzeros"] == old_metrics["row_nonzeros"]
    assert current_metrics["objective_nonzeros"] == old_metrics["objective_nonzeros"]
    assert current_offset == old_offset
    assert objective_unit == old_unit
    assert dict(units) == json.loads(
        (tmp_path / "s2-units.json").read_text(encoding="utf-8")
    )
    assert current_mps == old_blob.read_text(encoding="utf-8")
    assert builder.financial_facts() == old_facts
    assert builder.finance_components() == old_components
    assert list(builder.step_series()) == json.loads(
        (tmp_path / "s2-series.json").read_text(encoding="utf-8")
    )
    assert builder.template_stats["technical_binds"] > 0
    logging.warning("P1_S3_SAME_DOCKER_BUILD %s", json.dumps({
        "source": "public campus 8760 points, unchanged complete model",
        "s2_reference": old_metrics,
        "s3_production": current_metrics,
        "timing_note": "both model builds traced separately; MPS/output comparisons are outside timers",
        "resource_scope": "tracemalloc full build, not isolated process RSS",
        "basis": "test-only S2 technical/step kernels, all other S2 domains shared",
    }, sort_keys=True))


def test_public_campus_production_template_counts_and_resource_comparison(measured_generate):
    bundle, builder, full = measured_generate
    count = sum(len(builder.models[inst].relations) for inst in builder.models)
    assert builder.inventory.sealed
    assert builder.template_stats["technical_binds"] == builder.point_count * count
    assert 0 < builder.template_stats["technical_compiles"] <= count
    assert builder.template_stats["step_compiles"] == len(
        builder.analysis.relations("planning_step")
    )
    assert builder.template_stats["step_binds"] == (
        builder.point_count * builder.template_stats["step_compiles"]
    )
    assert len(bundle.files["input/problem.mps"]) == full["mps_bytes"]
    comparison = _reference_technical_benchmark(builder)
    metrics = {
        **full,
        "template_stats": builder.template_stats,
        "same_run_s2_s3_technical_bind_comparison": comparison,
        "full_build_s2_comparison_note":
            "Historical P1-S2 CI logs, not a second S2 full-model build in this run",
        "live_full_horizon": True,
    }
    logging.warning("P1_S3_BASELINE %s", json.dumps(metrics, sort_keys=True))
