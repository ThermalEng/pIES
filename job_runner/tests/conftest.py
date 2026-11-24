"""Shared fixtures for job_runner unit and Docker integration tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_roots(tmp_path: Path) -> tuple[Path, Path]:
    stage_root = tmp_path / "stages"
    plugin_root = tmp_path / "plugin_builds"
    stage_root.mkdir(parents=True, exist_ok=True)
    plugin_root.mkdir(parents=True, exist_ok=True)
    return stage_root, plugin_root


def docker_integration_enabled() -> bool:
    return os.environ.get("PIES_RUN_DOCKER_TESTS", "0") == "1"
