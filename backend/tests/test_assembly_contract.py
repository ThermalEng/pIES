"""`ies.assembly` 2.0.0 最小契约测试(roadmap 0.8.0 切片 S2b1)。

覆盖:
- 机器可读 JSON Schema 存在且可加载(2.0.0:含 project_baseline 与
  2.0 设备实例形态,无 calculation/time_axis/resources/model/parameters/data);
- 合法样例通过结构阶段,非法样例产出稳定结构诊断;
- 设备实例形态:definition 裸 ID(禁 @钉选)、asset_origin 必需、
  properties 精确 {value, unit}、predefined 严格判别联合;
- 唯一规范化:相同语义 → 相同规范文本(键序无关、数值唯一有限表示、
  非有限值拒绝);
- ValidatedAssemblyArtifact 不可变二件套与回执结构(版本均为 2.0.0)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from iesplan.assembly import (
    ASSEMBLY_SCHEMA_PATH,
    CANON_ALGORITHM_ID,
    CANON_ALGORITHM_VERSION,
    SCHEMA_ID,
    SCHEMA_VERSION,
    ValidationReceipt,
    canonicalize_assembly_doc,
    parse_assembly_doc,
    validate_assembly_text,
)
from iesplan.assembly.diags import (
    ASM_BIND_INVALID,
    ASM_EDGE_BAD_ENDPOINT,
    ASM_INPUT_UNDECLARED,
    ASM_REF_MODEL_UNREG,
    ASM_SYN_FIELD,
    ASM_SYN_FORBIDDEN,
    ASM_SYN_PATH,
    ASM_SYN_SCHEMA,
    ASM_SYN_SECTION,
    ASM_SYN_TYPE,
    ASM_SYN_VERSION_PIN,
)
from iesplan.assembly.parser import run_structure_checks

ASSEMBLY_DIR = Path(__file__).resolve().parent.parent / "iesplan" / "assembly"
SAMPLES_DIR = ASSEMBLY_DIR / "samples"
VALID_DIR = SAMPLES_DIR / "valid"
INVALID_DIR = SAMPLES_DIR / "invalid"


def _load(name: str) -> str:
    return (VALID_DIR / name).read_text(encoding="utf-8")


def _structure(text: str, **kwargs):
    """单管线结构判定:安全解析一次 + 结构校验一次。

    返回 (doc|None, diagnostics)。样例均为合法 YAML,解析必成功;
    结构非法时 doc 为 None,诊断由唯一一次结构校验产出。
    """
    parsed = parse_assembly_doc(text, **kwargs)
    assert parsed.doc is not None, [d.to_dict() for d in parsed.diagnostics]
    diags = []
    doc = run_structure_checks(parsed.doc, diags=diags)
    return doc, diags


def _campus_snapshot():
    """园区样例有效快照：样例 YAML 经 finance 唯一公开边界解析为密封对象。"""
    from iesplan.finance import effective_from_yaml

    text = (SAMPLES_DIR / "finance" / "campus.effective-finance.yaml").read_text(encoding="utf-8")
    return effective_from_yaml(text)


def _campus_snapshots():
    return {"fin:campus": _campus_snapshot()}


def _validate_campus(text, **kwargs):
    """园区样例完整管线(包内 CSV 与 finance 文件可读，无需注入)。"""
    kwargs.setdefault("package_dir", SAMPLES_DIR)
    return validate_assembly_text(text, **kwargs)


@pytest.fixture
def init_registry():
    from iesplan.devices import init_registry

    init_registry()
    yield


def _validated_text():
    """合法样例经完整管线签发的规范文本(包内 CSV 可读)。"""
    result = validate_assembly_text(_load("campus.assembly.yaml"), package_dir=SAMPLES_DIR)
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
    assert not any(d.blocking for d in result.diagnostics)
    return result.artifact.canonical_text


# ---------------------------------------------------------------------------
# JSON Schema
# ---------------------------------------------------------------------------


class TestSchemaFile:
    def test_schema_exists_and_loads(self):
        schema_path = ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH
        assert schema_path.exists(), f"缺少 {ASSEMBLY_SCHEMA_PATH}"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        assert schema["$id"] == f"{SCHEMA_ID}/4.0.0"
        assert schema["title"] == "ies.assembly 4.0.0 装配 YAML 契约"
        required = schema["required"]
        assert "schema" in required and "schema_version" in required
        assert set(required) >= {
            "assembly",
            "project_baseline",
            "devices",
            "connections",
            "finance",
            "finance_binding",
            "tariff_bindings",
            "planning",
            "outputs",
            "extensions",
        }
        assert "constraints" not in required
        assert "calculation" not in required
        assert "time_axis" not in required
        assert "resources" not in required

    def test_schema_device_shape(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        device = schema["properties"]["devices"]["additionalProperties"]
        assert set(device["required"]) >= {"definition", "asset_origin"}
        assert "model" not in device["properties"]
        assert "parameters" not in device["properties"]
        assert "data" not in device["properties"]
        assert device["properties"]["asset_origin"]["enum"] == ["existing", "new"]

    def test_schema_predefined_modes_closed_oneof(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        entry = schema["properties"]["devices"]["additionalProperties"]["properties"][
            "predefined_interfaces"
        ]["additionalProperties"]
        branches = entry["oneOf"]
        assert len(branches) == 3
        assert all(b["additionalProperties"] is False for b in branches)
        by_mode = {b["properties"]["mode"]["const"]: b for b in branches}
        assert set(by_mode) == {"constant", "data_repeat", "data_predict"}
        assert set(by_mode["constant"]["required"]) == {"mode", "value"}
        assert set(by_mode["data_repeat"]["required"]) == {"mode", "path"}
        assert set(by_mode["data_predict"]["required"]) == {"mode", "path", "target_type"}

    def test_schema_driver_targets_closed_oneof(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        instances = schema["properties"]["finance_binding"]["properties"]["instances"][
            "additionalProperties"
        ]
        costed = next(b for b in instances["oneOf"] if b["properties"]["type"] == {"const": "costed"})
        drivers = costed["properties"]["drivers"]["additionalProperties"]
        branches = drivers["oneOf"]
        assert len(branches) == 2
        assert all(b["additionalProperties"] is False for b in branches)
        prop_branch = next(b for b in branches if "property" in b["properties"])
        assert set(prop_branch["required"]) == {"property"}
        iface_branch = next(b for b in branches if "interface" in b["properties"])
        assert set(iface_branch["required"]) == {"interface", "aggregation"}

    def test_schema_variables_closed_per_public_contract(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        planning = schema["properties"]["planning"]
        assert set(planning["required"]) == {"objective", "variables"}
        assert planning["properties"]["variables"].get("minProperties") == 1
        constraints = planning["properties"]["constraints"]["additionalProperties"]
        assert set(constraints["required"]) == {"type", "expression"}
        assert set(constraints["properties"]["type"]["enum"]) == {
            "load_balance", "capacity_limit", "co2_limit",
            "annual_purchase_cost_limit", "ratio",
        }
        assert constraints["additionalProperties"] is False
        finance = schema["properties"]["finance"]
        assert finance["additionalProperties"] is False
        assert finance["properties"]["revision"]["type"] == "integer"
        assert finance["properties"]["revision"]["minimum"] == 1
        variables = schema["properties"]["planning"]["properties"]["variables"][
            "additionalProperties"
        ]
        assert set(variables["required"]) == {"device_ref", "parameter"}
        assert set(variables["properties"]) == {
            "device_ref", "parameter",
            "lower_bound", "upper_bound", "unit",
        }
        assert variables["additionalProperties"] is False
        assert "year_offset" not in variables["properties"]
        assert variables["properties"]["lower_bound"]["type"] == ["integer", "string", "null"]
        assert variables["properties"]["upper_bound"]["type"] == ["integer", "string", "null"]

    def test_schema_baseline_shape(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        baseline = schema["properties"]["project_baseline"]
        assert baseline["additionalProperties"] is False
        assert set(baseline["required"]) == {"resolution", "start_year", "scenario_mode"}
        assert set(baseline["properties"]["resolution"]["enum"]) == {"15min", "30min", "1h"}
        assert baseline["properties"]["start_year"]["type"] == "integer"
        assert baseline["properties"]["scenario_mode"]["const"] == "single"

    def test_schema_sections_have_additional_properties_false(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        # 顶层 + 节级均拒绝未知字段
        assert schema["additionalProperties"] is False
        assert schema["properties"]["assembly"]["additionalProperties"] is False
        assert schema["properties"]["project_baseline"]["additionalProperties"] is False
        assert schema["properties"]["outputs"]["additionalProperties"] is False
        # 容器型节:devices/connections/tariff_bindings/planning.constraints 的
        # additionalProperties 是嵌套对象 schema,均显式 additionalProperties: false
        for section in ("devices", "connections", "tariff_bindings"):
            assert schema["properties"][section]["additionalProperties"]["additionalProperties"] is False
        planning_constraints = schema["properties"]["planning"]["properties"]["constraints"]
        assert planning_constraints["additionalProperties"]["additionalProperties"] is False
        # finance 判别联合: ref 的 object/relative_file 分支互斥且封闭
        ref_branches = schema["properties"]["finance"]["properties"]["ref"]["oneOf"]
        assert len(ref_branches) == 2
        assert all(b["additionalProperties"] is False for b in ref_branches)
        # finance_binding 判别联合: costed/none 分支互斥且封闭
        bind_branches = schema["properties"]["finance_binding"]["properties"]["instances"][
            "additionalProperties"
        ]["oneOf"]
        assert len(bind_branches) == 2
        assert all(b["additionalProperties"] is False for b in bind_branches)


# ---------------------------------------------------------------------------
# JSON Schema 与 parser 接受集合一致
# ---------------------------------------------------------------------------


def _campus_tree():
    """园区样例解析树深拷贝(各一致用例独立变异)。"""
    import copy

    parsed = parse_assembly_doc(_load("campus.assembly.yaml"))
    assert parsed.ok and parsed.doc is not None, [d.to_dict() for d in parsed.diagnostics]
    return copy.deepcopy(parsed.doc)


def _schema_ok(doc) -> bool:
    """JSON Schema 直接判定: 无校验错误即接受。"""
    from jsonschema import Draft202012Validator

    schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
    return not list(Draft202012Validator(schema).iter_errors(doc))


def _structure_ok(doc) -> bool:
    """parser 结构判定: 无阻断诊断即接受。"""
    diags = []
    run_structure_checks(doc, diags=diags)
    return not any(d.blocking for d in diags)


def _agree(doc) -> None:
    """同一文档树在两层判定中接受/拒绝必须一致。"""
    assert _schema_ok(doc) == _structure_ok(doc), "schema 与 parser 接受集合不一致"


class TestSchemaParserAgreement:
    """parser/schema 接受集合一致(结构形态层；值域判定归 parser/领域阶段)。

    不复制 finance 领域校验: 用例只覆盖形态(未知键、缺失 required、
    坏判别式、坏枚举、坏标量类型)，finance_type 存在性等语义不在此判定。
    """

    def test_valid_campus_accepted_by_both(self):
        _agree(_campus_tree())

    def test_predefined_modes_valid_variants_accepted_by_both(self):
        doc = _campus_tree()
        heat = doc["devices"]["heat_load"]["predefined_interfaces"]["heat_demand"]
        heat.clear()
        heat.update({"mode": "constant", "value": 240.0})
        _agree(doc)
        doc = _campus_tree()
        heat = doc["devices"]["heat_load"]["predefined_interfaces"]["heat_demand"]
        heat["target_type"] = "heat_demand"
        heat["mode"] = "data_predict"
        _agree(doc)

    def test_predefined_modes_invalid_rejected_by_both(self):
        cases = [
            {"mode": "constant", "value": 1.0, "path": "data/x.data.csv"},
            {"mode": "data_repeat"},
            {"mode": "data_repeat", "path": "data/x.data.csv", "target_type": "t"},
            {"mode": "data_guess", "path": "data/x.data.csv"},
            {"path": "data/x.data.csv"},
        ]
        for entry in cases:
            doc = _campus_tree()
            doc["devices"]["heat_load"]["predefined_interfaces"]["heat_demand"] = entry
            assert not _schema_ok(doc), entry
            assert not _structure_ok(doc), entry

    def test_driver_targets_rejected_by_both(self):
        cases = [
            {},
            {"property": "rated_capacity_kwp", "aggregation": "sum_positive"},
            {"property": "rated_capacity_kwp", "interface": "electric_out"},
            {"interface": "electric_out"},
            {"interface": "electric_out", "aggregation": "nope"},
        ]
        for target in cases:
            doc = _campus_tree()
            doc["finance_binding"]["instances"]["pv1"]["drivers"]["capacity_kw"] = target
            assert not _schema_ok(doc), target
            assert not _structure_ok(doc), target

    def test_variables_shape_agrees(self):
        doc = _campus_tree()
        doc["planning"]["variables"] = {
            "x1": {
                "device_ref": "hp1",
                "parameter": "rated_heat_kw",
                "lower_bound": 0,
                "upper_bound": 1000,
                "unit": "kW",
            },
            "x2": {"device_ref": "bat1", "parameter": "capacity_kwh"},
            # 确定性投影输出规范十进制字符串界：结构层接受
            "x3": {
                "device_ref": "pv1",
                "parameter": "rated_capacity_kwp",
                "lower_bound": "0",
                "upper_bound": "500",
                "unit": "kWp",
            },
        }
        _agree(doc)
        bad_cases = [
            {"device_ref": "hp1"},
            {"device_ref": "hp1", "parameter": "p", "bogus": 1},
            {"device_ref": "hp1", "parameter": "p", "lower_bound": True},
            {"device_ref": "hp1", "parameter": "p", "lower_bound": 0.5},
            {"device_ref": "hp1", "parameter": "p", "unit": 5},
        ]
        for entry in bad_cases:
            bad = _campus_tree()
            bad["planning"]["variables"] = {"x1": entry}
            assert not _schema_ok(bad), entry
            assert not _structure_ok(bad), entry

    def test_empty_variables_rejected_by_both(self):
        bad = _campus_tree()
        bad["planning"]["variables"] = {}
        assert not _schema_ok(bad)
        assert not _structure_ok(bad)

    def test_missing_variables_rejected_by_both(self):
        bad = _campus_tree()
        del bad["planning"]["variables"]
        assert not _schema_ok(bad)
        assert not _structure_ok(bad)

    def test_constraint_type_required_by_both(self):
        bad = _campus_tree()
        bad["planning"]["constraints"] = {
            "typeless": {"expression": "pv_cap <= 500 kW"},
        }
        assert not _schema_ok(bad)
        assert not _structure_ok(bad)

    def test_finance_revision_shape_agrees(self):
        doc = _campus_tree()
        doc["finance"]["revision"] = 3
        _agree(doc)
        # 注: JSON Schema 视 2.0 为整数(数学值)，schema 接受而 parser 拒绝；
        # 该残留差异不在一致判定内(同 bounds 的 0.5 非整值两边一致拒绝)。
        for rev in ("3", 0, -1, True):
            bad = _campus_tree()
            bad["finance"]["revision"] = rev
            assert not _schema_ok(bad), rev
            assert not _structure_ok(bad), rev

    def test_device_unknown_key_rejected_by_both(self):
        doc = _campus_tree()
        doc["devices"]["grid"]["model_method"] = "milp"
        assert not _schema_ok(doc)
        assert not _structure_ok(doc)


# ---------------------------------------------------------------------------
# 合法 / 非法样例(结构阶段)
# ---------------------------------------------------------------------------


class TestSamples:
    def test_valid_sample_passes_structure(self):
        doc, diags = _structure(_load("campus.assembly.yaml"), source_name="campus.assembly.yaml")
        assert doc is not None, [d.to_dict() for d in diags]
        assert not any(d.blocking for d in diags)
        assert doc["schema"] == SCHEMA_ID
        assert doc["schema_version"] == SCHEMA_VERSION == "4.0.0"
        assert doc["assembly"]["id"] == "campus_demo"
        assert doc["project_baseline"] == {
            "resolution": "1h",
            "start_year": 2026,
            "scenario_mode": "single",
        }
        assert "resources" not in doc
        assert set(doc["devices"]) == {"grid", "pv1", "bat1", "hp1", "elec_load", "heat_load"}
        pv1 = doc["devices"]["pv1"]
        assert pv1["definition"] == {"id": "ies.device.pv"}
        assert pv1["asset_origin"] == "new"
        assert pv1["properties"]["rated_capacity_kwp"] == {"value": 300, "unit": "kWp"}
        assert pv1["predefined_interfaces"]["ambient_temperature"] == {
            "mode": "constant",
            "value": 25,
        }
        assert pv1["predefined_interfaces"]["solar_irradiance"] == {
            "mode": "data_repeat",
            "path": "data/pv_irradiance.data.csv",
        }
        assert "calculation" not in doc
        assert "time_axis" not in doc
        assert doc["finance"] == {
            "ref": {"kind": "relative_file", "path": "finance/campus.effective-finance.yaml"},
            "profile_id": "cn-north-demo",
        }
        binding = doc["finance_binding"]["instances"]
        assert set(binding) == {"grid", "pv1", "bat1", "hp1", "elec_load", "heat_load"}
        assert binding["grid"] == {"type": "none"}
        assert binding["pv1"]["finance_type"] == "pv_system"
        assert binding["pv1"]["drivers"]["capacity_kw"] == {"property": "rated_capacity_kwp"}
        assert binding["pv1"]["drivers"]["generated_kwh"] == {
            "interface": "electric_out",
            "aggregation": "integrate_positive",
        }

    @pytest.mark.parametrize(
        ("fname", "expected_code"),
        [
            ("bad.schema.assembly.yaml", ASM_SYN_SCHEMA),
            ("bad.version-pin.assembly.yaml", ASM_SYN_VERSION_PIN),
            ("bad.predefined-path.assembly.yaml", ASM_SYN_PATH),
            ("bad.forbidden-field.assembly.yaml", ASM_SYN_FORBIDDEN),
            ("bad.calculation.assembly.yaml", ASM_SYN_SECTION),
            ("bad.asset-origin.assembly.yaml", ASM_SYN_TYPE),
            ("bad.binding-mode.assembly.yaml", ASM_SYN_TYPE),
        ],
    )
    def test_invalid_samples_block_with_stable_code(self, fname, expected_code):
        doc, diags = _structure((INVALID_DIR / fname).read_text(encoding="utf-8"))
        assert doc is None
        codes = [d.code for d in diags]
        assert expected_code in codes, f"{fname}: {codes}"

    def test_calculation_rejected_as_unknown_section(self):
        text = _load("campus.assembly.yaml") + "\ncalculation:\n  mode: fixed_operation\n"
        doc, diags = _structure(text)
        assert doc is None
        diag = next(d for d in diags if d.code == ASM_SYN_SECTION)
        assert diag.params.get("section") == "calculation"
        assert diag.blocking is True

    def test_resources_rejected_as_unknown_section(self):
        text = _load("campus.assembly.yaml") + "\nresources:\n  datasets: {}\n"
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == ASM_SYN_SECTION and d.params.get("section") == "resources" for d in diags
        )

    def test_old_model_key_rejected(self):
        text = _load("campus.assembly.yaml").replace(
            "    definition:\n      id: ies.device.heat_pump",
            "    model: ies.device.heat_pump@2.0.0",
        )
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == "ASM-SYN-001" and d.location["field"] == "devices.hp1.model" for d in diags
        )

    def test_missing_baseline_rejected(self):
        text = _load("campus.assembly.yaml").replace("project_baseline:", "time_axis:")
        doc, diags = _structure(text)
        assert doc is None
        fields = {d.params.get("section") for d in diags if d.code == ASM_SYN_FIELD}
        assert "project_baseline" in fields

    def test_baseline_missing_member_rejected(self):
        text = _load("campus.assembly.yaml").replace("  start_year: 2026\n", "")
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == ASM_SYN_FIELD and d.location["field"] == "project_baseline.start_year"
            for d in diags
        )

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("resolution: 1h", "resolution: 5min"),
            ("start_year: 2026", "start_year: yes"),
            ("start_year: 2026", "start_year: true"),
            ("scenario_mode: single", "scenario_mode: multi"),
        ],
    )
    def test_baseline_illegal_member_rejected(self, old, new):
        text = _load("campus.assembly.yaml").replace(old, new)
        doc, diags = _structure(text)
        assert doc is None, new
        assert any(d.blocking for d in diags)

    def test_unknown_section_rejected(self):
        text = _load("campus.assembly.yaml") + "\npipelines: []\n"
        doc, diags = _structure(text)
        assert doc is None
        assert any(d.code == "ASM-SYN-002" for d in diags)

    def test_missing_section_rejected(self):
        text = _load("campus.assembly.yaml").replace("extensions: {}\n", "")
        doc, diags = _structure(text)
        assert doc is None
        fields = {d.params.get("section") for d in diags if d.code == ASM_SYN_FIELD}
        assert "extensions" in fields

    def test_unknown_device_key_rejected(self):
        text = _load("campus.assembly.yaml").replace(
            "    predefined_interfaces: {}",
            "    magic: 1\n    predefined_interfaces: {}",
            1,
        )
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == "ASM-SYN-001" and d.location["field"] == "devices.grid.magic" for d in diags
        )

    def test_property_value_must_be_scalar(self):
        text = _load("campus.assembly.yaml").replace("        value: 3.5", "        value: {a: 1}")
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == ASM_SYN_TYPE
            and d.location["field"] == "devices.hp1.properties.cop.value"
            for d in diags
        )

    def test_property_unit_required(self):
        text = _load("campus.assembly.yaml").replace('        unit: "1"\n', "", 1)
        doc, diags = _structure(text)
        assert doc is None
        assert any(d.code == ASM_SYN_TYPE and "unit" in d.location["field"] for d in diags)

    def test_constant_value_must_be_number(self):
        text = _load("campus.assembly.yaml").replace("        value: 25", "        value: hot")
        doc, diags = _structure(text)
        assert doc is None
        assert any(d.code == ASM_SYN_TYPE for d in diags)

    def test_extensions_must_be_namespaced(self):
        text = _load("campus.assembly.yaml").replace("extensions: {}", "extensions:\n  meta: 1")
        doc, diags = _structure(text)
        assert doc is None
        assert any("extensions_key_not_namespaced" in str(d.params) for d in diags)

    def test_old_schema_version_rejected(self):
        text = _load("campus.assembly.yaml").replace('schema_version: "4.0.0"', 'schema_version: "1.0.0"')
        doc, diags = _structure(text)
        assert doc is None
        assert any(d.code == "ASM-SYN-003" for d in diags)


# ---------------------------------------------------------------------------
# 领域门禁(完整管线:结构 + 模型/属性/预定义来源)
# ---------------------------------------------------------------------------


class TestDomainGates:
    def test_valid_sample_issues_artifact(self, init_registry):
        result = validate_assembly_text(_load("campus.assembly.yaml"), package_dir=SAMPLES_DIR)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
        assert not any(d.blocking for d in result.diagnostics)
        assert "data/campus_load.data.csv" in result.artifact.canonical_text

    def test_unknown_definition_blocked(self, init_registry):
        text = (INVALID_DIR / "bad.definition-unknown.assembly.yaml").read_text(encoding="utf-8")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        assert any(d.code == ASM_REF_MODEL_UNREG and d.blocking for d in result.diagnostics)

    def test_undeclared_property_blocked(self, init_registry):
        text = (INVALID_DIR / "bad.property-undeclared.assembly.yaml").read_text(encoding="utf-8")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        assert any(d.code == ASM_INPUT_UNDECLARED and d.blocking for d in result.diagnostics)

    def test_missing_predefined_binding_blocked(self, init_registry):
        text = (INVALID_DIR / "bad.predefined-missing.assembly.yaml").read_text(encoding="utf-8")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        assert any(
            d.code == ASM_BIND_INVALID
            and d.params.get("reason") == "missing_binding"
            and d.blocking
            for d in result.diagnostics
        )

    def test_predefined_endpoint_in_connections_blocked(self, init_registry):
        text = (INVALID_DIR / "bad.endpoint-predefined.assembly.yaml").read_text(encoding="utf-8")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        assert any(d.code == ASM_EDGE_BAD_ENDPOINT and d.blocking for d in result.diagnostics)

    def test_csv_device_mismatch_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "path: data/campus_load.data.csv", "path: data/campus_heat.data.csv"
        )
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        assert any(
            d.code == ASM_BIND_INVALID and d.params.get("reason") == "csv_device_mismatch"
            for d in result.diagnostics
        )

    def test_csv_missing_content_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "path: data/campus_load.data.csv", "path: data/gone.data.csv"
        )
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        assert any(d.blocking for d in result.diagnostics)


# ---------------------------------------------------------------------------
# finance 精确引用与成本绑定(结构阶段)
# ---------------------------------------------------------------------------


class TestFinanceStructure:
    def test_missing_finance_section_rejected(self):
        text = _load("campus.assembly.yaml").replace("finance:\n", "finance_gone:\n")
        doc, diags = _structure(text)
        assert doc is None
        fields = {d.params.get("section") for d in diags if d.code == ASM_SYN_FIELD}
        assert "finance" in fields

    def test_missing_finance_binding_rejected(self):
        text = _load("campus.assembly.yaml").replace("finance_binding:\n", "finance_binding_gone:\n")
        doc, diags = _structure(text)
        assert doc is None
        fields = {d.params.get("section") for d in diags if d.code == ASM_SYN_FIELD}
        assert "finance_binding" in fields

    def test_finance_ref_kind_rejected(self):
        text = _load("campus.assembly.yaml").replace("kind: relative_file", "kind: url")
        doc, diags = _structure(text)
        assert doc is None
        assert any(d.code == ASM_SYN_TYPE and "ref.kind" in d.location["field"] for d in diags)

    def test_object_ref_requires_object_id(self):
        text = _load("campus.assembly.yaml").replace(
            "    kind: relative_file\n    path: finance/campus.effective-finance.yaml",
            '    kind: object\n    object_id: ""',
        )
        doc, diags = _structure(text)
        assert doc is None
        assert any(d.blocking for d in diags)

    def test_none_binding_forbids_finance_type(self):
        text = _load("campus.assembly.yaml").replace(
            "    grid:\n      type: none",
            "    grid:\n      type: none\n      finance_type: pv_system",
        )
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == ASM_SYN_TYPE and d.params.get("reason") == "forbidden_for_type_none"
            for d in diags
        )

    def test_driver_property_interface_xor(self):
        text = _load("campus.assembly.yaml").replace(
            "          property: rated_capacity_kwp",
            "          property: rated_capacity_kwp\n          interface: electric_out",
        )
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == ASM_SYN_TYPE and d.params.get("reason") == "property_interface_xor"
            for d in diags
        )

    def test_driver_interface_requires_aggregation(self):
        text = _load("campus.assembly.yaml").replace(
            "          aggregation: integrate_positive\n", "", 1
        )
        doc, diags = _structure(text)
        assert doc is None
        assert any(d.code == ASM_SYN_TYPE for d in diags)

    def test_driver_property_forbids_aggregation(self):
        text = _load("campus.assembly.yaml").replace(
            "          property: rated_capacity_kwp",
            "          property: rated_capacity_kwp\n          aggregation: scalar",
        )
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == ASM_SYN_TYPE and d.params.get("reason") == "forbidden_for_property_driver"
            for d in diags
        )


# ---------------------------------------------------------------------------
# finance 领域门禁(快照身份/绑定覆盖/类别/driver/量纲)
# ---------------------------------------------------------------------------


class TestFinanceGates:
    def test_fixture_is_genuine_merger_output(self):
        """静态快照夹具须经 finance 公开边界解析为密封快照(血缘证明，非手写编造)。"""
        from iesplan.finance import effective_from_yaml, effective_to_yaml

        raw = (SAMPLES_DIR / "finance" / "campus.effective-finance.yaml").read_text(
            encoding="utf-8"
        )
        effective = effective_from_yaml(raw)
        assert effective.profile_id == "cn-north-demo"
        assert set(effective.finance_types) >= {"pv_system", "battery_system", "heat_pump_system"}
        assert set(effective.energy_prices) >= {"grid_import", "pv_export"}
        assert effective_from_yaml(effective_to_yaml(effective)).to_dict() == effective.to_dict()

    def test_snapshot_profile_mismatch_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace("profile_id: cn-north-demo", "profile_id: other")
        result = _validate_campus(text)
        assert result.artifact is None
        assert any(
            d.code == "ASM-FIN-001" and d.params.get("reason") == "snapshot_profile_mismatch"
            for d in result.diagnostics
        )

    def test_object_ref_without_injection_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "    kind: relative_file\n    path: finance/campus.effective-finance.yaml",
            "    kind: object\n    object_id: fin:campus",
        )
        result = _validate_campus(text)
        assert result.artifact is None
        assert any(
            d.code == "ASM-FIN-001" and d.params.get("reason") == "snapshot_unavailable"
            for d in result.diagnostics
        )

    def test_object_ref_with_injection_passes(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "    kind: relative_file\n    path: finance/campus.effective-finance.yaml",
            "    kind: object\n    object_id: fin:campus",
        )
        result = validate_assembly_text(
            text, package_dir=SAMPLES_DIR, finance_snapshots=_campus_snapshots()
        )
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
        assert result.artifact.receipt.to_dict()["finance_snapshot"]["profile_id"] == "cn-north-demo"

    def test_missing_instance_binding_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "    heat_load:\n      type: none\n", ""
        )
        result = _validate_campus(text)
        assert result.artifact is None
        assert any(
            d.code == "ASM-BIND-002" and d.params.get("reason") == "instance_missing"
            for d in result.diagnostics
        )

    def test_unknown_finance_type_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace("finance_type: pv_system", "finance_type: nope")
        result = _validate_campus(text)
        assert result.artifact is None
        assert any(
            d.code == "ASM-BIND-002" and d.params.get("reason") == "finance_type_unknown"
            for d in result.diagnostics
        )

    def test_undeclared_driver_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace("capacity_kw:\n", "capacity_nope:\n")
        result = _validate_campus(text)
        assert result.artifact is None
        assert any(
            d.code == "ASM-BIND-002" and d.params.get("reason") == "driver_undeclared"
            for d in result.diagnostics
        )

    def test_incomplete_drivers_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "        generated_kwh:\n"
            "          interface: electric_out\n"
            "          aggregation: integrate_positive\n",
            "",
        )
        result = _validate_campus(text)
        assert result.artifact is None
        diag = next(
            d
            for d in result.diagnostics
            if d.code == "ASM-BIND-002" and d.params.get("reason") == "drivers_incomplete"
        )
        assert list(diag.params["missing"]) == ["generated_kwh"]

    def test_existing_skips_sunk_drivers(self, init_registry):
        # existing 只要求 period_variable_om.linear 齐全；沉没分量不强制映射。
        # 存量 pv1 的 planning 不得再引用沉没分量(upfront/annual)，目标与约束
        # 同步改写为仅含周期分量与计费分量。
        text = _load("campus.assembly.yaml").replace(
            "  pv1:\n    definition:\n      id: ies.device.pv\n    asset_origin: new",
            "  pv1:\n    definition:\n      id: ies.device.pv\n    asset_origin: existing",
        )
        text = text.replace(
            "        capacity_kw:\n          property: rated_capacity_kwp\n",
            "",
        )
        text = text.replace(
            "    expression: pv1__upfront_capex + 1 year * pv1__annual_fixed_om + pv1__period_variable_om + import_meter__period_energy_purchase",
            "    expression: pv1__period_variable_om + import_meter__period_energy_purchase",
        )
        result = _validate_campus(text)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def test_driver_dims_mismatch_blocked(self, init_registry):
        # capacity_kw 改绑功率接口：CNY/kW × (kW·h) ≠ currency
        text = _load("campus.assembly.yaml").replace(
            "        capacity_kw:\n          property: rated_capacity_kwp",
            "        capacity_kw:\n"
            "          interface: electric_out\n"
            "          aggregation: integrate_positive",
        )
        result = _validate_campus(text)
        assert result.artifact is None
        assert any(
            d.code == "ASM-BIND-002" and d.params.get("reason") == "driver_dims_mismatch"
            for d in result.diagnostics
        )

    def test_driver_unknown_target_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "          property: rated_capacity_kwp",
            "          property: rated_capacity_nope",
        )
        result = _validate_campus(text)
        assert result.artifact is None
        assert any(
            d.code == "ASM-BIND-002" and d.params.get("reason") == "target_unknown_property"
            for d in result.diagnostics
        )

    def test_scalar_upfront_annual_and_interface_period_pass(self, init_registry):
        """合法组合放行: 标量 property 用于 upfront/annual, period 只用
        接口时序(园区样例即此形态)。"""
        result = _validate_campus(_load("campus.assembly.yaml"))
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
        assert not any(d.blocking for d in result.diagnostics)

    def test_scalar_period_variable_om_blocked(self, init_registry):
        """禁令组合阻断: 标量 property×无时间维度费率被 period_variable_om
        消费, 在装配公开输入边界以明确诊断拒绝, 不进入求解。"""
        from iesplan.finance import effective_from_yaml

        raw = (SAMPLES_DIR / "finance" / "campus.effective-finance.yaml").read_text(
            encoding="utf-8"
        )
        old_block = (
            "    period_variable_om:\n"
            "      linear:\n"
            "        generated_kwh:\n"
            "          unit_cost:\n"
            "            value: '0.01'\n"
            "            unit: CNY/kWh\n"
        )
        assert raw.count(old_block) == 1
        mutated = raw.replace(
            old_block,
            "    period_variable_om:\n"
            "      linear:\n"
            "        capacity_kw:\n"
            "          unit_cost:\n"
            "            value: '0.01'\n"
            "            unit: CNY/kW\n",
        )
        text = _load("campus.assembly.yaml").replace(
            "        generated_kwh:\n"
            "          interface: electric_out\n"
            "          aggregation: integrate_positive\n",
            "",
        )
        text = text.replace(
            "    kind: relative_file\n    path: finance/campus.effective-finance.yaml",
            "    kind: object\n    object_id: fin:scalar-period",
        )
        result = validate_assembly_text(
            text,
            package_dir=SAMPLES_DIR,
            finance_snapshots={"fin:scalar-period": effective_from_yaml(mutated)},
        )
        assert result.artifact is None
        diag = next(
            d
            for d in result.diagnostics
            if d.code == "ASM-BIND-002"
            and d.params.get("reason") == "period_variable_om_scalar_unsupported"
        )
        assert diag.blocking
        assert diag.params.get("driver") == "capacity_kw"
        assert diag.params.get("cost_unit") == "CNY/kW"


# ---------------------------------------------------------------------------
# 能源计费绑定(ASM-TAR-001)
# ---------------------------------------------------------------------------


class TestTariffGates:
    def _reason(self, text, reason):
        result = _validate_campus(text)
        assert result.artifact is None, [d.to_dict() for d in result.diagnostics]
        assert any(
            d.code == "ASM-TAR-001" and d.params.get("reason") == reason
            for d in result.diagnostics
        )

    def test_unknown_price_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace("price: grid_import", "price: nope")
        self._reason(text, "price_unknown")

    def test_unknown_instance_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace("instance: grid", "instance: nope", 1)
        self._reason(text, "instance_unknown")

    def test_unknown_interface_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "interface: electricity_import", "interface: nope", 1
        )
        self._reason(text, "interface_unknown")

    @pytest.mark.parametrize(
        ("binding", "interface"),
        [
            ("import_meter", "electricity_import"),  # out + purchase
            ("import_meter", "electricity_export"),  # in + purchase
            ("export_meter", "electricity_import"),  # out + sale
            ("export_meter", "electricity_export"),  # in + sale
        ],
    )
    def test_technical_direction_independent_of_purchase_sale(
        self, init_registry, binding, interface
    ):
        text = _load("campus.assembly.yaml")
        previous = (
            "electricity_import" if binding == "import_meter" else "electricity_export"
        )
        text = text.replace(f"    interface: {previous}", f"    interface: {interface}", 1)
        result = _validate_campus(text)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def test_carrier_mismatch_blocked(self, init_registry):
        # electricity 价格绑到 heat 接口：方向自由，载体仍阻断
        text = _load("campus.assembly.yaml").replace(
            "    instance: grid\n    interface: electricity_import",
            "    instance: hp1\n    interface: heat_out",
            1,
        )
        self._reason(text, "carrier_mismatch")

    def test_predefined_interface_not_meterable(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "    instance: grid\n    interface: electricity_import",
            "    instance: pv1\n    interface: solar_irradiance",
            1,
        )
        self._reason(text, "interface_not_meterable")

    def test_bidirectional_net_interface_not_meterable(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "    instance: grid\n    interface: electricity_import",
            "    instance: bat1\n    interface: electricity",
            1,
        )
        self._reason(text, "interface_not_meterable")

    def test_scalar_aggregation_rejected_at_structure(self):
        text = _load("campus.assembly.yaml").replace(
            "    aggregation: integrate_positive", "    aggregation: scalar", 1
        )
        doc, diags = _structure(text)
        assert doc is None
        assert any(d.code == "ASM-SYN-005" for d in diags)

    def test_binding_id_reserved_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace("  import_meter:\n", "  import__meter:\n")
        self._reason(text, "binding_id_reserved")

    def test_binding_id_clash_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace("  import_meter:\n", "  grid:\n")
        self._reason(text, "binding_id_clashes_instance")

    def test_time_series_price_passes(self, init_registry):
        text = _load("campus.assembly.yaml").replace("price: grid_import\n", "price: grid_import_tou\n", 1)
        result = _validate_campus(text)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def test_series_point_count_not_checked_at_assembly(self, init_registry):
        # 全期覆盖不在装配核对(只核对分辨率一致): 点数不符仍签发,
        # 覆盖核对统一在任务提交边界(见 tasks 域 check_tariff_horizon_coverage)。
        from iesplan.finance import effective_from_yaml

        raw = (SAMPLES_DIR / "finance" / "campus.effective-finance.yaml").read_text(
            encoding="utf-8"
        )
        mutated = raw.replace("point_count: 8760", "point_count: 8759")
        text = _load("campus.assembly.yaml").replace(
            "price: grid_import\n", "price: grid_import_tou\n", 1
        )
        text = text.replace(
            "    kind: relative_file\n    path: finance/campus.effective-finance.yaml",
            "    kind: object\n    object_id: fin:tou",
        )
        result = validate_assembly_text(
            text,
            package_dir=SAMPLES_DIR,
            finance_snapshots={"fin:tou": effective_from_yaml(mutated)},
        )
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def test_series_resolution_mismatch_blocked(self, init_registry):
        from iesplan.finance import effective_from_yaml

        raw = (SAMPLES_DIR / "finance" / "campus.effective-finance.yaml").read_text(
            encoding="utf-8"
        )
        mutated = raw.replace("resolution: 1h\n      point_count", "resolution: 30min\n      point_count")
        text = _load("campus.assembly.yaml").replace(
            "price: grid_import\n", "price: grid_import_tou\n", 1
        )
        text = text.replace(
            "    kind: relative_file\n    path: finance/campus.effective-finance.yaml",
            "    kind: object\n    object_id: fin:tou",
        )
        result = validate_assembly_text(
            text,
            package_dir=SAMPLES_DIR,
            finance_snapshots={"fin:tou": effective_from_yaml(mutated)},
        )
        assert result.artifact is None
        assert any(
            d.code == "ASM-TAR-001" and d.params.get("reason") == "series_resolution_mismatch"
            for d in result.diagnostics
        )


# ---------------------------------------------------------------------------
# 规划目标与约束(ASM-PLAN-001/002/003)
# ---------------------------------------------------------------------------


class TestPlanningGates:
    def _reason(self, text, code, reason):
        result = _validate_campus(text)
        assert result.artifact is None, [d.to_dict() for d in result.diagnostics]
        assert any(
            d.code == code and d.params.get("reason") == reason
            for d in result.diagnostics
        )

    def test_undefined_identifier_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "pv1__upfront_capex + 1 year", "nope__upfront_capex + 1 year"
        )
        self._reason(text, "ASM-PLAN-003", "undefined_identifier")

    def test_sunk_component_reference_blocked(self, init_registry):
        # 存量 pv1 引用沉没分量 upfront_capex
        text = _load("campus.assembly.yaml").replace(
            "  pv1:\n    definition:\n      id: ies.device.pv\n    asset_origin: new",
            "  pv1:\n    definition:\n      id: ies.device.pv\n    asset_origin: existing",
        )
        self._reason(text, "ASM-PLAN-003", "sunk_component_reference")

    def test_unproduced_none_instance_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "pv1__upfront_capex + 1 year", "grid__upfront_capex + 1 year"
        )
        self._reason(text, "ASM-PLAN-003", "unproduced_component")

    def test_direction_suffix_mismatch_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "import_meter__period_energy_purchase", "import_meter__period_energy_sale"
        )
        self._reason(text, "ASM-PLAN-003", "direction_suffix_mismatch")

    def test_currency_mismatch_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace("900000 CNY", "900000 USD")
        self._reason(text, "ASM-PLAN-002", "currency_mismatch")

    def test_dimension_mismatch_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "    expression: pv1__upfront_capex + 1 year * pv1__annual_fixed_om + pv1__period_variable_om + import_meter__period_energy_purchase",
            "    expression: pv1__upfront_capex + pv1__annual_fixed_om",
        )
        self._reason(text, "ASM-PLAN-002", "dimension_mismatch")

    def test_syntax_error_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "    expression: pv1__upfront_capex + 1 year * pv1__annual_fixed_om + pv1__period_variable_om + import_meter__period_energy_purchase",
            "    expression: pv1__upfront_capex +* 3",
        )
        self._reason(text, "ASM-PLAN-001", "expression_syntax")

    def test_invalid_sense_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace("sense: minimize", "sense: minimizex")
        self._reason(text, "ASM-PLAN-003", "objective_invalid")

    def test_valid_variable_passes(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "      unit: kWp\n  constraints:",
            "      unit: kWp\n"
            "    hp_capacity:\n"
            "      device_ref: hp1\n"
            "      parameter: rated_heat_kw\n"
            "      lower_bound: 0\n"
            "      upper_bound: 1000\n"
            "      unit: kW\n"
            "  constraints:",
        )
        result = _validate_campus(text)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def test_variable_string_bound_accepted(self, init_registry):
        # 界为十进制: 确定性投影输出规范字符串界，结构层接受(旧断言“字符串界
        # 阻断”与公共契约冲突，公共 PlanningConfig.to_dict 恒输出字符串界)。
        result = _validate_campus(_load("campus.assembly.yaml"))
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def test_variable_float_bound_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            '      lower_bound: "0"',
            "      lower_bound: 0.5",
        )
        result = _validate_campus(text)
        assert result.artifact is None
        assert any(
            d.code == "ASM-SYN-005" and d.blocking for d in result.diagnostics
        )

    def test_variable_reserved_collision_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "    pv_cap:\n"
            "      device_ref: pv1\n"
            "      parameter: rated_capacity_kwp\n"
            '      lower_bound: "0"\n'
            '      upper_bound: "500"\n'
            "      unit: kWp",
            "    pv1__upfront_capex:\n"
            "      device_ref: hp1\n"
            "      parameter: rated_heat_kw",
        )
        self._reason(text, "ASM-PLAN-003", "variable_reserved_collision")

    def test_variable_unknown_parameter_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "    pv_cap:\n"
            "      device_ref: pv1\n"
            "      parameter: rated_capacity_kwp\n"
            '      lower_bound: "0"\n'
            '      upper_bound: "500"\n'
            "      unit: kWp",
            "    hp_capacity:\n"
            "      device_ref: hp1\n"
            "      parameter: rated_nope",
        )
        self._reason(text, "ASM-PLAN-003", "variable_unknown_parameter")

    def test_disabled_constraint_skipped(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            '    annual_purchase_cap:\n'
            '      type: annual_purchase_cost_limit\n'
            '      expression: "import_meter__period_energy_purchase <= 900000 CNY"',
            '    annual_purchase_cap:\n'
            '      type: annual_purchase_cost_limit\n'
            '      expression: "nope__upfront_capex <= 1"\n'
            "      enabled: false",
        )
        result = _validate_campus(text)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def test_constraint_unknown_type_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "      type: annual_purchase_cost_limit",
            "      type: budget_cap",
        )
        result = _validate_campus(text)
        assert result.artifact is None
        self._reason(text, "ASM-PLAN-003", "constraint_invalid")


# ---------------------------------------------------------------------------
# 唯一规范化
# ---------------------------------------------------------------------------


class TestCanonicalization:
    def test_deterministic_bytes(self):
        t1 = _validated_text()
        t2 = _validated_text()
        assert t1 == t2

    def test_key_order_insensitive(self):
        import copy

        parsed = parse_assembly_doc(_load("campus.assembly.yaml"))
        assert parsed.doc is not None
        reordered = copy.deepcopy(parsed.doc)
        # 打乱顶层与嵌套键序(JSON/YAML 字段顺序无语义)
        keys = list(reordered)
        for k in keys:
            v = reordered.pop(k)
            reordered[k] = v
        devices = reordered["devices"]
        for dev_id in list(devices):
            entry = devices.pop(dev_id)
            devices[dev_id] = entry
        t1 = canonicalize_assembly_doc(parsed.doc)
        t2 = canonicalize_assembly_doc(reordered)
        assert t1 == t2

    def test_baseline_in_canonical_text(self):
        text = _validated_text()
        assert '"project_baseline":{"resolution":"1h","scenario_mode":"single","start_year":2026}' in text
        assert "time_axis" not in text
        assert "calculation" not in text
        assert "resources" not in text

    def test_number_unique_representation(self):
        parsed = parse_assembly_doc(_load("campus.assembly.yaml"))
        assert parsed.doc is not None
        parsed.doc["devices"]["pv1"]["properties"]["rated_capacity_kwp"]["value"] = 300.0
        t1 = canonicalize_assembly_doc(parsed.doc)
        parsed2 = parse_assembly_doc(_load("campus.assembly.yaml"))
        assert parsed2.doc is not None
        t2 = canonicalize_assembly_doc(parsed2.doc)
        assert t1 == t2  # 整值浮点与整数必须同规范字节 same canonical text
        assert '"rated_capacity_kwp":{"unit":"kWp","value":300}' in t1

    def test_nonfinite_number_rejected(self):
        parsed = parse_assembly_doc(_load("campus.assembly.yaml"))
        assert parsed.doc is not None
        parsed.doc["devices"]["pv1"]["properties"]["rated_capacity_kwp"]["value"] = float("nan")
        with pytest.raises(ValueError):
            canonicalize_assembly_doc(parsed.doc)

    def test_business_order_lists_preserved(self):
        parsed = parse_assembly_doc(_load("campus.assembly.yaml"))
        assert parsed.doc is not None
        parsed.doc["outputs"]["series"] = ["b.s1", "a.s2"]  # 声明顺序保留,不排序
        t1 = canonicalize_assembly_doc(parsed.doc)
        assert '"series":["b.s1","a.s2"]' in t1
        parsed2 = parse_assembly_doc(_load("campus.assembly.yaml"))
        assert parsed2.doc is not None
        parsed2.doc["outputs"]["series"] = ["a.s2", "b.s1"]
        t2 = canonicalize_assembly_doc(parsed2.doc)
        assert t1 != t2


# ---------------------------------------------------------------------------
# ValidatedAssemblyArtifact
# ---------------------------------------------------------------------------


class TestArtifact:
    def test_receipt_structure(self):
        text = _validated_text()
        result = validate_assembly_text(_load("campus.assembly.yaml"), package_dir=SAMPLES_DIR)
        assert result.artifact is not None
        receipt = result.artifact.receipt.to_dict()
        assert receipt["schema"] == SCHEMA_ID
        assert receipt["schema_version"] == SCHEMA_VERSION == "4.0.0"
        assert receipt["canonical_algorithm"] == {
            "id": CANON_ALGORITHM_ID,
            "version": CANON_ALGORITHM_VERSION,
        }
        assert CANON_ALGORITHM_VERSION == "4.0.0"
        assert set(receipt["dependencies"]["devices"]) >= {
            "ies.device.pv",
            "ies.device.electric_load",
        }
        assert "data/campus_load.data.csv" in receipt["resources"]
        assert receipt["finance_snapshot"] == {
            "ref": {
                "kind": "relative_file",
                "object_id": None,
                "media_type": None,
                "path": "finance/campus.effective-finance.yaml",
            },
            "profile_id": "cn-north-demo",
            "revision": None,
            "schema": "ies.effective-finance-config",
            "schema_version": "6.0.0",
            "currency": "CNY",
        }
        assert receipt["planning"] == {
            "objective_sense": "minimize",
            "variables": ["pv_cap"],
            "constraints": ["annual_purchase_cap"],
        }
        assert receipt["dependencies"]["baseline"] == {
            "resolution": "1h",
            "start_year": 2026,
            "scenario_mode": "single",
        }
        assert set(receipt["dependencies"]["equations"]) == {"contract", "ast"}
        # header-only
        assert "issued_at" not in receipt
        assert text == result.artifact.canonical_text

    def test_receipt_is_deterministic_and_deeply_immutable(self):
        dependencies = {"devices": {"pins": ["1.0.0"]}}
        resources = {"ds": {"columns": ["load"]}}
        first = ValidationReceipt(
            dependencies=dependencies,
            resources=resources,
        )
        expected = first.to_dict()
        dependencies["devices"]["pins"].append("tampered")
        resources["ds"]["columns"] = ["tampered"]
        assert first.to_dict() == expected
        with pytest.raises(TypeError):
            first.dependencies["devices"]["new"] = "forbidden"
        second = ValidationReceipt(
            dependencies={"devices": {"pins": ["1.0.0"]}},
            resources={"ds": {"columns": ["load"]}},
        )
        assert first.to_dict() == second.to_dict()
