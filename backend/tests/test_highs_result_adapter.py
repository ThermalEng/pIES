"""ies.result-adapter.highs@1.0.0 聚焦行为测试。

V1 夹具为 Docker 内真实求解垫片（``ies-highs-solver``，highspy 1.7.2）
逐字节产出，断言前已核对：
- ``SHIM_OPTIMAL``：具名 MPS（min x+2y s.t. x+y>=3000）最优解；
- ``SHIM_INFEASIBLE``：``x == 2`` 且 ``x <= 1``；
- ``SHIM_UNBOUNDED``：``min -x, x >= 0``；
- ``V1_TIME_LIMIT``：垫片真实超时恒 exit 2 且不写文件（走回执失败
  路径），此夹具仅按同一 V1 文法构造 kTimeLimit 令牌分支，验证已知
  词汇出现时明确报错而非发布部分解。
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

#: 真实垫片最优解（最优 x=3000, y=0，目标 3000）。
SHIM_OPTIMAL = """HIGHS_SOLUTION_V1
model_status: HighsModelStatus.kOptimal
objective_value: 3000.0
num_columns: 2
primal:
0: 3000.0
1: 0.0
"""

#: 真实垫片不可行解。
SHIM_INFEASIBLE = """HIGHS_SOLUTION_V1
model_status: HighsModelStatus.kInfeasible
objective_value: 0.0
num_columns: 1
primal:
0: 0.0
"""

#: 真实垫片无界解（注意目标为 -0.0）。
SHIM_UNBOUNDED = """HIGHS_SOLUTION_V1
model_status: HighsModelStatus.kUnbounded
objective_value: -0.0
num_columns: 1
primal:
0: 0.0
"""

#: V1 文法内的 kTimeLimit 令牌（垫片真实超时不写文件，此分支守词汇）。
V1_TIME_LIMIT = """HIGHS_SOLUTION_V1
model_status: HighsModelStatus.kTimeLimit
objective_value: 0.0
num_columns: 1
primal:
0: 0.0
"""

#: 具名 tiny MPS（列序 x, y；每变量单行）。
TINY_MPS = """NAME          IES_MILP
ROWS
 N  OBJ
 G  demand
COLUMNS
    x  OBJ  1.0  demand  1.0
    y  OBJ  2.0  demand  1.0
RHS
    RHS1  demand  3000.0
BOUNDS
 LO       BND1  x  0.0
 LO       BND1  y  0.0
OBJSENSE
 MIN
ENDATA
"""

#: 变量跨物理行的 MPS（writer 每行重复列名，同名连续行属同一列）。
WRAPPED_MPS = """NAME          IES_MILP
ROWS
 N  OBJ
 G  r1
 G  r2
COLUMNS
    x  OBJ  1.0  r1  1.0
    x  r2  2.0
    y  OBJ  2.0  r1  1.0
    y  r2  1.0
RHS
    RHS1  r1  3.0
    RHS1  r2  4.0
BOUNDS
 LO       BND1  x  0.0
 LO       BND1  y  0.0
OBJSENSE
 MIN
