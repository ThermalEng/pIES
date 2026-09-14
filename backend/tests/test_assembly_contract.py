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
    ValidatedAssemblyArtifact,
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
        assert schema["$id"] == f"{SCHEMA_ID}/2.0.0"
        assert schema["title"] == "ies.assembly 2.0.0 装配 YAML 契约"
        required = schema["required"]
        assert "schema" in required and "schema_version" in required
        assert set(required) >= {
            "assembly",
            "project_baseline",
            "devices",
            "connections",
            "constraints",
            "outputs",
            "extensions",
        }
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
        modes = device["properties"]["predefined_interfaces"]["additionalProperties"]["properties"][
            "mode"
        ]["enum"]
        assert set(modes) == {"constant", "data_repeat", "data_predict"}

    def test_schema_baseline_shape(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        baseline = schema["properties"]["project_baseline"]
        assert baseline["additionalProperties"] is False
        assert set(baseline["required"]) == {"resolution", "leap_year", "scenario_mode"}
        assert set(baseline["properties"]["resolution"]["enum"]) == {"15min", "30min", "1h"}
        assert baseline["properties"]["leap_year"]["type"] == "boolean"
        assert baseline["properties"]["scenario_mode"]["const"] == "single"

    def test_schema_sections_have_additional_properties_false(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        # 顶层 + 节级均拒绝未知字段
        assert schema["additionalProperties"] is False
        assert schema["properties"]["assembly"]["additionalProperties"] is False
        assert schema["properties"]["project_baseline"]["additionalProperties"] is False
        assert schema["properties"]["outputs"]["additionalProperties"] is False
        # 容器型节:devices/connections/constraints 的 additionalProperties 是
        # 嵌套的设备/边/约束对象 schema,均显式 additionalProperties: false
        for section in ("devices", "connections", "constraints"):
            assert schema["properties"][section]["additionalProperties"]["additionalProperties"] is False


# ---------------------------------------------------------------------------
# 合法 / 非法样例(结构阶段)
# ---------------------------------------------------------------------------


class TestSamples:
    def test_valid_sample_passes_structure(self):
        doc, diags = _structure(_load("campus.assembly.yaml"), source_name="campus.assembly.yaml")
        assert doc is not None, [d.to_dict() for d in diags]
        assert not any(d.blocking for d in diags)
        assert doc["schema"] == SCHEMA_ID
        assert doc["schema_version"] == SCHEMA_VERSION == "2.0.0"
        assert doc["assembly"]["id"] == "campus_demo"
        assert doc["project_baseline"] == {
            "resolution": "1h",
            "leap_year": False,
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
        text = _load("campus.assembly.yaml").replace("  leap_year: false\n", "")
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == ASM_SYN_FIELD and d.location["field"] == "project_baseline.leap_year"
            for d in diags
        )

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("resolution: 1h", "resolution: 5min"),
            ("leap_year: false", "leap_year: yes"),
            ("leap_year: false", "leap_year: 0"),
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
        text = _load("campus.assembly.yaml").replace('schema_version: "2.0.0"', 'schema_version: "1.0.0"')
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
        assert '"project_baseline":{"leap_year":false,"resolution":"1h","scenario_mode":"single"}' in text
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
        assert receipt["schema_version"] == SCHEMA_VERSION == "2.0.0"
        assert receipt["canonical_algorithm"] == {
            "id": CANON_ALGORITHM_ID,
            "version": CANON_ALGORITHM_VERSION,
        }
        assert CANON_ALGORITHM_VERSION == "2.0.0"
        assert set(receipt["dependencies"]["devices"]) >= {
            "ies.device.pv",
            "ies.device.electric_load",
        }
        assert "data/campus_load.data.csv" in receipt["resources"]
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
