"""N3 public input -> frozen AST -> shared compiler -> real solve -> directory."""

import json
from copy import deepcopy

import pytest
from bundle_testkit import content_bytes, materialized_file
from test_p3_network_boundaries import BOUNDARIES, _case, _solve
from test_p3_port_network import SOURCE, _inputs
from test_project_model import LOAD_YAML, _evaluation_n

from iesplan.application.worker.compute_cases import build_hourly_files
from iesplan.assembly import validate_assembly_doc
from iesplan.computation import materialize_non_predictive_series
from iesplan.computation.generators.highs_milp import HIGHS_MILP_PROVIDER
from iesplan.computation.generators.linear import LoweringUnsupportedError
from iesplan.computation.generators.project import _new_builder
from iesplan.core.content import read_content


def _attach(inputs, doc, documents, block, outputs=None, **validation):
    doc["system_equations"] = {"account": block}
    doc["outputs"]["system_series"] = (
        outputs
        if outputs is not None
        else [{"block": "account", "variable": name} for name in block["variables"]]
    )
    checked = validate_assembly_doc(
        doc, device_documents=documents, finance_snapshots={"obj-finance": inputs[3]}, **validation
    )
    return checked, (checked.artifact, *inputs[1:])


def _block():
    return {"constants": {}, "references": {}, "variables": {}, "relations": []}


def test_delivery_binds_existing_ports_and_restores_independent_system_identity(tmp_path):
    load = LOAD_YAML.replace("heat", "electricity")
    inputs, doc, documents = _case(
        {"source": SOURCE, "load": load},
        {"supply": {"from": "source.power_out", "to": "load.electricity_in"}},
        {"load": {"electricity_demand": {"mode": "constant", "value": 4}}},
    )
    block = _block()
    block["references"] = {
        "supply": {"kind": "interface", "instance": "source", "interface": "power_out"},
        "demand": {"kind": "interface", "instance": "load", "interface": "electricity_in"},
    }
    block["variables"] = {"dispatch": {"unit": "kW", "valid_range": {"minimum": 0, "maximum": None}}}
    block["relations"] = [
        {"id": "source_total", "expression": "supply[t] = dispatch[t]"},
        {"id": "sink_total", "expression": "demand[t] = dispatch[t]"},
    ]
    checked, new_inputs = _attach(inputs, doc, documents, block)
    assert checked.ok, [(d.location, d.params) for d in checked.diagnostics]
    bundle = HIGHS_MILP_PROVIDER.generate(*new_inputs)
    result = _solve(bundle, tmp_path)
    assert result.math_status == "solved"
    system = result.outputs["system_series"]["system:account.dispatch"]
    assert system["block"] == "account" and system["variable"] == "dispatch"
    assert system["unit"] == "kW" and list(system["values"]) == [4] * 8760
    assert list(result.outputs["series"]["source.dispatch"]["values"]) == [4] * 8760
    frozen = json.loads(read_content(build_hourly_files(result.outputs)["hourly/series.json"][0]))
    assert frozen["meta"]["series"]["source.dispatch"]["kind"] == "device"
    assert frozen["meta"]["series"]["system:account.dispatch"] == {
        "kind": "system",
        "series_id": "system:account.dispatch",
        "block": "account",
        "variable": "dispatch",
        "unit": "kW",
    }
    params = json.loads(materialized_file(bundle, "input/parameters.json"))
    location = params["model_sources"]["sources"]["technical:system:account.source_total"]["location"]
    assert location["block_id"] == "account" and "instance_id" not in location
    assert params["model_sources"]["sources"]["technical:system:account.source_total"]["inputs"] == [
        "system:account.dispatch",
        "source.power_out",
    ]


