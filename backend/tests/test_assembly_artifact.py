"""ies.assembly 3.0.0 校验入口测试(roadmap 0.12 切片 F1)。

覆盖:
- 手写 YAML 与 GUI 项目导出进入同一校验入口(validate_assembly_text /
  validate_project_export);
- 成功路径:签发不可变 ValidatedAssemblyArtifact(规范文本 + 校验回执);
- 失败路径:阻断诊断 → 无 artifact,结构/模型/数据/图系统/输出引用各阶段诊断定位;
- 资源解析(relative_file → 内容寻址对象);
- 严格精确版本(不匹配版本号 → 阻断)。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from iesplan.assembly import (
    AssemblyValidationResult,
    parse_assembly_doc,
    validate_assembly_doc,
    validate_assembly_text,
    validate_project_export,
)
from iesplan.assembly.diags import (
    ASM_INPUT_UNDECLARED,
    ASM_INPUT_UNFED,
    ASM_OUTPUT_REF,
    ASM_REF_MODEL_UNREG,
    ASM_RES_INVALID,
)
from iesplan.core.diagnostics import SEVERITY_BLOCKING, SEVERITY_ERROR

ASSEMBLY_DIR = Path(__file__).resolve().parent.parent / "iesplan" / "assembly"
SAMPLES_DIR = ASSEMBLY_DIR / "samples"
VALID_DIR = SAMPLES_DIR / "valid"
DATA_DIR = SAMPLES_DIR / "data"


def _campus_snapshot():
    """园区样例有效快照：样例 YAML 经 finance 唯一公开边界解析为密封对象。"""
    from iesplan.finance import effective_from_yaml

    text = (SAMPLES_DIR / "finance" / "campus.effective-finance.yaml").read_text(encoding="utf-8")
    return effective_from_yaml(text)


#: 导出 finance 精确引用(object 种，调用方显式注入快照)
_EXPORT_FINANCE = {
    "ref": {"kind": "object", "object_id": "fin:campus", "media_type": "application/yaml"},
    "profile_id": "cn-north-demo",
}


def _finance_snapshots():
    return {"fin:campus": _campus_snapshot()}


#: 热泵成本绑定(driver 按增量语义齐全：new 全量)
_HP_BINDING = {
    "type": "costed",
    "finance_type": "heat_pump_system",
    "drivers": {
        "heat_kw": {"property": "rated_heat_kw"},
        "consumed_kwh": {"interface": "electricity_in", "aggregation": "integrate_positive"},
    },
}

#: 导出计费绑定(空：样例项目无能源计费声明)
_EXPORT_TARIFF = {}

#: 导出规划(d2 热泵三项分量齐全的目标；单变量无约束)
_EXPORT_PLANNING = {
    "objective": {
        "sense": "minimize",
        "expression": "hp1__upfront_capex + 1 year * hp1__annual_fixed_om + hp1__period_variable_om",
    },
    "variables": {
        "hp_cap": {
            "device_ref": "hp1",
            "parameter": "rated_heat_kw",
            "lower_bound": "0",
            "upper_bound": "1000",
            "unit": "kW",
        },
    },
    "constraints": {},
}


@pytest.fixture
def init_registry():
    from iesplan.devices import init_registry

    init_registry()
    yield


def _export_baseline(**overrides):
    """GUI 导出 project_baseline(显式声明,无静默默认)。"""
    cfg = {
        "resolution": "1h",
        "start_year": 2026,
        "scenario_mode": "single",
    }
    cfg.update(overrides)
    return cfg


class TestHappyPath:
    def test_handwritten_sample_issues_artifact(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert isinstance(result, AssemblyValidationResult)
        assert result.ok, [(d.code, d.params) for d in result.diagnostics if d.blocking]
        artifact = result.artifact
        assert artifact is not None
        # 回执依赖锁包含全部设备(裸 ID → 统一 schema 版本)
        deps = artifact.receipt.dependencies
        devices_lock = deps["devices"]
        assert devices_lock.get("ies.device.heat_pump") == "2.0.0"
        assert devices_lock.get("ies.device.grid_connection") == "2.0.0"
        assert "calculation" not in deps
        # 规范文本携带项目基线,不含 calculation/time_axis/resources;
        # 文件绑定保留 path 与 content 身份
        assert '"project_baseline"' in artifact.canonical_text
        assert "calculation" not in artifact.canonical_text
        assert "time_axis" not in artifact.canonical_text
        assert "resources" not in artifact.canonical_text
        assert '"path":"data/campus_load.data.csv"' in artifact.canonical_text
        assert '"media_type":"text/csv"' in artifact.canonical_text
        # 资源媒体类型(按绑定路径记录)
        resources = artifact.receipt.resources
        assert resources["data/campus_load.data.csv"]["media_type"] == "text/csv"


class TestUnhappyPath:
    def test_unknown_device_blocks_with_stable_code(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        text = text.replace("id: ies.device.heat_pump", "id: ies.device.unknown")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert not result.ok and result.artifact is None
        assert any(d.code == ASM_REF_MODEL_UNREG for d in result.diagnostics)

    def test_definition_version_pin_blocked(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        # definition.id 禁止 @版本钉选(结构阶段拒绝)
        text = text.replace("id: ies.device.heat_pump", "id: ies.device.heat_pump@2.0.0")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        assert any(d.code == "ASM-SYN-007" and d.blocking for d in result.diagnostics)

    def test_undeclared_parameter_blocks(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        text = text.replace(
            "      cop:\n        value: 3.5\n        unit: \"1\"\n",
            "      cop:\n        value: 3.5\n        unit: \"1\"\n      magic:\n        value: 1\n        unit: \"1\"\n",
        )
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        assert any(d.code == ASM_INPUT_UNDECLARED for d in result.diagnostics)

    def test_binding_to_non_predefined_slot_blocks(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        text = text.replace(
            "    predefined_interfaces: {}",
            "    predefined_interfaces:\n      electricity_import:\n        mode: constant\n        value: 1\n",
            1,
        )
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        diag = next(d for d in result.diagnostics if d.code == "ASM-BIND-001")
        assert diag.params["reason"] == "not_predefined_slot"

    def test_resource_file_unreadable_blocks(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        # 指向不存在的相对文件
        text = text.replace("data/campus_load.data.csv", "data/missing.csv")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        assert any(d.code == ASM_RES_INVALID for d in result.diagnostics)

    def test_outputs_reference_unknown_device_blocks(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        text = text.replace(
            "  series:\n    - grid.electricity_import\n",
            "  series:\n    - ghost.electricity_import\n",
        )
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        diag = next(d for d in result.diagnostics if d.code == ASM_OUTPUT_REF)
        assert diag.params["scope"] == "ghost"

    def test_input_unfed_blocks(self, init_registry):
        # 热泵 electricity_in 未连接任何 source → 阶段 3 报告 ASM-INPUT-001
        # (预定义数据接口不参与边连接, 用真实 in 端口覆盖该规则)
        text = (
            "schema: ies.assembly\n"
            'schema_version: "3.0.0"\n'
            "assembly:\n  id: bad\n  name: bad\n"
            "project_baseline:\n  resolution: 1h\n  start_year: 2026\n  scenario_mode: single\n"
            "devices:\n"
            "  hp1:\n"
            "    definition:\n"
            "      id: ies.device.heat_pump\n"
            "    asset_origin: new\n"
            "    properties:\n"
            "      rated_heat_kw:\n"
            "        value: 600\n"
            "        unit: kW\n"
            "      cop:\n"
            "        value: 3.5\n"
            '        unit: "1"\n'
            "    predefined_interfaces: {}\n"
            "connections: {}\n"
            "finance:\n"
            "  ref:\n"
            "    kind: object\n"
            '    object_id: "fin:campus"\n'
            "  profile_id: cn-north-demo\n"
            "finance_binding:\n"
            "  instances:\n"
            "    hp1:\n"
            "      type: none\n"
            "tariff_bindings: {}\n"
            "planning:\n"
            "  objective:\n"
            "    sense: minimize\n"
            '    expression: "0"\n'
            "  variables:\n"
            "    hp_cap:\n"
            "      device_ref: hp1\n"
            "      parameter: rated_heat_kw\n"
            '      lower_bound: "0"\n'
            '      upper_bound: "1000"\n'
            "      unit: kW\n"
            "  constraints: {}\n"
            "outputs:\n  series: []\n  metrics: []\n"
            "extensions: {}\n"
        )
        result = validate_assembly_text(text, finance_snapshots=_finance_snapshots())
        assert result.artifact is None
        assert any(d.code == ASM_INPUT_UNFED for d in result.diagnostics)

    def test_nonfinite_parameter_blocks(self, init_registry):
        parsed = parse_assembly_doc((VALID_DIR / "campus.assembly.yaml").read_text())
        assert parsed.doc is not None
        parsed.doc["devices"]["pv1"]["properties"]["rated_capacity_kwp"]["value"] = float("inf")
        result = validate_assembly_doc(parsed.doc, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        # 非有限值在结构阶段即拒绝(非合法标量,阻断)
        assert any(d.code == "ASM-SYN-005" and d.blocking for d in result.diagnostics)


class TestProjectExport:
    def test_project_export_issues_artifact(self, init_registry):
        content = {
            "name": "campus",
            "model": {
                "devices": [
                    {
                        "name": "grid1",
                        "device_type": "ies.device.grid_connection",
                        "kind": "existing",
                        "params": {
                            "max_import_power_kw": 800,
                            "max_export_power_kw": 200,
                        },
                    },
                    {
                        "name": "hp1",
                        "device_type": "ies.device.heat_pump",
                        "kind": "new",
                        "params": {
                            "rated_heat_kw": 600,
                            "cop": 3.5,
                        },
                    },
                    {
                        "name": "load1",
                        "device_type": "ies.device.electric_load",
                        "kind": "existing",
                        "params": {
                            "peak_power_kw": 100,
                            # 2.0 显式来源声明:常量需求(数据序列来源由 S2c 独立入口提供)
                            "electricity_demand": 100.0,
                        },
                    },
                ],
                # predefined 接口不进入 connections(需求序列由来源声明提供)
                # 可连接负荷输入口必须由供电边 feeding(无来边即输入不完备)
                "connections": [
                    {
                        "name": "c1",
                        "from_device": "grid1",
                        "from_port": "electricity_import",
                        "to_device": "hp1",
                        "to_port": "electricity_in",
                    },
                    {
                        "name": "c2",
                        "from_device": "grid1",
                        "from_port": "electricity_import",
                        "to_device": "load1",
                        "to_port": "electricity_in",
                    },
                ],
            },
            "project_baseline": _export_baseline(),
        }
        result = validate_project_export(
            content,
            finance=_EXPORT_FINANCE,
            finance_binding={
                "instances": {
                    "grid1": {"type": "none"},
                    "hp1": _HP_BINDING,
                    "load1": {"type": "none"},
                }
            },
            tariff_bindings=_EXPORT_TARIFF,
            planning=_EXPORT_PLANNING,
            finance_snapshots=_finance_snapshots(),
        )
        assert result.ok, [(d.code, d.params) for d in result.diagnostics if d.blocking]
        artifact = result.artifact
        assert artifact is not None
        # 连接只映射显式端点;构造器不自动合成管道设备
        canonical = artifact.canonical_text
        assert "transport_pipe" not in canonical
        assert '"mode":"constant"' in canonical

    def test_project_export_explicit_transport_pipe_assembles(self, init_registry):
        # 显式声明的 transport_pipe 设备正常参与装配(损耗由设备方程表达):
        # hp1 热输出 → pipe1 热输入; 构造器不合成隐式管道, 显式设备照常解析。
        content = {
            "name": "campus-pipe",
            "model": {
                "devices": [
                    {
                        "name": "grid1",
                        "device_type": "ies.device.grid_connection",
                        "kind": "existing",
                        "params": {
                            "max_import_power_kw": 800,
                            "max_export_power_kw": 200,
                        },
                    },
                    {
                        "name": "hp1",
                        "device_type": "ies.device.heat_pump",
                        "kind": "new",
                        "params": {
                            "rated_heat_kw": 600,
                            "cop": 3.5,
                        },
                    },
                    {
                        "name": "load1",
                        "device_type": "ies.device.electric_load",
                        "kind": "existing",
                        "params": {
                            "peak_power_kw": 100,
                            # 2.0 显式来源声明:常量需求(数据序列来源由 S2c 独立入口提供)
                            "electricity_demand": 100.0,
                        },
                    },
                    {
                        "name": "pipe1",
                        "device_type": "ies.device.transport_pipe",
                        "kind": "new",
                        "params": {"loss_rate": 0.03},
                    },
                ],
                # predefined 接口不进入 connections(需求序列由来源声明提供)
                "connections": [
                    {
                        "name": "c1",
                        "from_device": "grid1",
                        "from_port": "electricity_import",
                        "to_device": "hp1",
                        "to_port": "electricity_in",
                    },
                    {
                        "name": "c2",
                        "from_device": "hp1",
                        "from_port": "heat_out",
                        "to_device": "pipe1",
                        "to_port": "heat_in",
                    },
                    {
                        "name": "c3",
                        "from_device": "grid1",
                        "from_port": "electricity_import",
                        "to_device": "load1",
                        "to_port": "electricity_in",
                    },
                ],
            },
            "project_baseline": _export_baseline(),
        }
        result = validate_project_export(
            content,
            finance=_EXPORT_FINANCE,
            finance_binding={
                "instances": {
                    "grid1": {"type": "none"},
                    "hp1": _HP_BINDING,
                    "load1": {"type": "none"},
                    "pipe1": {"type": "none"},
                }
            },
            tariff_bindings=_EXPORT_TARIFF,
            planning=_EXPORT_PLANNING,
            finance_snapshots=_finance_snapshots(),
        )
        assert result.ok, [(d.code, d.params) for d in result.diagnostics if d.blocking]
        artifact = result.artifact
        assert artifact is not None
        # 显式管道进入设备依赖锁(与手写样例的依赖锁断言同口径)
        assert artifact.receipt.dependencies["devices"].get("ies.device.transport_pipe") == "2.0.0"
        # 管道实例与其损耗方程进入规范文本(显式设备非隐式合成)
        assert "pipe1" in artifact.canonical_text
        assert "loss_rate" in artifact.canonical_text

    def test_project_export_rejects_graph_id_name_fallback(self, init_registry):
        # graph_id 不再作为装配名回退: 无显式 name 即阻断, 不静默命名。
        content = {
            "graph_id": 99,
            "model": {"devices": [], "connections": []},
            "project_baseline": _export_baseline(),
        }
        result = validate_project_export(content)
        assert result.artifact is None
        assert any(
            d.code == "ASM-CONV-001"
            and d.params.get("reason") == "assembly_name_missing"
            and d.blocking
            for d in result.diagnostics
        )

    def test_project_export_rejects_missing_baseline(self, init_registry):
        # 缺失项目基线不再静默默认:阻断诊断返回
        content = {
            "name": "no_baseline",
            "model": {"devices": [], "connections": []},
        }
        result = validate_project_export(content)
        assert result.artifact is None
        assert any(
            d.code == "ASM-CONV-001"
            and d.params.get("reason") == "project_baseline_missing"
            and d.blocking
            for d in result.diagnostics
        )

    def test_project_export_rejects_unknown_resolution(self, init_registry):
        # 非法分辨率(不能确定性切分一天):阻断诊断返回
        content = {
            "name": "bad_resolution",
            "model": {"devices": [], "connections": []},
            "project_baseline": _export_baseline(resolution="5min"),
        }
        result = validate_project_export(content)
        assert result.artifact is None
        assert any(
            d.code == "ASM-CONV-001"
            and d.params.get("reason") == "project_baseline_resolution_unknown"
            and d.blocking
            for d in result.diagnostics
        )

    def test_project_export_rejects_invalid_baseline_members(self, init_registry):
        # start_year 非整数 / scenario_mode 非 single:阻断诊断返回,不猜测、不回退
        for baseline, reason in [
            (_export_baseline(start_year="2026"), "project_baseline_start_year_invalid"),
            (_export_baseline(scenario_mode="multi"), "project_baseline_scenario_mode_invalid"),
        ]:
            content = {
                "name": "bad_baseline",
                "model": {"devices": [], "connections": []},
                "project_baseline": baseline,
            }
            result = validate_project_export(content)
            assert result.artifact is None, baseline
            assert any(
                d.code == "ASM-CONV-001" and d.params.get("reason") == reason and d.blocking
                for d in result.diagnostics
            ), (baseline, reason)

    def test_project_export_edge_to_predefined_blocked(self, init_registry):
        # 2.0 下 predefined 接口永不进入 connections:直接边指向需求接口
        # 时必须阻断(ASM-EDGE-011)，不静默改道、不合成管道
        content = {
            "name": "campus_pipe",
            "model": {
                "devices": [
                    {
                        "name": "grid1",
                        "device_type": "ies.device.grid_connection",
                        "kind": "existing",
                        "params": {
                        },
                    },
                    {
                        "name": "hp1",
                        "device_type": "ies.device.heat_pump",
                        "kind": "new",
                        "params": {
                            "rated_heat_kw": 600,
                            "cop": 3.5,
                        },
                    },
                    {
                        "name": "hload1",
                        "device_type": "ies.device.heat_load",
                        "kind": "existing",
                        "params": {
                            # 2.0 显式来源声明:常量需求(需求接口本身永不连边)
                            "heat_demand": 800.0,
                        },
                    },
                ],
                "connections": [
                    {
                        "name": "c1",
                        "from_device": "grid1",
                        "from_port": "electricity_import",
                        "to_device": "hp1",
                        "to_port": "electricity_in",
                    },
                    {
                        "name": "c2",
                        "from_device": "hp1",
                        "from_port": "heat_out",
                        "to_device": "hload1",
                        "to_port": "heat_demand",
                    },
                ],
            },
            "project_baseline": _export_baseline(),
        }
        result = validate_project_export(
            content,
            finance=_EXPORT_FINANCE,
            finance_binding={
                "instances": {
                    "grid1": {"type": "none"},
                    "hp1": _HP_BINDING,
                    "hload1": {"type": "none"},
                }
            },
            tariff_bindings=_EXPORT_TARIFF,
            planning=_EXPORT_PLANNING,
            finance_snapshots=_finance_snapshots(),
        )
        assert result.artifact is None
        assert any(
            d.code == "ASM-EDGE-011" and d.blocking for d in result.diagnostics
        )

    def test_project_export_rejects_unknown_predefined_mode_without_heuristics(self, init_registry):
        # 无启发式映射: 预定义来源未知模式不猜测 → 结构阶段即阻断
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        parsed = parse_assembly_doc(text)
        assert parsed.ok, parsed.diagnostics
        assert parsed.doc is not None
        heat = parsed.doc["devices"]["heat_load"]["predefined_interfaces"]["heat_demand"]
        heat["mode"] = "data_guess"
        result = validate_assembly_doc(parsed.doc, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        assert any(
            d.code == "ASM-SYN-005"
            and d.params.get("reason") == "invalid_enum"
            and d.blocking
            for d in result.diagnostics
        )

    def test_project_export_rejects_missing_predefined_mode(self, init_registry):
        # 预定义来源缺显式模式字段 → 阻断，不猜测 data_repeat/data_predict
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        parsed = parse_assembly_doc(text)
        assert parsed.ok, parsed.diagnostics
        assert parsed.doc is not None
        heat = parsed.doc["devices"]["heat_load"]["predefined_interfaces"]["heat_demand"]
        del heat["mode"]
        result = validate_assembly_doc(parsed.doc, package_dir=SAMPLES_DIR)
        assert result.artifact is None
        assert any(
            d.code == "ASM-SYN-005"
            and d.params.get("reason") == "invalid_enum"
            and d.blocking
            for d in result.diagnostics
        )

    def test_project_export_rejects_missing_name(self, init_registry):
        # 装配名必填: 缺失不再回退 legacy_export
        content = {
            "model": {"devices": [], "connections": []},
            "project_baseline": _export_baseline(),
        }
        result = validate_project_export(content)
        assert result.artifact is None
        assert any(
            d.params.get("reason") == "assembly_name_missing" and d.blocking
            for d in result.diagnostics
        )

    def test_project_export_rejects_bad_connections(self, init_registry):
        # 无名 / 端点缺失 / 设备或接口不可解析 / 边 id 重复一律阻断, 无静默丢弃;
        # 阻断统一使用 ASM-CONV-001 并携带对应 reason
        base_devices = [{"name": "grid1", "device_type": "ies.device.grid_connection", "kind": "existing", "params": {}}]
        good = {
            "name": "c1",
            "from_device": "grid1",
            "from_port": "electricity_import",
            "to_device": "grid1",
            "to_port": "electricity_export",
        }
        cases = [
            ({"connections": [{**good, "name": ""}]}, "connection_id_missing"),
            ({"connections": [{**good, "from_port": ""}]}, "connection_endpoint_missing"),
            ({"connections": [{**good, "to_device": "ghost"}]}, "connection_device_unresolved"),
            ({"connections": [{**good, "from_port": "nope"}]}, "connection_port_unresolved"),
            ({"connections": [good, dict(good)]}, "duplicate_connection"),
        ]
        for conn, reason in cases:
            content = {
                "name": "bad_conn",
                "model": {"devices": base_devices, **conn},
                "project_baseline": _export_baseline(),
            }
            result = validate_project_export(content)
            assert result.artifact is None, conn
            assert any(
                d.code == "ASM-CONV-001" and d.params.get("reason") == reason and d.blocking
                for d in result.diagnostics
            ), (conn, reason)


class TestArtifactTriple:
    def test_same_semantics_same_canonical_text(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        first = validate_assembly_text(text, package_dir=SAMPLES_DIR).artifact
        second = validate_assembly_text(text, package_dir=SAMPLES_DIR).artifact
        assert first is not None and second is not None
        assert first.canonical_text == second.canonical_text

    def test_artifact_invariants(self, init_registry):
        text = (VALID_DIR / "campus.assembly.yaml").read_text(encoding="utf-8")
        result = validate_assembly_text(text, package_dir=SAMPLES_DIR)
        artifact = result.artifact
        # 不可变(校验尝试修改失败)
        with pytest.raises(FrozenInstanceError):
            artifact.canonical_text = "x"
        assert artifact.receipt.schema_id == "ies.assembly"
        assert artifact.receipt.canonical_algorithm_id == "ies.assembly.canonical"
        # 诊断严重度分类
        for d in result.diagnostics:
            assert d.severity in (SEVERITY_BLOCKING, SEVERITY_ERROR, "warning", "info")