ENDATA
"""


def _facts() -> dict:
    """无财务口径的最小必需声明（常量零值 + 空数组 = 真实不适用）。

    配套生成器恒声明该映射；本夹具只验主解/序列/度量解析，故以零
    常量声明不断言任何税额。旧包缺该键即协议失败，见拒绝测试。
    """
    zero = {"constant": 0.0, "terms": {}}
    return {
        "currency": "CNY",
        "construction_total": {"constant": 0.0, "terms": {}},
        "annual_fixed_om": {"constant": 0.0, "terms": {}},
        "payments_applied": [],
        "net_cashflows": [],
        "construction_units": [],
        "annual_operation": [],
        "monthly_demand": [],
        "tax_cash": {
            "vat": [],
            "refunds": [],
            "income": [],
            "depreciation": [],
            "loss_balances": [],
        },
        "annual_net_flows": [dict(zero)],
    }


def _parameters(
    unit_map: dict | None = None,
    offset: object = 1.5,
    objective_unit: object = "CNY",
    outputs: object = None,
    components: object = None,
    step_series: object = None,
) -> bytes:
    doc = {
        "assembly": {"id": "assembly-7", "schema_version": "1.0.0"},
        "components": {} if components is None else components,
        "financial_facts": _facts(),
        "model_sources": {"schema": "ies.model-sources", "schema_version": "1.0.0",
                          "sources": {}, "groups": [], "outputs": []},
        "generator": "ies.generator.highs_milp@1.0.0",
        "objective_offset": offset,
        "objective_unit": objective_unit,
        "outputs": ["solution", "metrics"] if outputs is None else outputs,
        "precision": "medium",
        "result_adapter": ADAPTER_REF,
        "seed": 0,
        "solver": "ies.solver.highs@1.7.2",
        "step_series": [] if step_series is None else step_series,
        "unit_map": {"x": "kW", "y": "kW"} if unit_map is None else unit_map,
    }
    return (json.dumps(doc, sort_keys=True) + "\n").encode("utf-8")


def _bundle(files: dict | None = None) -> SolverBundle:
    if files is None:
        files = {
            PROBLEM_PATH: TINY_MPS.encode("utf-8"),
            PARAMETERS_PATH: _parameters(),
        }
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


def test_bundle_without_financial_facts_fails() -> None:
    """旧包缺必需财务事实声明即自然失败，不回退、不补零。"""
    doc = json.loads(_parameters().decode("utf-8"))
    del doc["financial_facts"]
    raw = (json.dumps(doc, sort_keys=True) + "\n").encode("utf-8")
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: raw,
    }
    with pytest.raises(KeyError):
        _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))


def test_optimal_money_objective_keeps_currency() -> None:
    """真实最优解：金额目标守币种原值，变量换算，offset 只加一次。"""
    result = _adapt(None, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert result.execution_status == "completed"
    assert result.math_status == "solved"
    assert result.bundle_id == "bundle-7"
    assert result.receipt_id == "receipt-7"
    assert set(result.outputs) == {"solution", "metrics", "series", "financial_facts", "model_sources"}
    assert dict(result.outputs["series"]) == {}
    solution = result.outputs["solution"]
    assert solution["objective_value"] == 3001.5
    assert solution["objective_unit"] == "CNY"
    assert [dict(item) for item in solution["variables"]] == [
        {"name": "x", "value": 3.0, "unit": "kW"},
        {"name": "y", "value": 0.0, "unit": "kW"},
    ]
    metrics = result.outputs["metrics"]
    assert metrics["model_status"] == "HighsModelStatus.kOptimal"
    assert metrics["objective_value"] == 3001.5
    assert metrics["objective_unit"] == "CNY"
    assert metrics["num_variables"] == 2


def test_objective_physical_unit_converts() -> None:
    """等效契约：物理目标按 objective_unit 反向换算（内部 J→kWh）。"""
    solution = SHIM_OPTIMAL.replace("objective_value: 3000.0", "objective_value: 7200000.0")
    solution = solution.replace("0: 3000.0", "0: 3600000.0", 1)
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(
            unit_map={"x": "kWh", "y": "kWh"},
            offset=0.0,
            objective_unit="kWh",
        ),
    }
    result = _adapt(files, _receipt(), solution.encode("utf-8"))
    assert result.math_status == "solved"
    out = result.outputs["solution"]
    assert out["objective_value"] == 2.0
    assert out["objective_unit"] == "kWh"
    assert [dict(item) for item in out["variables"]] == [
        {"name": "x", "value": 1.0, "unit": "kWh"},
        {"name": "y", "value": 0.0, "unit": "kWh"},
    ]


def test_objective_usd_identity_without_conversion() -> None:
    """USD 金额目标直接采用原值（from_si 拒绝 USD，不经过折算）。"""
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(offset=0.0, objective_unit="USD"),
    }
    result = _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert result.math_status == "solved"
    out = result.outputs["solution"]
    assert out["objective_value"] == 3000.0
    assert out["objective_unit"] == "USD"


def test_requested_subset_only() -> None:
    """只输出已请求的键；未知请求由生成器在 build 侧拒绝，此处忽略。"""
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(outputs=["solution"]),
    }
    result = _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert set(result.outputs) == {"solution", "series", "financial_facts", "model_sources"}
    assert dict(result.outputs["series"]) == {}
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(outputs=["solution", "nope"]),
    }
    result = _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert set(result.outputs) == {"solution", "series", "financial_facts", "model_sources"}
    assert dict(result.outputs["series"]) == {}


def test_infeasible_and_unbounded_have_no_business_outputs() -> None:
    """真实不可行/无界：数学状态明确，不发布业务输出。"""
    for text, math in (
        (SHIM_INFEASIBLE, "infeasible"),
        (SHIM_UNBOUNDED, "unbounded"),
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
        _adapt(None, _receipt(), V1_TIME_LIMIT.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"
    assert "时间限制" in str(exc_info.value)


def test_mps_repeated_name_lines_map() -> None:
    """MPS 同名连续行属同一列；非连续列名复现即损坏。"""
    files = {
        PROBLEM_PATH: WRAPPED_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(),
    }
    result = _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert result.math_status == "solved"
    assert [dict(item) for item in result.outputs["solution"]["variables"]] == [
        {"name": "x", "value": 3.0, "unit": "kW"},
        {"name": "y", "value": 0.0, "unit": "kW"},
    ]
    duplicated = WRAPPED_MPS.replace(
        "    y  r2  1.0\nRHS", "    y  r2  1.0\n    x  r2  9.0\nRHS"
    )
    files = {
        PROBLEM_PATH: duplicated.encode("utf-8"),
        PARAMETERS_PATH: _parameters(),
    }
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"


def test_corrupt_inputs_fail() -> None:
    """关键格式错误一律协议失败，不伪成功。"""
    bad_solutions = [
        b"garbage",
        # 原生 HiGHS raw 文本不是本管道契约，出现即拒收。
        b"Model status\nOptimal\n",
        SHIM_OPTIMAL.replace("HIGHS_SOLUTION_V1", "HIGHS_SOLUTION_V2").encode(),
        SHIM_OPTIMAL.replace("kOptimal", "kTimeTravel").encode(),
        SHIM_OPTIMAL.replace("objective_value: 3000.0", "objective_value: unknown").encode(),
        SHIM_OPTIMAL.replace("objective_value: 3000.0", "objective_value: nan").encode(),
        SHIM_OPTIMAL.replace("0: 3000.0", "0: nan").encode(),
        SHIM_OPTIMAL.replace("num_columns: 2", "num_columns: 3").encode(),
        SHIM_OPTIMAL.replace("0: 3000.0\n1: 0.0", "1: 0.0").encode(),
        "\n".join(SHIM_OPTIMAL.splitlines()[:-1]).encode(),
    ]
    for index, raw in enumerate(bad_solutions):
        with pytest.raises(ComputationUnavailableError) as exc_info:
            _adapt(None, _receipt(), raw)
        assert exc_info.value.reason == "result-protocol-failure", index
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(None, _receipt(), None)
    assert exc_info.value.reason == "result-protocol-failure"
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(offset=True),
    }
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(unit_map={"x": "kW", "ghost": "kW"}),
    }
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(unit_map={"x": "not_a_unit_xyz"}),
    }
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"


def test_declared_components_evaluated_from_same_solution() -> None:
    """声明分量用同一次真实解求值发布：金额守币种、年值与物理量换算。

    手算（tiny 最优解 x=3000, y=0；年内部以秒计，a = 31536000 s）：
    capex = 100 + 2×3000 = 6100 CNY；
    om_year = from_si(0.5×3000, CNY/a) = 1500×31536000 = 47304000000 CNY/a；
    energy = 3600000 J = 1.0 kWh。
    """
    components = {
        "capex": {
            "unit": "CNY",
            "caliber": "upfront",
            "constant": 100.0,
            "terms": {"x": 2.0},
        },
        "om_year": {
            "unit": "CNY/a",
            "caliber": "annual",
            "constant": 0.0,
            "terms": {"x": 0.5},
        },
        "energy": {
            "unit": "kWh",
            "caliber": "period",
            "constant": 3600000.0,
            "terms": {},
        },
    }
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(components=components),
    }
    result = _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert result.math_status == "solved"
    got = {k: dict(v) for k, v in result.outputs["solution"]["components"].items()}
    assert got["capex"] == {"value": 6100.0, "unit": "CNY", "caliber": "upfront"}
    assert got["om_year"]["unit"] == "CNY/a"
    assert got["om_year"]["caliber"] == "annual"
    assert got["om_year"]["value"] == pytest.approx(47304000000.0)
    assert got["energy"] == {"value": 1.0, "unit": "kWh", "caliber": "period"}


def test_components_fail_loudly() -> None:
    """分量声明引用未知 LP 变量、形态非法或缺键，一律协议失败。"""
    bad_decls = [
        {
            "capex": {
                "unit": "CNY",
                "caliber": "upfront",
                "constant": 0.0,
                "terms": {"ghost": 1.0},
            }
        },
        {
            "capex": {
                "unit": "CNY",
                "caliber": "upfront",
                "constant": 0.0,
            }
        },
        {"capex": "not-a-mapping"},
    ]
    for decl in bad_decls:
        files = {
            PROBLEM_PATH: TINY_MPS.encode("utf-8"),
            PARAMETERS_PATH: _parameters(components=decl),
        }
        with pytest.raises(ComputationUnavailableError) as exc_info:
            _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
        assert exc_info.value.reason == "result-protocol-failure"
    raw = json.loads(_parameters().decode("utf-8"))
    del raw["components"]
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: (json.dumps(raw, sort_keys=True) + "\n").encode("utf-8"),
    }
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"


def test_series_recovers_by_declared_columns() -> None:
    """步序列按声明显式列引用恢复，与列名模式无关。

    声明 columns 故意逆序（["y", "x"]），断言恢复值跟随声明顺序
    （[0.0, 3.0]）而非 MPS 列序；单位按声明反向换算，身份原样发布。
    主解 x=3000W→3.0kW、y=0 形成非恒定序列。
    """
    decl = [{
        "series_id": "inst.flow",
        "instance_id": "inst",
        "variable_id": "flow",
        "unit": "kW",
        "columns": ["y", "x"],
    }]
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(step_series=decl),
    }
    result = _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert result.math_status == "solved"
    assert set(result.outputs) == {"solution", "metrics", "series", "financial_facts", "model_sources"}
    series = {key: dict(item) for key, item in result.outputs["series"].items()}
    assert series == {
        "inst.flow": {
            "instance_id": "inst",
            "variable_id": "flow",
            "unit": "kW",
            "values": (0.0, 3.0),
        }
    }


def test_series_absent_without_solution_request() -> None:
    """metrics-only：未请求 solution 即无 series（缺席，不伪造空键）。"""
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(
            outputs=["metrics"],
            step_series=[{
                "series_id": "inst.flow",
                "instance_id": "inst",
                "variable_id": "flow",
                "unit": "kW",
                "columns": ["x", "y"],
            }],
        ),
    }
    result = _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert result.math_status == "solved"
    assert set(result.outputs) == {"metrics"}
    assert "series" not in result.outputs
    assert "solution" not in result.outputs


def test_legacy_bundle_without_step_series_fails() -> None:
    """旧声明（无 step_series 键）自然失败，不回退、不忽略、不另造校验器。"""
    raw = json.loads(_parameters().decode("utf-8"))
    del raw["step_series"]
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: (json.dumps(raw, sort_keys=True) + "\n").encode("utf-8"),
    }
    with pytest.raises(KeyError):
        _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))


def test_series_unknown_column_fails() -> None:
    """声明引用未知 LP 列即协议失败，不补零。"""
    decl = [{
        "series_id": "inst.flow",
        "instance_id": "inst",
        "variable_id": "flow",
        "unit": "kW",
        "columns": ["x", "ghost"],
    }]
    files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(step_series=decl),
    }
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(files, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    assert exc_info.value.reason == "result-protocol-failure"


def test_monthly_demand_facts_recovered_from_same_primal() -> None:
    """适配器只求值声明：x=3000 W、实际月峰3kW、每月支付90CNY。

    不能把主解 primal 的 x=3000 当 3000 kW；不能重新取峰或追加费。
    """
    doc = json.loads(_parameters().decode("utf-8"))
    zero = {"constant": 0.0, "terms": {}}
    charge = {"constant": 0.0, "terms": {"x": 0.03}}
    # 同一次主解：月需量费 90 + 维护 12 + 0.5×3000 = 1602。
    maintenance = {"constant": 12.0, "terms": {"x": 0.5}}
    negative = {"constant": -12.0, "terms": {"x": -0.53}}
    doc["financial_facts"]["monthly_demand"] = [{
        "billing_id": "real_meter", "charge_id": "fee",
        "calendar_year": 2024, "calendar_month": 2, "financial_year": 2,
        "peak_power": {"constant": 0.0, "terms": {"x": 0.001}},
        "peak_unit": "kW", "rate": {"value": "30", "unit": "CNY/kW"},
        "charge": charge,
    }]
    doc["financial_facts"]["annual_operation"] = [{
        "financial_year": 2, "energy_net": zero, "variable_om": zero,
        "fixed_om": zero, "maintenance": maintenance, "demand_charge": charge,
        "net": negative,
    }]
    doc["financial_facts"]["annual_net_flows"] = [zero, zero, negative]
    raw = (json.dumps(doc, sort_keys=True) + "\n").encode("utf-8")
    result = _adapt(
        {PROBLEM_PATH: TINY_MPS.encode("utf-8"), PARAMETERS_PATH: raw},
        _receipt(), SHIM_OPTIMAL.encode("utf-8"),
    )
    facts = result.outputs["financial_facts"]
    month = facts["monthly_demand"][0]
    assert month["billing_id"] == "real_meter"
    assert month["calendar_year"] == 2024 and month["calendar_month"] == 2
    assert month["peak_power"] == "3.0"
    assert month["peak_unit"] == "kW"
    assert month["charge"] == "90.0"
    assert month["rate"] == {"value": "30", "unit": "CNY/kW"}
    assert facts["annual_operation"][0]["demand_charge"] == "90.0"
    assert facts["annual_operation"][0]["maintenance"] == "1512.0"
    assert facts["annual_operation"][0]["net"] == "-1602.0"
    assert facts["annual_net_flows"][-1] == "-1602.0"
