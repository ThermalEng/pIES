"""Solver Bundle 公开契约入口 (1.2.0 契约解耦版)。

最小边界：manifest schema 文件加载、规范化、契约与插件兼容性校验、规范 YAML 导出。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from iesplan.computation.protocols import (
    CalculationConfig,
    SolverBundle,
    SolverBundleManifest,
    check_finite_json,
)
from iesplan.core.content import Content, MemoryContent
from iesplan.core.yamlmini import dump as _yaml_dump

if TYPE_CHECKING:
    from iesplan.computation.contracts.compatibility import StagePluginSpec

SCHEMA_PATH = pathlib.Path(__file__).parent / "schema" / "solver-bundle-1.0.0.schema.json"
RESERVED_MANIFEST_PATHS = frozenset({"bundle.yaml"})
INPUT_PATH_PREFIX = "input/"
OUTPUT_PATH_PREFIX = "output/"
PRIMAL_SOLUTION_OUTPUT_PATH = "output/primal_solution.json"


def load_bundle_schema() -> dict[str, Any]:
    """加载发布的 Bundle JSON Schema（包内资源，随包发布）。"""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def normalize_bundle(data: Mapping[str, Any]) -> dict[str, Any]:
    """规范化 Bundle 映射：递归键排序，序列顺序保持；拒绝非有限数值与非 JSON 值。"""
    if not isinstance(data, Mapping):
        raise TypeError(f"Bundle 须为 Mapping，得到 {type(data).__name__}")
    check_finite_json(data, "bundle")
    return {key: _normalize_value(data[key]) for key in sorted(data)}


def _normalize_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _normalize_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    return value


def validate_bundle(
    data: Mapping[str, Any],
    *,
    generator_spec: StagePluginSpec | None = None,
    solver_spec: StagePluginSpec | None = None,
    config: CalculationConfig | None = None,
) -> SolverBundleManifest:
    """校验契约解耦 SolverBundle manifest 映射并构造不可变 manifest 载体。"""
    normalized = normalize_bundle(data)
    validator = Draft202012Validator(load_bundle_schema())
    errors = sorted(validator.iter_errors(normalized), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.absolute_path)
        raise ValueError(f"Bundle 契约校验失败[{location or '<root>'}]: {first.message}")
    manifest = SolverBundleManifest(**normalized)
    bundle_generator = str(manifest.bundle["generator"])
    bundle_contract = str(manifest.bundle["contract"])

    if config is not None:
        if config.generator is None:
            raise ValueError("一体化配置不能绑定分步 SolverBundle")
        if bundle_generator != config.generator:
            raise ValueError(
                f"Bundle 生成器与计算配置不一致: {bundle_generator!r} x {config.generator!r}"
            )
    if generator_spec is not None:
        if generator_spec.kind != "generator":
            raise ValueError(f"generator_spec 须为 generator 类型，得到 {generator_spec.kind!r}")
        if bundle_generator != generator_spec.ref:
            raise ValueError(
                f"Bundle 生成器与插件描述不一致: {bundle_generator!r} x {generator_spec.ref!r}"
            )
        if bundle_contract not in generator_spec.produces:
            raise ValueError(
                f"Bundle 契约 {bundle_contract!r} 不在生成器 {generator_spec.ref!r} "
                f"声明的 produces={list(generator_spec.produces)!r} 中"
            )
    if solver_spec is not None:
        if solver_spec.kind != "solver":
            raise ValueError(f"solver_spec 须为 solver 类型，得到 {solver_spec.kind!r}")
        if config is not None and solver_spec.ref != config.solver:
            raise ValueError(
                f"solver_spec 与计算配置不一致: {solver_spec.ref!r} x {config.solver!r}"
            )
        if bundle_contract not in solver_spec.consumes:
            raise ValueError(
                f"求解器 {solver_spec.ref!r} consumes={list(solver_spec.consumes)!r} "
                f"不兼容 Bundle 契约 {bundle_contract!r}"
            )
    return manifest


def manifest_to_yaml_bytes(manifest: SolverBundleManifest) -> bytes:
    """manifest → bundle.yaml 规范字节（UTF-8，末尾换行）。"""
    if not isinstance(manifest, SolverBundleManifest):
        raise TypeError(
            "manifest 须为 SolverBundleManifest，"
            f"得到 {type(manifest).__name__}"
        )
    return _yaml_dump(manifest.to_dict()).encode("utf-8")


def export_bundle_files(bundle: SolverBundle) -> dict[str, Content]:
    """完整聚合 → 路径到内容映射（含 bundle.yaml 与全部输入文件）。"""
    if not isinstance(bundle, SolverBundle):
        raise TypeError(
            f"bundle 须为 SolverBundle，得到 {type(bundle).__name__}"
        )
    exported: dict[str, Content] = {
        "bundle.yaml": MemoryContent(manifest_to_yaml_bytes(bundle.manifest))
    }
    for path in sorted(bundle.files):
        exported[path] = bundle.files[path]
    return exported


__all__ = [
    "INPUT_PATH_PREFIX",
    "OUTPUT_PATH_PREFIX",
    "PRIMAL_SOLUTION_OUTPUT_PATH",
    "RESERVED_MANIFEST_PATHS",
    "SCHEMA_PATH",
    "export_bundle_files",
    "load_bundle_schema",
    "manifest_to_yaml_bytes",
    "normalize_bundle",
    "validate_bundle",
]
