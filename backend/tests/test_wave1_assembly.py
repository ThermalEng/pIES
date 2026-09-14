"""装配域公共契约与生产管线测试。

覆盖(公开入口运行时断言 + 纯标准库 AST 静态断言,无需数据库):
- 公共装配入口面(ies.assembly 1.0.0)完整可用,旧管线模块与入口已移除;
- 现行契约常量与 ValidatedAssemblyArtifact 不可变二件套行为;
- 诊断码静态登记(ASM 域 ↔ 消息键/修复键,不改写 core 目录);
- 内部依赖方向(assembly 只依赖 core/devices/computation 公开面与域内模块,无穿透);
- 生产管线行为(手写文本 / 项目导出 → 同一校验入口 → 同一产物)。
"""

from __future__ import annotations

import ast
import importlib.util
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import iesplan.assembly as assembly
from iesplan.assembly import (
    ASSEMBLY_SCHEMA_PATH,
    CANON_ALGORITHM_ID,
    CANON_ALGORITHM_VERSION,
    SCHEMA_ID,
    SCHEMA_VERSION,
    VALIDATOR_ID,
    VALIDATOR_VERSION,
    AssemblyValidationResult,
    ValidatedAssemblyArtifact,
    build_assembly_doc_from_content,
    canonicalize_assembly_doc,
    parse_assembly_doc,
    validate_assembly_text,
    validate_project_export,
)
from iesplan.assembly.diags import (
    ASM_ALL_CODES,
    ASM_FIX_HINT_KEYS,
    ASM_MESSAGE_KEYS,
    ASM_SOLV_NO_SOURCE,
    make_asm_diag,
)
from iesplan.core.diagnostics import DIAG_FIX_HINT_KEYS, DIAG_MESSAGE_KEYS, NEW_DIAG_CODES

BACKEND_DIR = Path(__file__).resolve().parents[1]
ASM_DIR = BACKEND_DIR / "iesplan" / "assembly"
ASSEMBLY_DIR = ASM_DIR
SAMPLES_DIR = ASSEMBLY_DIR / "samples"
VALID_DIR = SAMPLES_DIR / "valid"

#: 样例数据集元信息(供校验入口 datasets 参数)
SAMPLE_DATASETS = {
    "campus_load": {
        "columns": ["e_load"],
        "column_units": {"e_load": "kWh"},
        "resolution": "1h",
        "media_type": "text/csv",
    },
    "campus_heat": {
        "columns": ["h_load"],
        "column_units": {"h_load": "kWh"},
        "resolution": "1h",
        "media_type": "text/csv",
    },
}

#: 已移除的旧管线模块(实现史正典名收敛后不得存在)
REMOVED_MODULES = (
    "iesplan.assembly.parser10",
    "iesplan.assembly.builder10",
    "iesplan.assembly.checker",
    "iesplan.assembly.plan",
    "iesplan.assembly.validator2",
)

#: 已移除的旧管线入口(不得再经公共门面暴露)
REMOVED_ENTRIES = (
    "parse_assembly",
    "load_assembly_file",
    "build_assembly",
    "build_assembly_text",
    "dumps_assembly",
    "check_assembly",
    "check_assembly_text",
    "check_graph_inputs",
    "plan_from_assembly",
    "plan_from_content",
    "validate_interface_network2",
    "ParseResult",
    "CheckResult",
    "AssemblyCheckError",
    "ValidatedInterfaceNetwork",
)


