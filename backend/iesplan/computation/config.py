"""CalculationConfig 公开契约入口与统一能力/选项校验边界。

提供：
- 配置 schema 文件加载与“schema 校验 → 不可变 14 字段载体”构造；
- ``GeneratorDescriptor`` 与一体化 ``AlgorithmDescriptor`` 共用的静态能力与选项校验；
- 一体化 ``algorithm`` 描述符与原子注册表（``AlgorithmRegistry``）。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    CalculationConfig,
    check_provider_ref,
    freeze_json_value,
    thaw_json_value,
)

SCHEMA_PATH = (
    pathlib.Path(__file__).parent / "schema" / "calculation-config-1.0.0.schema.json"
)


def load_calculation_config_schema() -> dict[str, Any]:
    """加载发布的计算配置 JSON Schema（包内资源，随包发布）。"""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_calculation_config(data: Mapping[str, Any]) -> CalculationConfig:
    """校验计算配置映射并构造不可变载体。

    依次执行：JSON Schema 校验（未知字段与模式互斥即拒绝）→ frozen
    ``CalculationConfig`` 构造（含领域规则：引用形态、精度枚举、
    目标去重、收敛正值、输出非空）。``to_dict`` 输出即规范字段顺序。
    """
    if not isinstance(data, Mapping):
        raise TypeError(f"计算配置须为 Mapping，得到 {type(data).__name__}")
    schema = load_calculation_config_schema()
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(dict(data)))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.absolute_path)
        raise ValueError(f"计算配置契约校验失败[{location or '<root>'}]: {first.message}")
    return CalculationConfig(**dict(data))


def validate_options_schema(
    schema: object, field_name: str = "options_schema"
) -> Mapping[str, object]:
    """校验选项/参数 Schema 为完整 Draft 2020-12 JSON Schema 对象。

    根 schema 必须描述 object，且 ``additionalProperties`` 必须显式为 ``false``。
    """
    if not isinstance(schema, Mapping):
        raise TypeError(
            f"{field_name} 须为完整 JSON Schema 对象，得到 {type(schema).__name__}"
        )
    if schema.get("type") != "object":
        raise ValueError(f"{field_name} 根 schema 必须描述 object")
    if schema.get("additionalProperties") is not False:
        raise ValueError(f"{field_name} 必须显式声明 additionalProperties: false")
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise ValueError(f"{field_name} 自身非法: {exc}") from exc
    return freeze_json_value(dict(schema))


def check_descriptor_options(descriptor: Any, config: CalculationConfig) -> None:
    """按生成器 ``options_schema`` 或一体化算法 ``parameters_schema`` 校验 ``config.options``。

    非法选项属于用户计算配置错误，抛明确 ``ValueError``（字段路径与原因），
    不回退其他 provider、不删字段、不补默认。
    """
    raw_schema = getattr(descriptor, "options_schema", None)
    if raw_schema is None:
        raw_schema = getattr(descriptor, "parameters_schema", None)
    if raw_schema is None:
        raise TypeError(
            f"描述符 {getattr(descriptor, 'ref', descriptor)!r} 缺少 options_schema/parameters_schema"
        )
    schema = thaw_json_value(raw_schema)
    options = thaw_json_value(config.options)
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(options))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.absolute_path)
        raise ValueError(
            f"计算选项非法[{location or '<root>'}]: {first.message}"
        )


def validate_config_capabilities(
    descriptor: Any,
    config: CalculationConfig,
    *,
    ref_label: str = "计算能力",
) -> None:
    """统一校验 ``CalculationConfig`` 与生成器/一体化算法描述符的能力兼容性及选项。"""
    ref = getattr(descriptor, "ref", None) or (
        config.algorithm if config.algorithm is not None else config.generator
    )
    if config.mode not in descriptor.modes:
        raise ComputationUnavailableError(
            f"{ref_label}不支持所选 mode: {ref!r} x {config.mode!r}",
            reason="incompatible-mode",
        )
    if config.precision not in descriptor.precisions:
        raise ComputationUnavailableError(
            f"{ref_label}不支持所选精度: {ref!r} x {config.precision!r}",
            reason="incompatible-precision",
        )
    if config.solver is not None:
        supports_solver = getattr(descriptor, "supports_solver", None)
        solver_ok = (
            supports_solver(config.solver)
            if callable(supports_solver)
            else config.solver in getattr(descriptor, "solvers", ())
        )
        if not solver_ok:
            raise ComputationUnavailableError(
                f"{ref_label}不兼容所选求解器: {ref!r} x {config.solver!r}",
                reason="incompatible-solver",
            )
    if config.evaluator is not None:
        supports_evaluator = getattr(descriptor, "supports_evaluator", None)
        if callable(supports_evaluator):
            evaluator_ok = supports_evaluator(config.evaluator)
        else:
            declared_evaluators = getattr(descriptor, "evaluators", ())
            evaluator_ok = config.evaluator in (declared_evaluators or ())
        if not evaluator_ok:
            raise ComputationUnavailableError(
                f"{ref_label}不兼容所选评价器: {ref!r} x {config.evaluator!r}",
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
    result_adapter: str | None = None

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
        if self.result_adapter is not None:
            check_provider_ref(self.result_adapter, "result_adapter")
        if self.evaluators is None:
            evaluators = (
                (self.result_adapter,) if self.result_adapter is not None else ()
            )
        else:
            evaluators = tuple(self.evaluators)
            for ev in evaluators:
                check_provider_ref(ev, "evaluators")
        object.__setattr__(self, "evaluators", evaluators)
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
        """该算法是否兼容给定 evaluator 精确引用（未声明则拒绝，fail-closed）。"""
        return evaluator_ref in (self.evaluators or ())

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
            "result_adapter": self.result_adapter,
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
        """按一体化计算配置选中 provider 并完成能力与选项门禁。"""
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
    "AlgorithmDescriptor",
    "AlgorithmProvider",
    "AlgorithmRegistry",
    "SCHEMA_PATH",
    "build_algorithm_registry",
    "check_descriptor_options",
    "load_calculation_config_schema",
    "validate_calculation_config",
    "validate_config_capabilities",
    "validate_options_schema",
]
