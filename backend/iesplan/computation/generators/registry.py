"""GeneratorProvider 独立原子注册表(0.8 切片 A)。

- 候选 descriptor 必须完整构建并全部校验通过后一次性发布；任一候选
  非法(重复 ID、坏版本、字段非法)即整体拒绝，不发布部分状态；
- 注册表实例由 bootstrap 组合根持有并发布；API/Worker 不得自行发现
  或动态加载 provider，只消费组合根给定的已发布实例；
- 无可用 provider（空表或引用未知）统一抛
  ``ComputationUnavailableError``，不回退、不猜测。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from jsonschema import Draft202012Validator

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators.descriptor import GeneratorDescriptor
from iesplan.computation.protocols import CalculationConfig, thaw_json_value


class GeneratorRegistry:
    """已发布的生成器目录(不可变快照语义，实例级持有，无模块全局状态)。"""

    __slots__ = ("_providers",)

    def __init__(self, providers: Mapping[str, GeneratorDescriptor]) -> None:
        self._providers = dict(providers)

    def __len__(self) -> int:
        return len(self._providers)

    def snapshot(self) -> tuple[GeneratorDescriptor, ...]:
        """确定性快照（按稳定 ID 排序，供组合根健康探针与审计使用）。"""
        return tuple(self._providers[key] for key in sorted(self._providers))

    def get(self, ref: str) -> GeneratorDescriptor:
        """按精确引用取 provider；未知引用即明确不可用。"""
        try:
            return self._providers[ref]
        except KeyError:
            raise ComputationUnavailableError(
                f"生成器不可用（未注册）: {ref!r}", reason="no-provider"
            ) from None

    def resolve(self, config: CalculationConfig) -> GeneratorDescriptor:
        """按计算配置选中 provider 并完成生成前门禁。

        1. 精确引用选中 provider；未知即 ``ComputationUnavailableError``；
        2. 核对 mode/精度/solver 兼容性，不兼容即 ``ComputationUnavailableError``；
        3. 用 descriptor 的 options_schema 校验 ``config.options``：
           非法选项属于用户计算配置错误，抛明确 ``ValueError``（字段路径/
           原因），不是不可用；不回退其他 generator，不删字段、不补默认、
           不忽略未知项（schema 内 ``default`` 只作描述）。
        """
        descriptor = self.get(config.generator)
        if config.mode not in descriptor.modes:
            raise ComputationUnavailableError(
                f"生成器不支持所选 mode: {config.generator!r} x {config.mode!r}",
                reason="incompatible-mode",
            )
        if config.precision not in descriptor.precisions:
            raise ComputationUnavailableError(
                f"生成器不支持所选精度: {config.generator!r} x {config.precision!r}",
                reason="incompatible-precision",
            )
        if not descriptor.supports_solver(config.solver):
            raise ComputationUnavailableError(
                f"生成器不兼容所选求解器: {config.generator!r} x {config.solver!r}",
                reason="incompatible-solver",
            )
        _check_options(descriptor, config)
        return descriptor

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本（能力目录公布，不含实现）。"""
        return {"generators": [d.to_dict() for d in self.snapshot()]}


def build_registry(candidates: Iterable[GeneratorDescriptor]) -> GeneratorRegistry:
    """原子构建并发布生成器目录。

    全部候选先校验（类型、ID 唯一、版本形态由 descriptor 自身保证，
    此处只查重复引用）再一次性发布；任一重复即整体拒绝，不发布部分表。
    空候选集发布空表（当前尚无真实 provider 实现，选择时即不可用）。
    """
    providers: dict[str, GeneratorDescriptor] = {}
    for candidate in candidates:
        if not isinstance(candidate, GeneratorDescriptor):
            raise TypeError(
                "生成器候选须为 GeneratorDescriptor, "
                f"得到 {type(candidate).__name__}"
            )
        if candidate.ref in providers:
            raise ValueError(f"生成器重复注册: {candidate.ref!r}")
        providers[candidate.ref] = candidate
    return GeneratorRegistry(providers)


def _check_options(descriptor: GeneratorDescriptor, config: CalculationConfig) -> None:
    """生成前 options 门禁：用 descriptor schema 校验配置选项。

    非法选项是用户计算配置错误，抛明确 ``ValueError``（稳定字段路径与
    原因），不是 ``ComputationUnavailableError``；调用方不得据此回退
    其他 generator。只校验，不修改配置（schema 内 ``default`` 只作描述）。
    """
    schema = thaw_json_value(descriptor.options_schema)
    options = thaw_json_value(config.options)
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(options))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.absolute_path)
        raise ValueError(
            f"计算选项非法[{location or '<root>'}]: {first.message}"
        )


__all__ = ["GeneratorRegistry", "build_registry"]