def _sample_text() -> str:
    return (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")


def _export_content() -> dict:
    """最小项目导出内容(电网 → 热泵单边供电)。"""
    return {
        "graph_id": 90,
        "name": "wave1_pipeline",
        "model": {
            "devices": [
                {
                    "id": 1,
                    "device_type": "ies.device.grid_connection",
                    "kind": "existing",
                    "params": {
                        "type_detail": "ies.device.grid_connection",
                        "max_import_power_kw": 800,
                        "max_export_power_kw": 200,
                    },
                },
                {
                    "id": 2,
                    "device_type": "ies.device.heat_pump",
                    "kind": "new",
                    "params": {
                        "type_detail": "ies.device.heat_pump",
                        "rated_heat_kw": 600,
                        "cop": 3.5,
                    },
                },
            ],
            "ports": [
                {"id": 11, "device_id": 1, "name": "electricity_import",
                 "port_type": "electric", "direction": "out"},
                {"id": 21, "device_id": 2, "name": "electricity_in",
                 "port_type": "electric", "direction": "in"},
            ],
            "connections": [
                {"id": 101, "from_port_id": 11, "to_port_id": 21, "loss_rate": 0},
            ],
        },
        "calc_config": {
            "mode": "fixed_operation",
            "generator": "ies.algo.milp_hybrid@1.0.0",
            "solver": "ies.solver.highs@1.7.2",
            "time_axis": {"resolution": "1h", "start": "2025-01-01T00:00:00Z"},
        },
    }


class TestPublicEntries:
    def test_pipeline_entries_callable(self):
        for entry in (
            parse_assembly_doc,
            build_assembly_doc_from_content,
            validate_assembly_text,
            validate_project_export,
            canonicalize_assembly_doc,
        ):
            assert callable(entry)

    def test_contract_names_exposed(self):
        for name in (
            "SCHEMA_ID", "SCHEMA_VERSION", "ASSEMBLY_SCHEMA_PATH",
            "CANON_ALGORITHM_ID", "CANON_ALGORITHM_VERSION",
            "VALIDATOR_ID", "VALIDATOR_VERSION",
            "ValidationReceipt", "ValidatedAssemblyArtifact",
            "AssemblyValidationError", "AssemblyValidationResult",
            "ParseDocResult", "BuildDocResult",
            "CheckContext", "BusSummary", "ASM_ALL_CODES",
        ):
            assert name in assembly.__all__, f"公共入口缺失: {name}"

    def test_removed_entries_absent(self):
        for name in REMOVED_ENTRIES:
            assert not hasattr(assembly, name), f"旧管线入口残留: {name}"
            assert name not in assembly.__all__

    def test_removed_modules_absent(self):
        for mod in REMOVED_MODULES:
            assert importlib.util.find_spec(mod) is None, f"旧管线模块残留: {mod}"


class TestContract:
    def test_schema_identity(self):
        assert SCHEMA_ID == "ies.assembly"
        assert SCHEMA_VERSION == "1.0.0"
        assert VALIDATOR_ID == "ies.assembly.validator"
        assert VALIDATOR_VERSION == "1.0.0"
        assert CANON_ALGORITHM_ID == "ies.assembly.canonical"
        assert CANON_ALGORITHM_VERSION == "1.0.0"

    def test_schema_file_loads(self):
        path = ASSEMBLY_DIR / ASSEMBLY_SCHEMA_PATH
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict) and payload

    def test_sample_issues_immutable_artifact(self):
        result = validate_assembly_text(
            _sample_text(), package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS
        )
        assert isinstance(result, AssemblyValidationResult)
        assert result.ok, [(d.code, d.params) for d in result.diagnostics if d.blocking]
        artifact = result.artifact
        assert isinstance(artifact, ValidatedAssemblyArtifact)
        assert artifact.canonical_text.endswith("\n")
        assert json.loads(artifact.canonical_text)["schema_version"] == "1.0.0"
        receipt = artifact.receipt
        assert receipt.schema_id == "ies.assembly"
        assert receipt.validator_id == "ies.assembly.validator"
        assert receipt.canonical_algorithm_id == "ies.assembly.canonical"
        with pytest.raises(FrozenInstanceError):
            artifact.canonical_text = "x"

    def test_canonical_text_deterministic(self):
        first = validate_assembly_text(
            _sample_text(), package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS
        )
        second = validate_assembly_text(
            _sample_text(), package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS
        )
        assert first.ok and second.ok
        assert first.artifact.canonical_text == second.artifact.canonical_text


class TestDiagCodes:
    def test_codes_statically_registered_without_core_mutation(self):
        for code in ASM_ALL_CODES:
            assert code in NEW_DIAG_CODES, f"{code} 未在 core.NEW_DIAG_CODES 静态登记"
            assert code not in DIAG_MESSAGE_KEYS, f"{code} 不得改写 core 消息目录"
            assert code not in DIAG_FIX_HINT_KEYS, f"{code} 不得改写 core 修复目录"
            assert code in ASM_MESSAGE_KEYS, f"{code} 未登记 ASM 消息键"
            assert code in ASM_FIX_HINT_KEYS, f"{code} 未登记 ASM 修复键"

    def test_message_key_namespace(self):
        for code in ASM_ALL_CODES:
            assert ASM_MESSAGE_KEYS[code].startswith("ies.diag.asm."), code
            assert ASM_FIX_HINT_KEYS[code].startswith("ies.fix.asm."), code

    def test_make_diag_accepts_asm_codes(self):
        d = make_asm_diag(ASM_SOLV_NO_SOURCE, params={"carrier": "heat"})
        assert d.message_key == "ies.diag.asm.solv.no_source"
        assert d.fix_hint_key == "ies.fix.asm.solv.no_source"
        assert d.severity == "error"

    def test_codes_unique(self):
        assert len(ASM_ALL_CODES) == len(set(ASM_ALL_CODES))
        assert "ASM-SYN-001" in ASM_ALL_CODES and "ASM-CONST-003" in ASM_ALL_CODES