@pytest.mark.parametrize(
    "change, expected",
    [
        (
            lambda b: b["references"].update(
                {"missing": {"kind": "interface", "instance": "absent", "interface": "give"}}
            ),
            "references.missing.instance",
        ),
        (
            lambda b: b["relations"].append({"id": "wrong", "expression": "energy[t] = take[t]"}),
            "relations[0].expression",
        ),
        (
            lambda b: b["relations"].append({"id": "future", "expression": "energy[t] = energy[t+1]"}),
            "relations[0].expression",
        ),
        (
            lambda b: b["relations"].append({"id": "lag", "expression": "energy[t] = energy[t-1]"}),
            "relations[0].expression",
        ),
    ],
)
def test_input_diagnostics_point_to_author_field(change, expected):
    inputs, doc, documents = _case(
        {"unit": BOUNDARIES}, {}, {"unit": {"signal": {"mode": "constant", "value": 1}}}
    )
    block = _block()
    block["references"] = {"take": {"kind": "interface", "instance": "unit", "interface": "take"}}
    block["variables"] = {"energy": {"unit": "kWh", "valid_range": {"minimum": 0, "maximum": None}}}
    change(block)
    checked, _ = _attach(inputs, doc, documents, block)
    assert not checked.ok
    assert checked.diagnostics[0].location["field"].endswith(expected)


def test_system_state_uses_shared_initial_and_real_steps(tmp_path):
    inputs, doc, documents = _case(
        {"unit": BOUNDARIES}, {}, {"unit": {"signal": {"mode": "constant", "value": 1}}}
    )
    block = _block()
    block["constants"] = {"start": {"value": 2, "unit": "1"}}
    block["references"] = {
        "signal": {"kind": "interface", "instance": "unit", "interface": "signal"},
        "actual": {"kind": "variable", "instance": "unit", "variable": "count"},
    }
    block["variables"] = {
        "count": {
            "unit": "1",
            "valid_range": {"minimum": 0, "maximum": None},
            "initial": {"property_ref": "start"},
        }
    }
    block["relations"] = [
        {"id": "update", "expression": "count[t] = count[t-1] + signal[t]"},
        {"id": "physical", "expression": "actual[t] = count[t]"},
    ]
    checked, new_inputs = _attach(inputs, doc, documents, block)
    assert checked.ok, checked.diagnostics
    builder = _new_builder(new_inputs[0], new_inputs[1], new_inputs[2], new_inputs[3], new_inputs[5])
    builder.build()
    assert builder.analysis.variable_bounds("system:account__count__t256") == (259, 259)
    result = _solve(HIGHS_MILP_PROVIDER.generate(*new_inputs), tmp_path)
    assert list(result.outputs["system_series"]["system:account.count"]["values"]) == list(range(3, 8763))
    # Frozen AST is authoritative; no generation-time reparse of raw text.
    reversed_doc = deepcopy(doc)
    reversed_doc["system_equations"]["account"]["references"] = dict(
        reversed(list(block["references"].items()))
    )
    again = validate_assembly_doc(
        reversed_doc, device_documents=documents, finance_snapshots={"obj-finance": inputs[3]}
    )
    assert again.ok
    assert (
        content_bytes(HIGHS_MILP_PROVIDER.generate(again.artifact, *new_inputs[1:]).files)
        == content_bytes(HIGHS_MILP_PROVIDER.generate(*new_inputs).files)
    )


