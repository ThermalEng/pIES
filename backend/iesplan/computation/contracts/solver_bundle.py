"""Target 1.2.0 contract-decoupled SolverBundle manifest and validation."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from iesplan.computation.contracts.calculation_config import TargetCalculationConfig
from iesplan.computation.contracts.common import (
    check_finite_json,
    check_relative_path,
    validate_against_schema,
)
from iesplan.computation.contracts.compatibility import StagePluginSpec
from iesplan.computation.protocols import check_provider_ref, freeze_json_value, thaw_json_value
from iesplan.core.content import Content, MemoryContent
from iesplan.core.yamlmini import dump as _yaml_dump

TARGET_SOLVER_BUNDLE_SCHEMA = "ies.solver-bundle"
TARGET_SOLVER_BUNDLE_VERSION = "1.0.0"
PRIMAL_SOLUTION_OUTPUT_PATH = "output/primal_solution.json"

TARGET_SOLVER_BUNDLE_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "schema"
    / "solver-bundle-target-1.0.0.schema.json"
)


def load_target_bundle_schema() -> dict[str, Any]:
    """Load the bundled Draft 2020-12 JSON Schema for 1.2.0 SolverBundle."""
    return json.loads(TARGET_SOLVER_BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8"))


def normalize_target_bundle(data: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively sort mapping keys while keeping sequence order and finite JSON values."""
    if not isinstance(data, Mapping):
        raise TypeError(f"Bundle 须为 Mapping，得到 {type(data).__name__}")
    check_finite_json(data, "bundle")
    normalized: dict[str, Any] = {}
    for key in sorted(data):
        normalized[key] = _normalize_value(data[key])
    return normalized


