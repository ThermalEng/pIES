"""N2: complete port boundaries, same analysis/rows/source and public solves."""
from __future__ import annotations

import json
import subprocess
from copy import deepcopy

import pytest
from bundle_testkit import content_bytes, materialized_file
from test_highs_result_adapter import _receipt
from test_p3_port_network import SOURCE
from test_project_model import HEATER_YAML, LOAD_YAML, _evaluation_n, _finance_payload, _island_doc

from iesplan.application.tasks.submissions import _check_network_capability
from iesplan.assembly import build_port_network, validate_assembly_doc
from iesplan.assembly.context import CheckContext
from iesplan.assembly.rules.solvability import run_phase_d
from iesplan.assembly.schema import AssemblyDevice, AssemblyEdge, AssemblySpec
from iesplan.computation import CalculationConfig, materialize_non_predictive_series
from iesplan.computation.generators.highs_milp import DESCRIPTOR, HIGHS_MILP_PROVIDER
from iesplan.computation.generators.project import _new_builder
from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.finance import FinanceProfile, merge_effective
from iesplan.modeling import build_math_contribution, contribution_to_dict
from iesplan.tasks.contracts import InvalidRequestError

BOUNDARIES = """
schema: ies.device-model
schema_version: "1.0.0"
device: {id: acme.device.boundaries, names: {zh-CN: 边界, en-US: Boundaries}}
properties:
  initial_count: {value: 2, unit: "1", valid_range: {minimum: 0, maximum: null}}
interfaces:
  take: {type: in, carrier: gas, unit: kW, valid_range: {minimum: 0, maximum: 50}}
  give: {type: out, carrier: gas, unit: kW, valid_range: {minimum: 0, maximum: 50}}
  exchange: {type: bidirectional, carrier: gas, unit: kW, valid_range: {minimum: -50, maximum: 50}}
  signal: {type: predefined, carrier: gas, unit: "1", valid_range: {minimum: 0, maximum: null}}
  observation: {type: blind, carrier: gas, unit: "1", valid_range: {minimum: 0, maximum: null}}
equations:
  variables:
    count: {unit: "1", valid_range: {minimum: 0, maximum: null}, initial: {property_ref: initial_count}}
  relations:
    - {id: counter, expression: "count[t] = count[t-1] + signal[t]"}
"""


def _case(texts, connections, predefined=None):
    doc = _island_doc()
    doc["devices"], documents, models = {}, {}, {}
    for inst, text in texts.items():
        raw = yaml_load(text)
        if not models:
            # The public planning contract requires a scale. Keep this fixture
            # scalar fixed and independent of the physical boundary probe.
            raw["properties"]["fixture_scale"] = {
                "value": 1, "unit": "1", "valid_range": {"minimum": 1, "maximum": 1},
            }
        parsed = parse_device_model_v2(raw, file=f"{inst}.yaml")
        assert parsed.ok, parsed.diagnostics
        document = parsed.document
        documents[document.device.id] = document
        built = build_math_contribution(document)
        assert built.ok, built.diagnostics
        models[inst] = built.contribution
        doc["devices"][inst] = {
            "definition": {"id": document.device.id}, "asset_origin": "existing",
            "properties": {}, "predefined_interfaces": (predefined or {}).get(inst, {}),
        }
    doc["connections"] = connections
    doc["finance_binding"] = {"instances": {inst: {"type": "none"} for inst in models}}
    doc["tariff_bindings"], doc["demand_bindings"] = {}, {}
    doc["planning"] = {"objective": {"sense": "minimize", "expression": "0"},
                       "variables": {"fixture_scale": {
                           "device_ref": next(iter(texts)), "parameter": "fixture_scale", "unit": "1",
                           "lower_bound": "1", "upper_bound": "1",
                       }}, "constraints": {}}
    doc["outputs"] = {"series": [], "metrics": []}
    doc["extensions"] = {}
    finance = merge_effective(FinanceProfile.from_dict(_finance_payload()), None)
    validated = validate_assembly_doc(
        doc, device_documents=documents, finance_snapshots={"obj-finance": finance},
    )
    assert validated.ok, [(d.code, dict(d.params)) for d in validated.diagnostics]
    materialized = materialize_non_predictive_series(validated.artifact, {}, operation_years=1)
    config = CalculationConfig(
        mode="planning", generator=HIGHS_MILP_PROVIDER.ref,
        solver="ies.solver.highs@1.7.2", precision="medium",
        time_limit_seconds=30, outputs=["solution"],
    )
    return (validated.artifact, models, materialized, finance, config, _evaluation_n(1)), doc, documents


