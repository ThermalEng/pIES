"""0.9-E：真实 HiGHS 绑定、精确版本 readiness 与最小 Bundle 集成。

只在 Docker 内运行。``HighsMilpProvider`` 直接输入生成真实
Bundle，经 executor registry + ``DeploymentPolicy`` +
``SolverRuntime`` 执行，产出声明的真实原始 solution 文件。不实现
ResultAdapter，不解释求解业务状态；不声称网络/CPU/内存/OOM/身份
隔离（留 0.9-F）。
"""

from __future__ import annotations

import json
import logging
import math
import runpy
import subprocess
import threading
from pathlib import Path

import pytest
from bundle_testkit import materialized_file
from legacy_solve_testkit import (
    PROBLEM_PATH,
    SOLUTION_PATH,
    make_minimal_highs_bundle,
    restore_bundle_solution,
)
from legacy_solve_testkit import (
    succeeded_receipt as _synthetic_ok_receipt,
)

from iesplan.assembly import validate_assembly_text
from iesplan.computation import (
    HIGHS_ADAPTER_PATH,
    HIGHS_EXECUTABLE_REF,
    HIGHS_MILP_PROVIDER,
    HIGHS_VERSION,
    SOLVER_BUNDLE_MPS_CONTRACT,
    AttemptContext,
    CalculationConfig,
    ComputationUnavailableError,
    DeploymentPolicy,
    ExecutableReadiness,
    ExecutionReceipt,
    ExecutorDescriptor,
    LocalExecutable,
    LocalProcessProvider,
    SolverBundle,
    SolverRuntime,
    build_executor_registry,
    build_registry,
    materialize_non_predictive_series,
)
from iesplan.computation.generators.highs_milp import (
    GENERATOR_REF,
    SOLVER_REF,
)
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
)
from iesplan.computation.generators.mps import write_mps
from iesplan.core.content import read_content
from iesplan.devices import init_registry, list_devices
from iesplan.finance import effective_from_yaml
from iesplan.modeling import build_math_contribution

EXECUTOR_ID = "ies.executor.local_process"
EXECUTOR_VERSION = "1.0.0"
LOCAL_EXECUTOR_REF = f"{EXECUTOR_ID}@{EXECUTOR_VERSION}"
ADAPTER_REF = "ies.backend.result_adapter@1.0.0"
PROCESS_IO_GRACE_SECONDS = 600


def _adapt(bundle: SolverBundle, receipt: ExecutionReceipt, outputs: dict) -> object:
    from types import SimpleNamespace

    cr, eng_sol = restore_bundle_solution(bundle, receipt, outputs)
    out = dict(cr.outputs)
    if eng_sol:
        out.update(eng_sol)
        summary = eng_sol.get("solver_summary") or {}
        if summary.get("has_feasible_solution"):
            out["solution"] = {"objective_value": summary.get("objective_value")}
    return SimpleNamespace(execution_status=cr.execution_status, math_status=cr.math_status, outputs=out)


def _command(*, time_limit: int = 5, timeout_seconds: int = 10, arguments=None) -> dict[str, object]:
    return {
        "executor": LOCAL_EXECUTOR_REF,
        "executable": HIGHS_EXECUTABLE_REF,
        "arguments": arguments
        if arguments is not None
        else [
            PROBLEM_PATH,
            "--solution_file",
            SOLUTION_PATH,
            "--time_limit",
            str(time_limit),
            "--random_seed",
            "0",
        ],
        "working_directory": ".",
        "timeout_seconds": timeout_seconds,
        "network": False,
        "success_exit_codes": [0],
    }

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
SAMPLES = BACKEND_ROOT / "iesplan" / "assembly" / "samples"


def _campus_inputs():
    """真实 campus 直接输入（与 generator 测试同一来源）。"""
    init_registry()
    text = (SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    validated = validate_assembly_text(text, package_dir=SAMPLES)
    assert validated.ok, [(d.code, d.params) for d in validated.diagnostics if d.blocking]
    artifact = validated.artifact
    contributions = {}
    for doc in list_devices():
        built = build_math_contribution(doc)
        assert built.ok, (doc.device.id, built.diagnostics)
        contributions[doc.device.id] = built.contribution
    doc = json.loads(artifact.canonical_text)
    models = {inst: contributions[dev["definition"]["id"]] for inst, dev in doc["devices"].items()}
    finance = effective_from_yaml(
        (SAMPLES / "finance" / "campus.effective-finance.yaml").read_text(encoding="utf-8")
    )

    def collect(node, found: set):
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, dict) and "object_id" in content:
                found.add(content["object_id"])
            for value in node.values():
                collect(value, found)
        elif isinstance(node, list):
            for value in node:
                collect(value, found)

    object_ids: set = set()
    collect(doc["devices"], object_ids)
    resources = {}
    for object_id in sorted(object_ids):
        rel = object_id[5:] if object_id.startswith("file:") else object_id
        resources[object_id] = (SAMPLES / rel).read_bytes()
    mat = materialize_non_predictive_series(artifact, resources, operation_years=1)
    assert not mat.pending_predict
    config = CalculationConfig(
        mode="planning",
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        precision="medium",
        time_limit_seconds=600,
        outputs=["solution", "metrics"],
        seed=None,
    )
    return artifact, models, mat, finance, config


def _campus_evaluation():
    """campus 合成评价确认（与物化同一 N=1/K=0 口径，冻结值直传）。"""
    from iesplan.finance import EvaluationInput

    return EvaluationInput(
        evaluation_years=1,
        construction_period=0,
        operation_years=1,
        discount_rate=0.08,
        construction_payments=(),
        maintenance_expenses=(),
    )


