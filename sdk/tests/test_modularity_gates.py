"""Runtime Modularity Gate checks (Gates 1, 2, and 4) for pies-sdk."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

SDK_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_PACKAGES = frozenset(
    {
        "iesplan",
        "sqlalchemy",
        "fastapi",
        "redis",
        "alembic",
        "psycopg",
        "pydantic",
        "uvicorn",
        "celery",
    }
)


def _iter_imported_roots(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_gate1_independent_package_metadata() -> None:
    """Gate 1: pyproject.toml declares zero backend/ORM/FastAPI/Redis dependencies."""
    pyproject = tomllib.loads((SDK_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["dependencies"]
    dep_names = {d.split(">=")[0].split("==")[0].split("<")[0].strip().lower() for d in deps}
    assert dep_names == {"jsonschema"}
    assert "iesplan" not in dep_names
    assert not (dep_names & FORBIDDEN_PACKAGES)


def test_gate4_zero_forbidden_imports_in_sdk_and_examples() -> None:
    """Gate 4: AST and runtime sys.modules verify zero backend/DB/Redis/ORM imports."""
    py_files = [
        *sorted((SDK_ROOT / "pies_sdk").rglob("*.py")),
        *sorted((SDK_ROOT / "examples").rglob("*.py")),
    ]
    assert py_files
    for py_file in py_files:
        imported = _iter_imported_roots(py_file)
        overlap = imported & FORBIDDEN_PACKAGES
        assert not overlap, f"{py_file} imports forbidden packages: {sorted(overlap)}"

    loaded_roots = {name.split(".")[0] for name in sys.modules}
    assert not (loaded_roots & FORBIDDEN_PACKAGES)


def test_gate2_standalone_dockerfile_base_exists() -> None:
    """Gate 1 & 4: Dockerfile.base exists and builds detached from backend/."""
    dockerfile = (SDK_ROOT / "Dockerfile.base").read_text(encoding="utf-8")
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    assert "FROM python:3.12-slim" in instructions
    assert "backend" not in instructions
    assert "iesplan" not in instructions
    assert "pies_sdk.runner" in instructions