def test_two_sources_two_sinks_direct_export_is_not_substitution_effect(tmp_path):
    load = LOAD_YAML.replace("heat", "electricity")
    inputs, doc, documents = _case(
        {
            "old": SOURCE.replace("network_source", "old_source"),
            "new": SOURCE.replace("network_source", "new_source"),
            "load": load,
            "export": load,
        },
        {
            "a": {"from": "old.power_out", "to": "load.electricity_in"},
            "b": {"from": "new.power_out", "to": "load.electricity_in"},
            "c": {"from": "old.power_out", "to": "export.electricity_in"},
        },
        {name: {"electricity_demand": {"mode": "constant", "value": 4}} for name in ("load", "export")},
    )
    block = _block()
    block["constants"] = {"generation": {"value": 4, "unit": "kW"}}
    block["references"] = {
        name: {"kind": "interface", "instance": inst, "interface": port}
        for name, inst, port in (
            ("old", "old", "power_out"),
            ("new", "new", "power_out"),
            ("demand", "load", "electricity_in"),
            ("export", "export", "electricity_in"),
        )
    }
    block["variables"] = {
        name: {"unit": "kW", "valid_range": {"minimum": 0, "maximum": None}}
        for name in ("old_load", "old_export", "new_load", "new_export")
    }
    expressions = [
        "old[t] = old_load[t] + old_export[t]",
        "new[t] = new_load[t] + new_export[t]",
        "demand[t] = old_load[t] + new_load[t]",
        "export[t] = old_export[t] + new_export[t]",
        "old[t] = generation",
        "new[t] = generation",
        "new_export[t] = 0",
    ]
    block["relations"] = [{"id": f"relation_{i}", "expression": expr} for i, expr in enumerate(expressions)]
    checked, new_inputs = _attach(inputs, doc, documents, block)
    assert checked.ok, checked.diagnostics
    result = _solve(HIGHS_MILP_PROVIDER.generate(*new_inputs), tmp_path)
    assert result.math_status == "solved"
    curves = result.outputs["system_series"]
    assert set(curves["system:account.new_export"]["values"]) == {0}
    assert set(curves["system:account.new_load"]["values"]) == {4}
    assert set(curves["system:account.old_export"]["values"]) == {4}
    # No new direct exports does NOT imply no increase of old-source exports.
    assert set(result.outputs["series"]["export.electricity_in"]["values"]) == {4}


def test_bidirectional_decomposition_mutex_prevents_fictitious_cycle(tmp_path):
    inputs, doc, documents = _case(
        {"unit": BOUNDARIES}, {}, {"unit": {"signal": {"mode": "constant", "value": 1}}}
    )
    block = _block()
    block["references"] = {"net": {"kind": "interface", "instance": "unit", "interface": "exchange"}}
    block["variables"] = {
        name: {"unit": "kW", "valid_range": {"minimum": 0, "maximum": 5}} for name in ("give", "take")
    }
    block["relations"] = [
        {"id": "net", "expression": "net[t] = give[t] - take[t]"},
        {"id": "exclusive", "expression": "give[t] * take[t] = 0"},
    ]
    checked, new_inputs = _attach(inputs, doc, documents, block)
    assert checked.ok, checked.diagnostics
    result = _solve(HIGHS_MILP_PROVIDER.generate(*new_inputs), tmp_path)
    assert result.math_status == "solved"
    assert all(set(entry["values"]) == {0} for entry in result.outputs["system_series"].values())


def test_property_alias_bound_and_initial_share_planned_scale_and_blocks_are_independent(tmp_path):
    inputs, documents = _inputs(demand=3.2)
    doc = json.loads(inputs[0].canonical_text)
    doc["planning"]["variables"]["route_capacity"].update(lower_bound="4", upper_bound="4")
    block = _block()
    block["references"] = {"capacity": {"kind": "property", "instance": "route", "property": "capacity"}}
    block["variables"] = {
        "memory": {
            "unit": "kW",
            "valid_range": {"minimum": 0, "maximum": {"property_ref": "capacity"}},
            "initial": {"property_ref": "capacity"},
        },
    }
    block["relations"] = [{"id": "keep", "expression": "memory[t] = memory[t-1]"}]
    other = deepcopy(block)
    other["constants"] = {"zero": {"value": 0, "unit": "kW"}}
    other["variables"]["memory"]["initial"] = {"expression": "zero"}
    doc["system_equations"] = {"account": block, "other": other}
    doc["outputs"]["system_series"] = [{"block": b, "variable": "memory"} for b in doc["system_equations"]]
    checked = validate_assembly_doc(
        doc, device_documents=documents, finance_snapshots={"obj-finance": inputs[3]}
    )
    assert checked.ok, checked.diagnostics
    new_inputs = (checked.artifact, *inputs[1:])
    builder = _new_builder(new_inputs[0], new_inputs[1], new_inputs[2], new_inputs[3], new_inputs[5])
    independent_series = [entry for entry in builder.step_series() if "block" in entry]
    bundle = HIGHS_MILP_PROVIDER.generate(*new_inputs)
    parameters = json.loads(materialized_file(bundle, "input/parameters.json"))
    assert independent_series == parameters["system_series"]
    result = _solve(bundle, tmp_path)
    assert result.math_status == "solved"
    assert set(result.outputs["system_series"]["system:account.memory"]["values"]) == {4}
    assert set(result.outputs["system_series"]["system:other.memory"]["values"]) == {0}
    assert set(result.outputs["series"]["route.power_out"]["values"]) == {3.2}