def _solve(bundle, tmp_path):
    (tmp_path / "problem.mps").write_bytes(materialized_file(bundle, "input/problem.mps"))
    process = subprocess.run(
        ["/usr/local/bin/ies-highs-solver", "problem.mps", "--solution_file", "values.sol",
         "--time_limit", "30", "--random_seed", "0"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert process.returncode == 0, process.stderr
    return HIGHS_ADAPTER.adapt(
        bundle, _receipt(), {"output/solution.sol": (tmp_path / "values.sol").read_bytes()},
    )


def test_shared_network_partition_includes_sorted_closed_ports():
    ports = {"s.give": "heat", "l.take": "heat", "x.take": "gas", "x.give": "gas"}
    edges = {"delivery": ("s.give", "l.take")}
    network = build_port_network(ports, edges)
    assert network.closed_ports == ("x.give", "x.take")
    assert network.buses[0].port_refs == ("l.take", "s.give")
    assert build_port_network(dict(reversed(list(ports.items()))), edges) == network
    assert build_port_network({}, {}).closed_ports == ()


def test_unreferenced_ports_close_but_predefined_blind_and_state_do_not(tmp_path):
    inputs, _, _ = _case({"unit": BOUNDARIES}, {}, {
        "unit": {"signal": {"mode": "constant", "value": 1}},
    })
    artifact, models, series, finance, _, evaluation = inputs
    builder = _new_builder(artifact, models, series, finance, evaluation)
    model, _, _ = builder.build()
    operations = builder.analysis.relations("closed_port")
    assert {op.input_symbols for op in operations} == {("unit.take",), ("unit.give",), ("unit.exchange",)}
    for name in ("take", "give", "exchange"):
        assert builder.analysis.variable_bounds(f"unit__{name}__t0") == (0, 0)
        rows = [r for r in model.constraints if r.name.startswith(f"closed__unit__{name}__")]
        assert len(rows) == 8760
        for step in (0, 255, 256, 8759):
            row = next(r for r in rows if r.name == f"closed__unit__{name}__t{step}")
            assert row.sense == "eq" and row.rhs == 0
            assert row.expression.terms == (((f"unit__{name}__t{step}", 0), 1),)
    bundle = HIGHS_MILP_PROVIDER.generate(*inputs)
    result = _solve(bundle, tmp_path)
    assert result.math_status == "solved"
    curves = result.outputs["series"]
    for name in ("take", "give", "exchange"):
        assert all(value == 0 for value in curves[f"unit.{name}"]["values"])
    assert curves["unit.count"]["values"][0] == 3
    assert curves["unit.count"]["values"][-1] == 8762
    params = json.loads(materialized_file(bundle, "input/parameters.json"))
    sources = params["model_sources"]["sources"]
    for op in operations:
        source = sources[f"relation:{op.identity}"]
        assert source["location"]["field_path"] == "devices.unit"
        assert source["location"]["binding_id"] == op.input_symbols[0]
        group = next(g for g in params["model_sources"]["groups"]
                     if g["source"] == f"relation:{op.identity}" and g["role"] == "row:closed_port")
        assert group["indices"] == {"start": 0, "stride": 1, "count": 8760}


@pytest.mark.parametrize("carrier", ["electricity", "gas"])
@pytest.mark.parametrize("external_source", [False, True])
def test_missing_external_supply_is_not_free_energy(tmp_path, carrier, external_source):
    texts = {"heater": HEATER_YAML.replace("carrier: electricity", f"carrier: {carrier}"), "load": LOAD_YAML}
    links = {"heat": {"from": "heater.heat_out", "to": "load.heat_in"}}
    if external_source:
        texts["source"] = SOURCE.replace("electricity", carrier)
        links["supply"] = {"from": "source.power_out", "to": "heater.electricity_in"}
    inputs, _, _ = _case(texts, links, {"load": {"heat_demand": {"mode": "constant", "value": 4}}})
    bundle = HIGHS_MILP_PROVIDER.generate(*inputs)
    result = _solve(bundle, tmp_path)
    assert result.math_status == ("solved" if external_source else "infeasible")
    if external_source:
        assert result.outputs["series"]["heater.electricity_in"]["values"][0] == pytest.approx(4 / 0.9)
    else:
        sources = json.loads(materialized_file(bundle, "input/parameters.json"))["model_sources"]["sources"]
        assert sources["relation:network.closed.heater.electricity_in"]["location"]["binding_id"] == (
            "heater.electricity_in"
        )


def test_different_port_domains_balance_totals_not_pairwise_values(tmp_path):
    two = SOURCE.replace("power_out[t] = dispatch[t]", "power_out[t] = 2000")
    two = two.replace("maximum: 20}}\n  unused", "maximum: 2}}\n  unused", 1).replace(
        "power_out: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0",
        "power_out: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 2", 1,
    )
    three = SOURCE.replace("acme.device.network_source", "acme.device.second_source").replace(
        "power_out[t] = dispatch[t]", "power_out[t] = 3000",
    )
    three = three.replace("maximum: 20}}\n  unused", "maximum: 3}}\n  unused", 1).replace(
        "power_out: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0",
        "power_out: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 3", 1,
    )
    load = LOAD_YAML.replace("heat", "electricity").replace(
        "electricity_in: {type: in, carrier: electricity, unit: kW, "
        "valid_range: {minimum: 0, maximum: null}}",
        "electricity_in: {type: in, carrier: electricity, unit: kW, "
        "valid_range: {minimum: 5, maximum: 5}}",
    )
    texts = {"two": two, "three": three, "load": load}
    inputs, doc, documents = _case(texts, {
        "a": {"from": "two.power_out", "to": "load.electricity_in"},
        "b": {"from": "three.power_out", "to": "load.electricity_in"},
    }, {"load": {"electricity_demand": {"mode": "constant", "value": 5}}})
    bundle = HIGHS_MILP_PROVIDER.generate(*inputs)
    result = _solve(bundle, tmp_path)
    assert result.math_status == "solved"
    assert result.outputs["series"]["two.power_out"]["values"][0] == 2
    assert result.outputs["series"]["three.power_out"]["values"][0] == 3
    # Reordering documents/edges cannot change the network or matrix bytes.
    reversed_doc = deepcopy(doc)
    for key in ("devices", "connections"):
        reversed_doc[key] = dict(reversed(list(doc[key].items())))
    checked = validate_assembly_doc(reversed_doc, device_documents=documents,
                                    finance_snapshots={"obj-finance": inputs[3]})
    assert checked.ok, checked.diagnostics
    again = HIGHS_MILP_PROVIDER.generate(checked.artifact, *inputs[1:])
    assert content_bytes(again.files) == content_bytes(bundle.files)


@pytest.mark.parametrize("direction", ["out", "in"])
def test_one_direction_only_bus_is_not_an_input_infeasibility_proof(direction):
    text = SOURCE.replace("type: out", f"type: {direction}")
    document = parse_device_model_v2(yaml_load(text)).document
    spec = AssemblySpec(
        devices=[AssemblyDevice("a", document.device.id), AssemblyDevice("b", document.device.id)],
        edges=[AssemblyEdge("join", "a.power_out", "b.power_out")],
    )
    diagnostics, summaries = run_phase_d(
        spec, CheckContext(registry={document.device.id: document}, period_steps=8760),
    )
    assert len(summaries) == 1
    assert not any(d.blocking for d in diagnostics)


def test_closure_capability_uses_actual_frozen_ports_and_precise_location():
    inputs, _, _ = _case({"unit": BOUNDARIES}, {}, {"unit": {"signal": {"mode": "constant", "value": 1}}})
    artifact, models = inputs[:2]
    frozen = {inst: contribution_to_dict(model) for inst, model in models.items()}
    _check_network_capability(artifact, frozen, DESCRIPTOR.ref, DESCRIPTOR.capabilities)
    with pytest.raises(InvalidRequestError) as error:
        _check_network_capability(artifact, frozen, "other.generator@1.0.0", ("affine",))
    assert error.value.code == "TASK-REQ-007"
    assert error.value.params["port_refs"] == ["unit.exchange", "unit.give", "unit.take"]
    assert error.value.params["fields"] == ["devices.unit"]
    assert error.value.location == {
        "object_type": "device", "object_id": "unit", "field": "interfaces.exchange",
    }
    # Only predefined/blind/internal symbols do not demand network closure.
    no_ports = deepcopy(frozen)
    no_ports["unit"]["interfaces"] = {
        k: v for k, v in frozen["unit"]["interfaces"].items() if v["type"] in ("blind", "predefined")
    }
    _check_network_capability(artifact, no_ports, "other.generator@1.0.0", ())
