"""Pick pIES tests from a PR's changed paths. stdlib only; run in Docker."""

from __future__ import annotations

import argparse
import json
import posixpath
import re
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


# Explicit page-to-E2E contract. Pages have CamelCase names that ALIASES cannot
# classify reliably (EconomicBindingsPage, DataPage, etc.).
PAGE_SPECS = {
    "AdminAuditPage.tsx": ("admin-audit.spec.ts",),
    "AdminUsersPage.tsx": ("admin-help-lang.spec.ts",),
    "ConfigPage.tsx": ("config-revision.spec.ts",),
    "CustomizationPage.tsx": ("model-new.spec.ts",),
    "DataPage.tsx": ("data-config-tasks.spec.ts", "data-preview-d5.spec.ts", "device-data-upload.spec.ts"),
    "EconomicBindingsPage.tsx": ("economic-bindings-issue03.spec.ts",),
    "ExportPage.tsx": ("excel-export-lang.spec.ts", "export-assessment-r7.spec.ts"),
    "FinancePage.tsx": ("financial-evaluation.spec.ts",),
    "HelpPage.tsx": ("admin-help-lang.spec.ts",),
    "LoginPage.tsx": ("auth-navigation-model.spec.ts",),
    "ModelPage.tsx": ("auth-navigation-model.spec.ts", "model-load-state.spec.ts"),
    "PlanningPage.tsx": ("planning-issue05.spec.ts",),
    "ProjectListPage.tsx": ("data-project-d3.spec.ts",),
    "ProjectPage.tsx": ("data-project-d3.spec.ts",),
    "ProjectsPage.tsx": ("data-project-d3.spec.ts",),
    "ResultsPage.tsx": ("result-planning-solution.spec.ts",),
    "SettingsPage.tsx": ("admin-help-lang.spec.ts",),
    "TasksPage.tsx": ("task-selection.spec.ts", "task-draft-load.spec.ts"),
    "ValidationPage.tsx": ("evaluation-empty-gate.spec.ts",),
    "VersionsPage.tsx": ("project-versions.spec.ts",),
    "WorkbenchPage.tsx": ("auth-navigation-model.spec.ts", "page-loading.spec.ts"),
    "model/NewModelPage.tsx": ("model-new.spec.ts",),
    "model/DeviceNode.tsx": ("model-new.spec.ts",),
    "model/canvasModel.ts": ("auth-navigation-model.spec.ts", "model-new.spec.ts"),
    "model/model.css": ("model-new.spec.ts",),
    "model/text.ts": ("model-new.spec.ts",),
    "uploadHeadScan.ts": ("device-data-upload.spec.ts",),
    "workbench.css": ("page-loading.spec.ts",),
    "workbench.tsx": ("auth-navigation-model.spec.ts", "page-loading.spec.ts"),
}

# A single source file can feed multiple subsystems. Keep these real consumers
# targeted instead of widening every computation change to the full suite.
GENERATOR_PROJECT_CONSUMERS = (
    "test_vat_monthly.py",
    "test_income_annual.py",
    "test_npv_objective.py",
    "test_planning_build.py",
    "test_full_horizon_solve.py",
)


