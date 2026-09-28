"""ies.result-adapter.highs@1.0.0 聚焦行为测试。

夹具均为 Docker 内 HiGHS 1.7.2 真实产出（highspy 1.7.2 同版本库，
``writeSolution(path, 0)`` 即 CLI ``--solution_file`` 默认 raw 风格，
断言前已逐字节核对）：
- ``REAL_OPTIMAL``：具名 MPS（min x+2y s.t. x+y>=3000）最优解；
- ``REAL_INFEASIBLE``：``x in [0,1]`` 且 ``x == 2``；
- ``REAL_UNBOUNDED``：``min -x, x >= 0``；
- ``REAL_TIME_LIMIT``：同一小模型 ``time_limit=1e-9`` 强制超时。
"""

from __future__ import annotations

import json

import pytest

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    ComputeResult,
    ExecutionReceipt,
    SolverBundle,
    SolverBundleManifest,
)
from iesplan.computation.result_adapter import build_result_adapter_registry
from iesplan.computation.result_adapters.highs import (
    ADAPTER_REF,
    HIGHS_ADAPTER,
    HighsResultAdapter,
)

PROBLEM_PATH = "input/problem.mps"
PARAMETERS_PATH = "input/parameters.json"
SOLUTION_PATH = "output/solution.sol"

#: 真实最优解（最优 x=3000, y=0，目标 3000；列名透传 MPS 名）。
REAL_OPTIMAL = (
    """Model status
Optimal

# Primal solution values
Feasible
Objective 3000
# Columns 2
x 3000
y 0
# Rows 1
demand 3000

# Dual solution values
Feasible
# Columns 2
x 0
y 1
# Rows 1
demand 1

# Basis
HiGHS v1
Valid
# Columns 2
"""
    "1 0 \n"
    """# Rows 1
"""
    "0 \n"
)

#: 真实不可行运行的 solution（无主解、无对偶、无基址）。
REAL_INFEASIBLE = """Model status
Infeasible

# Primal solution values
None

# Dual solution values
None

# Basis
HiGHS v1
None
"""

#: 真实无界运行的 solution（注意对偶节为 Infeasible，行数为 0）。
REAL_UNBOUNDED = (
    """Model status
Unbounded

# Primal solution values
Feasible
Objective 0
# Columns 1
C0 0
# Rows 0

# Dual solution values
Infeasible
# Columns 1
C0 -1
# Rows 0

# Basis
HiGHS v1
Valid
# Columns 1
"""
    "0 \n"
    """# Rows 0

"""
)

#: 真实超时运行的 solution（time_limit=1e-9；无解块）。
REAL_TIME_LIMIT = """Model status
Time limit reached

# Primal solution values
None

# Dual solution values
None

# Basis
HiGHS v1
None
"""


def _parameters(
    unit_map: dict | None = None,
    offset: object = 1.5,
    outputs: object = None,
) -> bytes:
    doc = {
        "assembly": {"id": "assembly-7", "schema_version": "2.0.0"},
        "generator": "ies.generator.highs_milp@1.0.0",
        "objective_offset": offset,
        "outputs": ["solution", "metrics"] if outputs is None else outputs,
        "precision": "medium",
        "result_adapter": ADAPTER_REF,
        "seed": 0,
        "solver": "ies.solver.highs@1.7.2",
        "unit_map": {"x": "kW", "y": "kW"} if unit_map is None else unit_map,
    }
    return (json.dumps(doc, sort_keys=True) + "\n").encode("utf-8")


def _bundle(files: dict | None = None) -> SolverBundle:
    if files is None:
        files = {PARAMETERS_PATH: _parameters()}
    manifest = SolverBundleManifest(
        bundle={
            "generator": "ies.generator.highs_milp@1.0.0",
            "solver": "ies.solver.highs@1.7.2",
        },
        inputs=[{"path": path, "media_type": "application/octet-stream"} for path in sorted(files)],
        command={
            "executor": "ies.executor.local_process@1.0.0",
            "executable": "ies.solver.highs@1.7.2",
            "arguments": [],
            "working_directory": ".",
            "success_exit_codes": [0],
        },
        outputs=[
            {
                "path": SOLUTION_PATH,
                "media_type": "text/plain",
                "required": True,
            }
        ],
        result_adapter=ADAPTER_REF,
        extensions={},
    )
    return SolverBundle(manifest=manifest, files=files)


