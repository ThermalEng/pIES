"""Solver Bundle 公开契约入口。

最小边界：manifest schema 文件加载、规范化、校验与规范字节导出。
Bundle 消费已验证的装配产物、GeneratorDescriptor 与 CalculationConfig
固定下来的引用（绑定时交叉核对），不重新读取设备模型，不运行 solver，
不创建 Runtime/ExecutorProvider，不实现 ResultAdapter，不做任何动态
插件加载。

规范化只做键排序（字节稳定的规范形式，序列顺序保持语义）；不计算、
不存储任何内容摘要或完整性字段。manifest 规范字节导出复用
``core.yamlmini`` 公共序列化能力。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator

from iesplan.computation.generators.descriptor import GeneratorDescriptor
from iesplan.core.yamlmini import dump as _yaml_dump
from iesplan.computation.protocols import (
    CalculationConfig,
    SolverBundle,
    SolverBundleManifest,
)

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
) -> SolverBundleManifest:
    """校验 manifest 映射并构造不可变 manifest 载体。

    只针对 manifest 本体，不触及文件字节（完整聚合见 ``SolverBundle``）。
    依次执行：规范化 → JSON Schema 校验（未知字段即拒绝）→ frozen
    ``SolverBundleManifest`` 构造。给出 ``descriptor``/``config`` 时额外
    核对引用绑定：``bundle.generator`` 须等于 descriptor/config 的选择，
    ``bundle.solver`` 须等于 config 的选择且被 descriptor 兼容，
    ``result_adapter`` 须等于 descriptor 声明；不一致即拒绝，
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
    bundle = SolverBundleManifest(**normalized)
    bundle_generator = bundle.bundle["generator"]
    bundle_solver = bundle.bundle["solver"]
    if descriptor is not None:
        if not isinstance(descriptor, GeneratorDescriptor):
            raise TypeError(
                "descriptor 须为 GeneratorDescriptor，"
                f"得到 {type(descriptor).__name__}"
            )
        if bundle_generator != descriptor.ref:
            raise ValueError(
                f"Bundle 生成器与 descriptor 不一致: {bundle_generator!r} x {descriptor.ref!r}"
            )
        if bundle.result_adapter != descriptor.result_adapter:
            raise ValueError(
                "Bundle 结果适配器与 descriptor 不一致: "
                f"{bundle.result_adapter!r} x {descriptor.result_adapter!r}"
            )
    if config is not None:
        if bundle_generator != config.generator:
            raise ValueError(
                f"Bundle 生成器与计算配置不一致: {bundle_generator!r} x {config.generator!r}"
            )
        if bundle_solver != config.solver:
            raise ValueError(
                f"Bundle 求解器与计算配置不一致: {bundle_solver!r} x {config.solver!r}"
            )
        if descriptor is not None and not descriptor.supports_solver(bundle_solver):
            raise ValueError(
                f"descriptor 不兼容 Bundle 求解器: {descriptor.ref!r} x {bundle_solver!r}"
            )
    return bundle


def manifest_to_yaml_bytes(manifest: SolverBundleManifest) -> bytes:
    """manifest → bundle.yaml 规范字节（UTF-8，末尾换行）。

    复用 ``core.yamlmini.dump`` 公共序列化（键排序、双引号字符串、
    两空格缩进）；同一 manifest 恒得同一字节，可由 ``yamlmini.load``
    语义回读。
    """
    if not isinstance(manifest, SolverBundleManifest):
        raise TypeError(
            "manifest 须为 SolverBundleManifest，"
            f"得到 {type(manifest).__name__}"
        )
    return _yaml_dump(manifest.to_dict()).encode("utf-8")


def export_bundle_files(bundle: SolverBundle) -> dict[str, bytes]:
    """完整聚合 → 路径到字节映射（含 bundle.yaml 与全部输入文件）。

    返回独立副本；键为 Bundle 规范相对路径（bundle.yaml 在首位，
    其后输入文件按键排序），可直接物化为运行目录。
    """
    if not isinstance(bundle, SolverBundle):
        raise TypeError(
            f"bundle 须为 SolverBundle，得到 {type(bundle).__name__}"
        )
    exported: dict[str, bytes] = {
        "bundle.yaml": manifest_to_yaml_bytes(bundle.manifest)
    }
    for path in sorted(bundle.files):
        exported[path] = bundle.files[path]
    return exported


__all__ = [
    "SCHEMA_PATH",
    "export_bundle_files",
    "load_bundle_schema",
    "manifest_to_yaml_bytes",
    "normalize_bundle",
    "validate_bundle",
]