# Tests that exercise frontend logic without opening Chromium. They run with
# one Docker entrypoint and never duplicate the business assertions in E2E.
FRONTEND_GROUPS = {
    "auth": ("auth_admin_contracts.test.mjs", "error-envelope.test.mjs"),
    "admin": ("auth_admin_contracts.test.mjs",),
    "project": ("project_draft_commands.test.mjs", "project_versions_mapper.test.mjs"),
    "package": ("export_no_implicit_assessment.test.mjs",),
    "config": ("config_validation_mappers.test.mjs",),
    "datasets": ("dataset-version-mapping.test.mjs", "upload_head_scan.test.mjs"),
    "devices": ("modeling_mappers.test.mjs",),
    "modeling": ("modeling_mappers.test.mjs", "modeling_draft_mapper.test.mjs"),
    "assembly": ("project_draft_commands.test.mjs",),
    "finance": ("export_no_implicit_assessment.test.mjs",),
    "planning": ("config_validation_mappers.test.mjs",),
    "analysis": ("result_view_no_synthetic_diagnostics.test.mjs",),
    "computation": ("config_validation_mappers.test.mjs",),
    "results": ("result_view_no_synthetic_diagnostics.test.mjs",
                "hourly_absence.test.mjs", "hourly_consume.test.mjs",
                "hourly_pages.test.mjs", "task_selection.test.mjs"),
    "tasks": ("tasks_mappers.test.mjs", "task_selection.test.mjs",
              "evidence_collect.test.mjs"),
    "worker": ("tasks_mappers.test.mjs",),
    "storage": ("project_draft_commands.test.mjs",),
    "core": ("error-envelope.test.mjs",),
    "api": ("error-envelope.test.mjs", "auth_admin_contracts.test.mjs"),
    "bootstrap": (),
    "ui": (),
}
PAGE_LOGIC = {
    "ConfigPage.tsx": ("config_validation_mappers.test.mjs",),
    "DataPage.tsx": ("dataset-version-mapping.test.mjs", "upload_head_scan.test.mjs"),
    "HelpPage.tsx": ("help_markdown.test.mjs",),
    "ModelPage.tsx": ("modeling_mappers.test.mjs",),
    "EconomicBindingsPage.tsx": ("project_draft_commands.test.mjs",),
    "ResultsPage.tsx": ("result_view_no_synthetic_diagnostics.test.mjs",),
    "TasksPage.tsx": ("tasks_mappers.test.mjs", "task_selection.test.mjs"),
    "VersionsPage.tsx": ("project_versions_mapper.test.mjs",),
    "model/canvasModel.ts": ("modeling_draft_mapper.test.mjs",),
    "uploadHeadScan.ts": ("upload_head_scan.test.mjs",),
}
# All six established @basic cases currently live in these three spec files.
# Execute these alongside (never instead of) any selected domain-specific specs.
BASIC_SPECS = (
    "basic-operations.spec.ts",
    "auth-navigation-model.spec.ts",
    "device-data-upload.spec.ts",
)
HELP_BROWSER = "admin-help-lang.spec.ts"
HELP_LOGIC = "help_markdown.test.mjs"

def inventory() -> tuple[set[str], set[str], set[str]]:
    return ({p.name for p in BACKEND_DIR.glob("test_*.py")},
            {p.name for p in E2E_DIR.glob("*.spec.ts")},
            {p.name for p in (ROOT / "frontend/tests").glob("*.test.mjs")})


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


# Historical CI timings (October 2026), rounded per file; only schedule work.
# Never remove tests based on weight. Unmeasured files get a nonzero fallback.
BACKEND_SECONDS = {
    "test_full_horizon_solve.py": 540,
    "test_evaluation_cashflow_chain.py": 320,
    "test_issue04_d2.py": 230,
    "test_campus_project_chain.py": 140,
    "test_highs_milp_provider.py": 100,
    "test_generator_registry.py": 100,
    "test_worker_compute_chain.py": 110,
}
BROWSER_SECONDS = {
    "hourly-steps.spec.ts": 345, "task-draft-load.spec.ts": 220,
    "task-selection.spec.ts": 220, "tax-chain.spec.ts": 145,
    "financial-evaluation.spec.ts": 130, "export-assessment-r7.spec.ts": 110,
    "hourly-r3.spec.ts": 110, "result-switch-failure.spec.ts": 80,
    "evidence-r4.spec.ts": 90,
}