def _campus_bundle():
    """正式路径生成真实 Bundle（非手写 manifest）。"""
    artifact, models, materialized, finance, config = _campus_inputs()
    provider = build_registry((HIGHS_MILP_PROVIDER,)).resolve(config)
    assert provider is HIGHS_MILP_PROVIDER
    return provider.generate(artifact, models, materialized, finance, config, _campus_evaluation())


def _readiness(**kwargs) -> ExecutableReadiness:
    kwargs.setdefault("version", HIGHS_VERSION)
    kwargs.setdefault("self_test", ("--self-test",))
    kwargs.setdefault("output_path", "self-test.sol")
    return ExecutableReadiness(**kwargs)


def _binding(**kwargs) -> LocalExecutable:
    from legacy_solve_testkit import install_highs_stage_wrapper

    kwargs.setdefault("ref", HIGHS_EXECUTABLE_REF)
    kwargs.setdefault("path", install_highs_stage_wrapper())
    kwargs.setdefault("readiness", _readiness())
    return LocalExecutable(**kwargs)


def _descriptor() -> ExecutorDescriptor:
    return ExecutorDescriptor(
        executor_id=EXECUTOR_ID,
        version=EXECUTOR_VERSION,
        executables=(HIGHS_EXECUTABLE_REF,),
    )


def _provider() -> LocalProcessProvider:
    return LocalProcessProvider(_descriptor(), (_binding(),), base_environ={})


def _runtime(provider=None, policy=None) -> SolverRuntime:
    provider = provider if provider is not None else _provider()
    if policy is None:
        policy = _policy(max_timeout_seconds=1200)
    return SolverRuntime(build_executor_registry([provider]), policy)


def _attempt() -> AttemptContext:
    return AttemptContext(
        attempt_id="attempt-highs",
        bundle_id="bundle-highs",
        assembly_id="assembly-highs",
        calculation_config_id="config-highs",
    )


