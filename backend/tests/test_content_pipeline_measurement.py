"""Exercise the actual measurement CLI; labels must not impersonate an old implementation."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("content_pipeline_measurement.py")


@pytest.fixture
def measurement_env():
    # A script starts with tests/, not the checkout root, on sys.path. Use the
    # reviewed source rather than the wheel installed in the dependency image.
    return {**os.environ, "PYTHONPATH": str(SCRIPT.parent.parent)}


@pytest.mark.parametrize("source", [("--years", "1"), ("--snapshot-id", "1")])
def test_removed_baseline_flag_is_rejected_before_measurement(source, measurement_env):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *source, "--baseline-bytes"],
        env=measurement_env, capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode == 2
    assert "unrecognized arguments: --baseline-bytes" in completed.stderr
    assert completed.stdout == ""


def test_current_measurement_runs_all_preparation_stages_without_solver(measurement_env):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--years", "1"],
        env=measurement_env, capture_output=True, text=True, check=True, timeout=180,
    )
    result = json.loads(completed.stdout)
    assert result["implementation"] == "content_records"
    assert result["scope"] == "public campus"
    assert result["years"] == 1
    assert result["stages"] == "generate→persist→release→restore→runtime input copy; no solve"
    expected = {"generate", "persist_generated", "restore", "runtime_prepare"}
    assert set(result["timings_seconds"]) == expected
    assert set(result["stage_resources"]) == expected
    assert result["persisted_input_bytes"] > 0
    assert result["instrumentation"] == "boundary timers and GC callbacks, no tracemalloc"