def balanced_shards(items: set[str], lanes: int, weights: dict[str, int],
                    prefix: str = "") -> list[dict[str, str | int]]:
    """Stable, lossless longest-processing-time-first file partition."""
    if not items:
        return [{"lane": 1, "tests": ""}]
    groups: list[list[str]] = [[] for _ in range(min(lanes, len(items)))]
    loads = [0] * len(groups)
    for name in sorted(items, key=lambda n: (-weights.get(n, 25), n)):
        at = min(range(len(groups)), key=lambda idx: (loads[idx], idx))
        groups[at].append(name)
        loads[at] += weights.get(name, 25)
    result = [{"lane": i + 1, "tests": " ".join(prefix + name for name in sorted(group))}
              for i, group in enumerate(groups)]
    assert sorted(file for group in groups for file in group) == sorted(items)
    return result


def e2e_consumers(helper: str, available: set[str]) -> set[str]:
    """Read real relative TS imports, including transitive helpers; fail closed.

    Unknown shared test harness files still select all browser specs. This
    dependency graph is rebuilt from the PR's checked-out files on each run.
    """
    root = "frontend/tests/e2e/"
    target = helper.removeprefix(root)
    reverse: dict[str, set[str]] = {}
    for file in E2E_DIR.rglob("*.ts"):
        rel = file.relative_to(E2E_DIR).as_posix()
        contents = file.read_text(encoding="utf-8")
        modules = re.findall(r"""(?:\bfrom\s+|\bimport\s*)['"]([.][^'"]+)['"]""",
                             contents)
        for module in modules:
            base = posixpath.normpath(posixpath.join(posixpath.dirname(rel), module))
            for resolved in (base + ".ts", base + ".tsx", base + ".js",
                             base + "/index.ts", base + "/index.tsx", base):
                if (E2E_DIR / resolved).is_file():
                    reverse.setdefault(resolved, set()).add(rel)
                    break
    queue = [target]
    reached = {target}
    found: set[str] = set()
    while queue:
        for parent in reverse.get(queue.pop(), ()):
            if parent in reached:
                continue
            reached.add(parent)
            if parent in available:
                found.add(parent)
            queue.append(parent)
    return found


