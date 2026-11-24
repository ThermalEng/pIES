"""Container entrypoint CLI (`python -m pies_sdk.runner`).

Executes a plugin entrypoint purely against /work/in and /work/out without
touching backend internals, databases, queues, or the network.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pies_sdk.errors import EntrypointError, PiesSdkError
from pies_sdk.io import (
    DEFAULT_WORK_IN,
    DEFAULT_WORK_OUT,
    StageContext,
    extract_stage_parameters,
    validate_stage_outputs,
)
from pies_sdk.manifest import (
    CURRENT_PIES_VERSION,
    CURRENT_SDK_VERSION,
    load_plugin_manifest,
)


def resolve_entrypoint(entrypoint: str, plugin_root: Path) -> Callable[[StageContext], Any]:
    """Import `<module.path>:<public_function>` from `plugin_root`."""
    if ":" not in entrypoint:
        raise EntrypointError(f"Invalid entrypoint format: {entrypoint!r}")
    module_name, func_name = entrypoint.split(":", 1)
    if func_name.startswith("_"):
        raise EntrypointError(f"Entrypoint function must be public: {entrypoint!r}")

    root_str = str(plugin_root.resolve())
    inserted = False
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
        inserted = True
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise EntrypointError(
            f"Failed to import entrypoint module {module_name!r}: {exc}"
        ) from exc
    finally:
        if inserted and root_str in sys.path:
            # Keep sys.path clean if module import failed; if imported, package stays in sys.modules.
            pass

    target = getattr(module, func_name, None)
    if target is None or not callable(target):
        raise EntrypointError(
            f"Entrypoint function {func_name!r} not found or not callable in {module_name!r}"
        )
    return target


def run_plugin(
    *,
    manifest_path: Path = Path("plugin.yaml"),
    work_in: Path = DEFAULT_WORK_IN,
    work_out: Path = DEFAULT_WORK_OUT,
    pies_version: str = CURRENT_PIES_VERSION,
    sdk_version: str = CURRENT_SDK_VERSION,
) -> None:
    """Validate manifest, execute entrypoint(ctx) against /work/in, and validate /work/out."""
    manifest_file = Path(manifest_path)
    manifest = load_plugin_manifest(
        manifest_file,
        pies_version=pies_version,
        sdk_version=sdk_version,
        check_compat=True,
    )

    in_dir = Path(work_in)
    out_dir = Path(work_out)

    raw_params = extract_stage_parameters(in_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = StageContext(
        manifest=manifest,
        work_in=in_dir,
        work_out=out_dir,
        parameters=raw_params,
    )
    fn = resolve_entrypoint(manifest.entrypoint, manifest_file.parent)
    fn(ctx)

    validate_stage_outputs(manifest.kind, out_dir)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pies_sdk.runner",
        description="Execute a pIES algorithm plugin against /work/in and /work/out.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("plugin.yaml"),
        help="Path to plugin.yaml (default: ./plugin.yaml)",
    )
    parser.add_argument(
        "--work-in",
        type=Path,
        default=DEFAULT_WORK_IN,
        help="Read-only stage input directory (default: /work/in)",
    )
    parser.add_argument(
        "--work-out",
        type=Path,
        default=DEFAULT_WORK_OUT,
        help="Writable stage output directory (default: /work/out)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        run_plugin(
            manifest_path=args.manifest,
            work_in=args.work_in,
            work_out=args.work_out,
        )
    except PiesSdkError as exc:
        print(f"pies-sdk error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"plugin execution failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
