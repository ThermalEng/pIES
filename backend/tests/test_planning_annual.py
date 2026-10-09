"""F4a 年度新增容量公开契约: 必填 year_offset + 保存/提交边界核对。

覆盖(roadmap 0.12-4, assembly-yaml.md §分年投产):
- 核心契约: PlanningVariable 缺 year_offset/非整数/负数阻断, 0 起整数
  可用且 to_dict/from_dict 往返一致;
- 用户保存边界: 同实例同参数同一年份重复声明阻断(领域层), 跨年/
  跨参数声明通过; 0..N-1 范围不在本层(N 未知);
- 装配结构边界: 手写 YAML 缺 year_offset/非整数阻断;
- 任务提交边界(已确认 N): 越界 year_offset 阻断, 缺席 N 时不核对;
  固定建设成本为正的 costed 实例变量缺显式上界阻断, fixed 缺席或
  为零时不要求上界。
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
        "year_offset": 0,
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


class TestYearOffsetContract:
    def test_missing_year_offset_rejected(self):
        var = _var()
        del var["year_offset"]
        with pytest.raises(PlanningConfigError, match="缺少字段"):
            PlanningConfig.from_dict(
                {
                    "objective": {"sense": "minimize", "expression": "0"},
                    "variables": {"cap": var},
                    "constraints": {},
                }
            )

    @pytest.mark.parametrize("bad", [True, False, "0", 0.5, None, [0]])
    def test_non_integer_year_offset_rejected(self, bad):
        with pytest.raises(PlanningConfigError, match="year_offset"):
            _config({"cap": _var(year_offset=bad)})

    def test_negative_year_offset_rejected(self):
        with pytest.raises(PlanningConfigError, match="year_offset"):
            _config({"cap": _var(year_offset=-1)})


    def test_year_offset_roundtrip(self):
        config = _config({"cap": _var(year_offset=2)})
        restored = PlanningConfig.from_dict(config.to_dict())
        assert restored.variables["cap"].year_offset == 2
        assert restored.to_dict()["variables"]["cap"]["year_offset"] == 2


class TestNewBuildNonNegative:
    """年度新增容量非负（assembly-yaml.md 分年投产：在产累加语义）。

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
    def test_duplicate_instance_parameter_year_blocked(self):
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

    def test_same_instance_parameter_different_years_pass(self):
        config = _config({"a": _var(year_offset=0), "b": _var(year_offset=1)})
        assert validate_planning_domain(config) == []

    def test_same_instance_year_different_parameters_pass(self):
        config = _config(
            {"a": _var(parameter="rated_capacity_kwp"), "b": _var(parameter="cap_kw")}
        )
        assert validate_planning_domain(config) == []


class TestAssemblyStructureBoundary:
    def test_missing_year_offset_blocked(self):
        text = _load("campus.assembly.yaml").replace("      year_offset: 0\n", "")
        parsed = parse_assembly_doc(text)
        assert parsed.doc is not None
        diags: list = []
        assert run_structure_checks(dict(parsed.doc), diags=diags) is None
        assert any(
            d.code == "ASM-SYN-004"
            and d.params.get("reason") == "missing_field"
            and d.params.get("key") == "year_offset"
            and d.blocking
            for d in diags
        )

    def test_string_year_offset_blocked(self):
        text = _load("campus.assembly.yaml").replace(
            "      year_offset: 0\n", '      year_offset: "0"\n'
        )
        parsed = parse_assembly_doc(text)
        assert parsed.doc is not None
        diags: list = []
        assert run_structure_checks(dict(parsed.doc), diags=diags) is None
        assert any(d.code == "ASM-SYN-005" and d.blocking for d in diags)


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


class TestSubmitBoundaryAnnualScope:
    def test_year_zero_passes_for_single_year(self, init_registry):
        result = _validate(_load("campus.assembly.yaml"), evaluation_years=1)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def test_out_of_range_year_blocked(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "      year_offset: 0\n", "      year_offset: 1\n"
        )
        _reason(_validate(text, evaluation_years=1), "year_offset_out_of_range")

    def test_later_year_passes_for_two_years(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "      year_offset: 0\n", "      year_offset: 1\n"
        )
        result = _validate(text, evaluation_years=2)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    def test_no_evaluation_years_skips_range(self, init_registry):
        text = _load("campus.assembly.yaml").replace(
            "      year_offset: 0\n", "      year_offset: 5\n"
        )
        result = _validate(text)
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

    def test_positive_fixed_cost_rejected_without_upper(self, init_registry):
        # F4b：离散投产指示实现前，正固定成本案例明确拒收。
        text = self._object_finance_text(_load("campus.assembly.yaml")).replace(
            '      upper_bound: "500"\n', ""
        )
        result = _validate(
            text,
            finance_snapshots={"fin:test-fixed": self._fixed_snapshot()},
            evaluation_years=1,
        )
        _reason(result, "positive_fixed_cost_unsupported")

    def test_positive_fixed_cost_rejected_with_upper(self, init_registry):
        # F4b：已有显式上界也不得放行到旧按常数计费路径。
        text = self._object_finance_text(_load("campus.assembly.yaml"))
        result = _validate(
            text,
            finance_snapshots={"fin:test-fixed": self._fixed_snapshot()},
            evaluation_years=1,
        )
        _reason(result, "positive_fixed_cost_unsupported")

    def test_zero_fixed_cost_needs_no_upper(self, init_registry):
        # 园区样例 pv_system upfront fixed 为 0: 去掉上界仍签发。
        text = _load("campus.assembly.yaml").replace('      upper_bound: "500"\n', "")
        result = _validate(text, evaluation_years=1)
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