def pick(changed: list[str], full: bool = False) -> dict[str, str]:
    backend_available, browser_available, frontend_available = inventory()
    browser_available.discard(SPECIAL)
    backend: set[str] = set()
    browser: set[str] = set()
    frontend: set[str] = set()
    special = False
    backend_all = full or "__FULL__" in changed
    browser_all = backend_all
    frontend_all = backend_all
    startup = backend_all
    pg_concurrency = backend_all

    for path in changed:
        if path == "__FULL__":
            continue
        if path.startswith(".github/"):
            backend.add(SMOKE_BACKEND)
            browser.add(SMOKE_BROWSER)
            frontend.add("task_selection.test.mjs")
            if path == ".github/ci/select_tests.py":
                browser.update(BASIC_SPECS)  # exercise all six real @basic cases after selector edits
            # Infrastructure validations are expensive and independent of
            # ordinary selector/frontend-logic changes. Restrict by consumers.
            if path in (".github/workflows/docker-ci.yml",
                        ".github/ci/docker-compose.yml"):
                startup = True
                pg_concurrency = True
            elif path in (".github/ci/start-stack.sh",
                          ".github/ci/verify-cold-start.sh"):
                startup = True
            elif path == ".github/ci/verify-pg-init-concurrency.sh":
                pg_concurrency = True
        elif path in ("docker-compose.yml", "backend/Dockerfile", "backend/pyproject.toml"):
            backend.update((SMOKE_BACKEND, "test_bootstrap.py"))
            browser.add(SMOKE_BROWSER)
            startup = True
            pg_concurrency = True
        elif path.startswith("backend/tests/"):
            name = path.rsplit("/", 1)[-1]
            if name == "conftest.py":
                backend_all = True
            elif name in backend_available:
                backend.add(name)
                backend.add(SMOKE_BACKEND)
            elif path == "backend/tests/pg_init_concurrency_probe.py":
                # Standalone two-process PostgreSQL probe, executed by cold_start.
                pg_concurrency = True
                backend.update((SMOKE_BACKEND, "test_db_immutable_triggers.py"))
            elif path.endswith(".py"):
                backend_all = True
        elif path.startswith("backend/iesplan/"):
            if path == "backend/iesplan/db.py":
                # PostgreSQL initialization belongs to the cold-start x3 gate.
                # Keep fast backend contract tests scoped; no 125-file sweep.
                startup = True
                pg_concurrency = True
                backend.update((SMOKE_BACKEND, "test_bootstrap.py",
                                "test_db_immutable_triggers.py"))
                browser.add(SMOKE_BROWSER)
                continue
            if path.startswith("backend/iesplan/bootstrap/"):
                startup = True
                pg_concurrency = True
            # API/worker boot and configuration can regress cold-start while
            # leaving the schema initializer itself unchanged.
            if (path.startswith("backend/iesplan/worker/")
                    or path in ("backend/iesplan/main.py", "backend/iesplan/config.py")):
                startup = True
            area = find_area(path, "backend/iesplan/")
            if area is None:
                backend_all = True  # never quietly ignore a new subsystem
                browser.add(SMOKE_BROWSER)
            else:
                backend.update(expand(area, backend_available))
                backend.add(SMOKE_BACKEND)
                browser.update(BROWSER_GROUPS.get(area, ()))
                browser.add(SMOKE_BROWSER)
                frontend.update(FRONTEND_GROUPS.get(area, ()))
            if path == "backend/iesplan/computation/generators/project.py":
                backend.update(GENERATOR_PROJECT_CONSUMERS)
                browser.update(("tax-chain.spec.ts", "result-planning-solution.spec.ts"))
        elif path in ("frontend/Dockerfile", "frontend/package.json", "frontend/package-lock.json"):
            browser.update((SMOKE_BROWSER, "page-loading.spec.ts", *BASIC_SPECS))
            frontend_all = True
        elif path == "frontend/scripts/run-logic-tests.mjs":
            frontend_all = True
        elif path.startswith("frontend/tests/") and not path.startswith("frontend/tests/e2e/"):
            name = path.rsplit("/", 1)[-1]
            if name in frontend_available:
                frontend.add(name)
            elif path.endswith(".mjs"):
                frontend_all = True
        elif path.startswith("frontend/tests/e2e/"):
            name = path.rsplit("/", 1)[-1]
            if path == "frontend/tests/e2e/fixtures/session.ts":
                browser.update((SMOKE_BROWSER, *BASIC_SPECS, "error-envelope.spec.ts"))
                continue
            if name in (SPECIAL, "playwright.ext-device.config.ts", "docker-compose.ext-device.yml") or "device-catalog-external/" in path:
                special = True
                browser.add(SMOKE_BROWSER)
            elif "device-data-external/" in path:
                browser.update((SMOKE_BROWSER, "device-data-csv-extension.spec.ts"))
            elif name in browser_available:
                browser.update((SMOKE_BROWSER, name))
            elif path in ("frontend/tests/e2e/financial-scaffold.ts",
                          "frontend/tests/e2e/setup/api.ts"):
                consumers = e2e_consumers(path, browser_available)
                if consumers:
                    browser.update((SMOKE_BROWSER, *consumers))
                else:
                    browser_all = True  # no resolvable imports: never silently skip
            elif path.endswith((".ts", ".tsx", ".js", ".json")) and not path.endswith("package-lock.json"):
                browser_all = True  # other/unmapped shared harness stays fail-closed
        elif path.startswith("frontend/src/pages/"):
            page = path.removeprefix("frontend/src/pages/")
            selected = PAGE_SPECS.get(page)
            if selected is None:
                browser_all = True  # unmapped page: avoid false green
            else:
                browser.update((SMOKE_BROWSER, *selected))
                frontend.update(PAGE_LOGIC.get(page, ()))
                if page.endswith(".css") or page in ("workbench.tsx", "WorkbenchPage.tsx"):
                    browser.update(BASIC_SPECS)
        elif path.startswith("frontend/src/"):
            area = find_area(path, "frontend/src/")
            if path.startswith(("frontend/src/shared/http", "frontend/src/api/client", "frontend/src/features/auth/")):
                browser.update((SMOKE_BROWSER, *BASIC_SPECS, "auth-navigation-model.spec.ts",
                                "config-revision.spec.ts", "error-envelope.spec.ts"))
                frontend.update(("auth_admin_contracts.test.mjs", "error-envelope.test.mjs",
                                 "project_draft_commands.test.mjs"))
            elif path.startswith(("frontend/src/lib/helpMarkdown", "frontend/src/features/admin-help/")):
                browser.update((SMOKE_BROWSER, HELP_BROWSER, *BASIC_SPECS))
                frontend.add(HELP_LOGIC)
            elif path == "frontend/src/api/hourlyPages.ts":
                browser.update((SMOKE_BROWSER, "hourly-r3.spec.ts", "hourly-steps.spec.ts"))
                frontend.update(("hourly_pages.test.mjs", "hourly_absence.test.mjs",
                                 "hourly_consume.test.mjs"))
            elif path == "frontend/src/application/exportTaskReport.ts":
                browser.update((SMOKE_BROWSER, "export-assessment-r7.spec.ts",
                                "excel-export-lang.spec.ts"))
                frontend.add("export_no_implicit_assessment.test.mjs")
            elif path.startswith(("frontend/src/components/", "frontend/src/shared/", "frontend/src/application/",
                                  "frontend/src/i18n/", "frontend/src/App.tsx", "frontend/src/styles.css")):
                browser.update((SMOKE_BROWSER, *BASIC_SPECS))
                if path.startswith("frontend/src/i18n/"):
                    browser.add(HELP_BROWSER)
                    frontend.add(HELP_LOGIC)
            elif area is None and path.startswith("frontend/src/features/"):
                browser_all = True
                frontend_all = True
            else:
                browser.update((SMOKE_BROWSER, *BROWSER_GROUPS[area or "ui"]))
                frontend.update(FRONTEND_GROUPS.get(area or "ui", ()))
        elif path == "frontend/scripts/generate-help.mjs":
            browser.update((SMOKE_BROWSER, HELP_BROWSER))
            frontend.add(HELP_LOGIC)
        elif path.startswith("frontend/"):
            if path.endswith((".ts", ".tsx", ".js", ".mjs", ".css")):
                browser.update((SMOKE_BROWSER, *BASIC_SPECS))
        elif path.startswith("manual/user-guide/"):
            browser.update((SMOKE_BROWSER, HELP_BROWSER))
            frontend.add(HELP_LOGIC)
        elif path.endswith("/formats/finance-yaml.md") and path.startswith("manual/"):
            backend.update((SMOKE_BACKEND, "test_finance_taxrules.py"))
        elif path.startswith(("manual/", "docs/")) or path in ("README.md", "LICENSE", "AGENTS.md", "CLAUDE.md", ".gitignore", ".dockerignore"):
            pass  # documentation-only changes do not require an app build
        else:
            # New executable top-level area: require a full run rather than falsely green CI.
            backend_all = True
            browser_all = True
            frontend_all = True

    if backend_all:
        backend = set(backend_available)
    if browser_all:
        browser = set(browser_available)
    if frontend_all:
        frontend = set(frontend_available)
    backend &= backend_available
    browser &= browser_available
    frontend &= frontend_available
    if backend and SMOKE_BACKEND not in backend:
        backend.add(SMOKE_BACKEND)
    if browser and SMOKE_BROWSER not in browser:
        browser.add(SMOKE_BROWSER)
    backend_count = 4 if len(backend) >= 16 else 2 if len(backend) >= 7 else 1
    backend_groups = balanced_shards(backend, backend_count, BACKEND_SECONDS, "tests/")
    browser_count = 4 if len(browser) >= 20 else 2 if len(browser) >= 2 else 1
    browser_groups = balanced_shards(browser, browser_count, BROWSER_SECONDS)
    return {
        "backend_run": str(bool(backend)).lower(),
        "backend_tests": " ".join("tests/" + name for name in sorted(backend)),
        "backend_shards": json.dumps(backend_groups, separators=(",", ":")),
        "browser_run": str(bool(browser) or special).lower(),
        "browser_specs": " ".join(sorted(browser)),
        "browser_lanes": json.dumps([part["lane"] for part in browser_groups], separators=(",", ":")),
        "browser_shards": json.dumps(browser_groups, separators=(",", ":")),
        "browser_total": str(len(browser_groups)),
        "frontend_run": str(bool(frontend)).lower(),
        "frontend_tests": " ".join(sorted(frontend)),
        "startup_run": str(startup).lower(),
        "pg_run": str(pg_concurrency).lower(),
        "special_device": str(special).lower(),
    }


