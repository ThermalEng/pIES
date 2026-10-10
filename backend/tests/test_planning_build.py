"""首年建设容量公开契约: 无年度索引 + 保存/提交边界核对。

覆盖(roadmap 0.12-4, assembly-yaml.md §首年建设与全周期运行优化):
- 核心契约: PlanningVariable 按设备实例及参数定义建设容量与上下界,
  不携带 year_offset(出现即未知字段阻断), to_dict/from_dict 往返一致;
  新增容量非负(显式负界拒收, 缺下界语义为 0);
- 用户保存边界: 同实例同参数重复声明阻断(领域层), 跨参数声明通过;
- 装配输入边界: 同实例同参数重复声明阻断(含变量名/字段定位),
  同实例不同参数与同参数不同实例签发;
- 装配结构边界: 手写 YAML 含 year_offset 即未知键阻断;
- 任务提交边界: 正固定建设费 + 必建变量(下界 > 0)签发, 正固定费 +
  可选建设变量明确拒收(positive_fixed_cost_unsupported), fixed 缺席
  或为零不受影响; 无年度索引核对, 不猜 epsilon/最小建设量。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from iesplan.assembly import parse_assembly_doc, validate_assembly_doc
from iesplan.assembly.parser import run_structure_checks
from iesplan.core.contracts import PlanningConfig, PlanningConfigError
from iesplan.planning.contracts import validate_planning_domain

ASSEMBLY_DIR = Path(__file__).resolve().parent.parent / "iesplan" / "assembly"
SAMPLES_DIR = ASSEMBLY_DIR / "samples"
VALID_DIR = SAMPLES_DIR / "valid"


def _load(name: str) -> str:
    return (VALID_DIR / name).read_text(encoding="utf-8")


def _finance_text() -> str:
    return (SAMPLES_DIR / "finance" / "campus.effective-finance.yaml").read_text(
        encoding="utf-8"
    )


@pytest.fixture
def init_registry():
    from iesplan.devices import init_registry

    init_registry()
    yield


def _var(**overrides):
    base = {
        "device_ref": "pv_1",
        "parameter": "rated_capacity_kwp",
        "lower_bound": "0",
        "upper_bound": "1000",
        "unit": "kWp",
    }
    base.update(overrides)
    return base


def _config(variables: dict) -> PlanningConfig:
    return PlanningConfig.from_dict(
        {
            "objective": {"sense": "minimize", "expression": "0"},
            "variables": variables,
            "constraints": {},
        }
    )


class TestBuildVariableContract:
    def test_year_offset_rejected_as_unknown_field(self):
        with pytest.raises(PlanningConfigError, match="未知字段"):
            _config({"cap": _var(year_offset=0)})

    def test_roundtrip_without_year_offset(self):
        config = _config({"cap": _var()})
        assert "year_offset" not in config.to_dict()["variables"]["cap"]
        restored = PlanningConfig.from_dict(config.to_dict())
        assert restored.variables["cap"].lower_bound is not None


class TestNewBuildNonNegative:
    """年度新增容量非负（首年建设：在产即建设量语义）。

    新增不可为负：显式负下界/负上界在核心契约拒收；未填下界的
    模型语义下界为 0（新增量定义）。
    """

    @pytest.mark.parametrize(
        "overrides",
        [
            {"lower_bound": "-5"},
            {"lower_bound": -5},
            {"upper_bound": "-1"},
            {"upper_bound": -100},
        ],
    )
    def test_negative_bounds_rejected(self, overrides):
        with pytest.raises(PlanningConfigError, match="bound"):
            _config({"cap": _var(**overrides)})

    def test_missing_lower_bound_allowed(self):
        var = _var()
        del var["lower_bound"]
        config = _config({"cap": var})
        assert config.variables["cap"].lower_bound is None


class TestSaveBoundaryUniqueness:
    def test_duplicate_instance_parameter_blocked(self):
        config = _config(
            {
                "a": _var(),
                "b": _var(),
            }
        )
        diags = validate_planning_domain(config)
        assert len(diags) == 1
        # 领域诊断本身 blocking=False(与既有设备引用诊断一致):
        # 保存 API 见诊断即 PROJ-PLAN-003 阻断, 不依赖该标志。
        assert diags[0].code == "PROJ-PLAN-003"
        assert "a" in diags[0].params["detail"] and "b" in diags[0].params["detail"]

    def test_same_instance_different_parameters_pass(self):
        config = _config(
            {"a": _var(parameter="rated_capacity_kwp"), "b": _var(parameter="cap_kw")}
        )
        assert validate_planning_domain(config) == []


def _ensure_year_offset_line(text: str) -> str:
    """保证文本含一条 year_offset 声明（样例清理后也能构造旧输入）。"""
    if "year_offset" in text:
        return text
    return text.replace(
        '      parameter: rated_capacity_kwp\n',
        '      parameter: rated_capacity_kwp\n      year_offset: 0\n',
    )


class TestAssemblyStructureBoundary:
    def test_year_offset_rejected_as_unknown_key(self):
        text = _ensure_year_offset_line(_load("campus.assembly.yaml"))
        assert "year_offset" in text
        parsed = parse_assembly_doc(text)
        assert parsed.doc is not None
        diags: list = []
        assert run_structure_checks(dict(parsed.doc), diags=diags) is None
        assert any(
            d.code == "ASM-SYN-001"
            and d.params.get("reason") == "unknown_key"
            and d.params.get("key") == "year_offset"
            and d.blocking
            for d in diags
        )


def _validate(text: str, **kwargs):
    parsed = parse_assembly_doc(text)
    assert parsed.doc is not None, [d.to_dict() for d in parsed.diagnostics]
    kwargs.setdefault("package_dir", SAMPLES_DIR)
    return validate_assembly_doc(parsed.doc, **kwargs)


def _reason(result, reason: str) -> None:
    assert result.artifact is None, [d.to_dict() for d in result.diagnostics]
    assert any(
        d.code == "ASM-PLAN-003" and d.params.get("reason") == reason and d.blocking
        for d in result.diagnostics
    )


def _strip_year_offset(text: str) -> str:
    return text.replace("      year_offset: 0\n", "").replace(
        "      year_offset: 1\n", ""
    )


_PV_CAP_BLOCK = (
    "    pv_cap:\n"
    "      device_ref: pv1\n"
    "      parameter: rated_capacity_kwp\n"
    "      lower_bound: \"0\"\n"
    "      upper_bound: \"500\"\n"
    "      unit: kWp\n"
)


def _with_extra_var(text: str, block: str) -> str:
    assert _PV_CAP_BLOCK in text
    return text.replace(_PV_CAP_BLOCK, _PV_CAP_BLOCK + block, 1)


def _var_block(
    name: str, device_ref: str, parameter: str,
    lower: str, upper: str, unit: str,
) -> str:
    return (
        f"    {name}:\n"
        f"      device_ref: {device_ref}\n"
        f"      parameter: {parameter}\n"
        f"      lower_bound: \"{lower}\"\n"
        f"      upper_bound: \"{upper}\"\n"
        f"      unit: {unit}\n"
    )


class TestAssemblyBoundaryUniqueness:
    """装配输入边界: 同实例同参数只允许一个建设变量。

    保存边界已有领域层检查(见 TestSaveBoundaryUniqueness)；手写装配
    入口此前缺该规则，生成器 bound_prop 以后写入覆盖前者。
    """

    def test_duplicate_instance_parameter_rejected(self, init_registry):
        text = _with_extra_var(
            _strip_year_offset(_load("campus.assembly.yaml")),
            _var_block("pv_cap_2", "pv1", "rated_capacity_kwp", "0", "100", "kWp"),
        )
        result = _validate(text)
        assert result.artifact is None, [d.to_dict() for d in result.diagnostics]
        matches = [
            d for d in result.diagnostics
            if d.code == "ASM-PLAN-003"
            and d.params.get("reason") == "variable_duplicate_target"
            and d.blocking
        ]
        assert len(matches) == 1
        diag = matches[0]
        assert diag.location.get("field") == "planning.variables.pv_cap_2"
        assert diag.params.get("variable") == "pv_cap_2"
        assert diag.params.get("conflict") == "pv_cap"

    def test_same_instance_different_parameters_pass(self, init_registry):
        text = _with_extra_var(
            _strip_year_offset(_load("campus.assembly.yaml")),
            _var_block("pv_irr", "pv1", "reference_irradiance", "0", "2000", "W/m2"),
        )
        result = _validate(text)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def test_same_parameter_different_instances_pass(self, init_registry):
        text = _strip_year_offset(_load("campus.assembly.yaml"))
        gas_device = (
            "  gas1:\n"
            "    definition:\n"
            "      id: ies.device.gas_boiler\n"
            "    asset_origin: existing\n"
            "    properties: {}\n"
            "    predefined_interfaces: {}\n"
        )
        anchor_devices = "        path: data/campus_heat.data.csv\n\nconnections:"
        assert anchor_devices in text
        text = text.replace(
            anchor_devices,
            "        path: data/campus_heat.data.csv\n" + gas_device + "\nconnections:",
            1,
        )
        anchor_binding = "    heat_load:\n      type: none\n"
        assert anchor_binding in text
        text = text.replace(
            anchor_binding, anchor_binding + "    gas1:\n      type: none\n", 1,
        )
        text = _with_extra_var(
            text,
            _var_block("gas_cap", "gas1", "rated_heat_kw", "0", "500", "kW")
            + _var_block("hp_cap", "hp1", "rated_heat_kw", "0", "600", "kW"),
        )
        result = _validate(text)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]


class TestSubmitBoundaryBuildScope:
    def test_no_year_index_needed(self, init_registry):
        result = _validate(_strip_year_offset(_load("campus.assembly.yaml")))
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def _fixed_snapshot(self):
        from iesplan.finance import effective_from_yaml

        return effective_from_yaml(_finance_text().replace("value: '0'", "value: '900000'"))

    def _object_finance_text(self, text: str) -> str:
        old = (
            "finance:\n"
            "  ref:\n"
            "    kind: relative_file\n"
            "    path: finance/campus.effective-finance.yaml\n"
            "  profile_id: cn-north-demo\n"
        )
        new = (
            "finance:\n"
            "  ref:\n"
            "    kind: object\n"
            "    object_id: fin:test-fixed\n"
            "  profile_id: cn-north-demo\n"
        )
        assert old in text
        return text.replace(old, new)

    def test_mandatory_build_with_positive_fixed_passes(self, init_registry):
        # 必建变量(下界 > 0)建设必然发生：固定费按常数计一次精确，签发。
        text = self._object_finance_text(
            _strip_year_offset(_load("campus.assembly.yaml"))
        ).replace('      lower_bound: "0"\n', '      lower_bound: "10"\n')
        result = _validate(
            text,
            finance_snapshots={"fin:test-fixed": self._fixed_snapshot()},
        )
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def test_optional_build_with_positive_fixed_rejected(self, init_registry):
        # 可选建设变量(下界为 0/缺席) + 正固定费：LP 内无精确触发表达，
        # 在离散建模实现前明确拒收，不放行旧按常数计费路径。
        text = self._object_finance_text(
            _strip_year_offset(_load("campus.assembly.yaml"))
        )
        result = _validate(
            text,
            finance_snapshots={"fin:test-fixed": self._fixed_snapshot()},
        )
        _reason(result, "positive_fixed_cost_unsupported")

    def test_zero_fixed_cost_passes(self, init_registry):
        # 园区样例 pv_system upfront fixed 为 0: 去掉上界仍签发。
        text = _strip_year_offset(_load("campus.assembly.yaml")).replace(
            '      upper_bound: "500"\n', ""
        )
        result = _validate(text)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
