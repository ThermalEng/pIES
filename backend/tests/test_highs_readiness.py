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
import subprocess
from pathlib import Path

import pytest

from iesplan.assembly import validate_assembly_text
from iesplan.computation import (
    HIGHS_ADAPTER_PATH,
    HIGHS_EXECUTABLE_REF,
    HIGHS_MILP_PROVIDER,
    HIGHS_VERSION,
    AttemptContext,
    CalculationConfig,
    ComputationUnavailableError,
    DeploymentPolicy,
    ExecutableReadiness,
    ExecutorDescriptor,
    LocalExecutable,
    LocalProcessProvider,
    SolverRuntime,
    build_executor_registry,
    build_registry,
    materialize_non_predictive_series,
)
from iesplan.computation.generators.highs_milp import (
    EXECUTOR_REF,
    GENERATOR_REF,
    PROBLEM_PATH,
    SOLUTION_PATH,
    SOLVER_REF,
)
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
)
from iesplan.computation.generators.mps import write_mps
from iesplan.computation.runtime import validate_highs_arguments
from iesplan.core.yamlmini import dump as yaml_dump
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices import init_registry, list_devices
from iesplan.finance import effective_from_yaml
from iesplan.modeling import build_math_contribution

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
SAMPLES = BACKEND_ROOT / "iesplan" / "assembly" / "samples"

EXECUTOR_ID = "ies.executor.local_process"
EXECUTOR_VERSION = "1.0.0"


def _campus_inputs(*, dispatch=True):
    """真实 campus 直接输入（与 generator 测试同一来源）。"""
    init_registry()
    text = (SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    if not dispatch:
        source = yaml_load(text)
        source["devices"]["grid"]["properties"]["max_export_power_kw"]["value"] = 0
        source["devices"]["bat1"]["properties"]["capacity_kwh"]["value"] = 0
        source["devices"]["bat1"]["properties"]["rated_power_kw"]["value"] = 0
        text = yaml_dump(source)
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


def _campus_bundle(*, dispatch=True):
    """正式路径生成真实 Bundle（非手写 manifest）。"""
    artifact, models, materialized, finance, config = _campus_inputs(dispatch=dispatch)
    provider = build_registry((HIGHS_MILP_PROVIDER,)).resolve(config)
    assert provider is HIGHS_MILP_PROVIDER
    return provider.generate(artifact, models, materialized, finance, config, _campus_evaluation())


def _readiness(**kwargs) -> ExecutableReadiness:
    kwargs.setdefault("version", HIGHS_VERSION)
    kwargs.setdefault("self_test", ("--self-test",))
    kwargs.setdefault("output_path", "self-test.sol")
    return ExecutableReadiness(**kwargs)


def _binding(**kwargs) -> LocalExecutable:
    kwargs.setdefault("ref", HIGHS_EXECUTABLE_REF)
    kwargs.setdefault("path", HIGHS_ADAPTER_PATH)
    kwargs.setdefault("argv_validator", validate_highs_arguments)
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


def _runtime(provider=None) -> SolverRuntime:
    provider = provider if provider is not None else _provider()
    policy = DeploymentPolicy(
        executables=(HIGHS_EXECUTABLE_REF,),
        environment_vars=(),
        max_timeout_seconds=600,
        max_memory_mib=4096,
        max_cpu_cores=4,
        allow_network=False,
        max_output_files=16,
        # campus 真实 solution 约 1.7MB；部署按实际输出规模定配额。
        max_output_bytes=8_388_608,
    )
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
    """固定路径可执行并原样报告精确版本 1.7.2。"""
    completed = subprocess.run(
        [HIGHS_ADAPTER_PATH, "--version"],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout.decode("utf-8").strip() == "1.7.2"


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
    assert registry.resolve(EXECUTOR_REF, HIGHS_EXECUTABLE_REF).ref == EXECUTOR_REF
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
                argv_validator=validate_highs_arguments,
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


def test_end_to_end_campus_bundle(campus_dispatch_case) -> None:
    """campus 真实 Bundle 经受控执行产出声明 solution 文件。"""
    bundle = _campus_bundle(dispatch=campus_dispatch_case)
    manifest = bundle.manifest
    assert manifest.command["executable"] == HIGHS_EXECUTABLE_REF
    mps_bytes = bundle.files[PROBLEM_PATH]
    assert mps_bytes.startswith(b"NAME") and len(mps_bytes) > 100
    receipt, outputs = _runtime().run(bundle, _attempt())
    if receipt.status != "succeeded":
        logging.getLogger("pies.ci.highs_readiness").warning(
            "CI_SOLVER readiness status=%s reason=%s stdout_head=%r stdout_tail=%r stderr=%r",
            receipt.status, receipt.termination_reason,
            receipt.stdout[:1500], receipt.stdout[-6500:], receipt.stderr[-1500:],
        )
    assert receipt.status == "succeeded"
    assert receipt.executor_ref == EXECUTOR_REF
    assert receipt.executable_ref == HIGHS_EXECUTABLE_REF
    assert receipt.exit_code == 0
    assert receipt.signal_number is None
    assert receipt.termination_reason == "exited"
    assert set(outputs) == {SOLUTION_PATH}
    content = outputs[SOLUTION_PATH].decode("utf-8")
    facts = _parse_solution(content)
    assert facts["model_status"] == "HighsModelStatus.kOptimal"
    objective = float(facts["objective_value"])
    assert math.isfinite(objective)
    assert int(facts["num_columns"]) > 0
    assert len(facts["primals"]) == int(facts["num_columns"])
    assert receipt.outputs[0]["path"] == SOLUTION_PATH
    assert receipt.outputs[0]["size_bytes"] == len(outputs[SOLUTION_PATH])


def _tiny_model(*, lower, upper, obj_coeff: float, rows: tuple = ()):
    model = LinearModel(
        variables=(LinearVariable(name="x", lower=lower, upper=upper),),
        constraints=rows,
        objective=LinearExpression(terms=((("x", 0), obj_coeff),)),
        sense="minimize",
    )
    text, _ = write_mps(model)
    return text


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
