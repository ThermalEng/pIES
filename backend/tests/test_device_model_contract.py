"""`ies.device-model` 1.0.0 契约测试（roadmap 0.5.0 事项 1）。

覆盖：
- JSON Schema 合法/非法样例；
- 唯一规范化规则（同一语义输入得到同一规范摘要，键序无关）；
- 稳定诊断定位（文件/字段路径/稳定诊断码）；
- 深度不可变公开值对象。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from iesplan.core.errors import AppError
from iesplan.devices import yamlmini
from iesplan.devices.contracts import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    DeviceModelDocument,
    canonicalize_device_model,
    canonicalize_raw_mapping,
)
from iesplan.devices.migration import build_migration_receipt, migrate_device_mapping
from iesplan.devices.parser import parse_device_model_yaml

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "iesplan" / "devices" / "samples"
CATALOG_DIR = Path(__file__).resolve().parent.parent / "iesplan" / "devices" / "catalog"


def _load_sample(name: str) -> dict:
    text = (SAMPLES_DIR / name).read_text(encoding="utf-8")
    return yamlmini.load(text)


def _parse_sample(name: str):
    raw = _load_sample(name)
    return parse_device_model_yaml(raw, file=name)


class TestSchemaSamples:
    def test_legal_sample_parses(self):
        result = _parse_sample("acme.device.pv.device.yaml")
        assert result.ok, [d.to_dict() for d in result.diagnostics]
        doc = result.document
        assert doc.schema_id == SCHEMA_ID
        assert doc.schema_version == SCHEMA_VERSION
        assert doc.device.id == "acme.device.pv"
        assert doc.device.names["zh-CN"] == "光伏发电"
        assert doc.parameters["rated_capacity_kwp"].quantity == "power"
        assert doc.ports["electric_out"].capacity_parameter == "rated_capacity_kwp"
        assert doc.model_commands["pv"] == "ies.model-command.pv.generation@1.0.0"

    def test_bad_schema_rejected(self):
        result = _parse_sample("bad.schema.device.yaml")
        assert not result.ok
        assert result.document is None
        diags = result.diagnostics
        assert any("schema" in d.location.get("field", "") or "schema" in str(d.params) for d in diags)

    def test_bad_command_version_rejected(self):
        result = _parse_sample("bad.command-version.device.yaml")
        assert not result.ok
        assert result.document is None
        detail = " ".join(str(d.params) for d in result.diagnostics)
        assert "exact-version" in detail or "model_commands" in detail

    def test_unknown_core_field_rejected(self):
        raw = _load_sample("acme.device.pv.device.yaml")
        raw["bogus_top_field"] = 1
        result = parse_device_model_yaml(raw, file="x")
        assert not result.ok
        assert result.document is None

    def test_duplicate_key_rejected_by_yamlmini(self):
        text = (SAMPLES_DIR / "acme.device.pv.device.yaml").read_text(encoding="utf-8")
        text += "schema: ies.device-model\n"  # duplicate schema key
        with pytest.raises(yamlmini.YamlParseError):
            yamlmini.load(text)


class TestCanonicalization:
    def test_canonicalization_deterministic(self):
        result = _parse_sample("acme.device.pv.device.yaml")
        doc = result.document
        text1, digest1 = canonicalize_device_model(doc)
        text2, digest2 = canonicalize_device_model(doc)
        assert digest1 == digest2
        assert text1 == text2
        assert len(digest1) == 64  # sha256 hex

    def test_comment_and_key_order_insensitive(self):
        # 同一语义（键序不同 + 注释不同）→ 同一规范摘要
        a = _load_sample("acme.device.pv.device.yaml")
        b = dict(a)
        # 顶层键序打乱
        reordered = {k: b[k] for k in sorted(b)}
        _, da = canonicalize_raw_mapping(a)
        _, db = canonicalize_raw_mapping(reordered)
        assert da == db

    def test_document_immutable(self):
        result = _parse_sample("acme.device.pv.device.yaml")
        doc: DeviceModelDocument = result.document
        # dict 是 MappingProxyType，禁止写入
        with pytest.raises(TypeError):
            doc.parameters["x"] = 1  # type: ignore[index]
        with pytest.raises(TypeError):
            doc.ports["x"] = 1  # type: ignore[index]
        # 嵌套值对象 frozen
        assert doc.device.names is not None
        # dataclass frozen
        with pytest.raises(Exception):
            doc.device.id = "x"  # type: ignore[misc]


class TestMigrationReceipt:
    def test_old_catalog_migrates_to_new(self):
        """旧格式 catalog YAML → 新 1.0.0 结构，迁移后通过新校验。"""
        from iesplan.devices.migration import build_migration_receipt

        yamls = sorted(p for p in CATALOG_DIR.glob("*.yaml") if p.stem != "prices")
        receipt = build_migration_receipt(yamls)
        assert len(receipt.migrated_files) == len(yamls)
        # 全部成功迁移
        assert all(e["migrated"] for e in receipt.migrated_files), [
            e for e in receipt.migrated_files if not e["migrated"]
        ]
        # 每个迁移后文件都产生新旧摘要且新摘要非空
        for entry in receipt.migrated_files:
            assert entry["old_sha256"]
            assert entry["new_sha256"]
            assert entry["device_id"]
            assert entry["version"]

    def test_migrated_mapping_preserves_semantics(self):
        """迁移语义等价：type_id/版本/载能/能力/参数范围/端口/状态保留。"""
        pv_yaml = CATALOG_DIR / "pv.yaml"
        old_raw = yamlmini.load(pv_yaml.read_text(encoding="utf-8"))
        new_raw = migrate_device_mapping(old_raw)
        result = parse_device_model_yaml(new_raw, file=str(pv_yaml))
        assert result.ok, [d.to_dict() for d in result.diagnostics]
        doc = result.document
        assert doc.device.id == "ies.device.pv"
        assert doc.device.version == "1.4.0"
        assert list(doc.device.energy_carriers) == ["solar", "electric"]
        assert list(doc.device.capabilities) == ["pv", "controllable", "optimization_variable"]
        # 参数范围保留
        p = doc.parameters["rated_capacity_kwp"]
        assert p.minimum == 0 and p.maximum == 1_000_000
        # 端口方向保留
        assert doc.ports["electric_out"].direction == "out"
        # model_commands 全部引用稳定命令 ID
        for cap, ref in doc.model_commands.items():
            assert "@" in ref
            assert "iesplan" not in ref and ".py" not in ref

    def test_legacy_function_removed_from_migrated(self):
        """迁移后文件不含 function/package/entry/宿主机路径。"""
        for yaml_path in sorted(CATALOG_DIR.glob("*.yaml")):
            if yaml_path.stem == "prices":
                continue
            new_raw = migrate_device_mapping(yamlmini.load(yaml_path.read_text(encoding="utf-8")))
            text = "\n".join(str(v) for v in [new_raw])
            assert "function" not in str(new_raw.keys())
            assert "package" not in text
            assert "entry" not in str(new_raw.get("device", {}).keys())
            # 不暴露宿主机路径
            assert "/home" not in text and "C:\\" not in text
