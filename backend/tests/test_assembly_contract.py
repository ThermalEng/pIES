"""`ies.assembly` 2.0.0 最小契约测试(roadmap 0.8.0 切片 S2a)。

覆盖:
- 机器可读 JSON Schema 存在且可加载(2.0.0:含 project_baseline,无
  calculation/time_axis);
- 合法样例通过结构阶段,非法样例产出稳定结构诊断;
- calculation 章节以结构诊断拒绝;
- project_baseline 缺失/非法以结构诊断拒绝;
- 唯一规范化:相同语义 → 相同规范文本(键序无关、数值唯一有限表示、
  未解析资源拒绝、非有限值拒绝);
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
)
from iesplan.assembly.diags import (
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


def _resolved_doc(name: str) -> dict:
    """解析合法样例并把所有 relative_file 解析为 object(模拟校验器资源解析)。

    规范化器对未解析资源确定性拒绝;规范形态测试仅需对象形态占位。
    """
    doc, diags = _structure(_load(name))
    assert doc is not None, [d.to_dict() for d in diags]
    doc = {k: v for k, v in doc.items()}
    resources = {k: dict(v) for k, v in doc["resources"].items()}
    datasets = {
        ds_id: {
            "source": {
                "kind": "object",
                "object_id": f"file:{ds_id}.csv",
                "media_type": "text/csv",
            }
        }
        for ds_id in resources["datasets"]
    }
    resources["datasets"] = datasets
    doc["resources"] = resources
    return doc


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
            "resources",
            "devices",
            "connections",
            "constraints",
            "outputs",
            "extensions",
        }
        assert "calculation" not in required
        assert "time_axis" not in required

    def test_schema_enforces_exact_versions(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        model_pattern = schema["properties"]["devices"]["additionalProperties"]["properties"]["model"][
            "pattern"
        ]
        assert "@" in model_pattern
        # 拒绝 latest / 范围版本 / 未版本化
        import re

        assert re.match(model_pattern, "ies.device.heat_pump@2.0.0")
        assert not re.match(model_pattern, "ies.device.heat_pump")
        assert not re.match(model_pattern, "ies.device.heat_pump@latest")
        assert not re.match(model_pattern, "ies.device.heat_pump@>=1.0.0")

    def test_schema_baseline_shape(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        baseline = schema["properties"]["project_baseline"]
        assert baseline["additionalProperties"] is False
        assert set(baseline["required"]) == {"resolution", "leap_year", "scenario_mode"}
        assert set(baseline["properties"]["resolution"]["enum"]) == {"15min", "30min", "1h"}
        assert baseline["properties"]["leap_year"]["type"] == "boolean"
        assert baseline["properties"]["scenario_mode"]["const"] == "single"

    def test_schema_forbids_resolution_escape(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        path_pattern = schema["properties"]["resources"]["properties"]["datasets"]["additionalProperties"][
            "properties"
        ]["source"]["oneOf"][0]["properties"]["path"]["pattern"]
        import re

        assert re.match(path_pattern, "data/campus_load.data.csv")
        assert not re.match(path_pattern, "/etc/passwd")
        assert not re.match(path_pattern, "../data/x.csv")

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
        assert set(doc["resources"]["datasets"]) == {"campus_load", "campus_heat"}
        assert doc["resources"]["datasets"]["campus_load"]["source"]["kind"] == "relative_file"
        assert set(doc["devices"]) == {"grid", "pv1", "bat1", "hp1", "elec_load", "heat_load"}
        assert "calculation" not in doc
        assert "time_axis" not in doc

    @pytest.mark.parametrize(
        ("fname", "expected_code"),
        [
            ("bad.schema.assembly.yaml", ASM_SYN_SCHEMA),
            ("bad.version-pin.assembly.yaml", ASM_SYN_VERSION_PIN),
            ("bad.resource-path.assembly.yaml", ASM_SYN_PATH),
            ("bad.forbidden-field.assembly.yaml", ASM_SYN_FORBIDDEN),
            ("bad.calculation.assembly.yaml", ASM_SYN_SECTION),
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

    def test_time_axis_rejected_as_unknown_section(self):
        text = _load("campus.assembly.yaml") + '\ntime_axis:\n  resolution: 1h\n'
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == ASM_SYN_SECTION and d.params.get("section") == "time_axis" for d in diags
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

    def test_baseline_old_time_keys_rejected(self):
        text = _load("campus.assembly.yaml").replace(
            "  scenario_mode: single",
            '  scenario_mode: single\n  start: "2025-01-01T00:00:00Z"',
        )
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == "ASM-SYN-001" and d.location["field"] == "project_baseline.start"
            for d in diags
        )

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
            "    parameters:\n      rated_heat_kw: 600",
            "    magic: 1\n    parameters:\n      rated_heat_kw: 600",
        )
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == "ASM-SYN-001" and d.location["field"] == "devices.hp1.magic" for d in diags
        )

    def test_nested_parameters_must_be_scalar(self):
        text = _load("campus.assembly.yaml").replace("      cop: 3.5", "      cop: {a: 1}")
        doc, diags = _structure(text)
        assert doc is None
        assert any(
            d.code == ASM_SYN_TYPE and d.location["field"] == "devices.hp1.parameters.cop"
            for d in diags
        )

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
# 唯一规范化
# ---------------------------------------------------------------------------


class TestCanonicalization:
    def _doc(self):
        return _resolved_doc("campus.assembly.yaml")

    def test_deterministic_bytes(self):
        doc = self._doc()
        t1 = canonicalize_assembly_doc(doc)
        t2 = canonicalize_assembly_doc(doc)
        assert t1 == t2

    def test_key_order_insensitive(self):
        import copy

        doc = self._doc()
        reordered = copy.deepcopy(doc)
        # 打乱顶层与嵌套键序(JSON/YAML 字段顺序无语义)
        keys = list(reordered)
        for k in keys:
            v = reordered.pop(k)
            reordered[k] = v
        devices = reordered["devices"]
        for dev_id in list(devices):
            entry = devices.pop(dev_id)
            devices[dev_id] = entry
        t1 = canonicalize_assembly_doc(doc)
        t2 = canonicalize_assembly_doc(reordered)
        assert t1 == t2

    def test_baseline_in_canonical_text(self):
        doc = self._doc()
        text = canonicalize_assembly_doc(doc)
        assert '"project_baseline":{"leap_year":false,"resolution":"1h","scenario_mode":"single"}' in text
        assert "time_axis" not in text
        assert "calculation" not in text

    def test_number_unique_representation(self):
        doc = self._doc()
        doc["devices"]["pv1"]["parameters"]["rated_capacity_kwp"] = 300.0  # 整值浮点
        t1 = canonicalize_assembly_doc(doc)
        doc2 = self._doc()  # 原始 int 300
        t2 = canonicalize_assembly_doc(doc2)
        assert t1 == t2  # 整值浮点与整数必须同规范字节 same canonical text
        assert '"rated_capacity_kwp":300' in t1

    def test_nonfinite_number_rejected(self):
        doc = self._doc()
        doc["devices"]["pv1"]["parameters"]["rated_capacity_kwp"] = float("nan")
        with pytest.raises(ValueError):
            canonicalize_assembly_doc(doc)

    def test_unresolved_relative_file_rejected(self):
        # 重新构造含未解析资源的文档(绕过 _resolved_doc)
        result = parse_assembly_doc(_load("campus.assembly.yaml"))
        with pytest.raises(ValueError, match="relative_file"):
            canonicalize_assembly_doc(result.doc)

    def test_business_order_lists_preserved(self):
        doc = self._doc()
        doc["outputs"]["series"] = ["b.s1", "a.s2"]  # 声明顺序保留,不排序
        t1 = canonicalize_assembly_doc(doc)
        assert '"series":["b.s1","a.s2"]' in t1
        doc2 = self._doc()
        doc2["outputs"]["series"] = ["a.s2", "b.s1"]
        t2 = canonicalize_assembly_doc(doc2)
        assert t1 != t2


# ---------------------------------------------------------------------------
# ValidatedAssemblyArtifact
# ---------------------------------------------------------------------------


class TestArtifact:
    def _artifact(self) -> ValidatedAssemblyArtifact:
        doc = _resolved_doc("campus.assembly.yaml")
        text = canonicalize_assembly_doc(doc)
        receipt = ValidationReceipt(
            dependencies={"devices": {"ies.device.heat_pump": "1.3.0"}},
            resources={"campus_load": {"media_type": "text/csv"}},
        )
        return ValidatedAssemblyArtifact(canonical_text=text, receipt=receipt)

    def test_receipt_structure(self):
        artifact = self._artifact()
        receipt = artifact.receipt.to_dict()
        assert receipt["schema"] == SCHEMA_ID
        assert receipt["schema_version"] == SCHEMA_VERSION == "2.0.0"
        assert receipt["canonical_algorithm"] == {
            "id": CANON_ALGORITHM_ID,
            "version": CANON_ALGORITHM_VERSION,
        }
        assert CANON_ALGORITHM_VERSION == "2.0.0"
        # header-only
        assert "issued_at" not in receipt

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