def self_test() -> None:
    # Selection is lossless, disjoint and deterministic for both matrices.
    one = pick(["frontend/tests/e2e/infra.spec.ts"])
    assert one["browser_specs"] == "infra.spec.ts"
    assert one["browser_total"] == "1"
    assert [x["tests"] for x in json.loads(one["browser_shards"])] == ["infra.spec.ts"]
    multi = pick(["frontend/src/components/ui/Button.tsx"])
    assert len(multi["browser_specs"].split()) >= 2
    assert multi["browser_total"] == "2"
    all_specs = pick([".github/ci/select_tests.py"], full=True)
    assert len(all_specs["browser_specs"].split()) > 40
    assert all_specs["browser_total"] == "4"
    assert len(json.loads(all_specs["backend_shards"])) == 4
    for plan in (one, multi, all_specs):
        for field, shards in (("browser_specs", "browser_shards"),
                              ("backend_tests", "backend_shards")):
            picked = plan[field].split()
            partitioned = [n for part in json.loads(plan[shards]) for n in part["tests"].split()]
            assert sorted(picked) == sorted(partitioned) and len(partitioned) == len(set(partitioned))
    docs = pick(["docs/reviews/style.md"])
    assert docs["browser_run"] == "false" and docs["browser_total"] == "1"
    ext = pick(["frontend/tests/e2e/device-yaml-extension.spec.ts"])
    assert ext["special_device"] == "true" and ext["browser_total"] == "1"
    # Real test-import graph, including transitive helper consumers.
    scaffold = pick(["frontend/tests/e2e/financial-scaffold.ts"])
    assert {"financial-evaluation.spec.ts", "hourly-steps.spec.ts",
            "task-selection.spec.ts", "tax-chain.spec.ts",
            "economic-bindings-issue03.spec.ts", SMOKE_BROWSER} <= set(
                scaffold["browser_specs"].split())
    assert len(scaffold["browser_specs"].split()) < len(inventory()[1]) - 1
    common_api = pick(["frontend/tests/e2e/setup/api.ts"])
    assert {"financial-evaluation.spec.ts", "task-selection.spec.ts",
            SMOKE_BROWSER} <= set(common_api["browser_specs"].split())
    assert len(common_api["browser_specs"].split()) <= len(inventory()[1]) - 1
    assert len(pick(["frontend/tests/e2e/global-setup.ts"])["browser_specs"].split()) > 40
    heavy = balanced_shards({"test_full_horizon_solve.py",
                              "test_evaluation_cashflow_chain.py", "test_smoke.py"}, 2,
                             BACKEND_SECONDS)
    assert all("test_full_horizon_solve.py" not in part["tests"] or
               "test_evaluation_cashflow_chain.py" not in part["tests"]
               for part in heavy)
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
    economic = pick(["frontend/src/pages/EconomicBindingsPage.tsx"])
    assert economic["browser_specs"] == "economic-bindings-issue03.spec.ts infra.spec.ts"
    data = pick(["frontend/src/pages/DataPage.tsx"])
    assert "data-config-tasks.spec.ts" in data["browser_specs"]
    assert "device-data-upload.spec.ts" in data["browser_specs"]
    assert "economic-bindings-issue03.spec.ts" not in data["browser_specs"]
    assert "page-loading.spec.ts" not in economic["browser_specs"]
    generator = pick(["backend/iesplan/computation/generators/project.py"])
    assert all("tests/" + test in generator["backend_tests"].split() for test in GENERATOR_PROJECT_CONSUMERS)
    assert "tax-chain.spec.ts" in generator["browser_specs"]
    assert len(generator["backend_tests"].split()) < 30  # scoped, never automatic full-suite
    ordinary = pick(["backend/iesplan/computation/generators/mps.py"])
    assert "tests/test_vat_monthly.py" not in ordinary["backend_tests"].split()
    assert len(pick(["frontend/src/pages/NewUnmappedPage.tsx"])["browser_specs"].split()) > 40
    assert "tests/test_task_draft_load.py" in pick(["backend/tests/test_task_draft_load.py"])["backend_tests"]
    assert pick(["docs/reviews/notes.md"])["backend_run"] == "false"
    assert pick(["docs/reviews/notes.md"])["browser_run"] == "false"
    assert pick(["frontend/tests/e2e/device-yaml-extension.spec.ts"])["special_device"] == "true"
    assert len(pick(["frontend/src/features/new-subsystem/Feature.tsx"])["browser_specs"].split()) > 40
    assert len(pick(["backend/iesplan/new_module/file.py"])["backend_tests"].split()) > 80
    assert len(pick([".github/ci/select_tests.py"], full=True)["browser_specs"].split()) > 40
    assert len(pick([".github/ci/select_tests.py"], full=True)["frontend_tests"].split()) == len(inventory()[2])
    # Exact positive/negative triggers for separate cold-start and concurrent
    # PG checks; ordinary CI utilities and UI changes must not force either.
    assert pick([".github/workflows/docker-ci.yml"])["startup_run"] == "true"
    assert pick([".github/workflows/docker-ci.yml"])["pg_run"] == "true"
    for path in (".github/ci/docker-compose.yml", "docker-compose.yml",
                 "backend/Dockerfile", "backend/iesplan/db.py",
                 "backend/iesplan/bootstrap/__init__.py"):
        assert pick([path])["startup_run"] == "true", path
        assert pick([path])["pg_run"] == "true", path
    for path in (".github/ci/start-stack.sh", ".github/ci/verify-cold-start.sh",
                 "backend/iesplan/worker/main.py", "backend/iesplan/main.py"):
        assert pick([path])["startup_run"] == "true", path
        assert pick([path])["pg_run"] == "false", path
    assert pick([".github/ci/verify-pg-init-concurrency.sh"])["pg_run"] == "true"
    assert pick([".github/ci/verify-pg-init-concurrency.sh"])["startup_run"] == "false"
    assert pick(["backend/tests/pg_init_concurrency_probe.py"])["pg_run"] == "true"
    assert pick(["backend/tests/pg_init_concurrency_probe.py"])["startup_run"] == "false"
    for path in (".github/ci/test-frontend-logic.sh", ".github/ci/select_tests.py",
                 "frontend/src/styles.css", "frontend/src/components/ui/Button.tsx",
                 "docs/reviews/review.md", "README.md"):
        assert pick([path])["startup_run"] == "false", path
        assert pick([path])["pg_run"] == "false", path
    assert pick([".github/ci/select_tests.py"], full=True)["startup_run"] == "true"
    assert pick([".github/ci/select_tests.py"], full=True)["pg_run"] == "true"
    assert pick([".github/ci/start-stack.sh"])["startup_run"] == "true"
    database = pick(["backend/iesplan/db.py"])
    assert database["startup_run"] == "true"
    standalone_probe = pick(["backend/tests/pg_init_concurrency_probe.py"])
    assert standalone_probe["pg_run"] == "true"
    assert len(standalone_probe["backend_tests"].split()) < 8
    fixture = pick(["frontend/tests/e2e/fixtures/session.ts"])
    assert set(BASIC_SPECS) <= set(fixture["browser_specs"].split())
    assert len(fixture["browser_specs"].split()) < 8
    assert "tests/test_db_immutable_triggers.py" in database["backend_tests"].split()
    assert "tests/test_protocol_baseline.py" not in database["backend_tests"].split()
    assert len(database["backend_tests"].split()) < 10
    assert pick(["docs/readme.md"])["startup_run"] == "false"
    basic_specs = set(pick(["frontend/src/components/ui/Button.tsx"])["browser_specs"].split())
    assert set(BASIC_SPECS) <= basic_specs
    assert len(basic_specs) < 8
    assert set(BASIC_SPECS) <= set(pick([".github/ci/select_tests.py"])["browser_specs"].split())
    assert {"financial-evaluation.spec.ts", *BASIC_SPECS} <= set(
        pick(["frontend/src/components/ui/Button.tsx",
              "frontend/src/pages/FinancePage.tsx"])["browser_specs"].split())
    assert "basic-operations.spec.ts" in pick(["frontend/src/components/ui/Button.tsx"])["browser_specs"]
    assert "basic-operations.spec.ts" in pick(["frontend/src/components/FormField.tsx"])["browser_specs"]
    assert "auth-navigation-model.spec.ts" in pick(["frontend/src/shared/http.ts"])["browser_specs"]
    assert "config-revision.spec.ts" in pick(["frontend/src/shared/http.ts"])["browser_specs"]
    assert "auth_admin_contracts.test.mjs" in pick(["frontend/src/shared/http.ts"])["frontend_tests"]
    hourly = pick(["frontend/src/api/hourlyPages.ts"])
    assert {"hourly_pages.test.mjs", "hourly_absence.test.mjs",
            "hourly_consume.test.mjs"} <= set(hourly["frontend_tests"].split())
    assert "hourly-r3.spec.ts" in hourly["browser_specs"]
    assert len(hourly["browser_specs"].split()) < 8
    report = pick(["frontend/src/application/exportTaskReport.ts"])
    assert "export_no_implicit_assessment.test.mjs" in report["frontend_tests"]
    assert "export-assessment-r7.spec.ts" in report["browser_specs"]
    assert len(report["browser_specs"].split()) < 8
    assert "help_markdown.test.mjs" in pick(["frontend/src/lib/helpMarkdown.ts"])["frontend_tests"]
    assert "admin-help-lang.spec.ts" in pick(["frontend/src/lib/helpMarkdown.ts"])["browser_specs"]
    assert "help_markdown.test.mjs" in pick(["manual/user-guide/zh-CN/quickstart.md"])["frontend_tests"]
    assert "modeling_mappers.test.mjs" in pick(["frontend/src/features/modeling/form.ts"])["frontend_tests"]
    assert "dataset-version-mapping.test.mjs" in pick(["frontend/src/pages/DataPage.tsx"])["frontend_tests"]
    assert pick(["docs/development/note.md"])["frontend_run"] == "false"
    assert pick(["frontend/src/pages/model/model.css"])["browser_run"] == "true"
    assert len(pick(["frontend/src/pages/model/model.css"])["browser_specs"].split()) < 8
    assert len(pick(["frontend/src/styles.css"])["browser_specs"].split()) < 8
    assert len(pick(["frontend/src/pages/model/model.css"])["browser_specs"].split()) < 8
    assert pick(["docs/reviews/local-style.md"])["browser_run"] == "false"
    assert "basic-operations.spec.ts" in pick(["frontend/src/styles.css"])["browser_specs"]
    assert "frontend_run" in pick(["frontend/tests/auth_admin_contracts.test.mjs"])
    assert pick(["frontend/tests/auth_admin_contracts.test.mjs"])["frontend_tests"] == "auth_admin_contracts.test.mjs"
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
