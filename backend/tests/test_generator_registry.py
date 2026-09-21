"""生成器目录契约测试(0.8 切片 A)。

只覆盖公开 descriptor 与原子注册表，不涉及真实 generator 实现：

- descriptor 字段校验：非法 ref 形态/版本、空能力条目、非法 adapter
  ref 即拒绝；``ref`` 为 ``<稳定ID>@<语义化版本>`` 唯一身份；
- 原子发布：重复引用、非 descriptor 候选即整体拒绝，不发布部分表；
- 选择语义：空表/未知引用统一 ``ComputationUnavailableError``；
  solver 不兼容不回退到另一生成器；
- 快照确定性：按稳定 ID 排序；``to_dict`` 为独立副本。
"""

from __future__ import annotations

import pytest

from iesplan.computation import (
    NO_OPTIONS_SCHEMA,
    CalculationConfig,
    ComputationUnavailableError,
    GeneratorDescriptor,
    GeneratorRegistry,
    build_registry,
    check_provider_ref,
)

GENERATOR = "acme.generator@1.0.0"
SOLVER = "ies.solver.highs@1.7.2"


_OPTIONS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "gap_rel": {
            "type": "number",
            "minimum": 0,
            "maximum": 0.1,
            "default": 0.001,
        }
    },
    "additionalProperties": False,
}


def _descriptor(**kwargs) -> GeneratorDescriptor:
    kwargs.setdefault("generator_id", "acme.generator")
    kwargs.setdefault("version", "1.0.0")
    kwargs.setdefault("modes", ("planning",))
    kwargs.setdefault("precisions", ("low", "medium", "high"))
    kwargs.setdefault("solvers", (SOLVER,))
    kwargs.setdefault("options_schema", dict(_OPTIONS_SCHEMA))
    kwargs.setdefault("resource_limits", {"timeout_seconds": 600})
    kwargs.setdefault("unit_system", "SI")
    kwargs.setdefault("media_types", ("application/mps",))
    kwargs.setdefault("seed_semantics", "options.seed 为空时使用固定种子 0")
    kwargs.setdefault("assembly_schema_version", "1.0")
    kwargs.setdefault("result_adapter", "acme.result-adapter@1.0.0")
    return GeneratorDescriptor(**kwargs)


def _config(**kwargs) -> CalculationConfig:
    kwargs.setdefault("mode", "planning")
    kwargs.setdefault("generator", GENERATOR)
    kwargs.setdefault("solver", SOLVER)
    kwargs.setdefault("precision", "medium")
    kwargs.setdefault("time_limit_seconds", 600)
    kwargs.setdefault("outputs", ["solution"])
    return CalculationConfig(**kwargs)


def test_descriptor_ref_and_solver_support() -> None:
    """descriptor ref 为精确引用；solver 兼容可查询。"""
    desc = _descriptor()
    assert desc.ref == GENERATOR
    assert desc.supports_solver(SOLVER) is True
    assert desc.supports_solver("other.solver@9.9.9") is False
    dumped = desc.to_dict()
    assert dumped["generator_id"] == "acme.generator"
    assert dumped["solvers"] == [SOLVER]


def test_descriptor_rejects_bad_identity() -> None:
    """非法 ID/版本/adapter ref 的 descriptor 即拒绝。"""
    with pytest.raises(ValueError, match="generator"):
        _descriptor(generator_id="")
    with pytest.raises(ValueError, match="generator"):
        _descriptor(version="1.0")
    with pytest.raises(ValueError, match="result_adapter"):
        _descriptor(result_adapter="not-a-ref")
    with pytest.raises(ValueError, match="capabilities"):
        _descriptor(capabilities=("",))


def test_check_provider_ref_id_syntax() -> None:
    """稳定 ID 允许 current 点号/下划线/短横线形态；空格空段非法字符即拒绝。"""
    for good in (
        "ies.device.pv@2.0.0",
        "ies.solver.highs@1.7.2",
        "acme.generator@1.0.0",
        "acme.generator.highs_milp@1.0.0",
        "vendor.device.foo-bar_baz@0.1.0",
    ):
        assert check_provider_ref(good, "generator") == good
    for bad in (
        "has space@1.0.0",
        "a..b@1.0.0",
        ".a@1.0.0",
        "a.@1.0.0",
        "@1.0.0",
        "a/b@1.0.0",
        "a@1.0.0@2.0.0",
        "a@1.0",
        "a",
    ):
        with pytest.raises(ValueError):
            check_provider_ref(bad, "generator")


