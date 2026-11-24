"""Manifest validation, catalog contract, and isolation gate tests for `plugins/`."""

from __future__ import annotations

import ast
import importlib.metadata
import sys
from pathlib import Path

from pies_sdk import check_compatibility, load_plugin_manifest

from pies_builtin_plugins import (
    BUILTIN_STAGE_BY_REF,
    BUILTIN_STAGE_DESCRIPTORS,
    BUILTIN_STAGES_IMAGE,
    get_builtin_stage,
    list_builtin_stages,
)

PLUGINS_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_PACKAGES = frozenset(
    {
        "sqlalchemy",
        "fastapi",
        "redis",
        "psycopg",
        "alembic",
        "uvicorn",
        "pydantic",
        "docker",
        "passlib",
        "httpx",
        "celery",
    }
)

FORBIDDEN_IESPLAN_SUBMODULES = frozenset(
    {
        "api",
        "application",
        "audit",
        "bootstrap",
        "config",
        "configuration",
        "dataset",
        "db",
        "identity",
        "main",
        "model",
        "package",
        "persistence",
        "project",
        "results",
        "storage",
        "tasks",
        "worker",
    }
)


def _iter_imported_modules(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_all_four_builtin_manifests_validate_with_pies_sdk() -> None:
    assert BUILTIN_STAGES_IMAGE == "pies-builtin-stages:1.1.5"
    assert len(BUILTIN_STAGE_DESCRIPTORS) == 4
    expected_refs = {
        "ies.predictor.naive@1.0.0": "predictor",
        "ies.generator.highs_milp@1.0.0": "generator",
        "ies.solver.highs@1.7.2": "solver",
        "ies.result-adapter.highs@1.0.0": "evaluator",
    }
    assert set(BUILTIN_STAGE_BY_REF) == set(expected_refs)

    for desc in BUILTIN_STAGE_DESCRIPTORS:
        assert desc.image == BUILTIN_STAGES_IMAGE
        assert desc.kind == expected_refs[desc.ref]
        assert get_builtin_stage(desc.ref) == desc
        assert list_builtin_stages(desc.kind) == (desc,)

        manifest_path = PLUGINS_ROOT / desc.manifest_rel
        assert manifest_path.is_file(), f"Missing manifest: {manifest_path}"
        manifest = load_plugin_manifest(manifest_path, check_compat=True)
        check_compatibility(manifest)
        assert manifest.plugin_id == desc.plugin_id
        assert manifest.version == desc.version
        assert manifest.kind == desc.kind
        assert manifest.entrypoint == desc.entrypoint
        assert manifest.parameters_schema["additionalProperties"] is False
        assert desc.capabilities

        as_dict = desc.to_dict()
        assert as_dict["container_manifest_path"].startswith("/app/plugins/")
        assert as_dict["command"][:3] == ["python", "-m", "pies_sdk.runner"]


def test_gate4_zero_web_db_orm_dependencies_and_zero_sha256() -> None:
    py_files = [
        *sorted((PLUGINS_ROOT / "pies_builtin_plugins").rglob("*.py")),
        *sorted((PLUGINS_ROOT / "predictor_naive").rglob("*.py")),
        *sorted((PLUGINS_ROOT / "generator_highs_milp").rglob("*.py")),
        *sorted((PLUGINS_ROOT / "solver_highs").rglob("*.py")),
        *sorted((PLUGINS_ROOT / "evaluator_highs").rglob("*.py")),
    ]
    assert py_files

    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        assert "sha256" not in source.lower(), f"Forbidden SHA-256 reference in {py_file}"
        assert "hashlib" not in source, f"Forbidden hashlib usage in {py_file}"

        for module_name in _iter_imported_modules(py_file):
            root = module_name.split(".")[0]
            assert root not in FORBIDDEN_PACKAGES, f"{py_file} imports forbidden package {root!r}"
            if root == "iesplan":
                parts = module_name.split(".")
                if len(parts) > 1:
                    assert parts[1] not in FORBIDDEN_IESPLAN_SUBMODULES, (
                        f"{py_file} imports non-domain iesplan module {module_name!r}"
                    )

    loaded_roots = {name.split(".")[0] for name in sys.modules}
    assert not (loaded_roots & FORBIDDEN_PACKAGES)

    # Verify installed runtime packages inside stage container exclude DB/web/FastAPI stacks.
    installed = {dist.metadata["Name"].lower() for dist in importlib.metadata.distributions()}
    bad_installed = installed & FORBIDDEN_PACKAGES
    assert not bad_installed, f"Forbidden installed packages: {bad_installed}"
