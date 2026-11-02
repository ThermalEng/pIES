"""P3-N1: actual port boundaries, shared assembly/generation and real solves."""
from __future__ import annotations

import json
import subprocess

import pytest
from test_highs_result_adapter import _receipt
from test_project_model import LOAD_YAML, _evaluation_n, _finance_payload, _island_doc

from iesplan.assembly import build_port_buses, validate_assembly_doc
from iesplan.assembly.context import CheckContext
from iesplan.assembly.rules.solvability import build_buses, run_phase_d
from iesplan.assembly.schema import AssemblyDevice, AssemblyEdge, AssemblySpec
from iesplan.computation import CalculationConfig, materialize_non_predictive_series
from iesplan.computation.generators.highs_milp import HIGHS_MILP_PROVIDER
from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices import list_devices
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.finance import FinanceProfile, merge_effective
from iesplan.modeling import build_math_contribution

SOURCE = """
schema: ies.device-model
schema_version: "1.0.0"
device: {id: acme.device.network_source, names: {zh-CN: 测试电源, en-US: Test source}}
properties: {}
interfaces:
  power_out: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: 20}}
  unused: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: 20}}
equations:
  variables:
    dispatch: {unit: kW, valid_range: {minimum: 0, maximum: 20}}
  relations:
    - id: output
      expression: "power_out[t] = dispatch[t]"
"""

ROUTE = """
schema: ies.device-model
schema_version: "1.0.0"
device: {id: acme.device.network_route, names: {zh-CN: 限额传输, en-US: Limited transfer}}
properties:
  capacity: {value: 5, unit: kW, valid_range: {minimum: 0, maximum: 20}}
  efficiency: {value: 0.8, unit: "1", valid_range: {minimum: 0.1, maximum: 1}}
interfaces:
  power_in: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  power_out:
    type: out
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: {property_ref: capacity}}
equations:
  variables: {}
  relations:
    - id: transfer
      expression: "power_out[t] = efficiency * power_in[t]"
"""


def _inputs(demand: float = 4.0, parallel_demand: float | None = None):
    yamls = {
        "source": SOURCE,
        "route": ROUTE,
        "load": LOAD_YAML.replace("acme.device.load", "acme.device.network_load").replace(
            "heat", "electricity",
        ),
    }
    documents, models = {}, {}
    doc = _island_doc()
    doc["assembly"] = {"id": "port_network", "name": "Port boundary probe"}
    doc["devices"] = {}
    for inst, text in yamls.items():
        parsed = parse_device_model_v2(yaml_load(text), file=f"{inst}.yaml")
        assert parsed.ok, parsed.diagnostics
        document = parsed.document
        documents[document.device.id] = document
        built = build_math_contribution(document)
        assert built.ok, built.diagnostics
        models[inst] = built.contribution
        doc["devices"][inst] = {
            "definition": {"id": document.device.id}, "asset_origin": "existing",
            "properties": {},
            "predefined_interfaces": ({
                "electricity_demand": {"mode": "constant", "value": demand},
            } if inst == "load" else {}),
        }
    doc["connections"] = {
        "feed": {"from": "source.power_out", "to": "route.power_in"},
        "delivery": {"from": "route.power_out", "to": "load.electricity_in"},
    }
    if parallel_demand is not None:
        models["load_two"] = models["load"]
        doc["devices"]["load_two"] = {
            **doc["devices"]["load"],
            "predefined_interfaces": {
                "electricity_demand": {"mode": "constant", "value": parallel_demand},
            },
        }
        doc["connections"]["delivery_two"] = {
            "from": "route.power_out", "to": "load_two.electricity_in",
        }
    doc["finance_binding"] = {"instances": {inst: {"type": "none"} for inst in models}}
    doc["tariff_bindings"] = {}
    doc["demand_bindings"] = {}
    doc["planning"] = {
        "objective": {"sense": "minimize", "expression": "route_capacity"},
        "variables": {"route_capacity": {
            "device_ref": "route", "parameter": "capacity", "unit": "kW",
            "lower_bound": "5", "upper_bound": "5",
        }}, "constraints": {},
    }
    doc["outputs"] = {"series": ["source.power_out", "route.power_out"], "metrics": []}
    doc["extensions"] = {}
    finance = merge_effective(FinanceProfile.from_dict(_finance_payload()), None)
    result = validate_assembly_doc(
        doc, device_documents=documents, finance_snapshots={"obj-finance": finance},
    )
    assert result.ok, [(d.code, dict(d.params)) for d in result.diagnostics]
    materialized = materialize_non_predictive_series(result.artifact, {}, operation_years=1)
    config = CalculationConfig(
        mode="planning", generator=HIGHS_MILP_PROVIDER.ref,
        solver="ies.solver.highs@1.7.2", precision="medium",
        time_limit_seconds=30, outputs=["solution"],
    )
    return (result.artifact, models, materialized, finance, config, _evaluation_n(1)), documents


def test_same_device_same_carrier_does_not_connect_ports() -> None:
    buses = build_port_buses(
        {"s.out": "electricity", "r.in": "electricity",
         "r.out": "electricity", "l.in": "electricity"},
        {"up": ("s.out", "r.in"), "down": ("r.out", "l.in")},
    )
    assert {bus.port_refs for bus in buses} == {("r.in", "s.out"), ("l.in", "r.out")}
    assert {bus.connection_ids for bus in buses} == {("up",), ("down",)}