def test_descriptor_capability_fields_are_immutable() -> None:
    """手册清单字段齐备且跨边界不可变；to_dict 为独立副本。"""
    desc = _descriptor(media_types=["application/mps"])
    assert desc.unit_system == "SI"
    assert desc.seed_semantics.startswith("options.seed")
    assert desc.media_types == ("application/mps",)
    with pytest.raises(TypeError):
        desc.options_schema["type"] = "array"  # type: ignore[index]
    with pytest.raises(TypeError):
        desc.options_schema["properties"]["gap_rel"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        desc.resource_limits["timeout_seconds"] = 1  # type: ignore[index]
    dumped = desc.to_dict()
    assert dumped["options_schema"] == _OPTIONS_SCHEMA
    assert dumped["resource_limits"] == {"timeout_seconds": 600}
    assert dumped["unit_system"] == "SI"
    assert dumped["media_types"] == ["application/mps"]
    dumped["options_schema"]["properties"]["gap_rel"] = {}
    dumped["media_types"].append("application/json")
    assert desc.to_dict()["options_schema"] == _OPTIONS_SCHEMA
    assert desc.to_dict()["media_types"] == ["application/mps"]


def test_descriptor_rejects_missing_capability_fields() -> None:
    """缺失手册清单字段的 descriptor 即拒绝。"""
    with pytest.raises(ValueError, match="unit_system"):
        _descriptor(unit_system="")
    with pytest.raises(ValueError, match="seed_semantics"):
        _descriptor(seed_semantics="")
    with pytest.raises(ValueError, match="media_types"):
        _descriptor(media_types=("",))
    with pytest.raises(TypeError, match="options_schema"):
        _descriptor(options_schema=[])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="resource_limits"):
        _descriptor(resource_limits={"": 1})


def test_descriptor_rejects_missing_required_trio() -> None:
    """装配契约/求解器/结果适配器缺失即无效 descriptor，当场拒绝。"""
    with pytest.raises(ValueError, match="assembly_schema_version"):
        _descriptor(assembly_schema_version="")
    with pytest.raises(ValueError, match="assembly_schema_version"):
        _descriptor(assembly_schema_version="1")
    with pytest.raises(ValueError, match="assembly_schema_version"):
        _descriptor(assembly_schema_version="v1.0")
    with pytest.raises(ValueError, match="solvers"):
        _descriptor(solvers=())
    with pytest.raises(ValueError, match="solvers"):
        _descriptor(solvers=("not-a-ref",))
    with pytest.raises(ValueError, match="result_adapter"):
        _descriptor(result_adapter="")
    valid = _descriptor(
        assembly_schema_version="ies.assembly@1.0.0",
        solvers=(SOLVER, "other.solver@2.1.0"),
    )
    assert valid.assembly_schema_version == "ies.assembly@1.0.0"
    assert valid.supports_solver("other.solver@2.1.0") is True


def test_build_registry_is_atomic() -> None:
    """重复引用/非候选即整体拒绝；空候选发布空表。"""
    with pytest.raises(ValueError, match="重复注册"):
        build_registry([_descriptor(), _descriptor()])
    with pytest.raises(TypeError, match="GeneratorDescriptor"):
        build_registry([_descriptor(), object()])  # type: ignore[list-item]
    empty = build_registry(())
    assert len(empty) == 0
    assert empty.snapshot() == ()


def test_empty_or_unknown_generator_is_unavailable() -> None:
    """空表/未知引用统一 ComputationUnavailableError，不回退。"""
    empty = build_registry(())
    with pytest.raises(ComputationUnavailableError) as exc_info:
        empty.get(GENERATOR)
    assert exc_info.value.reason == "no-provider"
    with pytest.raises(ComputationUnavailableError):
        empty.resolve(_config())


def test_resolve_checks_solver_compatibility() -> None:
    """选中 provider 后 solver 不兼容即失败，不换另一生成器。"""
    registry = build_registry([_descriptor()])
    assert registry.resolve(_config()).ref == GENERATOR
    bad_solver = _config(solver="other.solver@9.9.9")
    with pytest.raises(ComputationUnavailableError) as exc_info:
        registry.resolve(bad_solver)
    assert exc_info.value.reason == "incompatible-solver"


def test_resolve_checks_mode_and_precision() -> None:
    """mode/精度不在 descriptor 能力内即明确不可用，不回退。"""
    registry = build_registry([_descriptor()])
    with pytest.raises(ComputationUnavailableError) as exc_info:
        registry.resolve(_config(mode="evaluation"))
    assert exc_info.value.reason == "incompatible-mode"
    narrow = build_registry([_descriptor(precisions=("low",))])
    with pytest.raises(ComputationUnavailableError) as exc_info:
        narrow.resolve(_config(precision="medium"))
    assert exc_info.value.reason == "incompatible-precision"


