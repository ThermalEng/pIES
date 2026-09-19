"""Solver Bundle 公开契约入口(0.8 切片 B)。

只提供最小边界：Bundle schema 文件加载、规范化与校验。Bundle 消费
已验证的装配产物、GeneratorDescriptor 与 CalculationConfig 固定下来的
引用（绑定时交叉核对），不重新读取设备模型，不运行 solver，不创建
Runtime/ExecutorProvider，不实现真实 generator 或 ResultAdapter，不做
任何动态插件加载。

规范化只做键排序（字节稳定的规范形式，序列顺序保持语义）；不计算、
不存储任何内容摘要或完整性字段。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator

from iesplan.computation.generators.descriptor import GeneratorDescriptor
from iesplan.computation.protocols import CalculationConfig, SolverBundle

SCHEMA_PATH = pathlib.Path(__file__).parent / "schema" / "solver-bundle-1.0.0.schema.json"


def load_bundle_schema() -> dict[str, Any]:
    """加载发布的 Bundle JSON Schema（包内资源，随包发布）。"""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def normalize_bundle(data: Mapping[str, Any]) -> dict[str, Any]:
    """规范化 Bundle 映射：递归键排序，序列顺序保持。

    同一逻辑 Bundle 恒得同一规范形式；非 JSON 值当场拒绝。
    """
    if not isinstance(data, Mapping):
        raise TypeError(f"Bundle 须为 Mapping，得到 {type(data).__name__}")
    normalized: dict[str, Any] = {}
    for key in sorted(data):
        if not isinstance(key, str):
            raise TypeError(f"Bundle 键须为字符串，得到 {key!r}")
        normalized[key] = _normalize_value(data[key])
    return normalized


def _normalize_value(value: object) -> object:
    if isinstance(value, Mapping):
        return normalize_bundle(value)
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Bundle 字段须为 JSON 值，得到 {type(value).__name__}")


def validate_bundle(
    data: Mapping[str, Any],
    *,
    descriptor: Any | None = None,
    config: CalculationConfig | None = None,
) -> SolverBundle:
    """校验 Bundle 映射并构造不可变载体。

    依次执行：规范化 → JSON Schema 校验（未知字段即拒绝）→ frozen
    ``SolverBundle`` 构造。给出 ``descriptor``/``config`` 时额外核对
    引用绑定：``generator_ref`` 须等于 descriptor/config 的选择，
    ``solver_ref`` 须等于 config 的选择且被 descriptor 兼容，
    ``result_adapter_ref`` 须等于 descriptor 声明；不一致即拒绝，
    不回退到另一 provider。
    """
    normalized = normalize_bundle(data)
    schema = load_bundle_schema()
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(normalized))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.absolute_path)
        raise ValueError(f"Bundle 契约校验失败[{location or '<root>'}]: {first.message}")
    bundle = SolverBundle(**normalized)
    if descriptor is not None:
        if not isinstance(descriptor, GeneratorDescriptor):
            raise TypeError(
                "descriptor 须为 GeneratorDescriptor，"
                f"得到 {type(descriptor).__name__}"
            )
        if bundle.generator_ref != descriptor.ref:
            raise ValueError(
                f"Bundle 生成器与 descriptor 不一致: {bundle.generator_ref!r} x {descriptor.ref!r}"
            )
        if bundle.result_adapter_ref != descriptor.result_adapter:
            raise ValueError(
                "Bundle 结果适配器与 descriptor 不一致: "
                f"{bundle.result_adapter_ref!r} x {descriptor.result_adapter!r}"
            )
    if config is not None:
        if bundle.generator_ref != config.generator:
            raise ValueError(
                f"Bundle 生成器与计算配置不一致: {bundle.generator_ref!r} x {config.generator!r}"
            )
        if bundle.solver_ref != config.solver:
            raise ValueError(
                f"Bundle 求解器与计算配置不一致: {bundle.solver_ref!r} x {config.solver!r}"
            )
        if descriptor is not None and not descriptor.supports_solver(bundle.solver_ref):
            raise ValueError(
                f"descriptor 不兼容 Bundle 求解器: {descriptor.ref!r} x {bundle.solver_ref!r}"
            )
    return bundle


__all__ = [
    "SCHEMA_PATH",
    "load_bundle_schema",
    "normalize_bundle",
    "validate_bundle",
]
