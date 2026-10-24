"""Pick pIES tests from a PR's changed paths. stdlib only; run in Docker."""

from __future__ import annotations

import argparse
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT / "backend/tests"
E2E_DIR = ROOT / "frontend/tests/e2e"
SPECIAL = "device-yaml-extension.spec.ts"
SMOKE_BACKEND = "test_smoke.py"
SMOKE_BROWSER = "infra.spec.ts"

# The areas intentionally follow public subsystem boundaries, not test internals.
# Each area is a bounded group, not the entire suite.
BACKEND_GROUPS = {
    "auth": ("test_auth_api.py", "test_csrf.py", "test_limits_api.py", "test_security_regression.py"),
    "admin": ("test_auth_api.py", "test_security.py"),
    "project": ("test_project_*.py", "test_package_project_models.py"),
    "package": ("test_package_*.py", "test_wave2_c_packages.py"),
    "config": ("test_config*.py", "test_calculation_config_schema.py", "test_wave2_a_configuration.py"),
    "datasets": ("test_dataset_api.py", "test_device_data_v2.py", "test_device_data_upload.py"),
    "devices": ("test_device_model_v2*.py", "test_device_candidate_gate.py", "test_s1_template_data_binding.py"),
    "modeling": ("test_modeling_contract2.py", "test_equation_grammar.py", "test_expression.py"),
    "assembly": ("test_assembly_*.py", "test_wave1_assembly.py"),
    "finance": ("test_finance*.py", "test_financial*.py", "test_evaluation_cashflow*.py",
                "test_vat_monthly.py", "test_income_annual.py"),
    "planning": ("test_planning_*.py", "test_linear_lowering.py", "test_npv_objective.py"),
    "analysis": ("test_analysis.py", "test_wave1_analysis.py", "test_financial_display.py"),
    "computation": ("test_computation_boundary.py", "test_generator_registry.py",
                    "test_highs_milp_provider.py", "test_solver_runtime.py"),
    "results": ("test_results_api.py", "test_result_contract_registry.py", "test_highs_result_adapter.py"),
    "tasks": ("test_tasks_api.py", "test_task_inputs_api.py", "test_worker_outcome.py"),
    "worker": ("test_worker_lease.py", "test_worker_outcome.py", "test_worker_compute_chain.py"),
    "storage": ("test_storage_contracts.py", "test_objects_api.py"),
    "core": ("test_units.py", "test_stdunits.py", "test_timeaxis.py",
             "test_diagnostics.py", "test_protocol_baseline.py"),
    "api": ("test_http_contract.py", "test_api_request_deps.py", "test_protocol_baseline.py"),
    "bootstrap": ("test_bootstrap.py", "test_highs_readiness.py"),
}
BROWSER_GROUPS = {
    "auth": ("auth-navigation-model.spec.ts", "logout-failure.spec.ts"),
    "admin": ("admin-audit.spec.ts",),
    "project": ("data-project-d3.spec.ts", "project-versions.spec.ts"),
    "package": ("project-import.spec.ts", "project-restore.spec.ts"),
    "config": ("config-revision.spec.ts", "task-selection.spec.ts"),
    "datasets": ("data-preview-d5.spec.ts", "device-data-upload.spec.ts"),
    "devices": ("model-new.spec.ts", "model-load-state.spec.ts"),
    "modeling": ("model-new.spec.ts", "model-electricity-carrier.spec.ts"),
    "assembly": ("economic-bindings-issue03.spec.ts", "s1-template-data-binding.spec.ts"),
    "finance": ("economic-bindings-issue03.spec.ts", "financial-evaluation.spec.ts"),
    "planning": ("planning-issue05.spec.ts", "result-planning-solution.spec.ts"),
    "analysis": ("result-planning-solution.spec.ts",),
    "computation": ("result-planning-solution.spec.ts",),
    "results": ("result-planning-solution.spec.ts", "result-switch-failure.spec.ts"),
    "tasks": ("task-draft-load.spec.ts", "task-selection.spec.ts"),
    "worker": ("task-selection.spec.ts", "hourly-steps.spec.ts"),
    "storage": ("project-restore.spec.ts",),
    "core": ("error-envelope.spec.ts",),
    "api": ("error-envelope.spec.ts",),
    "bootstrap": (),
    "ui": ("page-loading.spec.ts",),
}