def _receipt(**kwargs) -> ExecutionReceipt:
    kwargs.setdefault("receipt_id", "receipt-7")
    kwargs.setdefault("bundle_id", "bundle-7")
    kwargs.setdefault("assembly_id", "assembly-7")
    kwargs.setdefault("calculation_config_id", "config-7")
    kwargs.setdefault("attempt_id", "attempt-7")
    kwargs.setdefault("generator_ref", "ies.generator.highs_milp@1.0.0")
    kwargs.setdefault("solver_ref", "ies.solver.highs@1.7.2")
    kwargs.setdefault("executor_ref", "ies.executor.local_process@1.0.0")
    kwargs.setdefault("executable_ref", "ies.solver.highs@1.7.2")
    kwargs.setdefault("result_adapter_ref", ADAPTER_REF)
    kwargs.setdefault("status", "succeeded")
    kwargs.setdefault("exit_code", 0)
    kwargs.setdefault("termination_reason", "exited")
    kwargs.setdefault("started_at", "2026-09-24T00:00:00Z")
    kwargs.setdefault("ended_at", "2026-09-24T00:00:01Z")
    return ExecutionReceipt(**kwargs)


def _adapt(bundle_files, receipt, solution: bytes | None) -> ComputeResult:
    outputs = {} if solution is None else {SOLUTION_PATH: solution}
    return HIGHS_ADAPTER.adapt(_bundle(bundle_files), receipt, outputs)


def test_identity_and_registry() -> None:
    """公开身份与独立原子注册（精确引用选中，无回退）。"""
    assert isinstance(HIGHS_ADAPTER, HighsResultAdapter)
    assert HIGHS_ADAPTER.ref == ADAPTER_REF == "ies.result-adapter.highs@1.0.0"
    assert HIGHS_ADAPTER.descriptor.ref == ADAPTER_REF
    assert HIGHS_ADAPTER.available() is True
    registry = build_result_adapter_registry([HIGHS_ADAPTER])
    assert registry.resolve(ADAPTER_REF) is HIGHS_ADAPTER
    with pytest.raises(ComputationUnavailableError) as exc_info:
        registry.resolve("ies.result-adapter.other@1.0.0")
    assert exc_info.value.reason == "no-result-adapter"


def test_optimal_values_units_and_offset() -> None:
    """真实最优解：列名映射换算到业务单位，目标加回 offset，金额 CNY。"""
    result = _adapt(None, _receipt(), REAL_OPTIMAL.encode("utf-8"))
    assert result.execution_status == "completed"
    assert result.math_status == "solved"
    assert result.bundle_id == "bundle-7"
    assert result.receipt_id == "receipt-7"
    assert set(result.outputs) == {"solution", "metrics"}
    solution = result.outputs["solution"]
    assert solution["objective_value"] == 3001.5
    assert solution["objective_unit"] == "CNY"
    assert [dict(item) for item in solution["variables"]] == [
        {"name": "x", "value": 3.0, "unit": "kW"},
        {"name": "y", "value": 0.0, "unit": "kW"},
    ]
    metrics = result.outputs["metrics"]
    assert metrics["model_status"] == "Optimal"
    assert metrics["objective_value"] == 3001.5
    assert metrics["objective_unit"] == "CNY"
    assert metrics["num_variables"] == 2


