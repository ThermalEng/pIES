"""CalculationConfig 公开契约入口、阶段能力门禁与原子注册表。

提供：
- 13 字段 CalculationConfig schema 加载、校验与严格 JSON 解析；
- 统一 options/parameters schema 与阶段兼容门禁；
- ``SolverRegistry``、``EvaluatorRegistry``、``AlgorithmRegistry`` 原子注册表。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from iesplan.computation.contracts.common import (
    check_finite_json,
    check_parameters_against_schema,
    validate_against_schema,
    validate_parameters_schema,
)
from iesplan.computation.contracts.compatibility import StagePluginSpec
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    CalculationConfig,
    check_provider_ref,
    thaw_json_value,
)

SCHEMA_PATH = pathlib.Path(__file__).parent / "schema" / "calculation-config-1.0.0.schema.json"


def load_calculation_config_schema() -> dict[str, Any]:
    """加载发布的 13 字段计算配置 JSON Schema（包内资源，随包发布）。"""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_calculation_config(data: Mapping[str, Any]) -> CalculationConfig:
    """校验 13 字段计算配置映射并构造不可变载体。"""
    if not isinstance(data, Mapping):
        raise TypeError(f"计算配置须为 Mapping，得到 {type(data).__name__}")
    raw = dict(data)
    check_finite_json(raw, "calculation_config")
    validate_against_schema(
        load_calculation_config_schema(),
        raw,
        error_prefix="计算配置契约校验失败",
    )
    return CalculationConfig(**raw)


def _reject_non_finite_constant(token: str) -> Any:
    raise ValueError(f"计算配置 JSON 禁止非有限常量: {token!r}")


def parse_calculation_config_json(raw: str | bytes) -> CalculationConfig:
    """严格解析并校验 JSON 文本为 13 字段 ``CalculationConfig``。"""
    loaded = json.loads(raw, parse_constant=_reject_non_finite_constant)
    if not isinstance(loaded, Mapping):
        raise TypeError(f"计算配置 JSON 根节点须为 object，得到 {type(loaded).__name__}")
    return validate_calculation_config(loaded)


def validate_options_schema(
    schema: object, field_name: str = "options_schema"
) -> Mapping[str, object]:
    """校验选项/参数 Schema 为完整 Draft 2020-12 JSON Schema 对象。"""
    return validate_parameters_schema(schema, field_name)


def check_descriptor_options(descriptor: Any, config: CalculationConfig) -> None:
    """按生成器 ``options_schema`` 或一体化算法 ``parameters_schema`` 校验分槽 options。"""
    raw_schema = getattr(descriptor, "options_schema", None)
    slot = "generator"
    if raw_schema is None:
        raw_schema = getattr(descriptor, "parameters_schema", None)
        slot = "algorithm"
    if raw_schema is None:
        raise TypeError(
            f"描述符 {getattr(descriptor, 'ref', descriptor)!r} 缺少 options_schema/parameters_schema"
        )
    check_parameters_against_schema(
        raw_schema,
        config.slot_options(slot),
        location_label=f"options/{slot}",
    )


def validate_config_capabilities(
    descriptor: Any,
    config: CalculationConfig,
    *,
    ref_label: str,
) -> None:
    """核对 CalculationConfig 与单个核心插件 descriptor 的能力、模式与分槽选项。"""
    ref = descriptor.ref
    if config.mode not in descriptor.modes:
        raise ComputationUnavailableError(
            f"{ref_label}不支持所选计算模式: {ref!r} x {config.mode!r}",
            reason="incompatible-mode",
        )
    if config.precision not in descriptor.precisions:
        raise ComputationUnavailableError(
            f"{ref_label}不支持所选计算精度: {ref!r} x {config.precision!r}",
            reason="incompatible-precision",
        )
    supports_solver = getattr(descriptor, "supports_solver", None)
    if callable(supports_solver):
        assert config.solver is not None
        if not supports_solver(config.solver):
            raise ComputationUnavailableError(
                f"{ref_label}不兼容所选求解器: {ref!r} x {config.solver!r}",
                reason="incompatible-solver",
            )
    declared_evaluators = tuple(getattr(descriptor, "evaluators", ()) or ())
    if declared_evaluators:
        for ev in config.evaluators:
            ev_ref = str(ev["evaluator"])
            if ev_ref not in declared_evaluators:
                raise ComputationUnavailableError(
                    f"{ref_label}不兼容所选评价器: {ref!r} x {ev_ref!r}",
                    reason="incompatible-evaluator",
                )
    unsupported_outputs = tuple(
        name for name in config.outputs if name not in descriptor.outputs
    )
    if unsupported_outputs:
        raise ComputationUnavailableError(
            f"{ref_label}不支持所选 outputs: {ref!r} x {unsupported_outputs!r}",
            reason="incompatible-outputs",
        )
    check_descriptor_options(descriptor, config)


class _BaseStageSpecRegistry:
    """不可变插件契约原子注册表基类（无全局状态）。"""

    __slots__ = ("_specs",)
    _kind: str = ""
    _label: str = ""
    _collection_key: str = "plugins"

    def __init__(self, specs: Mapping[str, StagePluginSpec]) -> None:
        self._specs = dict(specs)

    def __len__(self) -> int:
        return len(self._specs)

    def snapshot(self) -> tuple[StagePluginSpec, ...]:
        """确定性快照（按精确引用排序）。"""
        return tuple(self._specs[key] for key in sorted(self._specs))

    def get(self, ref: str) -> StagePluginSpec:
        """按精确引用取插件契约；未知引用即明确不可用。"""
        try:
            return self._specs[ref]
        except KeyError:
            raise ComputationUnavailableError(
                f"{self._label}不可用（未注册）: {ref!r}", reason="no-provider"
            ) from None

    def as_mapping(self) -> dict[str, StagePluginSpec]:
        """返回引用到 StagePluginSpec 的只读映射副本。"""
        return dict(self._specs)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            self._collection_key: [
                {
                    "ref": spec.ref,
                    "plugin_id": spec.plugin_id,
                    "version": spec.version,
                    "kind": spec.kind,
                    "consumes": list(spec.consumes),
                    "produces": list(spec.produces),
                    "modes": list(spec.modes),
                    "precisions": list(spec.precisions),
                    "outputs": list(spec.outputs),
                    "parameters_schema": thaw_json_value(spec.parameters_schema),
                }
                for spec in self.snapshot()
            ]
        }


class SolverRegistry(_BaseStageSpecRegistry):
    """已发布的求解器目录（按精确引用排序，实例级持有）。"""

    _kind = "solver"
    _label = "求解器"
    _collection_key = "solvers"

    def resolve(self, config: CalculationConfig) -> StagePluginSpec:
        if config.solver is None:
            raise ValueError("求解器目录不接受一体化算法配置(solver 为 None)")
        spec = self.get(config.solver)
        check_parameters_against_schema(
            spec.parameters_schema,
            config.slot_options("solver"),
            location_label="options/solver",
        )
        return spec


class EvaluatorRegistry(_BaseStageSpecRegistry):
    """已发布的后置评价器目录（按精确引用排序，实例级持有）。"""

    _kind = "evaluator"
    _label = "评价器"
    _collection_key = "evaluators"

    def resolve_all(self, config: CalculationConfig) -> tuple[StagePluginSpec, ...]:
        resolved: list[StagePluginSpec] = []
        for idx, entry in enumerate(config.evaluators):
            spec = self.get(str(entry["evaluator"]))
            check_parameters_against_schema(
                spec.parameters_schema,
                entry["parameters"],  # type: ignore[arg-type]
                location_label=f"evaluators/{idx}/parameters",
            )
            resolved.append(spec)
        return tuple(resolved)


def _build_stage_registry(
    candidates: Iterable[StagePluginSpec],
    *,
    registry_cls: type[_BaseStageSpecRegistry],
) -> Any:
    specs: dict[str, StagePluginSpec] = {}
    for candidate in candidates:
        if not isinstance(candidate, StagePluginSpec):
            raise TypeError(
                f"{registry_cls._label}候选须为 StagePluginSpec，得到 {type(candidate).__name__}"
            )
        if candidate.kind != registry_cls._kind:
            raise ValueError(
                f"{registry_cls._label}候选 kind 须为 {registry_cls._kind!r}，得到 {candidate.kind!r}"
            )
        if candidate.ref in specs:
            raise ValueError(f"{registry_cls._label}重复注册: {candidate.ref!r}")
        specs[candidate.ref] = candidate
    return registry_cls(specs)


def build_solver_registry(candidates: Iterable[StagePluginSpec]) -> SolverRegistry:
    """原子构建并发布求解器目录。"""
    return _build_stage_registry(candidates, registry_cls=SolverRegistry)


def build_evaluator_registry(candidates: Iterable[StagePluginSpec]) -> EvaluatorRegistry:
    """原子构建并发布评价器目录。"""
    return _build_stage_registry(candidates, registry_cls=EvaluatorRegistry)


BUILTIN_PREDICTOR_NAIVE_SPEC = StagePluginSpec(
    plugin_id="ies.predictor.naive",
    version="1.0.0",
    kind="predictor",
    consumes=("ies.stage-input@1.0.0",),
    produces=("ies.prediction-output@1.0.0",),
    parameters_schema={"type": "object", "additionalProperties": False, "properties": {}},
)

BUILTIN_GENERATOR_HIGHS_MILP_SPEC = StagePluginSpec(
    plugin_id="ies.generator.highs_milp",
    version="1.0.0",
    kind="generator",
    consumes=("ies.stage-input@1.0.0",),
    produces=("ies.solver-bundle.mps@1.0.0",),
    modes=("planning",),
    precisions=("medium",),
    outputs=("solution", "metrics"),
    parameters_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "acceleration": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["off", "on_demand_mutex"],
                        "default": "on_demand_mutex",
                    }
                },
            }
        },
    },
)

BUILTIN_SOLVER_HIGHS_SPEC = StagePluginSpec(
    plugin_id="ies.solver.highs",
    version="1.7.2",
    kind="solver",
    consumes=("ies.solver-bundle.mps@1.0.0",),
    produces=("ies.primal-solution@1.0.0",),
    parameters_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {"threads": {"type": "integer", "minimum": 1}},
    },
)

BUILTIN_EVALUATOR_FINANCE_SPEC = StagePluginSpec(
    plugin_id="ies.evaluator.finance",
    version="1.0.0",
    kind="evaluator",
    consumes=("ies.engineering-solution@1.0.0",),
    produces=("ies.financial-evaluation@1.0.0",),
    outputs=("solution", "metrics"),
    parameters_schema={"type": "object", "additionalProperties": False, "properties": {}},
)


def builtin_stage_plugin_specs() -> dict[str, StagePluginSpec]:
    """返回内置四阶段标准契约描述符字典。"""
    specs = (
        BUILTIN_PREDICTOR_NAIVE_SPEC,
        BUILTIN_GENERATOR_HIGHS_MILP_SPEC,
        BUILTIN_SOLVER_HIGHS_SPEC,
        BUILTIN_EVALUATOR_FINANCE_SPEC,
    )
    return {spec.ref: spec for spec in specs}


def build_stage_plugin_map(
    *,
    predictor_registry: Any | None = None,
    generator_registry: Any | None = None,
    solver_registry: Any | None = None,
    evaluator_registry: Any | None = None,
    algorithm_registry: Any | None = None,
) -> dict[str, StagePluginSpec]:
    """聚合各已发布目录中的插件为统一 ``{ref: StagePluginSpec}`` 兼容检查映射。"""
    specs: dict[str, StagePluginSpec] = {}
    if predictor_registry is not None:
        for desc in predictor_registry.snapshot():
            if desc.ref == BUILTIN_PREDICTOR_NAIVE_SPEC.ref:
                specs[desc.ref] = BUILTIN_PREDICTOR_NAIVE_SPEC
            else:
                pid, ver = desc.ref.split("@", 1)
                specs[desc.ref] = StagePluginSpec(
                    plugin_id=pid,
                    version=ver,
                    kind="predictor",
                    consumes=("ies.stage-input@1.0.0",),
                    produces=("ies.prediction-output@1.0.0",),
                    parameters_schema=(
                        dict(desc.parameters_schema)
                        if getattr(desc, "parameters_schema", None) is not None
                        else {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        }
                    ),
                )
    if generator_registry is not None:
        for desc in generator_registry.snapshot():
            specs[desc.ref] = desc.to_stage_spec()
    if solver_registry is not None:
        specs.update(solver_registry.as_mapping())
    if evaluator_registry is not None:
        specs.update(evaluator_registry.as_mapping())
    if algorithm_registry is not None:
        for desc in algorithm_registry.snapshot():
            specs[desc.ref] = desc.to_stage_spec()
    return specs


@dataclass(frozen=True, slots=True)
class AlgorithmDescriptor:
    """一体化算法(``kind: algorithm``)公开能力描述(不可变值对象)。"""

    algorithm_id: str
    version: str
    parameters_schema: Mapping[str, object]
    modes: tuple[str, ...] = ("planning",)
    precisions: tuple[str, ...] = ("low", "medium", "high")
    outputs: tuple[str, ...] = ("solution", "metrics")
    capabilities: tuple[str, ...] = ()
    evaluators: tuple[str, ...] | None = None
    consumes: tuple[str, ...] = ("ies.stage-input@1.0.0",)
    produces: tuple[str, ...] = ("ies.engineering-solution@1.0.0",)

    def __post_init__(self) -> None:
        check_provider_ref(f"{self.algorithm_id}@{self.version}", "algorithm")
        modes = tuple(self.modes)
        if not modes or any(not isinstance(m, str) or not m for m in modes):
            raise ValueError("modes 须为非空能力集合")
        object.__setattr__(self, "modes", modes)
        precisions = tuple(self.precisions)
        if not precisions or any(p not in ("low", "medium", "high") for p in precisions):
            raise ValueError(
                f"precisions 须为 low|medium|high 的非空子集: {self.precisions!r}"
            )
        object.__setattr__(self, "precisions", precisions)
        outputs = tuple(self.outputs)
        if not outputs or any(not isinstance(o, str) or not o for o in outputs):
            raise ValueError("outputs 须为非空能力集合")
        object.__setattr__(self, "outputs", outputs)
        capabilities = tuple(self.capabilities)
        if any(not isinstance(c, str) or not c for c in capabilities):
            raise ValueError("capabilities 条目须为非空字符串")
        object.__setattr__(self, "capabilities", capabilities)
        evaluators = tuple(self.evaluators or ())
        for ev in evaluators:
            check_provider_ref(ev, "evaluators")
        object.__setattr__(self, "evaluators", evaluators)
        object.__setattr__(self, "consumes", tuple(check_provider_ref(c, "consumes") for c in self.consumes))
        object.__setattr__(self, "produces", tuple(check_provider_ref(c, "produces") for c in self.produces))
        object.__setattr__(
            self,
            "parameters_schema",
            validate_options_schema(self.parameters_schema, "parameters_schema"),
        )

    @property
    def ref(self) -> str:
        """一体化算法精确引用(``<稳定ID>@<语义化版本>``)。"""
        return f"{self.algorithm_id}@{self.version}"

    def supports_evaluator(self, evaluator_ref: str) -> bool:
        """该算法是否兼容给定 evaluator 精确引用。"""
        if not self.evaluators:
            return True
        return evaluator_ref in self.evaluators

    def to_stage_spec(self) -> StagePluginSpec:
        """投影为统一阶段契约 ``StagePluginSpec``。"""
        return StagePluginSpec(
            plugin_id=self.algorithm_id,
            version=self.version,
            kind="algorithm",
            parameters_schema=self.parameters_schema,
            consumes=self.consumes,
            produces=self.produces,
            modes=self.modes,
            precisions=self.precisions,
            outputs=self.outputs,
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "algorithm_id": self.algorithm_id,
            "version": self.version,
            "modes": list(self.modes),
            "precisions": list(self.precisions),
            "outputs": list(self.outputs),
            "capabilities": list(self.capabilities),
            "evaluators": list(self.evaluators or ()),
            "consumes": list(self.consumes),
            "produces": list(self.produces),
            "parameters_schema": thaw_json_value(self.parameters_schema),
        }


@runtime_checkable
class AlgorithmProvider(Protocol):
    """一体化算法公开身份(结构协议)。"""

    @property
    def descriptor(self) -> AlgorithmDescriptor:
        ...

    @property
    def ref(self) -> str:
        ...

    def available(self) -> bool:
        ...


class AlgorithmRegistry:
    """已发布的一体化算法目录(不可变快照语义，实例级持有，无全局状态)。"""

    __slots__ = ("_providers",)

    def __init__(self, providers: Mapping[str, AlgorithmProvider]) -> None:
        self._providers = dict(providers)

    def __len__(self) -> int:
        return len(self._providers)

    def snapshot(self) -> tuple[AlgorithmDescriptor, ...]:
        """确定性快照（按精确引用排序，只公开 descriptor 数据）。"""
        return tuple(
            self._providers[key].descriptor for key in sorted(self._providers)
        )

    def get(self, ref: str) -> AlgorithmProvider:
        """按精确引用取一体化算法；未知引用即明确不可用。"""
        try:
            return self._providers[ref]
        except KeyError:
            raise ComputationUnavailableError(
                f"一体化算法不可用（未注册）: {ref!r}", reason="no-provider"
            ) from None

    def resolve(self, config: CalculationConfig) -> AlgorithmProvider:
        """按一体化计算配置选中 provider 并完成能力与分槽选项门禁。"""
        if config.algorithm is None:
            raise ValueError("一体化算法目录不接受分步配置(algorithm 为 None)")
        provider = self.get(config.algorithm)
        validate_config_capabilities(
            provider.descriptor, config, ref_label="一体化算法"
        )
        return provider

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {"algorithms": [d.to_dict() for d in self.snapshot()]}


def build_algorithm_registry(
    candidates: Iterable[AlgorithmProvider],
) -> AlgorithmRegistry:
    """原子构建并发布一体化算法目录。"""
    providers: dict[str, AlgorithmProvider] = {}
    for candidate in candidates:
        if not isinstance(candidate, AlgorithmProvider):
            raise TypeError(
                "一体化算法候选须符合 AlgorithmProvider 协议，"
                f"得到 {type(candidate).__name__}"
            )
        descriptor = getattr(candidate, "descriptor", None)
        if not isinstance(descriptor, AlgorithmDescriptor):
            raise TypeError(
                "一体化算法候选须公开 AlgorithmDescriptor，"
                f"得到 {type(descriptor).__name__}"
            )
        if candidate.ref != descriptor.ref:
            raise ValueError(
                "一体化算法 ref 与描述符引用不一致: "
                f"{candidate.ref!r} x {descriptor.ref!r}"
            )
        if not candidate.available():
            raise ComputationUnavailableError(
                f"一体化算法候选当前不可用: {candidate.ref!r}",
                reason="no-provider",
            )
        if candidate.ref in providers:
            raise ValueError(f"一体化算法重复注册: {candidate.ref!r}")
        providers[candidate.ref] = candidate
    return AlgorithmRegistry(providers)


__all__ = [
    "BUILTIN_EVALUATOR_FINANCE_SPEC",
    "BUILTIN_GENERATOR_HIGHS_MILP_SPEC",
    "BUILTIN_PREDICTOR_NAIVE_SPEC",
    "BUILTIN_SOLVER_HIGHS_SPEC",
    "AlgorithmDescriptor",
    "AlgorithmProvider",
    "AlgorithmRegistry",
    "EvaluatorRegistry",
    "SCHEMA_PATH",
    "SolverRegistry",
    "build_algorithm_registry",
    "build_evaluator_registry",
    "build_solver_registry",
    "build_stage_plugin_map",
    "builtin_stage_plugin_specs",
    "check_descriptor_options",
    "load_calculation_config_schema",
    "parse_calculation_config_json",
    "validate_calculation_config",
    "validate_config_capabilities",
    "validate_options_schema",
]
