"""Tests for F3 `ies.evaluator.finance@1.0.0` forward financial evaluator stage."""

from __future__ import annotations

import ast
import importlib.metadata
import json
import math
import shutil
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from iesplan.assembly import validate_assembly_text
from iesplan.computation import (
    HIGHS_ADAPTER,
    HIGHS_ADAPTER_PATH,
    HIGHS_EXECUTABLE_REF,
    HIGHS_MILP_PROVIDER,
    HIGHS_VERSION,
    AttemptContext,
    CalculationConfig,
    DeploymentPolicy,
    ExecutableReadiness,
    ExecutionReceipt,
    ExecutorDescriptor,
    LocalExecutable,
    LocalProcessProvider,
    SolverRuntime,
    build_executor_registry,
    materialize_non_predictive_series,
)
from iesplan.computation.contracts import (
    PRIMAL_SOLUTION_OUTPUT_PATH,
    TargetCalculationConfig,
    TargetSolverBundle,
)
from iesplan.computation.generators.highs_milp import EXECUTOR_REF
from iesplan.computation.generators.target_bundle import generate_target_highs_bundle
from iesplan.computation.result_adapters.target_adapter import TargetResultAdapter
from iesplan.devices import init_registry, list_devices
from iesplan.finance import EvaluationInput, effective_from_yaml
from iesplan.modeling import build_math_contribution, contribution_to_dict
from pies_sdk import (
    StageContext,
    StageIOError,
    check_compatibility,
    load_plugin_manifest,
    read_compute_result,
    read_engineering_solution,
    read_json_file,
    write_engineering_solution,
    write_json_file,
)
from pies_sdk.runner import resolve_entrypoint, run_plugin

from pies_builtin_plugins.stage_io import build_financial_evaluation_payload

PLUGINS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGINS_ROOT.parent
IESPLAN_SAMPLES = REPO_ROOT / "backend" / "iesplan" / "assembly" / "samples"
EVAL_FINANCE_DIR = PLUGINS_ROOT / "evaluator_finance"
EVAL_FINANCE_MANIFEST = EVAL_FINANCE_DIR / "plugin.yaml"

