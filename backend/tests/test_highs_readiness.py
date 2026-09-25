"""0.9-E：真实 HiGHS 绑定、精确版本 readiness 与最小 Bundle 集成。

只在 Docker 内运行。``HighsMilpProvider`` 五项直接输入生成真实
Bundle，经 executor registry + ``DeploymentPolicy`` +
``SolverRuntime`` 执行，产出声明的真实原始 solution 文件。不实现
ResultAdapter，不解释求解业务状态；不声称网络/CPU/内存/OOM/身份
隔离（留 0.9-F）。
"""

from __future__ import annotations

import json
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
from iesplan.computation.runtime import validate_highs_arguments
from iesplan.devices import init_registry, list_devices
from iesplan.finance import effective_from_yaml
from iesplan.modeling import build_math_contribution

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
SAMPLES = BACKEND_ROOT / "iesplan" / "assembly" / "samples"

EXECUTOR_ID = "ies.executor.local_process"
EXECUTOR_VERSION = "1.0.0"


def _campus_inputs():
    """真实 campus 五项直接输入（与 generator 测试同一来源）。"""
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
    mat = materialize_non_predictive_series(artifact, resources)
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
    return artifact, models, mat.series, finance, config


def _campus_bundle():
    """正式路径生成真实 Bundle（非手写 manifest）。"""
    artifact, models, series, finance, config = _campus_inputs()
    provider = build_registry((HIGHS_MILP_PROVIDER,)).resolve(config)
    assert provider is HIGHS_MILP_PROVIDER
    return provider.generate(artifact, models, series, finance, config)


def _readiness(**kwargs) -> ExecutableReadiness:
    kwargs.setdefault("version", HIGHS_VERSION)
    kwargs.setdefault("self_test", ("--self-test",))
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
    with pytest.raises(ComputationUnavailableError):
        build_executor_registry([_provider(), bad_version])
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
    assert manifest.command["executable"] == HIGHS_EXECUTABLE_REF
    mps_bytes = bundle.files[PROBLEM_PATH]
    assert mps_bytes.startswith(b"NAME") and len(mps_bytes) > 100
    receipt, outputs = _runtime().run(bundle, _attempt())
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