def _iter_asm_modules():
    for path in sorted(ASM_DIR.rglob("*.py")):
        if path.name == "__init__.py":
            rel = path.parent.relative_to(BACKEND_DIR)
        else:
            rel = path.relative_to(BACKEND_DIR).with_suffix("")
        yield ".".join(rel.parts), path


def _iesplan_targets(path: Path) -> list[tuple[str, str]]:
    """源码中 iesplan 绝对导入目标(含函数级懒导入)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name != "*":
                    found.append((node.module, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, ""))
    return [(t, n) for t, n in found if t == "iesplan" or t.startswith("iesplan.")]


class TestDependencyDirection:
    def test_no_private_cross_imports(self):
        violations = []
        for mod, path in _iter_asm_modules():
            for target, name in _iesplan_targets(path):
                if target.startswith("iesplan.assembly.") and name.startswith("_"):
                    violations.append((mod, target, name))
        assert not violations, f"assembly 内部仍有私有符号导入: {violations}"

    def test_no_outward_or_penetrating_deps(self):
        """assembly 只依赖 core/devices/computation 公开面与域内模块,不穿透业务执行层。"""
        allowed_tops = {"assembly", "core", "devices", "computation"}
        forbidden_tops = {
            "services", "engines", "worker", "api", "application",
            "models", "storage", "tasks", "analysis", "finance", "metrics",
        }
        violations = []
        for mod, path in _iter_asm_modules():
            for target, _name in _iesplan_targets(path):
                parts = target.split(".")
                top = parts[1] if len(parts) > 1 else ""
                if top in forbidden_tops:
                    violations.append((mod, target))
                assert top in allowed_tops, f"{mod} 出现未知顶层依赖: {target}"
        assert not violations, f"存在穿透业务执行层的依赖: {sorted(set(violations))}"

    def test_removed_modules_not_referenced(self):
        removed = {m.rsplit(".", 1)[-1] for m in REMOVED_MODULES}
        violations = []
        for mod, path in _iter_asm_modules():
            for target, _name in _iesplan_targets(path):
                leaf = target.rsplit(".", 1)[-1]
                if leaf in removed:
                    violations.append((mod, target))
        assert not violations, f"仍引用已移除模块: {sorted(set(violations))}"


class TestProductionPipeline:
    def test_text_and_export_share_pipeline(self):
        """手写文本与项目导出进入同一校验入口,签发同一产物形态。"""
        text_result = validate_assembly_text(
            _sample_text(), package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS
        )
        assert text_result.ok and text_result.artifact is not None
        export_result = validate_project_export(_export_content())
        assert export_result.ok, [
            (d.code, d.params) for d in export_result.diagnostics if d.blocking
        ]
        for artifact in (text_result.artifact, export_result.artifact):
            assert artifact.receipt.schema_version == "1.0.0"
            assert json.loads(artifact.canonical_text)["schema"] == "ies.assembly"

    def test_export_failure_blocks_without_artifact(self):
        content = _export_content()
        content["model"]["devices"][1]["params"]["type_detail"] = "ies.device.unknown"
        result = validate_project_export(content)
        assert result.artifact is None
        assert any(d.blocking for d in result.diagnostics)

    def test_registry_failure_blocks_without_fallback(self, monkeypatch):
        """注册表不可用时装配直接阻断,不回退静态表。"""
        from iesplan.core.errors import AppError

        def fake_get_device(type_id):
            raise AppError("设备注册表尚未初始化", code="SYS-CFG-001")

        def fake_list_devices():
            raise AppError("设备注册表尚未初始化", code="SYS-CFG-001")

        monkeypatch.setattr("iesplan.devices.get_device", fake_get_device)
        monkeypatch.setattr("iesplan.devices.list_devices", fake_list_devices)
        with pytest.raises(AppError):
            validate_project_export(_export_content())
