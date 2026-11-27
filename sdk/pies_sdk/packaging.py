"""Plugin package validation (directory / *.algorithm.zip) and deterministic packing."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from pies_sdk.contracts import check_relative_path, read_json_file
from pies_sdk.errors import PiesSdkError, PluginValidationError
from pies_sdk.io import extract_stage_parameters, validate_stage_outputs
from pies_sdk.manifest import PluginManifest, load_plugin_manifest
from pies_sdk.market import parse_artifact_id
from pies_sdk.runner import run_plugin

_FORBIDDEN_FILENAMES = frozenset({"dockerfile", ".dockerignore"})
_PACK_EXCLUDE_DIRS = frozenset({"__pycache__", ".git"})
_PACK_EXCLUDE_SUFFIXES = frozenset({".pyc", ".pyo"})
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_DETERMINISTIC_ZIP_TIME = (2026, 1, 1, 0, 0, 0)

_SINGLE_OUTPUT_FILES: dict[str, str] = {
    "predictor": "prediction.json",
    "solver": "primal_solution.json",
    "algorithm": "solution.json",
}


def _check_entry_name(rel_posix: str, seen_lower: set[str]) -> str:
    if (
        not rel_posix
        or rel_posix.startswith("/")
        or "\\" in rel_posix
        or _DRIVE_PREFIX_RE.match(rel_posix)
        or ".." in rel_posix.split("/")
    ):
        raise PluginValidationError(f"Unsafe path in plugin package: {rel_posix!r}")
    parts = [part for part in rel_posix.split("/") if part]
    if not parts:
        raise PluginValidationError(f"Empty path segment in plugin package: {rel_posix!r}")
    if parts[-1].lower() in _FORBIDDEN_FILENAMES:
        raise PluginValidationError(
            f"Forbidden build file in plugin package: {rel_posix!r}"
        )
    lowered = "/".join(parts).lower()
    if lowered in seen_lower:
        raise PluginValidationError(
            f"Case-insensitive path collision in plugin package: {rel_posix!r}"
        )
    seen_lower.add(lowered)
    return "/".join(parts)


def _extract_and_validate_zip(archive_path: Path, dest_dir: Path) -> None:
    try:
        zf = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise PluginValidationError(f"Invalid plugin ZIP archive {archive_path}: {exc}") from exc

    seen_lower: set[str] = set()
    with zf:
        for info in zf.infolist():
            if info.flag_bits & 0x1:
                raise PluginValidationError(
                    f"Encrypted ZIP entry is forbidden in plugin archive: {info.filename!r}"
                )
            unix_mode = info.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise PluginValidationError(
                    f"Symlink entry is forbidden in plugin archive: {info.filename!r}"
                )
            file_fmt = stat.S_IFMT(unix_mode)
            if file_fmt != 0 and not (
                info.is_dir() or stat.S_ISREG(unix_mode)
            ):
                raise PluginValidationError(
                    f"Non-regular entry mode is forbidden in plugin archive: {info.filename!r}"
                )
            if info.is_dir() and info.filename.rstrip("/") == "":
                continue

            clean_rel = _check_entry_name(info.filename.rstrip("/"), seen_lower)
            target = (dest_dir / clean_rel).resolve()
            if not target.is_relative_to(dest_dir.resolve()):
                raise PluginValidationError(
                    f"Archive entry escapes target directory: {info.filename!r}"
                )
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))


def _scan_directory_files(
    root: Path,
    *,
    ignore_paths: frozenset[Path] = frozenset(),
) -> list[Path]:
    """Validate directory safety (no symlinks/hardlinks/collisions/Dockerfiles) and list files."""
    if not root.is_dir():
        raise PluginValidationError(f"Plugin directory does not exist: {root}")
    root_resolved = root.resolve()
    seen_lower: set[str] = set()
    collected: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_dir = Path(dirpath)
        kept_dirs: list[str] = []
        for dname in sorted(dirnames):
            if dname in _PACK_EXCLUDE_DIRS:
                continue
            full_dir = current_dir / dname
            st = os.lstat(full_dir)
            if stat.S_ISLNK(st.st_mode):
                raise PluginValidationError(
                    f"Symlink directory is forbidden in plugin package: {full_dir}"
                )
            if not stat.S_ISDIR(st.st_mode):
                raise PluginValidationError(
                    f"Non-directory entry is forbidden in plugin package: {full_dir}"
                )
            rel_dir = full_dir.relative_to(root).as_posix()
            _check_entry_name(rel_dir, seen_lower)
            kept_dirs.append(dname)
        dirnames[:] = kept_dirs

        for fname in sorted(filenames):
            full_file = current_dir / fname
            if full_file.resolve() in ignore_paths:
                continue
            if full_file.suffix.lower() in _PACK_EXCLUDE_SUFFIXES:
                continue
            st = os.lstat(full_file)
            if stat.S_ISLNK(st.st_mode):
                raise PluginValidationError(
                    f"Symlink file is forbidden in plugin package: {full_file}"
                )
            if not stat.S_ISREG(st.st_mode):
                raise PluginValidationError(
                    f"Non-regular file is forbidden in plugin package: {full_file}"
                )
            if st.st_nlink > 1:
                raise PluginValidationError(
                    f"Hardlinked file is forbidden in plugin package: {full_file}"
                )
            rel_file = full_file.relative_to(root).as_posix()
            _check_entry_name(rel_file, seen_lower)
            if not full_file.resolve().is_relative_to(root_resolved):
                raise PluginValidationError(f"File escapes plugin root: {full_file}")
            collected.append(full_file)

    return collected


def _check_entrypoint_source(manifest: PluginManifest, root: Path) -> None:
    module_name, func_name = manifest.entrypoint.split(":", 1)
    if func_name.startswith("_"):
        raise PluginValidationError(f"Entrypoint function must be public: {manifest.entrypoint!r}")
    parts = module_name.split(".")
    py_candidate = root.joinpath(*parts).with_suffix(".py")
    pkg_init = root.joinpath(*parts, "__init__.py")
    target = py_candidate if py_candidate.is_file() else pkg_init
    if not target.is_file():
        raise PluginValidationError(
            f"Entrypoint module {module_name!r} not found in plugin package"
        )
    try:
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise PluginValidationError(
            f"Failed to parse entrypoint module {target.name}: {exc}"
        ) from exc

    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for assign_target in node.targets:
                if isinstance(assign_target, ast.Name):
                    defined.add(assign_target.id)
    if func_name not in defined and not pkg_init.is_file():
        raise PluginValidationError(
            f"Entrypoint callable {func_name!r} not defined in {target.name}"
        )


def _check_requirements_file(manifest: PluginManifest, root: Path) -> None:
    if manifest.runtime.requirements is None:
        return
    req_rel = check_relative_path(manifest.runtime.requirements, "runtime.requirements")
    req_path = (root / req_rel).resolve()
    if not req_path.is_relative_to(root.resolve()) or not req_path.is_file():
        raise PluginValidationError(
            f"Declared runtime.requirements file does not exist: {req_rel!r}"
        )
    try:
        req_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PluginValidationError(
            f"Declared runtime.requirements is unreadable UTF-8: {exc}"
        ) from exc


def _check_market_fields(manifest: PluginManifest, root: Path) -> None:
    if not manifest.license:
        raise PluginValidationError("Market validation requires 'license' field in plugin.yaml")
    for required_doc in ("README.md", "LICENSE"):
        doc_path = root / required_doc
        if not doc_path.is_file() or not doc_path.read_text(encoding="utf-8").strip():
            raise PluginValidationError(
                f"Market validation requires non-empty {required_doc!r} at package root"
            )
    if not manifest.tests:
        raise PluginValidationError("Market validation requires at least one entry in 'tests'")


def _resolve_single_output_filename(manifest: PluginManifest) -> str:
    if manifest.kind == "evaluator":
        if "ies.financial-evaluation@1.0.0" in manifest.produces:
            return "financial_evaluation.json"
        return "evaluation_report.json"
    if manifest.kind in _SINGLE_OUTPUT_FILES:
        return _SINGLE_OUTPUT_FILES[manifest.kind]
    raise PluginValidationError(
        f"Plugin kind {manifest.kind!r} must use directory tests (in/ and expected/)"
    )


def _compare_json_values(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise PluginValidationError(f"Plugin test output mismatch for {label}")


def _copy_safe_tree(src_dir: Path, dst_dir: Path) -> None:
    for item in os.walk(src_dir, topdown=True, followlinks=False):
        cur = Path(item[0])
        rel = cur.relative_to(src_dir)
        (dst_dir / rel).mkdir(parents=True, exist_ok=True)
        for dname in item[1]:
            if os.lstat(cur / dname).st_mode & stat.S_IFMT(0o170000) == stat.S_IFLNK:
                raise PluginValidationError(f"Symlink forbidden inside test fixture: {cur / dname}")
        for fname in item[2]:
            src_file = cur / fname
            if os.lstat(src_file).st_mode & stat.S_IFMT(0o170000) == stat.S_IFLNK:
                raise PluginValidationError(f"Symlink forbidden inside test fixture: {src_file}")
            shutil.copy2(src_file, dst_dir / rel / fname)


def _run_offline_tests(manifest: PluginManifest, root: Path) -> None:
    root_resolved = root.resolve()
    with tempfile.TemporaryDirectory(prefix="pies-sdk-validate-") as tmp_str:
        tmp_root = Path(tmp_str)
        for test_spec in manifest.tests:
            in_rel = check_relative_path(test_spec.input, f"tests[{test_spec.id!r}].input")
            exp_rel = check_relative_path(test_spec.expected, f"tests[{test_spec.id!r}].expected")
            src_in = (root / in_rel).resolve()
            src_exp = (root / exp_rel).resolve()
            if not src_in.is_relative_to(root_resolved) or not src_exp.is_relative_to(root_resolved):
                raise PluginValidationError(f"Test {test_spec.id!r} escapes plugin root")

            work_in = tmp_root / test_spec.id / "in"
            work_out = tmp_root / test_spec.id / "out"
            work_in.mkdir(parents=True, exist_ok=True)
            work_out.mkdir(parents=True, exist_ok=True)

            if test_spec.mode == "file":
                if not src_in.is_file() or not src_exp.is_file():
                    raise PluginValidationError(
                        f"File test {test_spec.id!r} missing input or expected JSON file"
                    )
                shutil.copy2(src_in, work_in / "request.json")
            else:
                if not src_in.is_dir() or not src_exp.is_dir():
                    raise PluginValidationError(
                        f"Directory test {test_spec.id!r} missing in/ or expected/ directory"
                    )
                _copy_safe_tree(src_in, work_in)

            if (work_in / "request.json").is_file():
                manifest.validate_parameters(extract_stage_parameters(work_in))

            run_plugin(
                manifest_path=root / "plugin.yaml",
                work_in=work_in,
                work_out=work_out,
            )
            validate_stage_outputs(manifest.kind, work_out, produces=manifest.produces)

            if test_spec.mode == "file":
                out_name = _resolve_single_output_filename(manifest)
                out_file = work_out / out_name
                if not out_file.is_file():
                    raise PluginValidationError(
                        f"Test {test_spec.id!r} did not produce expected output {out_name!r}"
                    )
                _compare_json_values(
                    read_json_file(out_file),
                    read_json_file(src_exp),
                    f"tests[{test_spec.id!r}].{out_name}",
                )
            else:
                expected_files = sorted(p for p in src_exp.rglob("*") if p.is_file())
                if not expected_files:
                    raise PluginValidationError(
                        f"Directory test {test_spec.id!r} expected directory has no files"
                    )
                for exp_file in expected_files:
                    rel_file = exp_file.relative_to(src_exp)
                    actual_file = work_out / rel_file
                    if not actual_file.is_file():
                        raise PluginValidationError(
                            f"Test {test_spec.id!r} missing expected output file {rel_file.as_posix()!r}"
                        )
                    if exp_file.suffix.lower() == ".json":
                        _compare_json_values(
                            read_json_file(actual_file),
                            read_json_file(exp_file),
                            f"tests[{test_spec.id!r}].{rel_file.as_posix()}",
                        )
                    elif actual_file.read_bytes() != exp_file.read_bytes():
                        raise PluginValidationError(
                            f"Plugin test byte output mismatch for "
                            f"tests[{test_spec.id!r}].{rel_file.as_posix()}"
                        )


def _check_declared_test_paths(manifest: PluginManifest, root: Path) -> None:
    root_resolved = root.resolve()
    for test_spec in manifest.tests:
        in_rel = check_relative_path(test_spec.input, f"tests[{test_spec.id!r}].input")
        exp_rel = check_relative_path(test_spec.expected, f"tests[{test_spec.id!r}].expected")
        src_in = (root / in_rel).resolve()
        src_exp = (root / exp_rel).resolve()
        if not src_in.is_relative_to(root_resolved) or not src_exp.is_relative_to(root_resolved):
            raise PluginValidationError(f"Test {test_spec.id!r} escapes plugin root")
        if test_spec.mode == "file":
            if not src_in.is_file() or not src_exp.is_file():
                raise PluginValidationError(
                    f"File test {test_spec.id!r} missing input or expected file"
                )
        else:
            if not src_in.is_dir() or not src_exp.is_dir():
                raise PluginValidationError(
                    f"Directory test {test_spec.id!r} missing in/ or expected/ directory"
                )


def _validate_extracted_root(
    root: Path,
    *,
    run_tests: bool,
    require_market_fields: bool,
) -> dict[str, Any]:
    if not (root / "plugin.yaml").is_file():
        raise PluginValidationError(
            "Plugin package must place plugin.yaml directly at root (no wrapper directory)"
        )
    manifest = load_plugin_manifest(root / "plugin.yaml", check_compat=True)
    parse_artifact_id(
        manifest.plugin_id,
        kind=manifest.kind,
        allow_user_namespace=not require_market_fields,
    )
    _check_entrypoint_source(manifest, root)
    _check_requirements_file(manifest, root)
    _check_declared_test_paths(manifest, root)
    if require_market_fields:
        _check_market_fields(manifest, root)
    if run_tests and manifest.tests:
        _run_offline_tests(manifest, root)

    return {
        "ok": True,
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
        "kind": manifest.kind,
        "market_ready": require_market_fields,
        "tests_run": len(manifest.tests) if run_tests else 0,
        "test_ids": [t.id for t in manifest.tests],
    }


def validate_plugin_package(
    path: str | Path,
    *,
    run_tests: bool = True,
    require_market_fields: bool = False,
) -> dict[str, Any]:
    """Validate a plugin directory or `*.algorithm.zip` archive and optionally run offline tests."""
    target = Path(path)
    if target.is_dir():
        _scan_directory_files(target)
        return _validate_extracted_root(
            target,
            run_tests=run_tests,
            require_market_fields=require_market_fields,
        )
    if target.is_file():
        with tempfile.TemporaryDirectory(prefix="pies-sdk-unzip-") as tmp_dir:
            extracted = Path(tmp_dir)
            _extract_and_validate_zip(target, extracted)
            return _validate_extracted_root(
                extracted,
                run_tests=run_tests,
                require_market_fields=require_market_fields,
            )
    raise PluginValidationError(f"Plugin package path does not exist: {target}")


def pack_plugin_package(
    source_dir: str | Path,
    output_path: str | Path | None = None,
    *,
    run_validate: bool = True,
    require_market_fields: bool = False,
) -> dict[str, Any]:
    """Deterministically pack a plugin directory into `*.algorithm.zip` and return receipt."""
    src = Path(source_dir)
    if not src.is_dir():
        raise PluginValidationError(f"Plugin source directory does not exist: {src}")

    ignore_paths = frozenset({Path(output_path).resolve()}) if output_path is not None else frozenset()
    files = _scan_directory_files(src, ignore_paths=ignore_paths)

    if run_validate:
        _validate_extracted_root(
            src,
            run_tests=True,
            require_market_fields=require_market_fields,
        )
    else:
        _validate_extracted_root(
            src,
            run_tests=False,
            require_market_fields=require_market_fields,
        )

    manifest = load_plugin_manifest(src / "plugin.yaml", check_compat=True)
    dest = (
        Path(output_path)
        if output_path is not None
        else src.parent / f"{manifest.plugin_id}-{manifest.version}.algorithm.zip"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)

    sorted_files = sorted(files, key=lambda p: p.relative_to(src).as_posix())
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted_files:
            rel_posix = file_path.relative_to(src).as_posix()
            info = zipfile.ZipInfo(filename=rel_posix, date_time=_DETERMINISTIC_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(info, file_path.read_bytes())

    archive_bytes = dest.read_bytes()
    return {
        "archive_path": str(dest),
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
        "size_bytes": len(archive_bytes),
        "sha256": hashlib.sha256(archive_bytes).hexdigest(),
    }


__all__ = [
    "PiesSdkError",
    "pack_plugin_package",
    "validate_plugin_package",
]
