"""Deterministic static market index and artifact distribution builder."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pies_sdk import (
    MARKET_INDEX_SCHEMA,
    MARKET_INDEX_VERSION,
    PiesSdkError,
    pack_plugin_package,
    validate_market_index,
    verify_external_sha256,
    write_json_file,
)

from pies_market._common import MarketValidationError
from pies_market.check_pr import _load_and_check_publishers, scan_and_validate_artifacts


def _extract_readme_from_zip(archive: Path, dest: Path) -> bool:
    with zipfile.ZipFile(archive, "r") as zf:
        for candidate in ("README.md", "readme.md"):
            if candidate in zf.namelist():
                dest.write_bytes(zf.read(candidate))
                return True
    return False


def build_market_index(
    market_root: str | Path,
    out_dir: str | Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Validate market artifacts, emit deterministic `dist/` packages, `index.json`, and `site/`."""
    root = Path(market_root)
    if not root.is_dir():
        raise MarketValidationError(f"Market root does not exist: {root}")
    publishers_doc = _load_and_check_publishers(root)
    entries = scan_and_validate_artifacts(root, publishers_doc=publishers_doc)

    output_root = Path(out_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    for child in output_root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    site_src = root / "site"
    if site_src.is_dir():
        shutil.copytree(site_src, output_root, dirs_exist_ok=True)

    artifacts_out = output_root / "artifacts"
    artifacts_out.mkdir(parents=True, exist_ok=True)

    active_entries = sorted(
        (entry for entry in entries if not entry.delisted),
        key=lambda e: (e.id, e.kind, e.revision or 0, e.version or ""),
    )

    index_artifacts: list[dict[str, Any]] = []
    for entry in active_entries:
        if entry.kind == "device_model":
            assert entry.revision is not None
            rel_pkg = f"artifacts/{entry.id}-r{entry.revision}.device.yaml"
            dest_pkg = output_root / rel_pkg
            raw_bytes = entry.source_path.read_bytes()
            dest_pkg.write_bytes(raw_bytes)
            digest = hashlib.sha256(raw_bytes).hexdigest()
            verify_external_sha256(dest_pkg, digest, required=True)

            item: dict[str, Any] = {
                "id": entry.id,
                "kind": entry.kind,
                "revision": entry.revision,
                "name": dict(entry.name),
                "description": entry.description,
                "publisher": entry.publisher,
                "license": entry.license,
                "compatibility": dict(entry.compatibility),
                "url": rel_pkg,
                "sha256": digest,
                "size_bytes": len(raw_bytes),
            }
            if entry.readme_path is not None and entry.readme_path.is_file():
                rel_readme = f"artifacts/{entry.id}-r{entry.revision}.README.md"
                shutil.copy2(entry.readme_path, output_root / rel_readme)
                item["readme_url"] = rel_readme
            index_artifacts.append(item)
        else:
            assert entry.version is not None
            rel_pkg = f"artifacts/{entry.id}-{entry.version}.algorithm.zip"
            dest_pkg = output_root / rel_pkg
            if entry.source_path.is_dir():
                packed = pack_plugin_package(
                    entry.source_path,
                    output_path=dest_pkg,
                    run_validate=False,
                    require_market_fields=True,
                )
                digest = packed["sha256"]
                size_bytes = packed["size_bytes"]
            else:
                raw_bytes = entry.source_path.read_bytes()
                dest_pkg.write_bytes(raw_bytes)
                digest = hashlib.sha256(raw_bytes).hexdigest()
                size_bytes = len(raw_bytes)
            verify_external_sha256(dest_pkg, digest, required=True)

            item = {
                "id": entry.id,
                "kind": entry.kind,
                "version": entry.version,
                "name": dict(entry.name),
                "description": entry.description,
                "publisher": entry.publisher,
                "license": entry.license,
                "compatibility": dict(entry.compatibility),
                "consumes": list(entry.consumes),
                "produces": list(entry.produces),
                "url": rel_pkg,
                "sha256": digest,
                "size_bytes": size_bytes,
            }
            rel_readme = f"artifacts/{entry.id}-{entry.version}.README.md"
            if entry.readme_path is not None and entry.readme_path.is_file():
                shutil.copy2(entry.readme_path, output_root / rel_readme)
                item["readme_url"] = rel_readme
            elif entry.source_path.is_file() and _extract_readme_from_zip(
                entry.source_path, output_root / rel_readme
            ):
                item["readme_url"] = rel_readme
            index_artifacts.append(item)

    timestamp = (
        generated_at
        if generated_at is not None
        else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    index_payload = {
        "schema": MARKET_INDEX_SCHEMA,
        "schema_version": MARKET_INDEX_VERSION,
        "generated_at": timestamp,
        "artifacts": index_artifacts,
    }
    validated_index = validate_market_index(index_payload)
    write_json_file(output_root / "index.json", validated_index)
    return validated_index


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pies_market.build_index",
        description="Build deterministic static index.json and dist/ artifacts for pIES Market.",
    )
    parser.add_argument("--market-root", type=Path, default=Path("."))
    parser.add_argument("--out-dir", type=Path, default=Path("dist"))
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        index_doc = build_market_index(
            args.market_root,
            args.out_dir,
            generated_at=args.generated_at,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "out_dir": str(args.out_dir),
                        "generated_at": index_doc["generated_at"],
                        "artifact_count": len(index_doc["artifacts"]),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(
                f"BUILT {args.out_dir}/index.json "
                f"(artifacts={len(index_doc['artifacts'])}, generated_at={index_doc['generated_at']})"
            )
        return 0
    except PiesSdkError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"market build failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