def test_refs_single_source_and_exact_version() -> None:
    """引用与版本唯一来源：生成器声明 == 绑定引用，版本由引用导出。"""
    assert SOLVER_REF == HIGHS_EXECUTABLE_REF
    assert HIGHS_EXECUTABLE_REF == "ies.solver.highs@1.7.2"
    assert HIGHS_VERSION == "1.7.2"
    assert _readiness().version == HIGHS_VERSION
    assert _readiness().output_path == "self-test.sol"
    assert _binding().readiness is not None
    assert HIGHS_ADAPTER_PATH.startswith("/") and ".." not in (HIGHS_ADAPTER_PATH)
    pinned = (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"highspy==1.7.2"' in pinned


def test_adapter_reports_exact_version() -> None:
    """固定路径可执行并原样报告精确版本 1.7.2，且不污染 stderr。"""
    completed = subprocess.run(
        [HIGHS_ADAPTER_PATH, "--version"],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout.decode("utf-8").strip() == "1.7.2"
    assert completed.stderr == b""


def test_adapter_rejects_non_structured_args() -> None:
    """非结构化参数即拒绝：不是通用脚本执行器。"""
    for argv in (["--bogus"], ["input/problem.mps"], ["--version", "extra"]):
        completed = subprocess.run(
            [HIGHS_ADAPTER_PATH, *argv],
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode != 0


def test_readiness_available_and_atomic_reject() -> None:
    """就绪通过则注册；版本不符或路径缺失则原子拒绝。"""
    assert _provider().available() is True
    registry = build_executor_registry([_provider()])
    assert registry.resolve(LOCAL_EXECUTOR_REF, HIGHS_EXECUTABLE_REF).ref == LOCAL_EXECUTOR_REF
    bad_version = LocalProcessProvider(
        _descriptor(),
        (
            _binding(
                readiness=_readiness(version="9.9.9"),
            ),
        ),
        base_environ={},
    )
    assert bad_version.available() is False
    with pytest.raises(ComputationUnavailableError) as exc_info:
        build_executor_registry([bad_version])
    assert exc_info.value.reason == "no-executor"
    with pytest.raises(ComputationUnavailableError) as atomic_info:
        build_executor_registry([_provider(), bad_version])
    assert atomic_info.value.reason == "no-executor"
    bad_path = LocalProcessProvider(
        _descriptor(),
        (
            LocalExecutable(
                ref=HIGHS_EXECUTABLE_REF,
                path="/nonexistent/ies-highs-solver",
                readiness=_readiness(),
            ),
        ),
        base_environ={},
    )
    assert bad_path.available() is False
    with pytest.raises(ComputationUnavailableError):
        build_executor_registry([bad_path])


def test_readiness_self_test_failure_rejects() -> None:
    """版本正确但自检失败即不可用：非零退出与缺声明文件都不回退。"""
    nonzero = LocalProcessProvider(
        _descriptor(),
        (
            _binding(
                readiness=_readiness(self_test=("--self-test", "--bogus")),
            ),
        ),
        base_environ={},
    )
    assert nonzero.available() is False
    with pytest.raises(ComputationUnavailableError) as exc_info:
        build_executor_registry([nonzero])
    assert exc_info.value.reason == "no-executor"
    missing_output = LocalProcessProvider(
        _descriptor(),
        (
            _binding(
                readiness=_readiness(output_path="other.sol"),
            ),
        ),
        base_environ={},
    )
    assert missing_output.available() is False
    with pytest.raises(ComputationUnavailableError):
        build_executor_registry([missing_output])


def test_readiness_output_path_must_be_relative() -> None:
    """自检输出只接受非空规范相对路径。"""
    with pytest.raises(ValueError, match="非空"):
        _readiness(output_path="")
    with pytest.raises(ValueError, match="非法|越界"):
        _readiness(output_path="/abs.sol")


def _parse_solution(text: str) -> dict:
    lines = text.splitlines()
    assert lines[0] == "HIGHS_SOLUTION_V1"
    facts = {}
    primals: list[float] = []
    section = None
    for line in lines[1:]:
        if line == "primal:":
            section = "primal"
            continue
        if section == "primal":
            _, _, value = line.partition(":")
            primals.append(float(value.strip()))
        else:
            key, _, value = line.partition(":")
            facts[key.strip()] = value.strip()
    facts["primals"] = primals
    return facts


def test_end_to_end_campus_bundle() -> None:
    """campus 真实 Bundle 经受控执行产出声明 solution 文件。"""
    bundle = _campus_bundle()
    manifest = bundle.manifest
    assert manifest.bundle["contract"] == SOLVER_BUNDLE_MPS_CONTRACT
    mps_bytes = materialized_file(bundle, PROBLEM_PATH)
    assert mps_bytes.startswith(b"NAME") and len(mps_bytes) > 100
    cmd = _command(
        time_limit=600,
        timeout_seconds=1200,
        arguments=[
            PROBLEM_PATH,
            "--solution_file",
            SOLUTION_PATH,
            "--time_limit",
            "600",
            "--random_seed",
            "0",
            "--acceleration_mode",
            "on_demand_mutex",
            "--acceleration_file",
            "input/acceleration.json",
        ],
    )
    receipt, outputs = _runtime().run(bundle, _attempt(), command=cmd)
    if receipt.status != "succeeded":
        logging.getLogger("pies.ci.highs_readiness").warning(
            "CI_SOLVER readiness status=%s reason=%s stdout_head=%r stdout_tail=%r stderr=%r",
            receipt.status, receipt.termination_reason,
            receipt.stdout[:1500], receipt.stdout[-6500:], receipt.stderr[-1500:],
        )
    from iesplan.computation import PRIMAL_SOLUTION_OUTPUT_PATH

    assert receipt.status == "succeeded"
    assert receipt.executor_ref == LOCAL_EXECUTOR_REF
    assert receipt.executable_ref == HIGHS_EXECUTABLE_REF
    assert receipt.exit_code == 0
    assert receipt.signal_number is None
    assert receipt.termination_reason == "exited"
    assert set(outputs) == {PRIMAL_SOLUTION_OUTPUT_PATH}
    primal_doc = json.loads(read_content(outputs[PRIMAL_SOLUTION_OUTPUT_PATH]).decode("utf-8"))
    assert primal_doc["math_status"] == "solved"
    objective = float(primal_doc["objective_value"])
    assert math.isfinite(objective)
    assert int(primal_doc["column_count"]) > 0
    assert len(primal_doc["values"]) == int(primal_doc["column_count"])
    assert receipt.outputs[0]["path"] == PRIMAL_SOLUTION_OUTPUT_PATH
    assert receipt.outputs[0]["size_bytes"] == len(read_content(outputs[PRIMAL_SOLUTION_OUTPUT_PATH]))
    stage_records = _parse_stage_logs(receipt.stderr)
    assert [(r["stage"], r["event"]) for r in stage_records] == [
        ("read_model", "start"),
        ("read_model", "end"),
        ("mutex_round", "end"),
        ("acceleration", "applied"),
    ]
    assert stage_records[1]["status"] == "ok"
    assert "applied=on_demand_mutex" in stage_records[3]["status"]
    assert all(
        math.isfinite(float(r["elapsed_seconds"])) and float(r["elapsed_seconds"]) >= 0.0
        for r in stage_records
        if r["event"] == "end"
    )


def _tiny_model(*, lower, upper, obj_coeff: float, rows: tuple = ()):
    model = LinearModel(
        variables=(LinearVariable(name="x", lower=lower, upper=upper),),
        constraints=rows,
        objective=LinearExpression(terms=((("x", 0), obj_coeff),)),
        sense="minimize",
    )
    text, _ = write_mps(model)
    return text


def test_native_time_limit_incumbent_is_written_without_claiming_optimality():
    """真实HiGHS已有全零可行解后触及时限；非伪造状态或工程长算。"""
    import highspy
    import numpy as np

    handle = highspy.Highs()
    handle.setOptionValue("output_flag", False)
    handle.setOptionValue("presolve", "off")
    n = 100
    indices = np.arange(n, dtype=np.int32)
    handle.addVars(n, np.zeros(n), np.ones(n))
    handle.changeColsIntegrality(n, indices, np.ones(n, dtype=np.uint8))
    handle.changeColsCost(n, indices, -np.arange(1, n + 1, dtype=float))
    rng = np.random.default_rng(42)
    for _ in range(50):
        handle.addRow(0., 100., n, indices, rng.integers(1, 10, size=n).astype(float))
    initial = highspy.HighsSolution()
    initial.col_value = [0.] * n
    initial.value_valid = True
    handle.setSolution(initial)
    handle.setOptionValue("time_limit", 1e-9)
    assert handle.run() == highspy.HighsStatus.kWarning
    status = handle.getModelStatus()
    assert status == highspy.HighsModelStatus.kTimeLimit
    shim = runpy.run_path(HIGHS_ADAPTER_PATH)
    facts = _parse_solution(shim["_solution_text"](handle, status))
    assert facts["has_feasible_solution"] == "true"
    assert float(facts["objective_value"]) == 0.
    assert len(facts["primals"]) == n
    assert facts["best_bound"] == facts["relative_gap"] == "none"


def test_native_optimal_mip_preserves_actual_bound_and_nonzero_gap():
    """kOptimal表示满足容差，不得把原生非零间隙伪写为0。"""
    import highspy
    import numpy as np

    handle = highspy.Highs()
    handle.setOptionValue("output_flag", False)
    handle.setOptionValue("presolve", "off")
    handle.setOptionValue("mip_rel_gap", 0.5)
    handle.setOptionValue("time_limit", 3.)
    n = 100
    indices = np.arange(n, dtype=np.int32)
    handle.addVars(n, np.zeros(n), np.ones(n))
    handle.changeColsIntegrality(n, indices, np.ones(n, dtype=np.uint8))
    handle.changeColsCost(n, indices, -np.arange(1, n + 1, dtype=float))
    rng = np.random.default_rng(42)
    for _ in range(50):
        handle.addRow(0., 100., n, indices, rng.integers(1, 10, size=n).astype(float))
    assert handle.run() == highspy.HighsStatus.kOk
    assert handle.getModelStatus() == highspy.HighsModelStatus.kOptimal
    info = handle.getInfo()
    assert 0 < info.mip_gap <= .5
    assert info.mip_dual_bound != handle.getObjectiveValue()
    shim = runpy.run_path(HIGHS_ADAPTER_PATH)
    facts = _parse_solution(shim["_solution_text"](handle, handle.getModelStatus()))
    assert float(facts["best_bound"]) == info.mip_dual_bound
    assert float(facts["relative_gap"]) == info.mip_gap


def test_real_shim_time_limit_without_incumbent_is_normal_exit(tmp_path):
    mps_text = _knapsack_mps(n=100, m=50, rhs=100.0, seed=42)
    (tmp_path / "problem.mps").write_text(mps_text, encoding="utf-8")
    completed = subprocess.run(
        [HIGHS_ADAPTER_PATH, "problem.mps", "--solution_file", "solution.sol",
         "--time_limit", "0.000000001", "--random_seed", "0"],
        cwd=tmp_path, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    sol_bytes = (tmp_path / "solution.sol").read_bytes()
    facts = _parse_solution(sol_bytes.decode("utf-8"))
    assert facts["model_status"] == "HighsModelStatus.kTimeLimit"
    assert facts["has_feasible_solution"] == "false"
    assert facts["objective_value"] == "none"
    assert facts["primals"] == []

    # Adapter mapping preserves exit-0 / completed / math_status=not_solved semantics.
    mps_bytes = (tmp_path / "problem.mps").read_bytes()
    bundle = _minimal_bundle(
        mps_bytes,
        time_limit=1,
        timeout_seconds=1 + PROCESS_IO_GRACE_SECONDS,
    )
    receipt = _synthetic_ok_receipt(bundle=bundle)
    adapted = _adapt(bundle, receipt, {SOLUTION_PATH: sol_bytes})
    assert adapted.execution_status == "completed"
    assert adapted.math_status == "not_solved"
    assert "solver_summary" in adapted.outputs
    assert "solution" not in adapted.outputs
    assert adapted.outputs["solver_summary"]["termination_reason"] == "time_limit"
    assert adapted.outputs["solver_summary"]["has_feasible_solution"] is False


def _ge_row(rhs: float):
    return (
        LinearConstraint(
            name="r1",
            expression=LinearExpression(terms=((("x", 0), 1.0),)),
            sense="ge",
            rhs=rhs,
        ),
    )


def _run_tiny_mps(tmp_path, mps_text: str) -> tuple[str, dict]:
    (tmp_path / "input").mkdir(exist_ok=True)
    (tmp_path / "output").mkdir(exist_ok=True)
    (tmp_path / "input" / "problem.mps").write_text(mps_text, encoding="utf-8")
    completed = subprocess.run(
        [
            HIGHS_ADAPTER_PATH, "input/problem.mps",
            "--solution_file", "output/solution.sol",
            "--time_limit", "60", "--random_seed", "0",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    content = (tmp_path / "output" / "solution.sol").read_text(encoding="utf-8")
    return completed.stderr, _parse_solution(content)



def test_real_highs_zero_export_with_unbounded_opposite(tmp_path) -> None:
    """A synthetic zero-export balance solves without a surplus mutex binary.

    The balance row remains independent of the tautological mutual exclusion.
    Docker CI provides the real pinned HiGHS executable.
    """
    from iesplan.computation.generators.linear import lower_mutex

    zero_mutex = lower_mutex(
        "no_simultaneous_flow", ("export", 0), ("import", 0),
        {"export": ("var", 0.0, 0.0), "import": ("var", 0.0, None)},
    )
    assert zero_mutex.variables == ()
    assert zero_mutex.constraints == ()
    model = LinearModel(
        variables=(
            LinearVariable("export", lower=0.0, upper=0.0),
            LinearVariable("import", lower=0.0, upper=None),
            LinearVariable("served", lower=3.0, upper=3.0),
        ),
        constraints=zero_mutex.constraints + (
            LinearConstraint(
                name="flow_balance",
                expression=LinearExpression(terms=(
                    (("import", 0), 1.0),
                    (("export", 0), -1.0),
                    (("served", 0), -1.0),
                )),
                sense="eq", rhs=0.0,
            ),
        ),
        objective=LinearExpression(terms=((("import", 0), 1.0),)),
    )
    assert [row.name for row in model.constraints] == ["flow_balance"]
    assert all(var.kind == "continuous" for var in model.variables)
    mps_text, _ = write_mps(model)
    assert "no_simultaneous_flow__mutex" not in mps_text
    _, facts = _run_tiny_mps(tmp_path, mps_text)
    assert facts["model_status"] == "HighsModelStatus.kOptimal"
    assert int(facts["num_columns"]) == 3
    assert facts["primals"] == pytest.approx([0.0, 3.0, 3.0])
    assert float(facts["objective_value"]) == pytest.approx(3.0)
    assert float(facts["best_bound"]) == pytest.approx(3.0)
    assert float(facts["relative_gap"]) == 0.


@pytest.mark.parametrize(
    "purchased,sold,status",
    [(3.0, 0.0, "kOptimal"), (0.0, 2.0, "kOptimal"),
     (0.0, 0.0, "kOptimal"), (1.0, 1.0, "kInfeasible")],
)
def test_catalog_grid_mutex_preserves_both_directions(tmp_path, purchased, sold, status):
    """Real catalog + lowerer + HiGHS: retain export, import and exclusion."""
    from iesplan.computation.generators import lower_relation

    document = next(d for d in list_devices() if d.device.id == "ies.device.grid_connection")
    built = build_math_contribution(document)
    assert built.ok, built.diagnostics
    (relation,) = built.contribution.relations
    lowered = lower_relation(
        relation,
        {"electricity_import": ("var", 0.0, 3.0), "electricity_export": ("var", 0.0, 2.0)},
    )
    model = LinearModel(
        variables=(
            LinearVariable("electricity_import", lower=purchased, upper=purchased),
            LinearVariable("electricity_export", lower=sold, upper=sold),
        ) + lowered.variables,
        constraints=lowered.constraints,
        objective=LinearExpression(terms=(
            (("electricity_import", 0), 7.0), (("electricity_export", 0), -4.0),
        )),
    )
    text, _ = write_mps(model)
    _, facts = _run_tiny_mps(tmp_path, text)
    assert facts["model_status"] == f"HighsModelStatus.{status}"
    if status == "kOptimal":
        assert float(facts["objective_value"]) == pytest.approx(7*purchased - 4*sold)


class TestUnboundedLowerFiniteUpperHighs:
    """无下界＋有限上界的 MPS 表达与真实 HiGHS 行为。

    writer 曾对 lower=None 输出 FR+UP，HiGHS 判“BOUNDS 重复定义”
    丢弃 UP 行导致上界丢失；现输出 MI+UP（-inf 下界＋有限上界）。
    只在 Docker 内运行（需真实求解器二进制）。
    """

    def test_writer_emits_mi_plus_up_for_unbounded_lower(self) -> None:
        text = _tiny_model(lower=None, upper=5.0, obj_coeff=-1.0)
        lines = [line.split() for line in text.splitlines()]
        bounds = [parts for parts in lines if len(parts) >= 3 and parts[1] == "BND1"]
        kinds = sorted(parts[0] for parts in bounds)
        assert kinds == ["MI", "UP"]
        up = [parts for parts in bounds if parts[0] == "UP"][0]
        assert up[2] == "x" and float(up[3]) == 5.0

    def test_writer_keeps_zero_upper_and_free(self) -> None:
        zero = _tiny_model(lower=None, upper=0.0, obj_coeff=1.0)
        zero_kinds = sorted(
            line.split()[0] for line in zero.splitlines() if "BND1" in line
        )
        assert zero_kinds == ["MI"]
        free, _ = write_mps(
            LinearModel(
                variables=(LinearVariable(name="x", lower=None, upper=None),),
                objective=LinearExpression(terms=((("x", 0), 1.0),)),
            )
        )
        assert " FR " in free and " UP " not in free

    def test_highs_caps_maximization_at_positive_upper(self, tmp_path) -> None:
        text = _tiny_model(lower=None, upper=5.0, obj_coeff=-1.0, rows=_ge_row(3.0))
        stderr, facts = _run_tiny_mps(tmp_path, text)
        assert "duplicate" not in stderr.lower()
        assert facts["model_status"] == "HighsModelStatus.kOptimal"
        assert facts["primals"] == [5.0]

    def test_highs_negative_upper_caps(self, tmp_path) -> None:
        text = _tiny_model(lower=None, upper=-2.0, obj_coeff=-1.0)
        _, facts = _run_tiny_mps(tmp_path, text)
        assert facts["model_status"] == "HighsModelStatus.kOptimal"
        assert facts["primals"] == [-2.0]

    def test_highs_downward_unbounded_without_lower(self, tmp_path) -> None:
        text = _tiny_model(lower=None, upper=5.0, obj_coeff=1.0)
        _, facts = _run_tiny_mps(tmp_path, text)
        assert facts["model_status"] == "HighsModelStatus.kUnbounded"


def _parse_stage_logs(stderr_text: str) -> list[dict[str, str]]:
    """提取 ies-highs-solver 写入 stderr 的结构化阶段日志。"""
    records: list[dict[str, str]] = []
    for line in stderr_text.splitlines():
        if not line.startswith("ies-highs-solver: "):
            continue
        tokens = line.removeprefix("ies-highs-solver: ").split()
        if not any(tok.startswith("stage=") for tok in tokens):
            continue
        entry: dict[str, str] = {}
        for tok in tokens:
            key, sep, value = tok.partition("=")
            if sep:
                entry[key] = value
        records.append(entry)
    return records


def _minimal_bundle(
    mps_bytes: bytes,
    *,
    time_limit: int = 5,
    timeout_seconds: int = 10,
) -> SolverBundle:
    """构造可由 SolverRuntime 与 ResultAdapter 消费的最小合法 Bundle。"""
    del time_limit, timeout_seconds
    return make_minimal_highs_bundle(mps_bytes)


def _make_delayed_wrapper(
    tmp_path: Path,
    *,
    read_delay: float = 0.0,
    write_delay: float = 0.0,
    write_residual: bool = False,
) -> LocalProcessProvider:
    """构造测试专属可执行替身：调用真实 ies-highs-solver 并在读写期制造可控延迟。"""
    wrapper = tmp_path / "delayed_highs_wrapper.py"
    script = f"""#!/usr/local/bin/python
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import time

import highspy

loader = importlib.machinery.SourceFileLoader("ies_highs_under_test", {HIGHS_ADAPTER_PATH!r})
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

is_solve = len(sys.argv) == 8 and sys.argv[1] not in ("--version", "--self-test")
if is_solve:
    real_highs = highspy.Highs
    orig_solution_text = mod._solution_text

    class _WrappedHighs:
        def __init__(self) -> None:
            self._inner = real_highs()

        def readModel(self, path: str):
            if {read_delay!r} > 0:
                time.sleep({read_delay!r})
            return self._inner.readModel(path)

        def setOptionValue(self, key: str, value):
            if key == "time_limit":
                sys.stderr.write(f"test-wrapper: observed_time_limit={{value!r}}\\n")
                sys.stderr.flush()
            return self._inner.setOptionValue(key, value)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    def _wrapped_solution_text(handle, status) -> str:
        if {write_residual!r}:
            sol_path = sys.argv[3]
            with open(sol_path, "w", encoding="utf-8") as handle_out:
                handle_out.write(
                    "HIGHS_SOLUTION_V1\\n"
                    "model_status: HighsModelStatus.kOptimal\\n"
                    "has_feasible_solution: true\\n"
                    "objective_value: 0.0\\n"
                    "best_bound: 0.0\\n"
                    "relative_gap: 0.0\\n"
                    "num_columns: 1\\n"
                    "primal:\\n"
                    "0: 0.0\\n"
                )
            with open("output/primal_solution.json", "w", encoding="utf-8") as pj:
                pj.write('{{"schema":"ies.primal-solution","schema_version":"1.0.0","math_status":"solved","termination_reason":"optimal","has_feasible_solution":true,"column_count":1,"values":[0.0],"objective_value":0.0,"best_bound":0.0,"relative_gap":0.0,"receipt":{{"solver":"ies.solver.highs@1.7.2","solve_domain":"original","original_model_verified":true}},"extensions":{{}}}}')
        if {write_delay!r} > 0:
            time.sleep({write_delay!r})
        return orig_solution_text(handle, status)

    highspy.Highs = _WrappedHighs
    mod._solution_text = _wrapped_solution_text

rc = mod.main(sys.argv[1:])
if rc == 0 and "--solution_file" in sys.argv[1:]:
    import json
    from pathlib import Path
    if {str(HERE)!r} not in sys.path:
        sys.path.insert(0, {str(HERE)!r})
    from legacy_solve_testkit import v1_sol_to_primal_json
    sargs = sys.argv[1:]
    sol_p = Path(sargs[sargs.index("--solution_file") + 1])
    if sol_p.is_file():
        primal_out = json.dumps(v1_sol_to_primal_json(sol_p.read_text(encoding="utf-8")))
        Path("output/primal_solution.json").write_text(primal_out, encoding="utf-8")
sys.exit(rc)
"""
    wrapper.write_text(script, encoding="utf-8")
    wrapper.chmod(0o755)
    return LocalProcessProvider(
        _descriptor(),
        (
            LocalExecutable(
                ref=HIGHS_EXECUTABLE_REF,
                path=str(wrapper),
                readiness=_readiness(),
            ),
        ),
        base_environ={},
    )


def _policy(*, max_timeout_seconds: float) -> DeploymentPolicy:
    return DeploymentPolicy(
        executables=(HIGHS_EXECUTABLE_REF,),
        environment_vars=(),
        max_timeout_seconds=max_timeout_seconds,
        max_memory_mib=4096,
        max_cpu_cores=4,
        allow_network=False,
    )


def test_deployment_policy_timeout_boundary_via_runtime(monkeypatch) -> None:
    """完整进程预算等于部署上限时正常执行，超过 1 秒则在启动前拒绝且不裁剪时限。"""
    artifact, models, materialized, finance, base_config = _campus_inputs()
    config = CalculationConfig(
        mode=base_config.mode,
        generator=base_config.generator,
        solver=base_config.solver,
        precision=base_config.precision,
        time_limit_seconds=30,
        outputs=list(base_config.outputs),
        seed=base_config.seed,
    )
    provider = build_registry((HIGHS_MILP_PROVIDER,)).resolve(config)
    bundle = provider.generate(artifact, models, materialized, finance, config, _campus_evaluation())
    expected_timeout = 30 + PROCESS_IO_GRACE_SECONDS
    cmd = _command(time_limit=30, timeout_seconds=expected_timeout)

    # 边界放行：max_timeout_seconds == 630
    receipt_ok, outputs_ok = _runtime(policy=_policy(max_timeout_seconds=expected_timeout)).run(
        bundle, _attempt(), command=cmd
    )
    from iesplan.computation import PRIMAL_SOLUTION_OUTPUT_PATH

    assert receipt_ok.status == "succeeded"
    assert receipt_ok.exit_code == 0
    assert PRIMAL_SOLUTION_OUTPUT_PATH in outputs_ok

    # 超限拒绝：max_timeout_seconds == 629，启动前拒绝，零 Popen 调用，不裁剪用户求解时限
    rej_runtime = _runtime(policy=_policy(max_timeout_seconds=expected_timeout - 1))
    popen_calls: list[object] = []
    real_popen = subprocess.Popen

    def _spy_popen(*args_p, **kwargs_p):
        popen_calls.append(args_p)
        return real_popen(*args_p, **kwargs_p)

    monkeypatch.setattr(subprocess, "Popen", _spy_popen)
    receipt_rej, outputs_rej = rej_runtime.run(bundle, _attempt(), command=cmd)
    assert receipt_rej.status == "policy_rejected"
    assert str(expected_timeout - 1) in receipt_rej.error
    assert outputs_rej == {}
    assert popen_calls == []


def test_stage_timing_logs_cover_success_read_and_write_failures(tmp_path: Path) -> None:
    """stderr 分阶段记录单调耗时，并可区分正常结束、读模失败和写解失败。"""
    mps_text = _tiny_model(lower=0.0, upper=5.0, obj_coeff=1.0)
    (tmp_path / "problem.mps").write_text(mps_text, encoding="utf-8")

    # 正常完成：read_model -> run -> solution_write 三阶段均写 start/end 与 status=ok
    ok_proc = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            "problem.mps",
            "--solution_file",
            "solution.sol",
            "--time_limit",
            "5",
            "--random_seed",
            "0",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert ok_proc.returncode == 0, ok_proc.stderr
    ok_records = _parse_stage_logs(ok_proc.stderr)
    assert [(r["stage"], r["event"]) for r in ok_records] == [
        ("read_model", "start"),
        ("read_model", "end"),
        ("run", "start"),
        ("run", "end"),
        ("solution_write", "start"),
        ("solution_write", "end"),
    ]
    for rec in ok_records:
        if rec["event"] == "end":
            assert rec["status"] == "ok"
            elapsed = float(rec["elapsed_seconds"])
            assert math.isfinite(elapsed) and elapsed >= 0.0

    # 读模失败：read_model 报 status=error，不进入 run/solution_write
    err_read = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            "missing.mps",
            "--solution_file",
            "missing_out.sol",
            "--time_limit",
            "5",
            "--random_seed",
            "0",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert err_read.returncode == 2
    assert not (tmp_path / "missing_out.sol").exists()
    read_records = _parse_stage_logs(err_read.stderr)
    assert [(r["stage"], r["event"]) for r in read_records] == [
        ("read_model", "start"),
        ("read_model", "end"),
    ]
    assert read_records[1]["status"] == "error"
    assert float(read_records[1]["elapsed_seconds"]) >= 0.0

    # 写解失败：read_model/run 为 ok，solution_write 报 status=error
    err_write = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            "problem.mps",
            "--solution_file",
            "missing_dir/solution.sol",
            "--time_limit",
            "5",
            "--random_seed",
            "0",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert err_write.returncode == 2
    write_records = _parse_stage_logs(err_write.stderr)
    assert [(r["stage"], r["event"]) for r in write_records] == [
        ("read_model", "start"),
        ("read_model", "end"),
        ("run", "start"),
        ("run", "end"),
        ("solution_write", "start"),
        ("solution_write", "end"),
    ]
    assert [r["status"] for r in write_records if r["event"] == "end"] == ["ok", "ok", "error"]


def test_delayed_read_and_write_consume_process_grace_while_keeping_solve_limit(
    tmp_path: Path,
) -> None:
    """可控读模和写解延迟占用外层宽限；求解器收到的 time_limit 仍保持原样。"""
    from iesplan.computation import PRIMAL_SOLUTION_OUTPUT_PATH

    mps_bytes = _tiny_model(lower=1.0, upper=5.0, obj_coeff=2.0).encode("utf-8")
    bundle = _minimal_bundle(mps_bytes, time_limit=1, timeout_seconds=10)
    provider = _make_delayed_wrapper(tmp_path, read_delay=0.6, write_delay=0.6)
    receipt, outputs = _runtime(provider=provider, policy=_policy(max_timeout_seconds=10)).run(
        bundle, _attempt(), command=_command(time_limit=1, timeout_seconds=10)
    )
    assert receipt.status == "succeeded", receipt.stderr
    assert receipt.exit_code == 0
    assert PRIMAL_SOLUTION_OUTPUT_PATH in outputs
    assert "test-wrapper: observed_time_limit=1.0" in receipt.stderr

    records = _parse_stage_logs(receipt.stderr)
    ends = {r["stage"]: r for r in records if r["event"] == "end"}
    assert set(ends) == {"read_model", "run", "solution_write"}
    assert all(r["status"] == "ok" for r in ends.values())
    assert float(ends["read_model"]["elapsed_seconds"]) >= 0.5
    assert float(ends["solution_write"]["elapsed_seconds"]) >= 0.5


def test_excessive_read_delay_triggers_outer_timeout_and_logs_stage(tmp_path: Path) -> None:
    """超过外层宽限的读模延迟仍触发真实监督超时，日志可定位中断在 read_model。"""
    mps_bytes = _tiny_model(lower=1.0, upper=5.0, obj_coeff=2.0).encode("utf-8")
    bundle = _minimal_bundle(mps_bytes, time_limit=5, timeout_seconds=2)
    provider = _make_delayed_wrapper(tmp_path, read_delay=10.0)
    receipt, outputs = _runtime(provider=provider, policy=_policy(max_timeout_seconds=10)).run(
        bundle, _attempt(), command=_command(time_limit=5, timeout_seconds=2)
    )
    assert receipt.status == "timed_out"
    assert receipt.termination_reason == "timeout"
    assert outputs == {}
    records = _parse_stage_logs(receipt.stderr)
    assert ("read_model", "start") in [(r["stage"], r["event"]) for r in records]
    assert ("read_model", "end") not in [(r["stage"], r["event"]) for r in records]
    assert all(r["stage"] != "run" for r in records)

    adapted = _adapt(bundle, receipt, outputs)
    assert adapted.execution_status == "timed_out"
    assert adapted.math_status == "not_solved"
    assert "solution" not in adapted.outputs
    assert adapted.outputs["solver_summary"]["has_feasible_solution"] is False


def test_excessive_write_delay_with_residual_file_never_upgrades(tmp_path: Path) -> None:
    """写出阶段超时即便留下残留解文件，也保持 timed_out 且绝不升级为成功。"""
    mps_bytes = _tiny_model(lower=1.0, upper=5.0, obj_coeff=2.0).encode("utf-8")
    bundle = _minimal_bundle(mps_bytes, time_limit=5, timeout_seconds=2)
    provider = _make_delayed_wrapper(tmp_path, write_delay=10.0, write_residual=True)
    receipt, outputs = _runtime(provider=provider, policy=_policy(max_timeout_seconds=10)).run(
        bundle, _attempt(), command=_command(time_limit=5, timeout_seconds=2)
    )
    assert receipt.status == "timed_out"
    assert receipt.termination_reason == "timeout"
    records = _parse_stage_logs(receipt.stderr)
    events = [(r["stage"], r["event"]) for r in records]
    assert ("read_model", "end") in events
    assert ("run", "end") in events
    assert ("solution_write", "start") in events
    assert ("solution_write", "end") not in events

    # 即便 SolverRuntime 采回磁盘上残存的解文件，ResultAdapter 也不升级为成功。
    from iesplan.computation import PRIMAL_SOLUTION_OUTPUT_PATH

    assert PRIMAL_SOLUTION_OUTPUT_PATH in outputs
    adapted = _adapt(bundle, receipt, outputs)
    assert adapted.execution_status == "timed_out"
    assert adapted.math_status == "not_solved"
    assert "solution" not in adapted.outputs
    assert adapted.outputs["solver_summary"]["has_feasible_solution"] is False


@pytest.mark.parametrize("reason", ["cancelled", "fenced"])
def test_outer_cancel_or_fence_during_delayed_read_preserves_status_and_stage_log(
    tmp_path: Path,
    reason: str,
) -> None:
    """运行中取消或租约隔离正确终止整组、定位中断阶段且不升级残留解。"""
    mps_bytes = _tiny_model(lower=1.0, upper=5.0, obj_coeff=2.0).encode("utf-8")
    bundle = _minimal_bundle(mps_bytes, time_limit=5, timeout_seconds=30)
    provider = _make_delayed_wrapper(tmp_path, read_delay=10.0)
    flag = threading.Event()
    timer = threading.Timer(0.4, flag.set)
    timer.start()
    try:
        receipt, outputs = _runtime(
            provider=provider, policy=_policy(max_timeout_seconds=30)
        ).run(
            bundle,
            _attempt(),
            command=_command(time_limit=5, timeout_seconds=30),
            stop=lambda: reason if flag.is_set() else None,
        )
    finally:
        timer.cancel()
    assert receipt.status == reason
    assert receipt.termination_reason == reason
    assert outputs == {}
    records = _parse_stage_logs(receipt.stderr)
    events = [(r["stage"], r["event"]) for r in records]
    assert ("read_model", "start") in events
    assert ("read_model", "end") not in events
    adapted = _adapt(bundle, receipt, outputs)
    assert adapted.execution_status == reason
    assert adapted.math_status == "not_solved"
    assert "solution" not in adapted.outputs
    assert adapted.outputs["solver_summary"]["has_feasible_solution"] is False


def _knapsack_mps(*, n: int, m: int, rhs: float, seed: int) -> str:
    """用统一 write_mps 格式生成确定性二维多背包 0-1 MIP（x=0 恒可行）。"""
    import numpy as np

    rng = np.random.default_rng(seed)
    vars_ = tuple(
        LinearVariable(name=f"x{i:03d}", lower=0.0, upper=1.0, kind="binary")
        for i in range(n)
    )
    rows = []
    for row_idx in range(m):
        weights = rng.integers(1, 10, size=n)
        terms = tuple(((f"x{i:03d}", 0), float(weights[i])) for i in range(n))
        rows.append(
            LinearConstraint(
                name=f"cap_{row_idx:03d}",
                expression=LinearExpression(terms=terms),
                sense="le",
                rhs=rhs,
            )
        )
    obj_terms = tuple(((f"x{i:03d}", 0), -float(i + 1)) for i in range(n))
    model = LinearModel(
        variables=vars_,
        constraints=tuple(rows),
        objective=LinearExpression(terms=obj_terms),
        sense="minimize",
    )
    text, _ = write_mps(model)
    return text


def test_native_time_limit_with_incumbent_end_to_end_via_runtime() -> None:
    """真实 HiGHS 在 1 秒原生时限内带可行解正常退出并经 ResultAdapter 映射为 feasible。"""
    mps_bytes = _knapsack_mps(n=240, m=80, rhs=120.0, seed=7).encode("utf-8")

    total_budget = 1 + PROCESS_IO_GRACE_SECONDS
    bundle = _minimal_bundle(mps_bytes, time_limit=1, timeout_seconds=total_budget)
    receipt, outputs = _runtime(policy=_policy(max_timeout_seconds=total_budget)).run(
        bundle, _attempt(), command=_command(time_limit=1, timeout_seconds=total_budget)
    )
    from iesplan.computation import PRIMAL_SOLUTION_OUTPUT_PATH

    assert receipt.status == "succeeded", receipt.stderr
    assert receipt.exit_code == 0
    assert PRIMAL_SOLUTION_OUTPUT_PATH in outputs
    primal_doc = json.loads(read_content(outputs[PRIMAL_SOLUTION_OUTPUT_PATH]).decode("utf-8"))
    assert primal_doc["termination_reason"] == "time_limit"
    assert primal_doc["has_feasible_solution"] is True

    adapted = _adapt(bundle, receipt, outputs)
    assert adapted.execution_status == "completed"
    assert adapted.math_status == "feasible"
    summary = adapted.outputs["solver_summary"]
    assert summary["termination_reason"] == "time_limit"
    assert summary["has_feasible_solution"] is True
    assert math.isfinite(summary["objective_value"])
    assert math.isfinite(summary["best_bound"])
    assert math.isfinite(summary["relative_gap"])
    assert summary["relative_gap"] > 0.0
    assert adapted.outputs["solution"]["objective_value"] == summary["objective_value"]
