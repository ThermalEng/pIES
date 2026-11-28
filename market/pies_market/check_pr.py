"""Read-only PR validation and `--network=none` container self-test for pIES Market."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pies_sdk import (
    PLUGIN_KINDS,
    PiesSdkError,
    load_plugin_manifest,
    parse_artifact_id,
    parse_semver,
    read_json_file,
    validate_plugin_package,
    validate_publishers_registry,
    validate_stage_outputs,
)

from pies_market._common import MarketValidationError, load_strict_yaml
from pies_market.device_validate import validate_device_model_file

META_SCHEMA = "ies.market-artifact-meta"
META_VERSION = "1.0.0"

_META_COMMON_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "id",
        "kind",
        "name",
        "description",
        "publisher",
        "license",
        "compatibility",
        "delisted",
    }
)
_COMPAT_CLAUSE_RE = re.compile(r"^(>=|<=|>|<|==|!=)?\s*(\d+\.\d+\.\d+)$")
_DEFAULT_DOCKER_SELFTEST_IMAGE = "pies-plugin-base:latest"
_MAX_SELFTEST_CPU = 4
_MAX_SELFTEST_MEMORY_MB = 2048
_MAX_SELFTEST_TIMEOUT_SECONDS = 300
_MAX_SELFTEST_PIDS_LIMIT = 64


def _default_docker_image() -> str:
    return os.environ.get("PIES_PLUGIN_BASE_IMAGE") or _DEFAULT_DOCKER_SELFTEST_IMAGE


@dataclass(frozen=True, slots=True)
class MarketArtifactEntry:
    """Validated market artifact record discovered under `artifacts/`."""

    id: str
    kind: str
    publisher: str
    scope: str
    revision: int | None
    version: str | None
    name: Mapping[str, str]
    description: str
    license: str
    compatibility: Mapping[str, str]
    delisted: bool
    artifact_dir: Path
    source_path: Path
    readme_path: Path | None
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()


def _validate_compatibility(compat: object) -> dict[str, str]:
    if not isinstance(compat, Mapping) or not compat:
        raise MarketValidationError("meta.yaml compatibility must be a non-empty mapping")
    extra = sorted(set(compat) - {"pies", "sdk"})
    if extra:
        raise MarketValidationError(f"meta.yaml compatibility contains unknown keys: {extra!r}")
    out: dict[str, str] = {}
    for key, expr in compat.items():
        if not isinstance(expr, str) or not expr.strip():
            raise MarketValidationError(f"meta.yaml compatibility.{key} must be a non-empty string")
        for clause in (part.strip() for part in expr.split(",")):
            match = _COMPAT_CLAUSE_RE.fullmatch(clause)
            if not match:
                raise MarketValidationError(
                    f"Invalid compatibility.{key} constraint: {expr!r}"
                )
            parse_semver(match.group(2))
        out[str(key)] = expr
    return out


def _validate_localized_name(name: object) -> dict[str, str]:
    if not isinstance(name, Mapping) or not name:
        raise MarketValidationError("meta.yaml name must be a non-empty mapping")
    extra = sorted(set(name) - {"zh_cn", "en_us"})
    if extra:
        raise MarketValidationError(f"meta.yaml name contains unsupported locales: {extra!r}")
    out: dict[str, str] = {}
    for loc, text in name.items():
        if not isinstance(text, str) or not text.strip():
            raise MarketValidationError(f"meta.yaml name.{loc} must be a non-empty string")
        out[str(loc)] = text
    return out


def _load_and_check_publishers(market_root: Path) -> dict[str, Any]:
    pub_path = market_root / "publishers.yaml"
    if not pub_path.is_file():
        raise MarketValidationError(f"Missing required publishers.yaml at {pub_path}")
    raw_text = pub_path.read_text(encoding="utf-8")
    if "@" in raw_text:
        raise MarketValidationError("publishers.yaml must not contain plaintext '@' email addresses")
    pub_data = load_strict_yaml(pub_path, label="publishers.yaml")
    return validate_publishers_registry(pub_data)


def _check_publisher_ownership(
    *,
    scope: str,
    publisher: str,
    publishers_doc: Mapping[str, Any],
) -> None:
    if scope == "official":
        return
    if scope != "publisher":
        raise MarketValidationError(f"Forbidden artifact scope in public market: {scope!r}")
    reg = publishers_doc["publishers"].get(publisher)
    if reg is None:
        raise MarketValidationError(
            f"Unregistered publisher {publisher!r} in publishers.yaml"
        )
    status = reg.get("status")
    if status != "active":
        raise MarketValidationError(
            f"Publisher {publisher!r} must have explicit status='active' (got {status!r})"
        )


def _resolve_plugin_source(artifact_dir: Path, art_id: str, version: str) -> Path:
    plugin_dir = artifact_dir / "plugin"
    zip_path = artifact_dir / f"{art_id}-{version}.algorithm.zip"
    if plugin_dir.is_dir() and zip_path.is_file():
        raise MarketValidationError(
            f"Artifact {artifact_dir} cannot provide both plugin/ and {zip_path.name}"
        )
    if plugin_dir.is_dir():
        return plugin_dir
    if zip_path.is_file():
        return zip_path
    raise MarketValidationError(
        f"Plugin artifact {artifact_dir} missing plugin/ directory or {zip_path.name}"
    )


def _compare_json_files(expected_path: Path, actual_path: Path, label: str) -> None:
    exp = read_json_file(expected_path)
    act = read_json_file(actual_path)
    if exp != act:
        raise MarketValidationError(f"Docker self-test output mismatch for {label}")


def _compare_dir_outputs(expected_dir: Path, actual_dir: Path, test_id: str) -> None:
    expected_files = sorted(p for p in expected_dir.rglob("*") if p.is_file())
    if not expected_files:
        raise MarketValidationError(f"Docker self-test {test_id!r} expected directory has no files")
    for exp_file in expected_files:
        rel = exp_file.relative_to(expected_dir)
        act_file = actual_dir / rel
        if not act_file.is_file():
            raise MarketValidationError(
                f"Docker self-test {test_id!r} missing expected output file: {rel.as_posix()}"
            )
        if exp_file.suffix.lower() == ".json":
            _compare_json_files(exp_file, act_file, f"{test_id}:{rel.as_posix()}")
        elif exp_file.read_bytes() != act_file.read_bytes():
            raise MarketValidationError(
                f"Docker self-test {test_id!r} byte mismatch on output {rel.as_posix()}"
            )


def _make_world_readable(root: Path) -> None:
    os.chmod(root, 0o755)
    for item in root.rglob("*"):
        os.chmod(item, 0o755 if item.is_dir() else 0o644)


def _build_requirements_image(
    *,
    base_image: str,
    requirements_file: Path,
    tag: str,
) -> None:
    pip_args = ["pip", "install", "--no-cache-dir"]
    pip_index = os.environ.get("PIES_PIP_INDEX_URL", "").strip()
    if pip_index:
        pip_args.extend(["--index-url", pip_index])
    pip_args.extend(["-r", "/tmp/plugin-requirements.txt"])

    with tempfile.TemporaryDirectory(prefix="pies-mkt-buildctx-") as ctx_tmp:
        ctx_dir = Path(ctx_tmp)
        shutil.copy2(requirements_file, ctx_dir / "requirements.txt")
        dockerfile = "\n".join(
            [
                f"FROM {base_image}",
                "USER 0",
                "COPY requirements.txt /tmp/plugin-requirements.txt",
                f"RUN {json.dumps(pip_args)}",
                "USER 10001:10001",
                "",
            ]
        )
        (ctx_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        proc = subprocess.run(
            ["docker", "build", "--rm", "-t", tag, str(ctx_dir)],
            capture_output=True,
            text=True,
            timeout=_MAX_SELFTEST_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0:
            raise MarketValidationError(
                f"Failed to build ephemeral self-test image for {requirements_file.name}: "
                f"{proc.stderr or proc.stdout}"
            )


def _run_docker_plugin_selftest(entry: MarketArtifactEntry, *, docker_image: str) -> int:
    container_shared = os.environ.get("PIES_TEST_CONTAINER_ROOT")
    host_shared = os.environ.get("PIES_TEST_HOST_ROOT")
    run_id = uuid.uuid4().hex[:8]
    if container_shared:
        container_run_dir = Path(container_shared) / f"mkt-selftest-{run_id}"
        host_run_dir = Path(host_shared or container_shared) / f"mkt-selftest-{run_id}"
        container_run_dir.mkdir(parents=True, exist_ok=True)
        cleanup_dir: Path | None = container_run_dir
    else:
        container_run_dir = Path(tempfile.mkdtemp(prefix="pies-market-selftest-"))
        host_run_dir = container_run_dir
        cleanup_dir = container_run_dir

    ephemeral_image: str | None = None
    try:
        plugin_dir = container_run_dir / "plugin"
        host_plugin_dir = host_run_dir / "plugin"
        if entry.source_path.is_dir():
            shutil.copytree(entry.source_path, plugin_dir)
        else:
            plugin_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(entry.source_path, "r") as zf:
                zf.extractall(plugin_dir)
        _make_world_readable(plugin_dir)

        manifest = load_plugin_manifest(plugin_dir / "plugin.yaml", check_compat=True)
        run_image = docker_image
        if manifest.runtime.requirements:
            req_path = plugin_dir / manifest.runtime.requirements
            ephemeral_image = f"pies-market-st-{run_id}:local"
            _build_requirements_image(
                base_image=docker_image,
                requirements_file=req_path,
                tag=ephemeral_image,
            )
            run_image = ephemeral_image

        res_default = manifest.resources.default
        cpus = max(1, min(int(res_default.cpu), _MAX_SELFTEST_CPU))
        memory_mb = max(64, min(int(res_default.memory_mb), _MAX_SELFTEST_MEMORY_MB))
        pids_limit = max(4, min(int(res_default.process_limit), _MAX_SELFTEST_PIDS_LIMIT))
        timeout_seconds = max(1, min(int(res_default.timeout_seconds), _MAX_SELFTEST_TIMEOUT_SECONDS))

        for idx, test_spec in enumerate(manifest.tests):
            case_in = container_run_dir / f"case_{idx}" / "in"
            case_out = container_run_dir / f"case_{idx}" / "out"
            host_case_in = host_run_dir / f"case_{idx}" / "in"
            host_case_out = host_run_dir / f"case_{idx}" / "out"
            case_out.mkdir(parents=True, exist_ok=True)
            os.chmod(case_out, 0o777)

            src_in = plugin_dir / test_spec.input
            src_exp = plugin_dir / test_spec.expected
            if test_spec.mode == "file":
                case_in.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_in, case_in / "request.json")
            else:
                shutil.copytree(src_in, case_in)
            _make_world_readable(case_in)

            container_name = f"pies-mkt-st-{run_id}-{idx}"
            cmd = [
                "docker",
                "run",
                "--rm",
                "--name",
                container_name,
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--cpus",
                str(cpus),
                "--memory",
                f"{memory_mb}m",
                "--pids-limit",
                str(pids_limit),
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-v",
                f"{host_plugin_dir.resolve()}:/plugin:ro",
                "-v",
                f"{host_case_in.resolve()}:/work/in:ro",
                "-v",
                f"{host_case_out.resolve()}:/work/out:rw",
                run_image,
                "python",
                "-m",
                "pies_sdk.runner",
                "--manifest",
                "/plugin/plugin.yaml",
                "--work-in",
                "/work/in",
                "--work-out",
                "/work/out",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    check=False,
                )
                raise MarketValidationError(
                    f"Docker --network=none self-test timed out after {timeout_seconds}s for "
                    f"{entry.id}@{entry.version} case {test_spec.id!r}"
                ) from exc
            if proc.returncode != 0:
                raise MarketValidationError(
                    f"Docker --network=none self-test failed for {entry.id}@{entry.version} "
                    f"case {test_spec.id!r}: {proc.stderr or proc.stdout}"
                )

            validate_stage_outputs(manifest.kind, case_out, produces=manifest.produces)
            if test_spec.mode == "file":
                exp_data = read_json_file(src_exp)
                out_files = [p for p in case_out.iterdir() if p.is_file() and p.suffix == ".json"]
                if len(out_files) != 1 or read_json_file(out_files[0]) != exp_data:
                    raise MarketValidationError(
                        f"Docker self-test output mismatch for {entry.id}@{entry.version} "
                        f"case {test_spec.id!r}"
                    )
            else:
                _compare_dir_outputs(src_exp, case_out, test_spec.id)
        return len(manifest.tests)
    finally:
        if ephemeral_image is not None:
            subprocess.run(
                ["docker", "rmi", "-f", ephemeral_image],
                capture_output=True,
                check=False,
            )
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def scan_and_validate_artifacts(
    market_root: Path,
    *,
    publishers_doc: Mapping[str, Any],
) -> list[MarketArtifactEntry]:
    """Scan `artifacts/<publisher>/<id>/<ver_or_rev>/` and validate every artifact."""
    artifacts_root = market_root / "artifacts"
    if not artifacts_root.is_dir():
        raise MarketValidationError(f"Missing required artifacts directory: {artifacts_root}")

    entries: list[MarketArtifactEntry] = []
    seen_versions: set[tuple[str, str, int | str]] = set()
    seen_ids_versions: set[tuple[str, int | str]] = set()

    for pub_dir in sorted(p for p in artifacts_root.iterdir() if not p.name.startswith(".")):
        if not pub_dir.is_dir():
            raise MarketValidationError(f"Unexpected file directly under artifacts/: {pub_dir.name}")
        for id_dir in sorted(p for p in pub_dir.iterdir() if not p.name.startswith(".")):
            if not id_dir.is_dir():
                raise MarketValidationError(
                    f"Unexpected file directly under artifacts/{pub_dir.name}/: {id_dir.name}"
                )
            ver_dirs = sorted(p for p in id_dir.iterdir() if not p.name.startswith("."))
            if not ver_dirs:
                raise MarketValidationError(f"Empty artifact directory: {id_dir}")
            for ver_dir in ver_dirs:
                if not ver_dir.is_dir():
                    raise MarketValidationError(
                        f"Unexpected file directly under {id_dir}: {ver_dir.name}"
                    )
                meta_path = ver_dir / "meta.yaml"
                meta = load_strict_yaml(meta_path, label=f"{ver_dir.name}/meta.yaml")

                kind = meta.get("kind")
                if kind == "device_model":
                    expected_keys = _META_COMMON_KEYS | {"revision"}
                elif kind in PLUGIN_KINDS:
                    expected_keys = _META_COMMON_KEYS | {"version"}
                else:
                    raise MarketValidationError(f"Unsupported artifact kind in {meta_path}: {kind!r}")

                required_keys = expected_keys - {"delisted"}
                missing = sorted(required_keys - set(meta))
                extra = sorted(set(meta) - expected_keys)
                if missing or extra:
                    raise MarketValidationError(
                        f"Invalid keys in {meta_path}: missing={missing!r}, extra={extra!r}"
                    )
                if meta["schema"] != META_SCHEMA or meta["schema_version"] != META_VERSION:
                    raise MarketValidationError(f"Invalid meta.yaml schema header in {meta_path}")

                delisted = meta.get("delisted", False)
                if not isinstance(delisted, bool):
                    raise MarketValidationError(f"meta.yaml delisted must be a boolean in {meta_path}")

                art_id = meta["id"]
                publisher = meta["publisher"]
                parsed = parse_artifact_id(art_id, kind=kind, allow_user_namespace=False)
                if publisher != parsed.publisher:
                    raise MarketValidationError(
                        f"meta.yaml publisher {publisher!r} mismatches id {art_id!r}"
                    )
                if pub_dir.name != publisher or id_dir.name != art_id:
                    raise MarketValidationError(
                        f"Artifact directory {ver_dir} does not match publisher={publisher!r}, "
                        f"id={art_id!r}"
                    )

                _check_publisher_ownership(
                    scope=parsed.scope,
                    publisher=publisher,
                    publishers_doc=publishers_doc,
                )

                localized_name = _validate_localized_name(meta["name"])
                description = meta["description"]
                if not isinstance(description, str) or not description.strip():
                    raise MarketValidationError(f"meta.yaml description must be non-empty in {meta_path}")
                lic = meta["license"]
                if not isinstance(lic, str) or not lic.strip():
                    raise MarketValidationError(f"meta.yaml license must be non-empty in {meta_path}")
                compat = _validate_compatibility(meta["compatibility"])

                revision: int | None = None
                version: str | None = None
                ver_key: int | str
                if kind == "device_model":
                    rev = meta["revision"]
                    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 1:
                        raise MarketValidationError(f"device_model revision must be int >= 1: {rev!r}")
                    if ver_dir.name != f"r{rev}":
                        raise MarketValidationError(
                            f"device_model folder {ver_dir.name!r} must equal 'r{rev}'"
                        )
                    revision = rev
                    ver_key = rev
                else:
                    ver = meta["version"]
                    if not isinstance(ver, str):
                        raise MarketValidationError(f"plugin version must be string: {ver!r}")
                    parse_semver(ver)
                    if ver_dir.name != ver:
                        raise MarketValidationError(
                            f"plugin folder {ver_dir.name!r} must equal version {ver!r}"
                        )
                    version = ver
                    ver_key = ver

                if (kind, art_id, ver_key) in seen_versions or (art_id, ver_key) in seen_ids_versions:
                    raise MarketValidationError(f"Duplicate artifact entry ({art_id!r}, {ver_key!r})")
                seen_versions.add((kind, art_id, ver_key))
                seen_ids_versions.add((art_id, ver_key))

                if kind == "device_model":
                    source_path = ver_dir / f"{art_id}.device.yaml"
                    device_doc = validate_device_model_file(source_path, allow_user_namespace=False)
                    if device_doc["device"]["id"] != art_id:
                        raise MarketValidationError(
                            f"Device YAML id {device_doc['device']['id']!r} mismatches meta {art_id!r}"
                        )
                    readme_candidate = ver_dir / "README.md"
                    if (
                        not readme_candidate.is_file()
                        or not readme_candidate.read_text(encoding="utf-8").strip()
                    ):
                        raise MarketValidationError(
                            f"Device model artifact missing non-empty README.md: {ver_dir}"
                        )
                    entries.append(
                        MarketArtifactEntry(
                            id=art_id,
                            kind=kind,
                            publisher=publisher,
                            scope=parsed.scope,
                            revision=revision,
                            version=None,
                            name=localized_name,
                            description=description,
                            license=lic,
                            compatibility=compat,
                            delisted=delisted,
                            artifact_dir=ver_dir,
                            source_path=source_path,
                            readme_path=readme_candidate,
                        )
                    )
                else:
                    assert version is not None
                    source_path = _resolve_plugin_source(ver_dir, art_id, version)
                    validate_plugin_package(
                        source_path,
                        run_tests=False,
                        require_market_fields=True,
                    )
                    if source_path.is_dir():
                        manifest = load_plugin_manifest(source_path / "plugin.yaml", check_compat=True)
                        readme_candidate = (
                            ver_dir / "README.md"
                            if (ver_dir / "README.md").is_file()
                            else source_path / "README.md"
                        )
                    else:
                        with tempfile.TemporaryDirectory(prefix="pies-mkt-meta-") as tmp_dir:
                            with zipfile.ZipFile(source_path, "r") as zf:
                                zf.extract("plugin.yaml", tmp_dir)
                                manifest = load_plugin_manifest(
                                    Path(tmp_dir) / "plugin.yaml",
                                    check_compat=True,
                                )
                        readme_candidate = ver_dir / "README.md"
                        if not readme_candidate.is_file():
                            readme_candidate = None
                    if manifest.plugin_id != art_id:
                        raise MarketValidationError(
                            f"plugin.yaml plugin_id {manifest.plugin_id!r} mismatches meta {art_id!r}"
                        )
                    if manifest.version != version:
                        raise MarketValidationError(
                            f"plugin.yaml version {manifest.version!r} mismatches meta {version!r}"
                        )
                    if manifest.kind != kind:
                        raise MarketValidationError(
                            f"plugin.yaml kind {manifest.kind!r} mismatches meta {kind!r}"
                        )
                    if manifest.license != lic:
                        raise MarketValidationError(
                            f"plugin.yaml license {manifest.license!r} mismatches meta {lic!r}"
                        )
                    entries.append(
                        MarketArtifactEntry(
                            id=art_id,
                            kind=kind,
                            publisher=publisher,
                            scope=parsed.scope,
                            revision=None,
                            version=version,
                            name=localized_name,
                            description=description,
                            license=lic,
                            compatibility=compat,
                            delisted=delisted,
                            artifact_dir=ver_dir,
                            source_path=source_path,
                            readme_path=readme_candidate,
                            consumes=manifest.consumes,
                            produces=manifest.produces,
                        )
                    )
    return entries


def check_market_pr(
    market_root: str | Path,
    *,
    docker_selftest: bool = False,
    docker_image: str | None = None,
) -> dict[str, Any]:
    """Validate an entire market checkout and optionally run `--network=none` Docker self-tests."""
    root = Path(market_root)
    if not root.is_dir():
        raise MarketValidationError(f"Market root does not exist: {root}")
    publishers_doc = _load_and_check_publishers(root)
    entries = scan_and_validate_artifacts(root, publishers_doc=publishers_doc)

    resolved_image = docker_image or _default_docker_image()
    docker_tests_run = 0
    if docker_selftest:
        for entry in entries:
            if entry.kind != "device_model" and not entry.delisted:
                docker_tests_run += _run_docker_plugin_selftest(entry, docker_image=resolved_image)

    return {
        "ok": True,
        "publishers_count": len(publishers_doc["publishers"]),
        "artifacts_checked": len(entries),
        "active_artifacts": sum(1 for item in entries if not item.delisted),
        "delisted_artifacts": sum(1 for item in entries if item.delisted),
        "docker_tests_run": docker_tests_run,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pies_market.check_pr",
        description="Validate pIES Market registry and artifacts for read-only PR CI.",
    )
    parser.add_argument("--market-root", type=Path, default=Path("."))
    parser.add_argument("--docker-selftest", action="store_true")
    parser.add_argument("--docker-image", default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        summary = check_market_pr(
            args.market_root,
            docker_selftest=args.docker_selftest,
            docker_image=args.docker_image,
        )
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                f"OK artifacts={summary['artifacts_checked']} "
                f"(active={summary['active_artifacts']}, delisted={summary['delisted_artifacts']}, "
                f"docker_tests={summary['docker_tests_run']})"
            )
        return 0
    except PiesSdkError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"market check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
