"""Standardized /work/in -> /work/out stage I/O helpers for all 5 plugin kinds."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pies_sdk.contracts import (
    SOLVER_BUNDLE_SCHEMA,
    SOLVER_BUNDLE_VERSION,
    ComputeResultPayload,
    PredictionOutputPayload,
    check_finite_json,
    check_relative_path,
    read_json_file,
    write_json_file,
)
from pies_sdk.errors import StageIOError
from pies_sdk.iteration import ITERATION_LOG_FILENAME, IterationWriter
from pies_sdk.manifest import PLUGIN_KINDS, PluginManifest
from pies_sdk.yaml_safe import dump_safe_yaml, load_safe_yaml

DEFAULT_WORK_IN = Path("/work/in")
DEFAULT_WORK_OUT = Path("/work/out")


def _ensure_work_in(work_in: Path) -> Path:
    path = Path(work_in)
    if not path.is_dir():
        raise StageIOError(f"Input directory does not exist: {path}")
    return path


def _ensure_work_out(work_out: Path) -> Path:
    path = Path(work_out)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_join(root: Path, rel_path: str) -> Path:
    rel = check_relative_path(rel_path)
    resolved_root = root.resolve()
    target = (resolved_root / rel).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise StageIOError(f"Path escapes stage directory {root}: {rel_path!r}")
    return target


# ---------------------------------------------------------------------------
# Stage 1: predictor (/work/in/request.json -> /work/out/prediction.json)
# ---------------------------------------------------------------------------


def read_predictor_request(work_in: Path = DEFAULT_WORK_IN) -> dict[str, Any]:
    root = _ensure_work_in(work_in)
    data = read_json_file(root / "request.json")
    if not isinstance(data, dict):
        raise StageIOError("predictor /work/in/request.json must be a JSON object")
    return data


def write_prediction_output(
    work_out: Path,
    payload: PredictionOutputPayload | Mapping[str, Any],
) -> Path:
    root = _ensure_work_out(work_out)
    model = (
        payload
        if isinstance(payload, PredictionOutputPayload)
        else PredictionOutputPayload.from_dict(payload)
    )
    return write_json_file(root / "prediction.json", model.to_dict())


def read_prediction_output(work_out: Path = DEFAULT_WORK_OUT) -> PredictionOutputPayload:
    data = read_json_file(Path(work_out) / "prediction.json")
    return PredictionOutputPayload.from_dict(data)


# ---------------------------------------------------------------------------
# Stage 2: generator (/work/in/ -> /work/out/bundle/)
# ---------------------------------------------------------------------------


def read_generator_inputs(work_in: Path = DEFAULT_WORK_IN) -> dict[str, Any]:
    """Load available frozen inputs for a generator stage."""
    root = _ensure_work_in(work_in)
    bundle: dict[str, Any] = {}
    for filename in (
        "request.json",
        "calculation_config.json",
        "assembly.json",
        "sequences.json",
        "finance.json",
        "evaluation.json",
    ):
        candidate = root / filename
        if candidate.is_file():
            bundle[filename.removesuffix(".json")] = read_json_file(candidate)
    for yaml_name in ("assembly.yaml", "effective_finance.yaml"):
        candidate = root / yaml_name
        if candidate.is_file():
            parsed = load_safe_yaml(candidate.read_text(encoding="utf-8"))
            bundle[yaml_name.removesuffix(".yaml")] = parsed
    if "request" not in bundle and "calculation_config" not in bundle:
        raise StageIOError(
            "generator /work/in must contain 'calculation_config.json' or 'request.json'"
        )
    return bundle


def write_solver_bundle(
    work_out: Path,
    manifest: Mapping[str, Any],
    files: Mapping[str, bytes | str],
) -> Path:
    """Write standard Solver Bundle under /work/out/bundle/."""
    root = _ensure_work_out(work_out)
    bundle_dir = root / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    if not isinstance(manifest, Mapping):
        raise StageIOError("Solver Bundle manifest must be a mapping")
    if manifest.get("schema") != SOLVER_BUNDLE_SCHEMA:
        raise StageIOError(f"Invalid Solver Bundle schema: {manifest.get('schema')!r}")
    if manifest.get("schema_version") != SOLVER_BUNDLE_VERSION:
        raise StageIOError(
            f"Invalid Solver Bundle schema_version: {manifest.get('schema_version')!r}"
        )
    check_finite_json(manifest, "bundle.yaml")

    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise StageIOError("Solver Bundle manifest requires non-empty 'inputs'")
    declared_paths: set[str] = set()
    for entry in inputs:
        if not isinstance(entry, Mapping) or "path" not in entry:
            raise StageIOError("Solver Bundle input entry requires 'path'")
        declared_paths.add(check_relative_path(entry["path"], "bundle.inputs.path"))

    if set(files) != declared_paths:
        raise StageIOError(
            f"Solver Bundle files {sorted(files)!r} do not match declared inputs {sorted(declared_paths)!r}"
        )

    for rel_path, content in files.items():
        target = _safe_join(bundle_dir, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        elif isinstance(content, str):
            target.write_text(content, encoding="utf-8")
        else:
            raise StageIOError(
                f"Solver Bundle file {rel_path!r} must be bytes or str, got {type(content).__name__}"
            )

    manifest_path = bundle_dir / "bundle.yaml"
    manifest_path.write_text(dump_safe_yaml(dict(manifest)), encoding="utf-8")
    return bundle_dir


def validate_solver_bundle_dir(bundle_dir: Path) -> dict[str, Any]:
    """Validate a Solver Bundle directory containing bundle.yaml and declared inputs."""
    manifest_path = bundle_dir / "bundle.yaml"
    if not manifest_path.is_file():
        raise StageIOError(f"Solver Bundle missing bundle.yaml: {manifest_path}")
    manifest = load_safe_yaml(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise StageIOError("bundle.yaml must be a YAML mapping")
    if manifest.get("schema") != SOLVER_BUNDLE_SCHEMA:
        raise StageIOError(f"Invalid Solver Bundle schema: {manifest.get('schema')!r}")
    if manifest.get("schema_version") != SOLVER_BUNDLE_VERSION:
        raise StageIOError(
            f"Invalid Solver Bundle schema_version: {manifest.get('schema_version')!r}"
        )
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise StageIOError("bundle.yaml requires non-empty 'inputs'")
    for entry in inputs:
        if not isinstance(entry, Mapping) or "path" not in entry:
            raise StageIOError("bundle.yaml input entry requires 'path'")
        target = _safe_join(bundle_dir, entry["path"])
        if not target.is_file():
            raise StageIOError(f"Declared Solver Bundle input missing: {entry['path']}")
    return manifest


# ---------------------------------------------------------------------------
# Stage 3: solver (/work/in/bundle/ -> /work/out/solution.*)
# ---------------------------------------------------------------------------


def _read_frozen_bundle_manifest(bundle_dir: Path) -> dict[str, Any]:
    """Read frozen /work/in/bundle/bundle.yaml directly without re-checking inputs."""
    manifest_path = bundle_dir / "bundle.yaml"
    if not manifest_path.is_file():
        raise StageIOError(f"Solver Bundle missing bundle.yaml: {manifest_path}")
    manifest = load_safe_yaml(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise StageIOError("bundle.yaml must be a YAML mapping")
    return manifest


def read_solver_bundle(work_in: Path = DEFAULT_WORK_IN) -> tuple[dict[str, Any], Path]:
    root = _ensure_work_in(work_in)
    bundle_dir = root / "bundle"
    manifest = _read_frozen_bundle_manifest(bundle_dir)
    return manifest, bundle_dir


def write_solver_solution(
    work_out: Path,
    filename: str,
    content: bytes | str | Mapping[str, Any],
) -> Path:
    root = _ensure_work_out(work_out)
    rel = check_relative_path(filename, "solution filename")
    if "/" in rel or not rel.startswith("solution.") or len(rel) <= len("solution."):
        raise StageIOError(
            f"Solver output filename must match 'solution.<ext>', got {filename!r}"
        )
    target = _safe_join(root, rel)
    if isinstance(content, Mapping):
        return write_json_file(target, dict(content))
    if isinstance(content, bytes):
        target.write_bytes(content)
        return target
    if isinstance(content, str):
        target.write_text(content, encoding="utf-8")
        return target
    raise StageIOError(f"Unsupported solution content type: {type(content).__name__}")


def list_solver_solutions(work_out: Path = DEFAULT_WORK_OUT) -> list[Path]:
    root = Path(work_out)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and p.name.startswith("solution."))


# ---------------------------------------------------------------------------
# Stage 4: evaluator (/work/in/{bundle/,solution.*,receipt.json} -> /work/out/result.json)
# ---------------------------------------------------------------------------


def _find_evaluator_solutions(root: Path) -> list[Path]:
    direct = sorted(p for p in root.iterdir() if p.is_file() and p.name.startswith("solution."))
    if direct:
        return direct
    bundle_out = root / "bundle" / "output"
    if bundle_out.is_dir():
        return sorted(
            p for p in bundle_out.iterdir() if p.is_file() and p.name.startswith("solution.")
        )
    return []


def read_evaluator_inputs(work_in: Path = DEFAULT_WORK_IN) -> dict[str, Any]:
    root = _ensure_work_in(work_in)
    bundle_dir = root / "bundle"
    bundle_manifest = _read_frozen_bundle_manifest(bundle_dir)
    solutions = _find_evaluator_solutions(root)
    if not solutions:
        raise StageIOError("evaluator /work/in missing solution.* output file")
    receipt_path = root / "receipt.json"
    receipt = read_json_file(receipt_path) if receipt_path.is_file() else None
    request_path = root / "request.json"
    request = read_json_file(request_path) if request_path.is_file() else None
    if receipt is None and request is None:
        raise StageIOError("evaluator /work/in requires 'receipt.json' or 'request.json'")

    finance: Any = None
    if (root / "finance.json").is_file():
        finance = read_json_file(root / "finance.json")
    elif (root / "effective_finance.yaml").is_file():
        finance = load_safe_yaml((root / "effective_finance.yaml").read_text(encoding="utf-8"))

    evaluation = (
        read_json_file(root / "evaluation.json") if (root / "evaluation.json").is_file() else None
    )

    return {
        "bundle_manifest": bundle_manifest,
        "bundle_dir": bundle_dir,
        "solution_paths": solutions,
        "receipt": receipt,
        "request": request,
        "finance": finance,
        "evaluation": evaluation,
    }


# ---------------------------------------------------------------------------
# Stage 5: algorithm (/work/in/request.json -> /work/out/result.json)
# ---------------------------------------------------------------------------


def read_algorithm_request(work_in: Path = DEFAULT_WORK_IN) -> dict[str, Any]:
    root = _ensure_work_in(work_in)
    data = read_json_file(root / "request.json")
    if not isinstance(data, dict):
        raise StageIOError("algorithm /work/in/request.json must be a JSON object")
    return data


def write_compute_result(
    work_out: Path,
    result: ComputeResultPayload | Mapping[str, Any],
) -> Path:
    root = _ensure_work_out(work_out)
    payload = (
        result if isinstance(result, ComputeResultPayload) else ComputeResultPayload.from_dict(result)
    )
    return write_json_file(root / "result.json", payload.to_dict())


def read_compute_result(work_out: Path = DEFAULT_WORK_OUT) -> ComputeResultPayload:
    data = read_json_file(Path(work_out) / "result.json")
    return ComputeResultPayload.from_dict(data)


# ---------------------------------------------------------------------------
# Unified stage contract validation & StageContext
# ---------------------------------------------------------------------------


def validate_stage_outputs(kind: str, work_out: Path = DEFAULT_WORK_OUT) -> None:
    """Validate that /work/out satisfies the stage output contract for `kind`."""
    if kind not in PLUGIN_KINDS:
        raise StageIOError(f"Unknown plugin kind: {kind!r}")
    root = Path(work_out)
    if not root.is_dir():
        raise StageIOError(f"Output directory does not exist: {root}")
    if kind == "predictor":
        read_prediction_output(root)
    elif kind == "generator":
        validate_solver_bundle_dir(root / "bundle")
    elif kind == "solver":
        if not list_solver_solutions(root):
            raise StageIOError("solver /work/out missing 'solution.*' output file")
    elif kind in ("evaluator", "algorithm"):
        read_compute_result(root)


def extract_stage_parameters(work_in: Path) -> dict[str, Any]:
    """Extract parameters/options mapping from /work/in request or calculation config."""
    root = Path(work_in)
    req_path = root / "request.json"
    if req_path.is_file():
        req = read_json_file(req_path)
        if isinstance(req, Mapping):
            if "parameters" in req:
                params = req["parameters"]
                if not isinstance(params, Mapping):
                    raise StageIOError("request.json 'parameters' must be a JSON object")
                return dict(params)
            if "options" in req:
                opts = req["options"]
                if not isinstance(opts, Mapping):
                    raise StageIOError("request.json 'options' must be a JSON object")
                return dict(opts)
    cfg_path = root / "calculation_config.json"
    if cfg_path.is_file():
        cfg = read_json_file(cfg_path)
        if isinstance(cfg, Mapping) and "options" in cfg:
            opts = cfg["options"]
            if not isinstance(opts, Mapping):
                raise StageIOError("calculation_config.json 'options' must be a JSON object")
            return dict(opts)
    return {}


@dataclass(frozen=True, slots=True)
class StageContext:
    """Execution context passed to a plugin entrypoint `<module>:<func>(ctx)`."""

    manifest: PluginManifest
    work_in: Path
    work_out: Path
    parameters: Mapping[str, Any]

    @property
    def kind(self) -> str:
        return self.manifest.kind

    def read_request(self) -> dict[str, Any]:
        return read_json_file(self.work_in / "request.json")

    def iteration_writer(self, *, truncate: bool = False) -> IterationWriter:
        return IterationWriter(self.work_out / ITERATION_LOG_FILENAME, truncate=truncate)

    def write_prediction(self, payload: PredictionOutputPayload | Mapping[str, Any]) -> Path:
        return write_prediction_output(self.work_out, payload)

    def write_bundle(
        self, manifest: Mapping[str, Any], files: Mapping[str, bytes | str]
    ) -> Path:
        return write_solver_bundle(self.work_out, manifest, files)

    def write_solution(
        self, filename: str, content: bytes | str | Mapping[str, Any]
    ) -> Path:
        return write_solver_solution(self.work_out, filename, content)

    def write_result(self, result: ComputeResultPayload | Mapping[str, Any]) -> Path:
        return write_compute_result(self.work_out, result)
