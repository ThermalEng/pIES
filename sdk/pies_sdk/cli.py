"""Command-line entrypoint for `pies-sdk validate` and `pies-sdk pack`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pies_sdk.errors import PiesSdkError
from pies_sdk.packaging import pack_plugin_package, validate_plugin_package


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pies-sdk",
        description="Validate and package pIES algorithm plugins.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    val_parser = sub.add_parser("validate", help="Validate a plugin directory or *.algorithm.zip")
    val_parser.add_argument("path", type=Path, help="Plugin directory or *.algorithm.zip file")
    val_parser.add_argument(
        "--no-tests",
        action="store_true",
        help="Skip executing declared offline tests",
    )
    val_parser.add_argument(
        "--market",
        action="store_true",
        help="Enforce public market submission requirements (README.md, LICENSE, tests, no user.*)",
    )
    val_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")

    pack_parser = sub.add_parser("pack", help="Pack a plugin directory into *.algorithm.zip")
    pack_parser.add_argument("dir", type=Path, help="Plugin source directory containing plugin.yaml")
    pack_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output archive path (default: <plugin_id>-<version>.algorithm.zip)",
    )
    pack_parser.add_argument(
        "--market",
        action="store_true",
        help="Enforce public market requirements before packing",
    )
    pack_parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip executing offline tests before packing (safety and manifest checks still run)",
    )
    pack_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")

    return parser


def _emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    use_json = bool(getattr(args, "json", False))

    try:
        if args.command == "validate":
            result = validate_plugin_package(
                args.path,
                run_tests=not args.no_tests,
                require_market_fields=args.market,
            )
            if use_json:
                _emit_json(result)
            else:
                print(
                    f"VALID {result['plugin_id']}@{result['version']} ({result['kind']}), "
                    f"tests_run={result['tests_run']}"
                )
            return 0

        if args.command == "pack":
            result = pack_plugin_package(
                args.dir,
                output_path=args.output,
                run_validate=not args.no_validate,
                require_market_fields=args.market,
            )
            if use_json:
                _emit_json({"ok": True, **result})
            else:
                print(
                    f"PACKED {result['archive_path']} "
                    f"({result['size_bytes']} bytes, sha256={result['sha256']})"
                )
            return 0
    except PiesSdkError as exc:
        if use_json:
            _emit_json({"ok": False, "error": str(exc)})
        else:
            print(f"pies-sdk error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if use_json:
            _emit_json({"ok": False, "error": str(exc)})
        else:
            print(f"pies-sdk failed: {exc}", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