def test_descriptor_rejects_non_schema_options() -> None:
    """旧 properties 片段、空 {}、缺 additionalProperties、非法 schema 即拒绝。"""
    with pytest.raises(ValueError, match="根 schema"):
        _descriptor(options_schema={"gap_rel": {"type": "number"}})
    with pytest.raises(ValueError, match="根 schema"):
        _descriptor(options_schema={})
    with pytest.raises(ValueError, match="additionalProperties"):
        _descriptor(
            options_schema={"type": "object", "properties": {"gap_rel": {}}}
        )
    with pytest.raises(ValueError, match="自身非法"):
        _descriptor(
            options_schema={
                "type": "object",
                "properties": {"gap_rel": {"type": "nonsense"}},
                "additionalProperties": False,
            }
        )
    with pytest.raises(TypeError, match="JSON Schema"):
        _descriptor(options_schema="schema.json")  # type: ignore[arg-type]


def test_no_options_schema_explicit() -> None:
    """无选项用显式完整 schema；空 options 通过门禁。"""
    desc = _descriptor(options_schema=dict(NO_OPTIONS_SCHEMA))
    assert desc.to_dict()["options_schema"]["additionalProperties"] is False
    registry = build_registry([desc])
    assert registry.resolve(_config(options={})).ref == GENERATOR


def test_resolve_options_gate() -> None:
    """合法 options 通过；未知键/类型/范围错误抛 ValueError，不回退。"""
    registry = build_registry([_descriptor()])
    assert registry.resolve(_config(options={"gap_rel": 0.01})).ref == GENERATOR
    with pytest.raises(ValueError) as exc_info:
        registry.resolve(_config(options={"unknown_opt": 1}))
    assert "unknown_opt" in str(exc_info.value)
    with pytest.raises(ValueError) as exc_info:
        registry.resolve(_config(options={"gap_rel": "0.01"}))
    assert "gap_rel" in str(exc_info.value)
    with pytest.raises(ValueError) as exc_info:
        registry.resolve(_config(options={"gap_rel": 0.5}))
    assert "gap_rel" in str(exc_info.value)


def test_invalid_options_not_unavailable_and_no_fallback() -> None:
    """非法选项是配置错误（ValueError），不是不可用；不换另一生成器。"""
    first = _descriptor()
    second = _descriptor(
        generator_id="other.generator",
        solvers=("other.solver@9.9.9",),
        modes=("planning",),
        precisions=("medium",),
        result_adapter="other.adapter@1.0.0",
    )
    registry = build_registry([first, second])
    with pytest.raises(ValueError):
        registry.resolve(_config(options={"unknown_opt": 1}))
    assert len(registry) == 2


def test_descriptor_requires_modes_and_precisions() -> None:
    """modes/precisions 为公开非空能力集合；不接受离散化声明。"""
    with pytest.raises(ValueError, match="modes"):
        _descriptor(modes=())
    with pytest.raises(ValueError, match="precisions"):
        _descriptor(precisions=())
    with pytest.raises(ValueError, match="precisions"):
        _descriptor(precisions=("ultra",))
    desc = _descriptor()
    assert desc.to_dict()["modes"] == ["planning"]
    assert desc.to_dict()["precisions"] == ["low", "medium", "high"]
    assert "discretizations" not in desc.to_dict()


def test_snapshot_is_deterministic_and_dump_is_copy() -> None:
    """快照按稳定 ID 排序；to_dict 为独立副本。"""
    registry = build_registry([
        _descriptor(generator_id="zeta.generator", solvers=("zeta.solver@1.0.0",)),
        _descriptor(generator_id="acme.generator", solvers=(SOLVER,)),
    ])
    assert [d.generator_id for d in registry.snapshot()] == [
        "acme.generator", "zeta.generator",
    ]
    dumped = registry.to_dict()
    assert [g["generator_id"] for g in dumped["generators"]] == [
        "acme.generator", "zeta.generator",
    ]
    dumped["generators"].clear()
    assert len(registry) == 2


def test_registry_instances_hold_no_global_state() -> None:
    """注册表实例级持有：两次构建互不影响，无模块全局发布状态。"""
    first = build_registry([_descriptor()])
    second = build_registry(())
    assert len(first) == 1
    assert len(second) == 0
    assert isinstance(first, GeneratorRegistry)
