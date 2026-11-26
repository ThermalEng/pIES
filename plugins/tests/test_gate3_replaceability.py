"""Gate 3 Replaceability test using `sdk/examples/reference_algorithm`."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pies_sdk import read_engineering_solution, validate_stage_outputs

REPO_ROOT = Path(__file__).resolve().parents[2]
REF_PLUGIN_DIR = REPO_ROOT / "sdk" / "examples" / "reference_algorithm"


def test_gate3_reference_algorithm_runs_under_identical_stage_runner(tmp_path: Path) -> None:
    """Gate 3: An independent `algorithm` plugin replaces the pipeline via `pies_sdk.runner`."""
    assert REF_PLUGIN_DIR.is_dir(), f"Missing reference_algorithm dir: {REF_PLUGIN_DIR}"

    work_in = tmp_path / "in"
    work_out = tmp_path / "out"
    work_in.mkdir()
    work_out.mkdir()
    (work_in / "request.json").write_text(
        (REF_PLUGIN_DIR / "tests" / "minimal" / "input.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pies_sdk.runner",
            "--manifest",
            str(REF_PLUGIN_DIR / "plugin.yaml"),
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
    validate_stage_outputs("algorithm", work_out)
    actual = read_engineering_solution(work_out / "solution.json").to_dict()
    expected = json.loads(
        (REF_PLUGIN_DIR / "tests" / "minimal" / "expected.json").read_text(encoding="utf-8")
    )
    assert actual == expected