def test_requested_subset_only() -> None:
    """只输出已请求的键；未知请求明确拒绝。"""
    files = {PARAMETERS_PATH: _parameters(outputs=["solution"])}
    result = _adapt(files, _receipt(), REAL_OPTIMAL.encode("utf-8"))
    assert set(result.outputs) == {"solution"}
    files = {PARAMETERS_PATH: _parameters(outputs=["metrics"])}
    result = _adapt(files, _receipt(), REAL_OPTIMAL.encode("utf-8"))
    assert set(result.outputs) == {"metrics"}
    files = {PARAMETERS_PATH: _parameters(outputs=["solution", "nope"])}
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(files, _receipt(), REAL_OPTIMAL.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"


def test_infeasible_and_unbounded_have_no_business_outputs() -> None:
    """真实不可行/无界：数学状态明确，不发布业务输出。"""
    for text, math in (
        (REAL_INFEASIBLE, "infeasible"),
        (REAL_UNBOUNDED, "unbounded"),
    ):
        result = _adapt(None, _receipt(), text.encode("utf-8"))
        assert result.execution_status == "completed"
        assert result.math_status == math
        assert dict(result.outputs) == {}
        assert result.receipt_id == "receipt-7"


def test_all_receipt_statuses_map() -> None:
    """七种回执状态真实映射：OOM 拆分，其余各归其位，无伪业务输出。"""
    cases = [
        (
            {
                "status": "policy_rejected",
                "exit_code": None,
                "termination_reason": "",
                "started_at": "",
                "ended_at": "",
            },
            "rejected",
        ),
        (
            {"status": "process_failed", "termination_reason": "oom", "exit_code": None, "signal_number": 9},
            "resource_exhausted",
        ),
        (
            {"status": "process_failed", "termination_reason": "exited", "exit_code": 2},
            "failed",
        ),
        ({"status": "output_protocol_failed"}, "protocol_failed"),
        (
            {"status": "timed_out", "termination_reason": "timeout", "exit_code": None, "signal_number": 15},
            "timed_out",
        ),
        (
            {
                "status": "cancelled",
                "termination_reason": "cancelled",
                "exit_code": None,
                "signal_number": 15,
            },
            "cancelled",
        ),
        (
            {"status": "fenced", "termination_reason": "fenced", "exit_code": None, "signal_number": 15},
            "fenced",
        ),
    ]
    for facts, expected in cases:
        result = _adapt(None, _receipt(**facts), None)
        assert result.execution_status == expected
        assert result.math_status == "not_solved"
        assert dict(result.outputs) == {}
        assert result.receipt_id == "receipt-7"


def test_time_limit_file_is_explicit_error() -> None:
    """文件自报时间限制：明确报错，不接受部分解当成功。"""
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(None, _receipt(), REAL_TIME_LIMIT.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"
    assert "时间限制" in str(exc_info.value)


def test_corrupt_and_unknown_inputs_fail() -> None:
    """格式损坏、未知状态、非有限、缺失一律协议失败，不伪成功。"""
    bad_solutions = [
        b"garbage",
        # 旧版 V1 格式在 1.7.2 下不存在，出现即拒收。
        b"HIGHS_SOLUTION_V1\nmodel_status: HighsModelStatus.kOptimal\n",
        REAL_OPTIMAL.replace("Optimal", "kOptimal").encode(),
        REAL_OPTIMAL.replace("Objective 3000", "Objective nan").encode(),
        REAL_OPTIMAL.replace("Objective 3000", "Objective inf").encode(),
        REAL_OPTIMAL.replace("x 3000", "x inf").encode(),
        REAL_OPTIMAL.replace("x 3000", "x 0x10").encode(),
        REAL_OPTIMAL.replace("# Columns 2", "# Columns -2").encode(),
        REAL_OPTIMAL.replace("x 3000\ny 0", "x 3000").encode(),
        REAL_OPTIMAL.replace("x 3000\ny 0", "x 3000\nx 1").encode(),
        "\n".join(REAL_OPTIMAL.splitlines()[:-3]).encode(),
        (REAL_OPTIMAL + "trailing junk\n").encode(),
    ]
    for index, raw in enumerate(bad_solutions):
        with pytest.raises(ComputationUnavailableError) as exc_info:
            _adapt(None, _receipt(), raw)
        assert exc_info.value.reason == "result-protocol-failure", index
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(None, _receipt(), None)
    assert exc_info.value.reason == "result-protocol-failure"
    files = {PARAMETERS_PATH: _parameters(offset="1.5")}
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(files, _receipt(), REAL_OPTIMAL.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"
    files = {
        PARAMETERS_PATH: _parameters(unit_map={"x": "kW", "ghost": "kW"}),
    }
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(files, _receipt(), REAL_OPTIMAL.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"
    files = {
        PARAMETERS_PATH: _parameters(unit_map={"x": "not_a_unit_xyz"}),
    }
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(files, _receipt(), REAL_OPTIMAL.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"
