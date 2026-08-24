"""ies.assembly 1.0.0 校验入口测试(roadmap 0.7.0 事项 2)。

覆盖:
- 手写 YAML 与 GUI 项目导出进入同一校验入口(validate_assembly_text /
  validate_project_export);
- 成功路径:签发不可变 ValidatedAssemblyArtifact(三件套一致 + 校验回执);
- 失败路径:阻断诊断 → 无 artifact,结构/模型/数据/图系统/计算兼容各阶段诊断定位;
- 资源解析(relative_file → 内容寻址对象 + 摘要一致性);
- 严格精确版本(不匹配版本号 → 阻断)。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from iesplan.assembly import (
    AssemblyValidationError,
    AssemblyValidationResult,
    parse_assembly_doc,
    validate_assembly_doc,
    validate_assembly_text,
    validate_project_export,
)
from iesplan.assembly.diags import (
    ASM_CALC_OPTIONS,
    ASM_INPUT_UNDECLARED,
    ASM_INPUT_UNFED,
    ASM_OUTPUT_REF,
    ASM_REF_DATASET,
    ASM_REF_MODEL_UNREG,
    ASM_RES_INVALID,
)
from iesplan.core.diagnostics import SEVERITY_BLOCKING, SEVERITY_ERROR

ASSEMBLY_DIR = Path(__file__).resolve().parent.parent / "iesplan" / "assembly"
SAMPLES_DIR = ASSEMBLY_DIR / "samples"
VALID_DIR = SAMPLES_DIR / "valid"
DATA_DIR = SAMPLES_DIR / "data"


#: 样例数据集元信息(供四阶段校验;注入到 validate_xxx 的 datasets 参数)
SAMPLE_DATASETS = {
    "campus_load": {
        "columns": ["e_load"],
        "column_units": {"e_load": "kWh"},
        "resolution": "1h",
        "sha256": hashlib.sha256((DATA_DIR / "campus_load.data.csv").read_bytes()).hexdigest(),
        "media_type": "text/csv",
    },
    "campus_heat": {
        "columns": ["h_load"],
        "column_units": {"h_load": "kWh"},
        "resolution": "1h",
        "sha256": hashlib.sha256((DATA_DIR / "campus_heat.data.csv").read_bytes()).hexdigest(),
        "media_type": "text/csv",
    },
}


@pytest.fixture
def init_registry():
    from iesplan.devices import init_registry
    from iesplan.devices.pricing import load_price_book

    init_registry(book=load_price_book())
    yield


class TestHappyPath:
    def test_handwritten_sample_issues_artifact(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS)
        assert isinstance(result, AssemblyValidationResult)
        assert result.ok, [(d.code, d.params) for d in result.diagnostics if d.blocking]
        artifact = result.artifact
        assert artifact is not None
        assert artifact.verify()
        assert artifact.verify_or_raise() is artifact
        # 三件套一致
        assert len(artifact.assembly_sha256) == 64
        assert artifact.receipt.assembly_sha256 == artifact.assembly_sha256
        # 回执依赖锁包含全部设备与计算引用
        deps = artifact.receipt.dependencies
        devices_lock = deps["devices"]
        assert "ies.device.heat_pump" in devices_lock
        assert "ies.device.grid_connection" in devices_lock
        assert deps["calculation"]["solver"].endswith("@1.7.2")
        # 资源摘要
        resources = artifact.receipt.resources
        assert "campus_load" in resources
        assert resources["campus_load"]["sha256"] == SAMPLE_DATASETS["campus_load"]["sha256"]
        assert resources["campus_load"]["media_type"] == "text/csv"


class TestUnhappyPath:
    def test_unknown_device_blocks_with_stable_code(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        text = text.replace("ies.device.heat_pump@1.3.0", "ies.device.unknown@1.0.0")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS)
        assert not result.ok and result.artifact is None
        assert any(d.code == ASM_REF_MODEL_UNREG for d in result.diagnostics)

    def test_version_mismatch_blocks(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        # heat_pump 1.3.0 → 1.0.0 触发精确版本不匹配
        text = text.replace("ies.device.heat_pump@1.3.0", "ies.device.heat_pump@1.0.0")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS)
        assert result.artifact is None
        diag = next(d for d in result.diagnostics if d.code == ASM_REF_MODEL_UNREG)
        assert diag.params["reason"] == "version_mismatch"
        assert diag.blocking is True

    def test_undeclared_parameter_blocks(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        text = text.replace("      cop_profile: 0\n", "      cop_profile: 0\n      magic: 1\n")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS)
        assert result.artifact is None
        assert any(d.code == ASM_INPUT_UNDECLARED for d in result.diagnostics)

    def test_missing_dataset_blocks(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        # 删除 elec_load.data 绑定 → 该设备缺数据
        text = text.replace(
            "    data:\n      e_load:\n        dataset: campus_load\n        column: e_load\n",
            "",
        )
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS)
        assert result.artifact is None
        # 缺 data → ASM-INPUT-004 + ASM-INPUT-PARAM(load_profile 必填) + ASM-REF-DATASET
        codes = {d.code for d in result.diagnostics}
        assert "ASM-INPUT-004" in codes or "ASM-INPUT-002" in codes

    def test_dataset_column_mismatch_blocks(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        text = text.replace("column: e_load\n", "column: nope\n")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS)
        assert result.artifact is None
        diag = next(d for d in result.diagnostics if d.code == ASM_REF_DATASET)
        assert diag.params["reason"] == "column_not_in_dataset"

    def test_resource_file_unreadable_blocks(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        # 指向不存在的相对文件
        text = text.replace("data/campus_load.data.csv", "data/missing.csv")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS)
        assert result.artifact is None
        assert any(d.code == ASM_RES_INVALID for d in result.diagnostics)

    def test_outputs_reference_unknown_device_blocks(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        text = text.replace(
            "  series:\n    - grid.electric_out\n",
            "  series:\n    - ghost.electric_out\n",
        )
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS)
        assert result.artifact is None
        diag = next(d for d in result.diagnostics if d.code == ASM_OUTPUT_REF)
        assert diag.params["scope"] == "ghost"

    def test_input_unfed_blocks(self, init_registry):
        # 单负荷设备,未连接到任何 source → 阶段 3 报告 ASM-INPUT-001
        zero_sha = "0" * 64
        text = (
            "schema: ies.assembly\n"
            'schema_version: "1.0.0"\n'
            "assembly:\n  id: bad\n  name: bad\n"
            'time_axis:\n  start: "2025-01-01T00:00:00Z"\n'
            '  end: "2025-01-02T00:00:00Z"\n  resolution: 1h\n  endpoint: left_closed_right_open\n'
            f"resources:\n  datasets:\n    ds:\n      source:\n        kind: object\n"
            f"        object_id: sha256:{zero_sha}\n"
            f'        sha256: "{zero_sha}"\n'
            "        media_type: text/csv\n"
            "devices:\n"
            "  load:\n"
            "    model: ies.device.electric_load@1.2.0\n"
            "    parameters: {peak_power_kw: 100}\n"
            "    data:\n"
            "      e_load:\n"
            "        dataset: ds\n"
            "        column: e_load\n"
            "connections: {}\n"
            "constraints: {}\n"
            "calculation:\n"
            "  mode: fixed_operation\n"
            "  generator: acme.generator.highs_milp@1.0.0\n"
            "  solver: ies.solver.highs@1.7.2\n"
            "outputs:\n  series: []\n  metrics: []\n"
            "extensions: {}\n"
        )
        datasets = {"ds": {"columns": ["e_load"], "column_units": {"e_load": "kWh"}, "resolution": "1h"}}
        result = validate_assembly_text(text, datasets=datasets)
        assert result.artifact is None
        assert any(d.code == ASM_INPUT_UNFED for d in result.diagnostics)

    def test_nonfinite_option_blocks(self, init_registry):
        parsed = parse_assembly_doc((VALID_DIR / "campus.assembly.yaml").read_text())
        assert parsed.doc is not None
        parsed.doc["calculation"]["options"]["relative_gap"] = float("inf")
        result = validate_assembly_doc(parsed.doc, package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS)
        assert result.artifact is None
        # 资源已经全部解析为 object 形态,不会触发 resource 诊断;
        # 非有限值进入计算兼容阶段(ASM-CALC-002)
        assert any(d.code == ASM_CALC_OPTIONS for d in result.diagnostics)


class TestProjectExport:
    def test_project_export_issues_artifact(self, init_registry):
        content = {
            "graph_id": 42,
            "name": "campus",
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
                            "cop_profile": 0,
                        },
                    },
                    {
                        "id": 3,
                        "device_type": "ies.device.electric_load",
                        "kind": "existing",
                        "params": {
                            "type_detail": "ies.device.electric_load",
                            "peak_power_kw": 100,
                            "load_profile": {"dataset_version_id": 17, "unit": "kW"},
                        },
                    },
                ],
                "ports": [
                    {
                        "id": 11,
                        "device_id": 1,
                        "name": "electric_out",
                        "port_type": "electric",
                        "direction": "out",
                    },
                    {
                        "id": 21,
                        "device_id": 2,
                        "name": "electric_in",
                        "port_type": "electric",
                        "direction": "in",
                    },
                    {
                        "id": 22,
                        "device_id": 2,
                        "name": "heat_out",
                        "port_type": "thermal",
                        "direction": "out",
                    },
                    {
                        "id": 31,
                        "device_id": 3,
                        "name": "electric_in",
                        "port_type": "electric",
                        "direction": "in",
                    },
                ],
                "connections": [
                    {"id": 101, "from_port_id": 11, "to_port_id": 21, "loss_rate": 0},
                    {"id": 102, "from_port_id": 11, "to_port_id": 31, "loss_rate": 0},
                ],
            },
            "calc_config": {"algorithm": "ies.algo.milp_hybrid@1.0.0", "tolerances": {"mip_rel_gap": 0.001}},
            "dataset_bindings": [{"dataset_version_id": 17}],
        }
        datasets = {
            17: {
                "columns": ["e_load"],
                "column_units": {"e_load": "kWh"},
                "resolution": "1h",
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "media_type": "text/csv",
            },
        }
        result = validate_project_export(content, datasets=datasets)
        assert result.ok, [(d.code, d.params) for d in result.diagnostics if d.blocking]
        artifact = result.artifact
        assert artifact is not None
        assert artifact.verify()
        # loss_rate 0 → 直接连接;无管道设备
        canonical = artifact.canonical_text
        assert "transport_pipe" not in canonical

    def test_project_export_pins_legacy_config_algorithm_and_solver(self, init_registry):
        content = {
            "graph_id": 7,
            "name": "legacy_config",
            "model": {"devices": [], "ports": [], "connections": []},
            "calc_config": {
                "algorithm": {"mode": "auto", "name": "ies.algo.milp_hybrid"},
                "solver": "highs",
            },
        }
        result = validate_project_export(content)
        assert result.ok, [(d.code, d.params) for d in result.diagnostics if d.blocking]
        assert result.artifact is not None
        calculation = json.loads(result.artifact.canonical_text)["calculation"]
        assert calculation["generator"] == "ies.algo.milp_hybrid@1.0.0"
        assert calculation["solver"] == "ies.solver.highs@1.7.2"

    def test_project_export_does_not_guess_unknown_algorithm_version(self, init_registry):
        content = {
            "graph_id": 8,
            "name": "unknown_algorithm",
            "model": {"devices": [], "ports": [], "connections": []},
            "calc_config": {
                "algorithm": {"mode": "manual", "name": "ies.algo.unknown"},
                "solver": "highs",
            },
        }
        result = validate_project_export(content)
        assert result.artifact is None
        assert any(
            d.location.get("field") == "calculation.generator" and d.blocking for d in result.diagnostics
        )

    def test_project_export_loss_rate_wraps_pipe(self, init_registry):
        content = {
            "graph_id": 43,
            "name": "campus_pipe",
            "model": {
                "devices": [
                    {
                        "id": 1,
                        "device_type": "ies.device.grid_connection",
                        "kind": "existing",
                        "params": {
                            "type_detail": "ies.device.grid_connection",
                            "max_import_power_kw": 800,
                            "max_export_power_kw": 0,
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
                            "cop_profile": 0,
                        },
                    },
                    {
                        "id": 3,
                        "device_type": "ies.device.heat_load",
                        "kind": "existing",
                        "params": {
                            "type_detail": "ies.device.heat_load",
                            "heat_profile": {"dataset_version_id": 18, "unit": "kW"},
                        },
                    },
                ],
                "ports": [
                    {
                        "id": 11,
                        "device_id": 1,
                        "name": "electric_out",
                        "port_type": "electric",
                        "direction": "out",
                    },
                    {
                        "id": 21,
                        "device_id": 2,
                        "name": "electric_in",
                        "port_type": "electric",
                        "direction": "in",
                    },
                    {
                        "id": 22,
                        "device_id": 2,
                        "name": "heat_out",
                        "port_type": "thermal",
                        "direction": "out",
                    },
                    {"id": 31, "device_id": 3, "name": "heat_in", "port_type": "thermal", "direction": "in"},
                ],
                "connections": [
                    {"id": 101, "from_port_id": 11, "to_port_id": 21, "loss_rate": 0},
                    {
                        "id": 102,
                        "from_port_id": 22,
                        "to_port_id": 31,
                        "loss_rate": 0.05,
                        "params": {"delay_steps": 2},
                    },
                ],
            },
            "calc_config": {"algorithm": "ies.algo.milp_hybrid@1.0.0"},
            "dataset_bindings": [{"dataset_version_id": 18}],
        }
        datasets = {
            18: {
                "columns": ["h_load"],
                "column_units": {"h_load": "kWh"},
                "resolution": "1h",
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "media_type": "text/csv",
            },
        }
        result = validate_project_export(content, datasets=datasets)
        assert result.ok, [(d.code, d.params) for d in result.diagnostics if d.blocking]
        canonical = result.artifact.canonical_text
        assert "transport_pipe@1.0.0" in canonical

    def test_project_export_missing_dataset_sha_blocks(self, init_registry):
        content = {
            "graph_id": 44,
            "name": "missing",
            "model": {"devices": [], "ports": [], "connections": []},
            "calc_config": {},
            "dataset_bindings": [{"dataset_version_id": 999}],
        }
        result = validate_project_export(content, datasets={999: {"columns": [], "resolution": "1h"}})
        assert result.artifact is None
        assert any(d.params.get("reason") == "dataset_sha256_required" for d in result.diagnostics)

    def test_artifact_corrupted_canonical_text_raises(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS)
        artifact = result.artifact
        # 篡改 canonical_text(模拟外部篡改)
        tampered_text = artifact.canonical_text.replace("campus_demo", "campus_demo_x")
        from iesplan.assembly.contracts import ValidatedAssemblyArtifact

        bad = ValidatedAssemblyArtifact(
            canonical_text=tampered_text,
            assembly_sha256=artifact.assembly_sha256,
            receipt=artifact.receipt,
        )
        assert not bad.verify()
        with pytest.raises(AssemblyValidationError):
            bad.verify_or_raise()


class TestArtifactTriple:
    def test_artifact_invariants(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR, datasets=SAMPLE_DATASETS)
        artifact = result.artifact
        # 不可变(校验尝试修改失败)
        with pytest.raises(FrozenInstanceError):
            artifact.canonical_text = "x"
        # 三件套:文本 + sha256 + 回执
        text = artifact.canonical_text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert digest == artifact.assembly_sha256
        assert artifact.receipt.assembly_sha256 == artifact.assembly_sha256
        assert artifact.receipt.schema_id == "ies.assembly"
        assert artifact.receipt.canonical_algorithm_id == "ies.assembly.canonical"
        # 诊断严重度分类
        for d in result.diagnostics:
            assert d.severity in (SEVERITY_BLOCKING, SEVERITY_ERROR, "warning", "info")