def test_shared_port_mixes_once_and_partition_is_order_independent() -> None:
    ports = {"s.out": "heat", "a.in": "heat", "b.in": "heat", "s.unused": "heat"}
    links = {"a": ("s.out", "a.in"), "b": ("s.out", "b.in")}
    buses = build_port_buses(ports, links)
    assert len(buses) == 1
    assert buses[0].port_refs == ("a.in", "b.in", "s.out")
    assert build_port_buses(dict(reversed(list(ports.items()))), dict(reversed(list(links.items())))) == buses


def test_disconnected_ports_do_not_implicitly_form_a_bus() -> None:
    assert build_port_buses({"device.in": "electricity", "device.out": "electricity"}, {}) == ()


def test_grid_summary_does_not_turn_its_other_port_into_a_source_or_sink() -> None:
    registry = {document.device.id: document for document in list_devices()}
    spec = AssemblySpec(
        devices=[
            AssemblyDevice("grid", "ies.device.grid_connection", properties={
                "max_import_power_kw": {"value": 800, "unit": "kW"},
                "max_export_power_kw": {"value": 200, "unit": "kW"},
            }),
            AssemblyDevice("pv", "ies.device.pv"),
            AssemblyDevice("load", "ies.device.electric_load"),
        ],
        edges=[
            AssemblyEdge("purchase", "grid.electricity_import", "load.electricity_in"),
            AssemblyEdge("sale", "pv.electric_out", "grid.electricity_export"),
        ],
    )
    _diags, summaries = run_phase_d(spec, CheckContext(registry=registry, period_steps=8760))
    assert len(summaries) == 2
    purchase = next(bus for bus in summaries if "grid.electricity_import" in bus.port_refs)
    sale = next(bus for bus in summaries if "grid.electricity_export" in bus.port_refs)
    assert purchase.source_port_refs == ["grid.electricity_import"]
    assert purchase.sink_port_refs == ["load.electricity_in"]
    assert sale.source_port_refs == ["pv.electric_out"]
    assert sale.sink_port_refs == ["grid.electricity_export"]


def test_assembly_and_generation_share_actual_port_membership() -> None:
    from iesplan.computation.generators.project import _new_builder

    inputs, documents = _inputs()
    artifact, models, series, finance, _config, evaluation = inputs
    doc = json.loads(artifact.canonical_text)
    spec = AssemblySpec(
        devices=[AssemblyDevice(inst, node["definition"]["id"]) for inst, node in doc["devices"].items()],
        edges=[AssemblyEdge(key, node["from"], node["to"]) for key, node in doc["connections"].items()],
    )
    summaries = build_buses(spec, CheckContext(registry=documents))
    builder = _new_builder(artifact, models, series, finance, evaluation)
    model, _, _ = builder.build()
    operations = builder.analysis.relations("bus")
    actual = [tuple(f"{inst}.{name}" for inst, name, _sign in op.payload.ports) for op in operations]
    assert actual == [tuple(bus["port_refs"]) for bus in summaries]
    assert len(operations) == 2
    assert not any("source.unused" in ports for ports in actual)
    rows = [row for row in model.constraints if row.name.startswith("bus__") and row.name.endswith("__t0")]
    assert len(rows) == 2
    assert all(len(row.expression.terms) == 2 for row in rows)
    # The fixed 4 kW load requires exactly 5 kW upstream through 80% transfer.
    assert builder.analysis.variable_bounds("source__power_out__t0") == pytest.approx((5000.0, 5000.0))


@pytest.mark.parametrize("demand,parallel,expected", [
    (4.0, None, "solved"), (5.0, None, "solved"), (6.0, None, "infeasible"),
    (2.0, 3.0, "solved"),
])
def test_route_capacity_and_losses_survive_public_generation_and_real_solve(
    tmp_path, demand, parallel, expected,
) -> None:
    inputs, _documents = _inputs(demand, parallel)
    bundle = HIGHS_MILP_PROVIDER.generate(*inputs)
    (tmp_path / "problem.mps").write_bytes(bundle.files["input/problem.mps"])
    result = subprocess.run(
        ["/usr/local/bin/ies-highs-solver", "problem.mps", "--solution_file", "values.sol",
         "--time_limit", "30", "--random_seed", "0"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    adapted = HIGHS_ADAPTER.adapt(
        bundle, _receipt(), {"output/solution.sol": (tmp_path / "values.sol").read_bytes()},
    )
    assert adapted.math_status == expected
    if expected == "solved":
        total = demand + (parallel or 0.0)
        values = {item["name"]: item["value"] for item in adapted.outputs["solution"]["variables"]}
        assert values["source__power_out__t0"] == pytest.approx(total / 0.8)
        assert values["route__power_in__t0"] == pytest.approx(total / 0.8)
        assert values["route__power_out__t0"] == pytest.approx(total)
        assert values["source__power_out__t8759"] == pytest.approx(total / 0.8)
        assert values["load__electricity_in__t0"] == pytest.approx(demand)
        if parallel is not None:
            assert values["load_two__electricity_in__t0"] == pytest.approx(parallel)
    parameters = json.loads(bundle.files["input/parameters.json"])
    buses = [source for source in parameters["model_sources"]["sources"].values() if source["kind"] == "bus"]
    downstream = (("load.electricity_in", "route.power_out") if parallel is None else
                  ("load.electricity_in", "load_two.electricity_in", "route.power_out"))
    assert {tuple(bus["inputs"]) for bus in buses} == {
        ("route.power_in", "source.power_out"), downstream,
    }