def test_unsupported_conversion_keeps_block_relation_and_actual_step():
    inputs, doc, documents = _case(
        {"unit": BOUNDARIES}, {}, {"unit": {"signal": {"mode": "constant", "value": 1}}}
    )
    block = _block()
    block["variables"] = {
        name: {"unit": "1", "valid_range": {"minimum": 0, "maximum": None}} for name in ("give", "take")
    }
    block["relations"] = [{"id": "exclusive", "expression": "give[t] * take[t] = 0"}]
    checked, new_inputs = _attach(inputs, doc, documents, block)
    assert checked.ok
    with pytest.raises(LoweringUnsupportedError, match="缺有限上界") as missing:
        HIGHS_MILP_PROVIDER.generate(*new_inputs)
    assert missing.value.model_source["location"]["block_id"] == "account"
    assert missing.value.model_source["location"]["relation_id"] == "exclusive"
    assert missing.value.model_source["index"] == 0
    block["relations"] = [{"id": "nonlinear", "expression": "give[t] * take[t] = give[t]"}]
    checked, new_inputs = _attach(inputs, doc, documents, block)
    assert checked.ok
    with pytest.raises(LoweringUnsupportedError, match="变量×变量"):
        HIGHS_MILP_PROVIDER.generate(*new_inputs)


def test_capability_gate_and_duplicate_output_are_public_input_errors():
    from iesplan.application.tasks.submissions import _check_system_capabilities
    from iesplan.tasks.contracts import InvalidRequestError

    inputs, doc, documents = _case(
        {"unit": BOUNDARIES}, {}, {"unit": {"signal": {"mode": "constant", "value": 1}}}
    )
    # An actually empty declaration requires neither capability.
    _check_system_capabilities(inputs[0], "test.generator@1.0.0", ())
    idle, _ = _attach(inputs, doc, documents, _block())
    assert idle.ok
    _check_system_capabilities(idle.artifact, "test.generator@1.0.0", ())
    block = _block()
    block["variables"] = {"value": {"unit": "1", "valid_range": {"minimum": 0, "maximum": 0}}}
    checked, _ = _attach(inputs, doc, documents, block)
    assert checked.ok
    with pytest.raises(InvalidRequestError) as missing:
        _check_system_capabilities(checked.artifact, "test.generator@1.0.0", ())
    assert missing.value.code == "TASK-REQ-007"
    assert missing.value.params["missing_capabilities"] == [
        "modeling.system_equations",
        "outputs.system_series",
    ]
    _check_system_capabilities(
        checked.artifact, "test.generator@1.0.0", ("modeling.system_equations", "outputs.system_series")
    )
    checked, _ = _attach(
        inputs, doc, documents, block, outputs=[{"block": "account", "variable": "value"}] * 2
    )
    assert not checked.ok
    assert checked.diagnostics[-1].location["field"] == "outputs.system_series[1]"