_FORBIDDEN_PACKAGES = frozenset(
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
_FORBIDDEN_IESPLAN_SUBMODULES = frozenset(
    {
        "api",
        "application",
        "audit",
        "bootstrap",
        "computation",
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


def _assert_eval_close(actual: Any, expected: Any, *, path: str = "root") -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected dict, got {type(actual).__name__}"
        assert set(actual) == set(expected), (
            f"{path}: keys differ: {sorted(actual)} != {sorted(expected)}"
        )
        for key in expected:
            _assert_eval_close(actual[key], expected[key], path=f"{path}.{key}")
        return
    if isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected list, got {type(actual).__name__}"
        assert len(actual) == len(expected), f"{path}: len {len(actual)} != {len(expected)}"
        for idx, (a_item, e_item) in enumerate(zip(actual, expected, strict=True)):
            _assert_eval_close(a_item, e_item, path=f"{path}[{idx}]")
        return
    if isinstance(expected, bool):
        assert actual is expected, f"{path}: {actual!r} is not {expected!r}"
        return
    if isinstance(expected, int):
        assert isinstance(actual, int) and not isinstance(actual, bool), f"{path}: {actual!r}"
        assert actual == expected, f"{path}: {actual} != {expected}"
        return
    if isinstance(expected, float):
        assert isinstance(actual, (int, float)) and not isinstance(actual, bool)
        if not math.isfinite(expected):
            assert actual == expected
        else:
            assert float(actual) == pytest.approx(expected, rel=1e-7, abs=1e-6), (
                f"{path}: {actual} != {expected}"
            )
        return
    if isinstance(expected, str):
        try:
            e_dec = Decimal(expected)
        except Exception:
            assert actual == expected, f"{path}: {actual!r} != {expected!r}"
            return
        assert isinstance(actual, str), f"{path}: expected decimal string, got {actual!r}"
        assert float(Decimal(actual)) == pytest.approx(float(e_dec), rel=1e-7, abs=1e-6), (
            f"{path}: {actual} != {expected}"
        )
        return
    assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def _load_campus_frozen_inputs() -> tuple[Any, dict[str, Any], Any, Any, EvaluationInput, dict[str, bytes]]:
    init_registry()
    raw_text = (IESPLAN_SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    unit_block = (
        "  construction_units:\n"
        "    u_pv1:\n"
        "      device_ref: pv1\n"
        "      mode: optional\n"
        "      scales:\n"
        "        pv_cap:\n"
        '          active_lower_bound: "0"\n'
        "          active_upper_bound: null\n"
    )
    text = raw_text.replace("  constraints:\n", unit_block + "  constraints:\n", 1)
    validated = validate_assembly_text(text, package_dir=IESPLAN_SAMPLES)
    assert validated.ok and validated.artifact is not None
    artifact = validated.artifact

    contributions = {}
    for dev in list_devices():
        built = build_math_contribution(dev)
        assert built.ok
        contributions[dev.device.id] = built.contribution

    doc = json.loads(artifact.canonical_text)
    models = {inst: contributions[dev["definition"]["id"]] for inst, dev in doc["devices"].items()}
    finance = effective_from_yaml(
        (IESPLAN_SAMPLES / "finance" / "campus.effective-finance.yaml").read_text(encoding="utf-8")
    )
    found: set[str] = set()

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, dict) and "object_id" in content:
                found.add(str(content["object_id"]))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(doc.get("devices") or {})
    resources = {
        oid: (IESPLAN_SAMPLES / (oid[5:] if oid.startswith("file:") else oid)).read_bytes()
        for oid in sorted(found)
    }
    materialized = materialize_non_predictive_series(artifact, resources, operation_years=1)
    evaluation = EvaluationInput(
        evaluation_years=1,
        construction_period=0,
        operation_years=1,
        discount_rate=0.08,
        construction_payments=(),
        maintenance_expenses=(),
    )
    return artifact, models, materialized, finance, evaluation, resources


def _evaluation_to_dict(ev: EvaluationInput) -> dict[str, Any]:
    return {
        "evaluation_years": ev.evaluation_years,
        "construction_period": ev.construction_period,
        "operation_years": ev.operation_years,
        "discount_rate": ev.discount_rate,
        "construction_payments": [{"t": p.t, "ratio": str(p.ratio)} for p in ev.construction_payments],
        "maintenance_expenses": [],
        "comparison": ev.comparison.to_dict(),
    }


def test_evaluator_finance_manifest_and_offline_minimal_fixture(tmp_path: Path) -> None:
    """Validate manifest contracts (`consumes`/`produces`) and run offline directory fixture."""
    assert EVAL_FINANCE_MANIFEST.is_file()
    manifest = load_plugin_manifest(EVAL_FINANCE_MANIFEST, pies_version="1.2.0", check_compat=True)
    check_compatibility(manifest, pies_version="1.2.0")

    assert manifest.plugin_id == "ies.evaluator.finance"
    assert manifest.version == "1.0.0"
    assert manifest.kind == "evaluator"
    assert manifest.compatibility["pies"] == ">=1.2.0,<2.0.0"
    assert manifest.consumes == ("ies.engineering-solution@1.0.0",)
    assert manifest.produces == ("ies.financial-evaluation@1.0.0",)
    assert manifest.parameters_schema["additionalProperties"] is False
    assert manifest.advanced is None
    assert len(manifest.tests) == 1
    test_spec = manifest.tests[0]
    assert test_spec.is_directory

    work_in = tmp_path / "in"
    work_out = tmp_path / "out"
    shutil.copytree(EVAL_FINANCE_DIR / test_spec.input, work_in)

    run_plugin(
        manifest_path=EVAL_FINANCE_MANIFEST,
        work_in=work_in,
        work_out=work_out,
        pies_version="1.2.0",
    )
    actual = read_compute_result(work_out).to_dict()
    expected = read_json_file(EVAL_FINANCE_DIR / test_spec.expected / "result.json")
    assert actual == expected

    fin_eval = actual["outputs"]["financial_evaluation"]
    assert fin_eval["schema"] == "ies.financial-evaluation"
    assert fin_eval["schema_version"] == "1.0.0"
    assert Decimal(fin_eval["construction_total"]) == Decimal("1000")
    assert [Decimal(v) for v in fin_eval["annual_net_flows"]] == [Decimal("-1000"), Decimal("6072.5")]
    assert Decimal(fin_eval["evaluation"]["npv"]) == Decimal("5072.5")


def test_gate4_evaluator_finance_zero_forbidden_deps_and_zero_sha256() -> None:
    """`plugins/evaluator_finance` must remain isolated from computation/DB/web and SHA-256."""
    py_files = sorted(EVAL_FINANCE_DIR.rglob("*.py"))
    assert py_files
    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        assert "sha256" not in source.lower(), f"Forbidden SHA-256 reference in {py_file}"
        assert "hashlib" not in source, f"Forbidden hashlib usage in {py_file}"
        assert "validate_assembly_text" not in source, (
            f"Frozen evaluator stage must not repeat submission validation in {py_file}"
        )

        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module_name in modules:
                root = module_name.split(".")[0]
                assert root not in _FORBIDDEN_PACKAGES, f"{py_file} imports forbidden {root!r}"
                if root == "iesplan" and "." in module_name:
                    sub = module_name.split(".")[1]
                    assert sub not in _FORBIDDEN_IESPLAN_SUBMODULES, (
                        f"{py_file} imports forbidden iesplan.{sub}"
                    )

    installed = {dist.metadata["Name"].lower() for dist in importlib.metadata.distributions()}
    assert not (installed & _FORBIDDEN_PACKAGES)


def test_f3_campus_full_parity_with_115_evaluator(tmp_path: Path) -> None:
    """F2 generate_f2 -> solve_f2 -> restore -> evaluator_finance matches 1.1.5 evaluation."""
    artifact, models, materialized, finance, evaluation, _resources = _load_campus_frozen_inputs()

    # 1) 1.1.5 direct reference financial evaluation
    legacy_cfg = CalculationConfig(
        mode="planning",
        generator="ies.generator.highs_milp@1.0.0",
        solver="ies.solver.highs@1.7.2",
        evaluator=HIGHS_ADAPTER.ref,
        precision="medium",
        time_limit_seconds=120,
        outputs=("solution", "metrics"),
        seed=7,
    )
    legacy_bundle = HIGHS_MILP_PROVIDER.generate(
        artifact, models, materialized, finance, legacy_cfg, evaluation
    )
    executor_id, _, executor_version = EXECUTOR_REF.rpartition("@")
    runtime = SolverRuntime(
        build_executor_registry(
            [
                LocalProcessProvider(
                    ExecutorDescriptor(
                        executor_id=executor_id,
                        version=executor_version,
                        executables=(HIGHS_EXECUTABLE_REF,),
                    ),
                    (
                        LocalExecutable(
                            ref=HIGHS_EXECUTABLE_REF,
                            path=HIGHS_ADAPTER_PATH,
                            readiness=ExecutableReadiness(
                                version=HIGHS_VERSION,
                                self_test=("--self-test",),
                                output_path="self-test.sol",
                            ),
                        ),
                    ),
                    base_environ={},
                )
            ]
        ),
        DeploymentPolicy(
            executables=(HIGHS_EXECUTABLE_REF,),
            environment_vars=(),
            max_timeout_seconds=1200,
            max_memory_mib=4096,
            max_cpu_cores=4,
            allow_network=False,
        ),
    )
    receipt, outputs = runtime.run(
        legacy_bundle,
        AttemptContext(
            attempt_id="f3-parity-1",
            bundle_id="f3-bundle-1",
            assembly_id="campus_demo",
            calculation_config_id="f3-cfg-1",
        ),
    )
    assert receipt.status == "succeeded"
    legacy_result = HIGHS_ADAPTER.adapt(legacy_bundle, receipt, outputs)
    expected_eval = build_financial_evaluation_payload(
        _evaluation_to_dict(evaluation), finance, legacy_result
    )
    assert expected_eval is not None

    # 2) F2 generate_f2 -> solve_f2 -> TargetResultAdapter -> ies.engineering-solution@1.0.0
    target_cfg = TargetCalculationConfig(
        mode="planning",
        predictions=(),
        generator="ies.generator.highs_milp@1.0.0",
        solver="ies.solver.highs@1.7.2",
        algorithm=None,
        evaluators=({"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}},),
        precision="medium",
        time_limit_seconds=120,
        options={"generator": {}, "solver": {}},
        seed=7,
        outputs=("solution", "metrics"),
    )
    gen_in = tmp_path / "f2_gen_in"
    gen_in.mkdir()
    write_json_file(
        gen_in / "request.json",
        {
            "artifact": artifact.to_dict(),
            "device_models": {inst: contribution_to_dict(c) for inst, c in models.items()},
            "materialized": materialized.to_dict(),
            "finance": finance.to_dict(),
            "calculation_config": target_cfg.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    generate_f2 = resolve_entrypoint("f2_generator:generate_f2", PLUGINS_ROOT / "generator_highs_milp")
    f2_gen_out = tmp_path / "f2_bundle_out"
    f2_gen_out.mkdir()
    generate_f2(
        StageContext(
            manifest=load_plugin_manifest(
                PLUGINS_ROOT / "generator_highs_milp" / "plugin.yaml", check_compat=False
            ),
            work_in=gen_in,
            work_out=f2_gen_out,
            parameters={},
        )
    )

    f2_solver_in = tmp_path / "f2_solver_in"
    f2_solver_out = tmp_path / "f2_solver_out"
    f2_solver_in.mkdir()
    f2_solver_out.mkdir()
    (f2_solver_in / "bundle").symlink_to(f2_gen_out / "bundle")
    write_json_file(f2_solver_in / "request.json", {"time_limit_seconds": 120, "seed": 7})
    solve_f2 = resolve_entrypoint("f2_solver:solve_f2", PLUGINS_ROOT / "solver_highs")
    solve_f2(
        StageContext(
            manifest=load_plugin_manifest(PLUGINS_ROOT / "solver_highs" / "plugin.yaml", check_compat=False),
            work_in=f2_solver_in,
            work_out=f2_solver_out,
            parameters={"threads": 1},
        )
    )

    bundle = generate_target_highs_bundle(artifact, models, materialized, finance, target_cfg, evaluation)
    assert isinstance(bundle, TargetSolverBundle)
    sol_json_out = tmp_path / "restored_solution.json"
    exec_receipt = ExecutionReceipt(
        receipt_id="f3-rec-1",
        bundle_id="f3-bnd-1",
        assembly_id="campus_demo",
        calculation_config_id="f3-cfg-1",
        attempt_id="f3-att-1",
        generator_ref="ies.generator.highs_milp@1.0.0",
        solver_ref="ies.solver.highs@1.7.2",
        executor_ref=EXECUTOR_REF,
        executable_ref=HIGHS_EXECUTABLE_REF,
        result_adapter_ref="ies.result-adapter.target@1.0.0",
        status="succeeded",
        exit_code=0,
        termination_reason="exited",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
    )
    outcome = TargetResultAdapter().restore_with_outcome(
        bundle,
        exec_receipt,
        {PRIMAL_SOLUTION_OUTPUT_PATH: (f2_solver_out / "primal_solution.json").read_bytes()},
        solution_output_path=sol_json_out,
    )
    assert outcome.compute_result.math_status == "solved"

    # 3) Run F3 `ies.evaluator.finance@1.0.0` via `run_plugin` with ONLY solution.json + frozen context
    # (NO SolverBundle, NO problem.mps, NO variable_map/parameters.json*)
    eval_in = tmp_path / "f3_eval_in"
    eval_out = tmp_path / "f3_eval_out"
    eval_in.mkdir()
    (eval_in / "solution.json").symlink_to(sol_json_out)
    write_json_file(eval_in / "assembly.json", artifact.to_dict())
    write_json_file(eval_in / "device_models.json", {k: contribution_to_dict(v) for k, v in models.items()})
    write_json_file(eval_in / "sequences.json", materialized.to_dict())
    write_json_file(eval_in / "finance.json", finance.to_dict())
    write_json_file(eval_in / "evaluation.json", _evaluation_to_dict(evaluation))

    run_plugin(
        manifest_path=EVAL_FINANCE_MANIFEST,
        work_in=eval_in,
        work_out=eval_out,
        pies_version="1.2.0",
    )
    result = read_compute_result(eval_out)
    assert result.execution_status == "completed"
    assert result.math_status == "solved"
    actual_eval = result.outputs["financial_evaluation"]
    _assert_eval_close(actual_eval, expected_eval)


def test_f3_independent_algorithm_to_evaluator_without_bundle(tmp_path: Path) -> None:
    """Algorithm outputting ies.engineering-solution@1.0.0 feeds evaluator_finance with zero bundle."""
    # First run standalone sdk/examples/reference_algorithm via runner to confirm independent dispatch
    ref_dir = REPO_ROOT / "sdk" / "examples" / "reference_algorithm"
    ref_in = tmp_path / "ref_in"
    ref_out = tmp_path / "ref_out"
    ref_in.mkdir()
    shutil.copy(ref_dir / "tests" / "minimal" / "input.json", ref_in / "request.json")
    run_plugin(manifest_path=ref_dir / "plugin.yaml", work_in=ref_in, work_out=ref_out)
    assert read_compute_result(ref_out).math_status == "solved"

    # Now run an inline standalone F1 `algorithm` plugin emitting ies.engineering-solution@1.0.0
    alg_dir = tmp_path / "alg_plugin"
    alg_dir.mkdir()
    (alg_dir / "plugin.yaml").write_text(
        "\n".join(
            [
                "schema: ies.algorithm-plugin",
                'schema_version: "1.0.0"',
                "plugin_id: acme.algorithm.synthetic_f1",
                'version: "1.0.0"',
                "kind: algorithm",
                "name:",
                "  en_us: Synthetic F1 Algorithm",
                "description: Produces ies.engineering-solution@1.0.0 without SolverBundle.",
                "compatibility:",
                '  pies: ">=1.2.0,<2.0.0"',
                "produces:",
                "  - ies.engineering-solution@1.0.0",
                "runtime:",
                "  language: python",
                '  python: "3.12"',
                "entrypoint: alg_main:solve",
                "parameters_schema:",
                "  type: object",
                "  additionalProperties: false",
                "  properties: {}",
                "resources:",
                "  default: {cpu: 1, memory_mb: 256, timeout_seconds: 60, process_limit: 8}",
                "  maximum: {cpu: 1, memory_mb: 512, timeout_seconds: 120, process_limit: 8}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (alg_dir / "alg_main.py").write_text(
        "\n".join(
            [
                "from pies_sdk import ComputeResultPayload, StageContext, write_engineering_solution",
                "def solve(ctx: StageContext) -> None:",
                "    req = ctx.read_request()",
                "    write_engineering_solution(ctx.work_out / 'solution.json', req['solution'])",
                "    ctx.write_result(",
                "        ComputeResultPayload(",
                "            bundle_id='alg-standalone',",
                "            receipt_id='alg-rec-1',",
                "            execution_status='completed',",
                "            math_status='solved',",
                "            outputs={'solver_summary': req['solution']['solver_summary']},",
                "        )",
                "    )",
                "",
            ]
        ),
        encoding="utf-8",
    )
    minimal_in = EVAL_FINANCE_DIR / "tests" / "minimal" / "in"
    alg_in = tmp_path / "alg_in"
    alg_out = tmp_path / "alg_out"
    alg_in.mkdir()
    write_json_file(alg_in / "request.json", {"solution": read_json_file(minimal_in / "solution.json")})
    run_plugin(manifest_path=alg_dir / "plugin.yaml", work_in=alg_in, work_out=alg_out, pies_version="1.2.0")
    assert read_engineering_solution(alg_out / "solution.json").solver_summary.has_feasible_solution is True

    # Feed algorithm's solution.json directly into evaluator_finance with ZERO SolverBundle/MPS/variable_map
    eval_in = tmp_path / "eval_in"
    eval_out = tmp_path / "eval_out"
    eval_in.mkdir()
    (eval_in / "solution.json").symlink_to(alg_out / "solution.json")
    for fname in ("assembly.json", "finance.json", "evaluation.json"):
        shutil.copy(minimal_in / fname, eval_in / fname)

    run_plugin(
        manifest_path=EVAL_FINANCE_MANIFEST,
        work_in=eval_in,
        work_out=eval_out,
        pies_version="1.2.0",
    )
    res = read_compute_result(eval_out)
    assert res.execution_status == "completed"
    assert res.math_status == "solved"
    expected = read_json_file(EVAL_FINANCE_DIR / "tests" / "minimal" / "expected" / "result.json")
    assert res.to_dict()["outputs"] == expected["outputs"]


def test_f3_rejects_infeasible_solution_and_missing_frozen_inputs(tmp_path: Path) -> None:
    """Reject non-feasible solution.json or missing required frozen inputs without silent defaults."""
    minimal_in = EVAL_FINANCE_DIR / "tests" / "minimal" / "in"

    # 1) Non-feasible engineering solution -> StageIOError
    inf_in = tmp_path / "inf_in"
    inf_out = tmp_path / "inf_out"
    inf_in.mkdir()
    for fname in ("assembly.json", "finance.json", "evaluation.json"):
        shutil.copy(minimal_in / fname, inf_in / fname)
    bad_sol = deepcopy(read_json_file(minimal_in / "solution.json"))
    bad_sol["solver_summary"] = {
        "termination_reason": "infeasible",
        "math_status": "infeasible",
        "has_feasible_solution": False,
        "objective_value": None,
        "objective_unit": None,
        "best_bound": None,
        "relative_gap": None,
    }
    bad_sol["construction_units"] = []
    bad_sol["series"] = {}
    bad_sol["system_series"] = {}
    write_engineering_solution(inf_in / "solution.json", bad_sol)

    with pytest.raises(StageIOError, match="non-feasible"):
        run_plugin(
            manifest_path=EVAL_FINANCE_MANIFEST,
            work_in=inf_in,
            work_out=inf_out,
            pies_version="1.2.0",
        )

    # 2) Missing required frozen context (e.g., finance) -> KeyError
    missing_in = tmp_path / "missing_in"
    missing_out = tmp_path / "missing_out"
    missing_in.mkdir()
    for fname in ("assembly.json", "evaluation.json", "solution.json"):
        shutil.copy(minimal_in / fname, missing_in / fname)
    with pytest.raises(KeyError, match="finance"):
        run_plugin(
            manifest_path=EVAL_FINANCE_MANIFEST,
            work_in=missing_in,
            work_out=missing_out,
            pies_version="1.2.0",
        )


def test_f3_rejects_omitted_truncated_or_unit_mismatched_solution_and_loads_frozen_yaml(
    tmp_path: Path,
) -> None:
    """Reject incomplete/truncated/mismatched solutions at boundary; load frozen assembly.yaml."""
    from pies_sdk import dump_safe_yaml

    minimal_in = EVAL_FINANCE_DIR / "tests" / "minimal" / "in"
    base_sol = read_json_file(minimal_in / "solution.json")
    expected = read_json_file(EVAL_FINANCE_DIR / "tests" / "minimal" / "expected" / "result.json")

    def _run_case(name: str, mutate_sol: Any, *, use_yaml: bool = False) -> None:
        in_dir = tmp_path / f"{name}_in"
        out_dir = tmp_path / f"{name}_out"
        in_dir.mkdir()
        for fname in ("finance.json", "evaluation.json"):
            shutil.copy(minimal_in / fname, in_dir / fname)
        if use_yaml:
            (in_dir / "assembly.yaml").write_text(
                dump_safe_yaml(read_json_file(minimal_in / "assembly.json")),
                encoding="utf-8",
            )
        else:
            shutil.copy(minimal_in / "assembly.json", in_dir / "assembly.json")
        sol = deepcopy(base_sol)
        mutate_sol(sol)
        write_engineering_solution(in_dir / "solution.json", sol)
        run_plugin(
            manifest_path=EVAL_FINANCE_MANIFEST,
            work_in=in_dir,
            work_out=out_dir,
            pies_version="1.2.0",
        )
        if use_yaml:
            assert read_compute_result(out_dir).to_dict()["outputs"] == expected["outputs"]

    # 0) Frozen assembly.yaml loads without repeating submission validation
    _run_case("frozen_yaml", lambda _s: None, use_yaml=True)

    # 1) Omitted required construction_unit
    with pytest.raises(StageIOError, match="construction_units mismatch"):
        _run_case("missing_unit", lambda s: s.__setitem__("construction_units", []))

    # 2) Omitted required planning scale inside construction_unit
    with pytest.raises(StageIOError, match="planning variables mismatch"):
        _run_case("missing_scale", lambda s: s["construction_units"][0].__setitem__("scales", []))

    # 3) Omitted required financial series
    with pytest.raises(StageIOError, match="missing required financial series"):
        _run_case("missing_series", lambda s: s.__setitem__("series", {}))

    # 4) Truncated horizon series (1 step instead of 8760)
    with pytest.raises(StageIOError, match="expected_steps"):
        _run_case(
            "truncated_series",
            lambda s: s["series"]["g.out_e"].__setitem__("values", [1.0]),
        )

    # 5) Device interface unit mismatch when device_models is provided
    bad_unit_in = tmp_path / "bad_unit_in"
    bad_unit_out = tmp_path / "bad_unit_out"
    bad_unit_in.mkdir()
    for fname in ("assembly.json", "finance.json", "evaluation.json"):
        shutil.copy(minimal_in / fname, bad_unit_in / fname)
    write_json_file(
        bad_unit_in / "device_models.json",
        {"g": {"interfaces": {"out_e": {"unit": "MW"}}}},
    )
    write_engineering_solution(bad_unit_in / "solution.json", base_sol)
    with pytest.raises(StageIOError, match="unit mismatch"):
        run_plugin(
            manifest_path=EVAL_FINANCE_MANIFEST,
            work_in=bad_unit_in,
            work_out=bad_unit_out,
            pies_version="1.2.0",
        )