# API and application routes often have a module name rather than their owning
# domain in the path. Map these to the correct bounded test group.
ALIASES = {
    "auth": "auth", "identity": "auth", "csrf": "auth", "limits": "auth",
    "admin": "admin", "audit": "admin", "admin_audit": "admin",
    "project": "project", "projects": "project", "project_models": "project",
    "project_versions": "project", "project_import": "package",
    "package": "package", "packages": "package", "exports": "package",
    "config": "config", "configuration": "config", "config_revisions": "config",
    "datasets": "datasets", "dataset": "datasets", "objects": "storage",
    "devices": "devices", "model_templates": "devices", "customization": "devices",
    "model": "modeling", "modeling": "modeling", "assembly": "assembly",
    "assembly_bindings": "assembly",
    "finance": "finance", "evaluation": "finance", "metrics": "finance",
    "planning": "planning", "analysis": "analysis", "computation": "computation",
    "price_series": "datasets",
    "results": "results", "result": "results",
    "tasks": "tasks", "task": "tasks", "worker": "worker",
    "storage": "storage", "core": "core", "bootstrap": "bootstrap",
    "main": "bootstrap", "deployment": "bootstrap", "deps": "api",
    "health": "bootstrap", "wrapper_keys": "api", "validation": "assembly",
}


def inventory() -> tuple[set[str], set[str]]:
    return ({p.name for p in BACKEND_DIR.glob("test_*.py")},
            {p.name for p in E2E_DIR.glob("*.spec.ts")})


def expand(group: str, available: set[str]) -> set[str]:
    return {p for pattern in BACKEND_GROUPS[group] for p in available if fnmatch(p, pattern)}


def find_area(path: str, prefix: str) -> str | None:
    relative = path.removeprefix(prefix)
    parts = [p.replace("-", "_").removesuffix(".py").removesuffix(".ts").removesuffix(".tsx").lower()
             for p in relative.split("/")]
    for p in parts:
        if p in ALIASES:
            return ALIASES[p]
    # Page components often use names such as FinancePage.tsx or ResultsPage.tsx.
    for part in reversed(parts):
        for name, area in ALIASES.items():
            if len(name) >= 5 and part.startswith(name):
                return area
    return None


def pick(changed: list[str], full: bool = False) -> dict[str, str]:
    backend_available, browser_available = inventory()
    browser_available.discard(SPECIAL)
    backend: set[str] = set()
    browser: set[str] = set()
    special = False
    backend_all = full or "__FULL__" in changed
    browser_all = backend_all

    for path in changed:
        if path == "__FULL__":
            continue
        if path.startswith(".github/"):
            backend.add(SMOKE_BACKEND)
            browser.add(SMOKE_BROWSER)
        elif path in ("docker-compose.yml", "backend/Dockerfile", "backend/pyproject.toml"):
            backend.update((SMOKE_BACKEND, "test_bootstrap.py"))
            browser.add(SMOKE_BROWSER)
        elif path.startswith("backend/tests/"):
            name = path.rsplit("/", 1)[-1]
            if name == "conftest.py":
                backend_all = True
            elif name in backend_available:
                backend.add(name)
                backend.add(SMOKE_BACKEND)
            elif path.endswith(".py"):
                backend_all = True
        elif path.startswith("backend/iesplan/"):
            area = find_area(path, "backend/iesplan/")
            if area is None:
                backend_all = True  # never quietly ignore a new subsystem
                browser.add(SMOKE_BROWSER)
            else:
                backend.update(expand(area, backend_available))
                backend.add(SMOKE_BACKEND)
                browser.update(BROWSER_GROUPS.get(area, ()))
                browser.add(SMOKE_BROWSER)
        elif path in ("frontend/Dockerfile", "frontend/package.json", "frontend/package-lock.json"):
            browser.update((SMOKE_BROWSER, "page-loading.spec.ts"))
        elif path.startswith("frontend/tests/e2e/"):
            name = path.rsplit("/", 1)[-1]
            if name in (SPECIAL, "playwright.ext-device.config.ts", "docker-compose.ext-device.yml") or "device-catalog-external/" in path:
                special = True
                browser.add(SMOKE_BROWSER)
            elif "device-data-external/" in path:
                browser.update((SMOKE_BROWSER, "device-data-csv-extension.spec.ts"))
            elif name in browser_available:
                browser.update((SMOKE_BROWSER, name))
            elif path.endswith((".ts", ".json")) and not path.endswith("package-lock.json"):
                browser_all = True  # shared harness changed
        elif path.startswith("frontend/src/"):
            area = find_area(path, "frontend/src/")
            if area is None and path.startswith("frontend/src/features/"):
                browser_all = True  # new feature without test mapping: do not silently miss coverage
            else:
                browser.update((SMOKE_BROWSER, *BROWSER_GROUPS[area or "ui"]))
        elif path.startswith("frontend/"):
            # Non-app files: docs / generated metadata do not trigger a full browser suite.
            if path.endswith((".ts", ".tsx", ".js", ".mjs", ".css")):
                browser.update((SMOKE_BROWSER, "page-loading.spec.ts"))
        elif path.startswith("manual/user-guide/"):
            browser.update((SMOKE_BROWSER, "admin-help-lang.spec.ts"))
        elif path.startswith(("manual/", "docs/")) or path in ("README.md", "LICENSE", "AGENTS.md", "CLAUDE.md", ".gitignore", ".dockerignore"):
            pass  # documentation-only changes do not require an app build
        else:
            # New executable top-level area: require a full run rather than falsely green CI.
            backend_all = True
            browser_all = True

    if backend_all:
        backend = set(backend_available)
    if browser_all:
        browser = set(browser_available)
    backend &= backend_available
    browser &= browser_available
    if backend and SMOKE_BACKEND not in backend:
        backend.add(SMOKE_BACKEND)
    if browser and SMOKE_BROWSER not in browser:
        browser.add(SMOKE_BROWSER)
    return {
        "backend_run": str(bool(backend)).lower(),
        "backend_tests": " ".join("tests/" + name for name in sorted(backend)),
        "browser_run": str(bool(browser) or special).lower(),
        "browser_specs": " ".join(sorted(browser)),
        "special_device": str(special).lower(),
    }