def _normalize_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {k: _normalize_value(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    return value


def _freeze_io_entries(entries: object, name: str) -> tuple[Mapping[str, object], ...]:
    if isinstance(entries, Mapping) or not isinstance(entries, (list, tuple)):
        raise TypeError(f"{name} 须为条目序列，得到 {type(entries).__name__}")
    allowed = ("path", "media_type", "required") if name == "outputs" else ("path", "media_type")
    frozen: list[Mapping[str, object]] = []
    seen_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise TypeError(f"{name} 条目须为 Mapping，得到 {type(entry).__name__}")
        extra = sorted(k for k in entry if k not in allowed)
        if extra:
            raise ValueError(f"{name} 条目不接受额外字段: {extra!r}")
        for req_key in ("path", "media_type"):
            if req_key not in entry:
                raise ValueError(f"{name} 条目缺必需字段: {req_key}")
        rel_path = check_relative_path(entry["path"], f"{name}.path")
        if rel_path in seen_paths:
            raise ValueError(f"{name}.path 不得重复: {rel_path!r}")
        seen_paths.add(rel_path)
        media_type = entry["media_type"]
        if not isinstance(media_type, str) or not media_type:
            raise ValueError(f"{name}.media_type 须为非空字符串: {media_type!r}")
        if name == "outputs" and (
            "required" not in entry or not isinstance(entry["required"], bool)
        ):
            raise ValueError(f"{name} 条目 required 须为布尔值")
        frozen.append(freeze_json_value(dict(entry)))
    if not frozen:
        raise ValueError(f"{name} 不得为空")
    if name == "outputs":
        primal_entries = [e for e in frozen if e["path"] == PRIMAL_SOLUTION_OUTPUT_PATH]
        if not primal_entries:
            raise ValueError(f"outputs 必须包含 {PRIMAL_SOLUTION_OUTPUT_PATH!r}")
        primal = primal_entries[0]
        if primal["required"] is not True or primal["media_type"] != "application/json":
            raise ValueError(
                f"{PRIMAL_SOLUTION_OUTPUT_PATH!r} 须声明 required=true 且 media_type='application/json'"
            )
    return tuple(frozen)


@dataclass(frozen=True, slots=True)
class TargetSolverBundleManifest:
    """1.2.0 contract-decoupled SolverBundle manifest (no solver/command/result_adapter bindings)."""

    schema: str = TARGET_SOLVER_BUNDLE_SCHEMA
    schema_version: str = TARGET_SOLVER_BUNDLE_VERSION
    bundle: Mapping[str, object] = field(default_factory=dict)
    inputs: tuple[Mapping[str, object], ...] = ()
    outputs: tuple[Mapping[str, object], ...] = ()
    extensions: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != TARGET_SOLVER_BUNDLE_SCHEMA:
            raise ValueError(f"Bundle schema 非法: {self.schema!r}")
        if self.schema_version != TARGET_SOLVER_BUNDLE_VERSION:
            raise ValueError(f"Bundle 版本非法: {self.schema_version!r}")
        if not isinstance(self.bundle, Mapping):
            raise TypeError("bundle 须为 Mapping")
        if set(self.bundle) != {"generator", "contract"}:
            raise ValueError(f"bundle 须恰为 generator/contract 二键: {sorted(self.bundle)!r}")
        check_provider_ref(self.bundle["generator"], "bundle.generator")
        check_provider_ref(self.bundle["contract"], "bundle.contract")
        if not isinstance(self.extensions, Mapping):
            raise TypeError("extensions 须为 Mapping")
        check_finite_json(self.extensions, "extensions")
        object.__setattr__(self, "bundle", freeze_json_value(dict(self.bundle)))
        object.__setattr__(self, "inputs", _freeze_io_entries(self.inputs, "inputs"))
        object.__setattr__(self, "outputs", _freeze_io_entries(self.outputs, "outputs"))
        object.__setattr__(self, "extensions", freeze_json_value(dict(self.extensions)))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to an independent JSON-compatible dictionary."""
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "bundle": thaw_json_value(self.bundle),
            "inputs": [dict(thaw_json_value(entry)) for entry in self.inputs],
            "outputs": [dict(thaw_json_value(entry)) for entry in self.outputs],
            "extensions": thaw_json_value(self.extensions),
        }


@dataclass(frozen=True, slots=True)
class TargetSolverBundle:
    """Immutable decoupled SolverBundle aggregate (manifest + chunked input Content mapping)."""

    manifest: TargetSolverBundleManifest
    files: Mapping[str, Content] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, TargetSolverBundleManifest):
            raise TypeError(
                f"manifest 须为 TargetSolverBundleManifest，得到 {type(self.manifest).__name__}"
            )
        if not isinstance(self.files, Mapping):
            raise TypeError(f"files 须为 Mapping，得到 {type(self.files).__name__}")
        for path, content in self.files.items():
            check_relative_path(path, "files")
            if not isinstance(content, (Content, bytes)):
                raise TypeError(f"files[{path!r}] 须为 Content，得到 {type(content).__name__}")
        declared = {str(entry["path"]) for entry in self.manifest.inputs}
        if set(self.files) != declared:
            raise ValueError(
                "manifest.inputs 与 files 必须恰好对应: "
                f"清单 {sorted(declared)!r} x 文件 {sorted(self.files)!r}"
            )
        object.__setattr__(
            self,
            "files",
            MappingProxyType(
                {
                    path: MemoryContent(value) if isinstance(value, bytes) else value
                    for path, value in sorted(self.files.items())
                }
            ),
        )


def validate_target_bundle(
    data: Mapping[str, Any],
    *,
    generator_spec: StagePluginSpec | None = None,
    solver_spec: StagePluginSpec | None = None,
    config: TargetCalculationConfig | None = None,
) -> TargetSolverBundleManifest:
    """Validate decoupled SolverBundle manifest and check optional contract bindings."""
    normalized = normalize_target_bundle(data)
    validate_against_schema(
        load_target_bundle_schema(),
        normalized,
        error_prefix="Bundle 契约校验失败",
    )
    manifest = TargetSolverBundleManifest(**normalized)
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


def target_manifest_to_yaml_bytes(manifest: TargetSolverBundleManifest) -> bytes:
    """Serialize TargetSolverBundleManifest to canonical UTF-8 YAML bytes."""
    if not isinstance(manifest, TargetSolverBundleManifest):
        raise TypeError(
            f"manifest 须为 TargetSolverBundleManifest，得到 {type(manifest).__name__}"
        )
    return _yaml_dump(manifest.to_dict()).encode("utf-8")


def export_target_bundle_files(bundle: TargetSolverBundle) -> dict[str, Content]:
    """Export bundle.yaml + all declared inputs as an ordered Content mapping."""
    if not isinstance(bundle, TargetSolverBundle):
        raise TypeError(f"bundle 须为 TargetSolverBundle，得到 {type(bundle).__name__}")
    exported: dict[str, Content] = {
        "bundle.yaml": MemoryContent(target_manifest_to_yaml_bytes(bundle.manifest))
    }
    for path in sorted(bundle.files):
        exported[path] = bundle.files[path]
    return exported
