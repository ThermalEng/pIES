"""Tests for `python -m pies_sdk.runner` CLI and reference example plugins."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pies_sdk.io import read_compute_result
from pies_sdk.iteration import read_iterations
from pies_sdk.runner import main, run_plugin

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def test_reference_algorithm_via_cli_and_expected(tmp_path: Path) -> None:
    plugin_dir = EXAMPLES_DIR / "reference_algorithm"
    work_in = tmp_path / "in"
    work_out = tmp_path / "out"
    work_in.mkdir()
    work_out.mkdir()
    (work_in / "request.json").write_text(
        (plugin_dir / "tests" / "minimal" / "input.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pies_sdk.runner",
            "--manifest",
            str(plugin_dir / "plugin.yaml"),
            "--work-in",
            str(work_in),
            "--work-out",
            str(work_out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    actual = json.loads((work_out / "result.json").read_text(encoding="utf-8"))
    expected = json.loads(
        (plugin_dir / "tests" / "minimal" / "expected.json").read_text(encoding="utf-8")
    )
    assert actual == expected


def test_iteration_loop_plugin_via_runner(tmp_path: Path) -> None:
    plugin_dir = EXAMPLES_DIR / "iteration_loop"
    work_in = tmp_path / "in"
    work_out = tmp_path / "out"
    work_in.mkdir()
    work_out.mkdir()
    (work_in / "request.json").write_text(
        (plugin_dir / "tests" / "minimal" / "input.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    run_plugin(
        manifest_path=plugin_dir / "plugin.yaml",
        work_in=work_in,
        work_out=work_out,
    )

    actual = read_compute_result(work_out).to_dict()
    expected = json.loads(
        (plugin_dir / "tests" / "minimal" / "expected.json").read_text(encoding="utf-8")
    )
    assert actual == expected

    iterations = read_iterations(work_out / "iterations.jsonl")
    assert len(iterations) == 7
    assert iterations[0].status == "running"
    assert iterations[-1].status == "converged"
    assert iterations[-1].metric == "max_relative_delta"


def test_runner_returns_exit_code_2_on_invalid_parameters(tmp_path: Path) -> None:
    plugin_dir = EXAMPLES_DIR / "reference_algorithm"
    work_in = tmp_path / "in"
    work_out = tmp_path / "out"
    work_in.mkdir()
    work_out.mkdir()
    (work_in / "request.json").write_text(
        json.dumps(
            {
                "bundle_id": "b1",
                "receipt_id": "r1",
                "demand_kw": [10.0],
                "price_per_kwh": [0.5],
                "parameters": {"unknown_param": 123},
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "--manifest",
            str(plugin_dir / "plugin.yaml"),
            "--work-in",
            str(work_in),
            "--work-out",
            str(work_out),
        ]
    )
    assert rc == 2