STORAGE = """
schema: ies.device-model
schema_version: "1.0.0"
device: {id: acme.device.ledger_storage, names: {zh-CN: 状态储存, en-US: Storage}}
properties:
  eta_c: {value: 0.9, unit: "1", valid_range: {minimum: 0, maximum: 1}}
  eta_d: {value: 0.98, unit: "1", valid_range: {minimum: 0, maximum: 1}}
  initial: {value: 0, unit: kWh, valid_range: {minimum: 0, maximum: 10}}
interfaces:
  charge: {type: predefined, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: 4}}
  discharge: {type: predefined, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: 4}}
equations:
  variables:
    stored: {unit: kWh, valid_range: {minimum: 0, maximum: 10}, initial: {property_ref: initial}}
  relations:
    - id: update
      expression: "stored[t] = stored[t-1] + (eta_c * charge[t] - discharge[t] / eta_d) * step_duration"
"""


def test_two_step_energy_accounts_keep_losses_and_leap_year_connection(tmp_path):
    inputs, doc, documents = _case(
        {"store": STORAGE},
        {},
        {
            "store": {
                "charge": {"mode": "constant", "value": 0},
                "discharge": {"mode": "constant", "value": 0},
            }
        },
    )
    doc["project_baseline"]["start_year"] = 2027
    count = 17544
    data = (
        "# schema: ies.device-data\n# schema_version: 1.0.0\n"
        "# dataset_id: ledger\n# device_id: acme.device.ledger_storage\n"
        "# source_mode: data_full\n# resolution: 1h\n"
        "# unit.charge: kW\n# unit.discharge: kW\nstep,charge,discharge\n"
        + "\n".join(f"{i},{4 if i == 0 else 0},{1.764 if i == 1 else 0}" for i in range(count))
        + "\n"
    ).encode()
    doc["devices"]["store"]["predefined_interfaces"] = {
        name: {"mode": "data_full", "path": "ledger.csv"} for name in ("charge", "discharge")
    }
    block = _block()
    block["constants"] = {"zero": {"value": 0, "unit": "kWh"}, "share": {"value": 0.5, "unit": "1"}}
    block["references"] = {
        "actual": {"kind": "variable", "instance": "store", "variable": "stored"},
        **{
            name: {"kind": "interface", "instance": "store", "interface": name}
            for name in ("charge", "discharge")
        },
        **{name: {"kind": "property", "instance": "store", "property": name} for name in ("eta_c", "eta_d")},
    }
    block["variables"] = {
        name: {
            "unit": "kWh",
            "valid_range": {"minimum": 0, "maximum": None},
            "initial": {"property_ref": "zero"},
        }
        for name in ("source_a", "source_b")
    }
    block["relations"] = [
        {
            "id": f"update_{name}",
            "expression": (
                f"{name}[t] = {name}[t-1] + share * "
                "(eta_c * charge[t] - discharge[t] / eta_d) * step_duration"
            ),
        }
        for name in block["variables"]
    ] + [{"id": "total", "expression": "actual[t] = source_a[t] + source_b[t]"}]
    checked, new_inputs = _attach(
        inputs,
        doc,
        documents,
        block,
        dataset_contents={"ledger.csv": {"object_id": "ledger-data", "media_type": "text/csv", "data": data}},
        operation_years=2,
    )
    assert checked.ok, checked.diagnostics
    materialized = materialize_non_predictive_series(
        checked.artifact, {"ledger-data": data}, operation_years=2
    )
    assert len(materialized.series[0].values) == count
    new_inputs = (checked.artifact, inputs[1], materialized, *inputs[3:5], _evaluation_n(2))
    result = _solve(HIGHS_MILP_PROVIDER.generate(*new_inputs), tmp_path)
    assert result.math_status == "solved"
    actual = result.outputs["series"]["store.stored"]["values"]
    ledger = result.outputs["system_series"]
    for step in (0, 1, 255, 256, 8759, 8760, 17543):
        expected = 3.6 if step == 0 else 1.8
        assert actual[step] == pytest.approx(expected)
        a = ledger["system:account.source_a"]["values"][step]
        b = ledger["system:account.source_b"]["values"][step]
        assert a == pytest.approx(expected / 2) and b == pytest.approx(expected / 2)
        assert a + b == pytest.approx(actual[step])
