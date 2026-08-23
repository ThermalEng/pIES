"""`ies.assembly` 1.0.0 契约测试(roadmap 0.7.0 事项 1)。

覆盖:
- 机器可读 JSON Schema 存在且可加载;
- 合法样例通过结构阶段,非法样例产出稳定结构诊断;
- 唯一规范化:相同语义 → 相同规范文本与摘要(键序无关、时间换算 UTC、
  数值唯一有限表示、未解析资源拒绝、非有限值拒绝);
- ValidatedAssemblyArtifact 三件套一致性校验与回执结构;
- 新增诊断码登记(ASM-SYN-006..009 / ASM-RES / ASM-CALC / ASM-OUT / ASM-ART / ASM-CONV)。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from iesplan.assembly import (
    ASSEMBLY_SCHEMA_PATH,
    CANON_ALGORITHM_ID,
    CANON_ALGORITHM_VERSION,
    SCHEMA_ID,
    SCHEMA_VERSION,
    AssemblyValidationError,
    ValidationReceipt,
    ValidatedAssemblyArtifact,
    assembly_sha256,
    canonicalize_assembly_doc,
    parse_assembly_doc,
)
from iesplan.assembly.diags import (
    ASM_ALL_CODES,
    ASM_ART_MISMATCH,
    ASM_CALC_MODE,
    ASM_CALC_OPTIONS,
    ASM_CONV_UNMAPPABLE,
    ASM_INPUT_UNDECLARED,
    ASM_OUTPUT_REF,
    ASM_RES_INVALID,
    ASM_SYN_FIELD,
    ASM_SYN_FORBIDDEN,
    ASM_SYN_PATH,
    ASM_SYN_SCHEMA,
    ASM_SYN_TYPE,
    ASM_SYN_VERSION_PIN,
)
from iesplan.core.diagnostics import DIAG_FIX_HINT_KEYS, DIAG_MESSAGE_KEYS

ASSEMBLY_DIR = Path(__file__).resolve().parent.parent / "iesplan" / "assembly"
SAMPLES_DIR = ASSEMBLY_DIR / "samples"
VALID_DIR = SAMPLES_DIR / "valid"
INVALID_DIR = SAMPLES_DIR / "invalid"


def _load(name: str) -> str:
    return (VALID_DIR / name).read_text(encoding="utf-8")


def _resolved_doc(name: str) -> dict:
    """解析合法样例并把所有 relative_file 解析为 object(模拟校验器资源解析)。

    规范化器对未解析资源确定性拒绝;规范形态测试不需要真实字节摘要,
    用占位 SHA-256 表达内容寻址形态即可。
    """
    result = parse_assembly_doc(_load(name))
    assert result.ok, [d.to_dict() for d in result.diagnostics]
    doc = {k: v for k, v in result.doc.items()}
    resources = {k: dict(v) for k, v in doc["resources"].items()}
    datasets = {
        ds_id: {
            "source": {
                "kind": "object",
                "object_id": f"sha256:{'0' * 64}",
                "sha256": "0" * 64,
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
        assert schema["$id"] == f"{SCHEMA_ID}/1.0.0"
        assert schema["title"] == "ies.assembly 1.0.0 装配 YAML 契约"
        required = schema["required"]
        assert "schema" in required and "schema_version" in required
        assert set(required) >= {
            "assembly", "time_axis", "resources", "devices", "connections",
            "constraints", "calculation", "outputs", "extensions",
        }

    def test_schema_enforces_exact_versions(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        model_pattern = schema["properties"]["devices"]["additionalProperties"]["properties"]["model"]["pattern"]
        assert "@" in model_pattern
        # 拒绝 latest / 范围版本 / 未版本化
        import re
        assert re.match(model_pattern, "ies.device.heat_pump@1.3.0")
        assert not re.match(model_pattern, "ies.device.heat_pump")
        assert not re.match(model_pattern, "ies.device.heat_pump@latest")
        assert not re.match(model_pattern, "ies.device.heat_pump@>=1.0.0")

    def test_schema_forbids_resolution_escape(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        path_pattern = (
            schema["properties"]["resources"]["properties"]["datasets"]
            ["additionalProperties"]["properties"]["source"]["oneOf"][0]
            ["properties"]["path"]["pattern"]
        )
        import re
        assert re.match(path_pattern, "data/campus_load.data.csv")
        assert not re.match(path_pattern, "/etc/passwd")
        assert not re.match(path_pattern, "../data/x.csv")

    def test_invalid_sample_diagnostics_located(self):
        result = parse_assembly_doc((INVALID_DIR / "bad.forbidden-field.assembly.yaml").read_text(encoding="utf-8"))
        diag = next(d for d in result.diagnostics if d.code == ASM_SYN_FORBIDDEN)
        # 顶层扫描路径前缀 + 设备/参数路径
        assert diag.location["field"].endswith("devices.hp1.parameters.command")
        assert diag.blocking is True

    def test_schema_sections_have_additional_properties_false(self):
        schema = json.loads((ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH).read_text(encoding="utf-8"))
        # 顶层 + 节级均拒绝未知字段
        assert schema["additionalProperties"] is False
        assert schema["properties"]["assembly"]["additionalProperties"] is False
        assert schema["properties"]["time_axis"]["additionalProperties"] is False
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
        result = parse_assembly_doc(_load("campus.assembly.yaml"), source_name="campus.assembly.yaml")
        assert result.ok, [d.to_dict() for d in result.diagnostics]
        assert result.doc is not None
        doc = result.doc
        assert doc["schema"] == SCHEMA_ID
        assert doc["schema_version"] == SCHEMA_VERSION
        assert doc["assembly"]["id"] == "campus_demo"
        assert set(doc["resources"]["datasets"]) == {"campus_load", "campus_heat"}
        assert doc["resources"]["datasets"]["campus_load"]["source"]["kind"] == "relative_file"
        assert set(doc["devices"]) == {"grid", "pv1", "bat1", "hp1", "elec_load", "heat_load"}
        assert doc["calculation"]["mode"] == "fixed_operation"
        assert doc["calculation"]["random_seed"] == 42

    @pytest.mark.parametrize(
        ("fname", "expected_code"),
        [
            ("bad.schema.assembly.yaml", ASM_SYN_SCHEMA),
            ("bad.version-pin.assembly.yaml", ASM_SYN_VERSION_PIN),
            ("bad.resource-path.assembly.yaml", ASM_SYN_PATH),
            ("bad.forbidden-field.assembly.yaml", ASM_SYN_FORBIDDEN),
            ("bad.mode.assembly.yaml", ASM_CALC_MODE),
        ],
    )
    def test_invalid_samples_block_with_stable_code(self, fname, expected_code):
        result = parse_assembly_doc((INVALID_DIR / fname).read_text(encoding="utf-8"))
        assert not result.ok
        assert result.doc is None
        codes = [d.code for d in result.diagnostics]
        assert expected_code in codes, f"{fname}: {codes}"

    def test_invalid_sample_diagnostics_located(self):
        result = parse_assembly_doc((INVALID_DIR / "bad.forbidden-field.assembly.yaml").read_text(encoding="utf-8"))
        diag = next(d for d in result.diagnostics if d.code == ASM_SYN_FORBIDDEN)
        # 位置路径以禁止键所在的设备/参数路径结尾(扫描起点不影响定位)
        assert diag.location["field"].endswith("devices.hp1.parameters.command")
        assert diag.blocking is True

    def test_unknown_section_rejected(self):
        text = _load("campus.assembly.yaml") + "\npipelines: []\n"
        result = parse_assembly_doc(text)
        assert not result.ok
        assert any(d.code == "ASM-SYN-002" for d in result.diagnostics)

    def test_missing_section_rejected(self):
        text = _load("campus.assembly.yaml").replace("extensions: {}\n", "")
        result = parse_assembly_doc(text)
        assert not result.ok
        fields = {d.params.get("section") for d in result.diagnostics if d.code == ASM_SYN_FIELD}
        assert "extensions" in fields

    def test_unknown_device_key_rejected(self):
        text = _load("campus.assembly.yaml").replace(
            "    parameters:\n      rated_heat_kw: 600", "    magic: 1\n    parameters:\n      rated_heat_kw: 600"
        )
        result = parse_assembly_doc(text)
        assert not result.ok
        assert any(
            d.code == "ASM-SYN-001" and d.location["field"] == "devices.hp1.magic"
            for d in result.diagnostics
        )

    def test_naive_timestamp_rejected(self):
        text = _load("campus.assembly.yaml").replace(
            'start: "2025-01-01T00:00:00+08:00"', 'start: "2025-01-01T00:00:00"'
        )
        result = parse_assembly_doc(text)
        assert not result.ok
        assert any("timestamp_must_have_zone" in str(d.params) for d in result.diagnostics)

    def test_nested_parameters_must_be_scalar(self):
        text = _load("campus.assembly.yaml").replace(
            "      cop_profile: 0", "      cop_profile: {a: 1}"
        )
        result = parse_assembly_doc(text)
        assert not result.ok
        assert any(
            d.code == ASM_SYN_TYPE and d.location["field"] == "devices.hp1.parameters.cop_profile"
            for d in result.diagnostics
        )

    def test_extensions_must_be_namespaced(self):
        text = _load("campus.assembly.yaml").replace("extensions: {}", "extensions:\n  meta: 1")
        result = parse_assembly_doc(text)
        assert not result.ok
        assert any("extensions_key_not_namespaced" in str(d.params) for d in result.diagnostics)


# ---------------------------------------------------------------------------
# 唯一规范化
# ---------------------------------------------------------------------------


class TestCanonicalization:
    def _doc(self):
        return _resolved_doc("campus.assembly.yaml")

    def test_deterministic_bytes_and_digest(self):
        doc = self._doc()
        t1, d1 = canonicalize_assembly_doc(doc)
        t2, d2 = canonicalize_assembly_doc(doc)
        assert t1 == t2 and d1 == d2 and d1 == assembly_sha256(t1)
        assert len(d1) == 64

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
        t1, d1 = canonicalize_assembly_doc(doc)
        t2, d2 = canonicalize_assembly_doc(reordered)
        assert d1 == d2
        assert t1 == t2

    def test_time_converted_to_utc_z(self):
        doc = self._doc()
        # start +08:00 → 前一日 16:00 UTC;end +08:00 → 16:00 UTC
        text, _ = canonicalize_assembly_doc(doc)
        assert '"start":"2024-12-31T16:00:00Z"' in text
        assert '"end":"2025-01-01T16:00:00Z"' in text
        assert "2025-01-01T00:00:00+08:00" not in text

    def test_number_unique_representation(self):
        doc = self._doc()
        doc["devices"]["grid"]["parameters"]["max_import_power_kw"] = 800.0  # 整值浮点
        t1, d1 = canonicalize_assembly_doc(doc)
        doc2 = self._doc()  # 原始 int 800
        t2, d2 = canonicalize_assembly_doc(doc2)
        assert d1 == d2, "整值浮点与整数必须同规范字节"
        assert '"max_import_power_kw":800' in t1
        # 非整值浮点使用最短往返表示
        doc3 = self._doc()
        doc3["calculation"]["options"]["relative_gap"] = 0.0001
        t3, _ = canonicalize_assembly_doc(doc3)
        assert '"relative_gap":0.0001' in t3

    def test_nonfinite_number_rejected(self):
        doc = self._doc()
        doc["devices"]["grid"]["parameters"]["max_import_power_kw"] = float("nan")
        with pytest.raises(ValueError):
            canonicalize_assembly_doc(doc)
        doc2 = self._doc()
        doc2["calculation"]["options"]["relative_gap"] = float("inf")
        with pytest.raises(ValueError):
            canonicalize_assembly_doc(doc2)

    def test_unresolved_relative_file_rejected(self):
        # 重新构造含未解析资源的文档(绕过 _resolved_doc)
        result = parse_assembly_doc(_load("campus.assembly.yaml"))
        with pytest.raises(ValueError, match="relative_file"):
            canonicalize_assembly_doc(result.doc)

    def test_business_order_lists_preserved(self):
        doc = self._doc()
        doc["outputs"]["series"] = ["b.s1", "a.s2"]  # 声明顺序保留,不排序
        t1, _ = canonicalize_assembly_doc(doc)
        assert '"series":["b.s1","a.s2"]' in t1
        doc2 = self._doc()
        doc2["outputs"]["series"] = ["a.s2", "b.s1"]
        t2, _ = canonicalize_assembly_doc(doc2)
        assert t1 != t2


# ---------------------------------------------------------------------------
# ValidatedAssemblyArtifact
# ---------------------------------------------------------------------------


class TestArtifact:
    def _artifact(self) -> ValidatedAssemblyArtifact:
        doc = _resolved_doc("campus.assembly.yaml")
        text, digest = canonicalize_assembly_doc(doc)
        receipt = ValidationReceipt(
            assembly_sha256=digest,
            dependencies={"devices": {"ies.device.heat_pump": "1.3.0"}},
            resources={"campus_load": {"sha256": "x" * 64, "media_type": "text/csv"}},
        )
        return ValidatedAssemblyArtifact(canonical_text=text, assembly_sha256=digest, receipt=receipt)

    def test_verify_passes(self):
        artifact = self._artifact()
        assert artifact.verify()
        assert artifact.verify_or_raise() is artifact

    def test_tampered_text_fails_verify(self):
        artifact = self._artifact()
        tampered = artifact.canonical_text.replace("campus_demo", "campus_demo2")
        bad = ValidatedAssemblyArtifact(
            canonical_text=tampered,
            assembly_sha256=artifact.assembly_sha256,
            receipt=artifact.receipt,
        )
        assert not bad.verify()
        with pytest.raises(AssemblyValidationError):
            bad.verify_or_raise()

    def test_receipt_structure(self):
        artifact = self._artifact()
        receipt = artifact.receipt.to_dict()
        assert receipt["schema"] == SCHEMA_ID
        assert receipt["schema_version"] == SCHEMA_VERSION
        assert receipt["canonical_algorithm"] == {
            "id": CANON_ALGORITHM_ID,
            "version": CANON_ALGORITHM_VERSION,
        }
        assert receipt["assembly_sha256"] == artifact.assembly_sha256
        assert receipt["issued_at"].endswith("Z")

    def test_tampered_receipt_sha_fails_verify(self):
        artifact = self._artifact()
        bad_receipt = ValidationReceipt(assembly_sha256="0" * 64)
        bad = ValidatedAssemblyArtifact(
            canonical_text=artifact.canonical_text,
            assembly_sha256=artifact.assembly_sha256,
            receipt=bad_receipt,
        )
        assert not bad.verify()


# ---------------------------------------------------------------------------
# 诊断码登记
# ---------------------------------------------------------------------------


class TestDiagRegistration:
    def test_new_codes_registered(self):
        for code in (
            ASM_SYN_SCHEMA, ASM_SYN_VERSION_PIN, ASM_SYN_FORBIDDEN, ASM_SYN_PATH,
            ASM_RES_INVALID, ASM_CALC_MODE, ASM_CALC_OPTIONS, ASM_OUTPUT_REF,
            ASM_ART_MISMATCH, ASM_CONV_UNMAPPABLE, ASM_INPUT_UNDECLARED,
        ):
            assert code in ASM_ALL_CODES
            assert code in DIAG_MESSAGE_KEYS, f"{code} 未登记消息键"
            assert code in DIAG_FIX_HINT_KEYS, f"{code} 未登记修复键"
            assert DIAG_MESSAGE_KEYS[code].startswith("ies.diag.asm.")

    def test_codes_unique(self):
        assert len(ASM_ALL_CODES) == len(set(ASM_ALL_CODES))
        assert "ASM-SYN-006" in ASM_ALL_CODES and "ASM-CONV-001" in ASM_ALL_CODES