def self_test() -> None:
    assert pick([".github/workflows/docker-ci.yml"])["backend_tests"] == "tests/test_smoke.py"
    assert pick([".github/workflows/docker-ci.yml"])["browser_specs"] == "infra.spec.ts"
    assert "test_finance.py" in pick(["backend/iesplan/finance/config.py"])["backend_tests"]
    assert "test_auth_api.py" in pick(["backend/iesplan/api/auth.py"])["backend_tests"]
    assert "test_analysis.py" in pick(["backend/iesplan/analysis/metrics.py"])["backend_tests"]
    assert "economic-bindings-issue03.spec.ts" in pick(["frontend/src/features/finance/api.ts"])["browser_specs"]
    assert "model-new.spec.ts" in pick(["frontend/src/features/modeling/form.ts"])["browser_specs"]
    assert "economic-bindings-issue03.spec.ts" in pick(["frontend/src/features/assembly-bindings/api.ts"])["browser_specs"]
    assert "s1-template-data-binding.spec.ts" in pick(["frontend/src/features/validation/api.ts"])["browser_specs"]
    assert "admin-audit.spec.ts" in pick(["frontend/src/features/admin-audit/api.ts"])["browser_specs"]
    assert "financial-evaluation.spec.ts" in pick(["frontend/src/pages/FinancePage.tsx"])["browser_specs"]
    assert "tests/test_task_draft_load.py" in pick(["backend/tests/test_task_draft_load.py"])["backend_tests"]
    assert pick(["docs/reviews/notes.md"])["backend_run"] == "false"
    assert pick(["docs/reviews/notes.md"])["browser_run"] == "false"
    assert pick(["frontend/tests/e2e/device-yaml-extension.spec.ts"])["special_device"] == "true"
    assert len(pick(["frontend/src/features/new-subsystem/Feature.tsx"])["browser_specs"].split()) > 40
    assert len(pick(["backend/iesplan/new_module/file.py"])["backend_tests"].split()) > 80
    assert len(pick([".github/ci/select_tests.py"], full=True)["browser_specs"].split()) > 40
    print("Selector self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--changed", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--full", choices=("true", "false"), default="false")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if not args.changed or not args.output:
        parser.error("--changed and --output are required")
    paths = [part.decode("utf-8") for part in args.changed.read_bytes().split(b"\0") if part]
    result = pick(paths, full=args.full == "true")
    args.output.write_text("".join(f"{key}={value}\n" for key, value in result.items()), encoding="utf-8")
    print("Changed paths:", paths)
    print("Selected:", result)


if __name__ == "__main__":
    main()
