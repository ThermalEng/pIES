"""0.10-C 组合链契约测试：artifact → Bundle → receipt → result。

真实 campus 装配经公开入口（装配校验、设备贡献、物化序列、财务、
计算配置）生成 Bundle；真实 SolverRuntime + LocalProcessProvider +
ies.solver.highs@1.7.2 垫片执行并签发回执；注册表按回执固定引用
选中真实 ResultAdapter 解释。不断言大模型手算最优值，只断言：
成功状态、业务单位、追溯 id、声明输出子集、报告目标与 MPS 目标行
独立复算一致、两次运行解字节一致；失败分支覆盖不可行模型与策略
拒绝；成功链另断言声明财务分量，以及两份独立真实求解作为评价
输入的一致性（契约测试数据组织，非产品多年编排）。
无 API/DB/Worker。
"""

from __future__ import annotations

import json
import math
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
    DeploymentPolicy,
    ExecutableReadiness,
    ExecutorDescriptor,
    LocalExecutable,
    LocalProcessProvider,
    SolverRuntime,
    build_executor_registry,
    build_registry,
    build_result_adapter_registry,
    materialize_non_predictive_series,
)
from iesplan.computation.generators.highs_milp import (
    EXECUTOR_REF,
    GENERATOR_REF,
    SOLVER_REF,
)
from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER
from iesplan.computation.runtime import validate_highs_arguments
from iesplan.devices import init_registry, list_devices
from iesplan.finance import (
    IRREvaluationStatus,
    PaybackStatus,
    effective_from_yaml,
    evaluate_project_investment,
)
from iesplan.modeling import build_math_contribution

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent / "iesplan" / "assembly" / "samples"

PROBLEM_PATH = "input/problem.mps"
PARAMETERS_PATH = "input/parameters.json"
SOLUTION_PATH = "output/solution.sol"


def _campus_inputs():
    """真实 campus 五项直接输入（与 generator/readiness 测试同一来源）。"""
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


def _generate_bundle(inputs):
    """正式生成路径（注册表按配置选中 provider，与线上同一构成）。"""
    artifact, models, series, finance, config = inputs
    provider = build_registry((HIGHS_MILP_PROVIDER,)).resolve(config)
    assert provider is HIGHS_MILP_PROVIDER
    return provider.generate(artifact, models, series, finance, config)


def _runtime(policy=None) -> SolverRuntime:
    """真实执行编排：本地进程 provider + 真实垫片绑定 + 部署策略。"""
    executor_id, _, executor_version = EXECUTOR_REF.rpartition("@")
    provider = LocalProcessProvider(
        ExecutorDescriptor(
            executor_id=executor_id,
            version=executor_version,
            executables=(HIGHS_EXECUTABLE_REF,),
        ),
        (
            LocalExecutable(
                ref=HIGHS_EXECUTABLE_REF,
                path=HIGHS_ADAPTER_PATH,
                argv_validator=validate_highs_arguments,
                readiness=ExecutableReadiness(
                    version=HIGHS_VERSION,
                    self_test=("--self-test",),
                    output_path="self-test.sol",
                ),
            ),
        ),
        base_environ={},
    )
    if policy is None:
        policy = DeploymentPolicy(
            executables=(HIGHS_EXECUTABLE_REF,),
            environment_vars=(),
            max_timeout_seconds=600,
            max_memory_mib=4096,
            max_cpu_cores=4,
            allow_network=False,
            max_output_files=16,
            # campus 真实 solution 约 1.7MB；配额按实际输出规模。
            max_output_bytes=8_388_608,
        )
    return SolverRuntime(build_executor_registry([provider]), policy)


def _attempt(suffix: str) -> AttemptContext:
    return AttemptContext(
        attempt_id=f"attempt-10c-{suffix}",
        bundle_id=f"bundle-10c-{suffix}",
        assembly_id="assembly-10c",
        calculation_config_id="config-10c",
    )


def _adapt(bundle, receipt, collected):
    """按回执固定引用选中适配器解释（只凭三元组）。"""
    registry = build_result_adapter_registry([HIGHS_ADAPTER])
    adapter = registry.resolve(receipt.result_adapter_ref)
    assert adapter is HIGHS_ADAPTER
    return adapter.adapt(bundle, receipt, collected)


def _parse_v1_solution(raw: bytes) -> tuple[float, list[float]]:
    """测试侧独立读取 V1 目标与主解（只核对链路数值，不复用适配器）。"""
    lines = raw.decode("utf-8").splitlines()
    assert lines[0] == "HIGHS_SOLUTION_V1"
    objective: float | None = None
    primals: list[float] = []
    in_primal = False
    for line in lines[1:]:
        if line == "primal:":
            in_primal = True
            continue
        key, _, value = line.partition(":")
        if not in_primal:
            if key.strip() == "objective_value":
                objective = float(value.strip())
            continue
        primals.append(float(value.strip()))
    assert objective is not None
    assert math.isfinite(objective)
    return objective, primals


def _mps_columns_and_objective(raw: bytes) -> tuple[list[str], dict[str, float]]:
    """测试侧独立读取 MPS 列序与目标行系数（只核对链路数值）。

    与适配器同配对 writer 规则：同名连续行属同一列（见 `write_mps`）。
    """
    lines = raw.decode("utf-8").splitlines()
    start = lines.index("COLUMNS") + 1
    order: list[str] = []
    coeffs: dict[str, float] = {}
    previous: str | None = None
    for line in lines[start:]:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "RHS":
            break
        if parts[0] in ("INTORG", "INTEND"):
            previous = None
            continue
        if len(parts) < 3 or len(parts) % 2 == 0:
            raise AssertionError(f"MPS 列行非法: {line!r}")
        if parts[0] != previous:
            order.append(parts[0])
            previous = parts[0]
        for row, value in zip(parts[1::2], parts[2::2], strict=True):
            if row == "OBJ":
                coeffs[order[-1]] = float(value)
    return order, coeffs


def test_chain_success_reports_business_result() -> None:
    """成功链：求解→回执→解释，业务数值/单位/追溯一致且确定。"""
    bundle = _generate_bundle(_campus_inputs())
    assert bundle.manifest.command["executable"] == HIGHS_EXECUTABLE_REF
    parameters = json.loads(bundle.files[PARAMETERS_PATH].decode("utf-8"))
    unit_map = parameters["unit_map"]
    assert parameters["objective_unit"] == "CNY"

    runtime = _runtime()
    receipt, collected = runtime.run(bundle, _attempt("ok"))
    assert receipt.status == "succeeded"
    assert receipt.exit_code == 0
    assert receipt.executor_ref == EXECUTOR_REF
    assert receipt.executable_ref == HIGHS_EXECUTABLE_REF
    assert receipt.bundle_id == "bundle-10c-ok"
    assert receipt.executor_ref == EXECUTOR_REF
    assert set(collected) == {SOLUTION_PATH}

    result = _adapt(bundle, receipt, collected)
    assert result.execution_status == "completed"
    assert result.math_status == "solved"
    assert result.bundle_id == receipt.bundle_id
    assert result.receipt_id == receipt.receipt_id
    solution = result.outputs["solution"]
    assert solution["objective_unit"] == "CNY"
    assert solution["objective_value"] > 0
    assert [item["name"] for item in solution["variables"]] == list(unit_map)
    assert [item["unit"] for item in solution["variables"]] == list(unit_map.values())

    # 独立复算：MPS 目标行点乘主解 + offset == 报告目标（业务数值保真）。
    file_objective, primals = _parse_v1_solution(collected[SOLUTION_PATH])
    order, mps_coeffs = _mps_columns_and_objective(bundle.files[PROBLEM_PATH])
    assert len(primals) == len(order)
    dot = sum(mps_coeffs[name] * primals[order.index(name)] for name in mps_coeffs)
    assert dot + parameters["objective_offset"] == pytest.approx(
        solution["objective_value"], rel=1e-9
    )
    assert file_objective + parameters["objective_offset"] == pytest.approx(
        solution["objective_value"], rel=1e-9
    )

    # 确定性：同一 Bundle 再跑一次，解字节与业务输出一致。
    _, collected_again = runtime.run(bundle, _attempt("ok2"))
    assert collected_again[SOLUTION_PATH] == collected[SOLUTION_PATH]


def test_chain_infeasible_model_reports_math_status() -> None:
    """不可行链：垫片 exit 0 写 kInfeasible 文件，解释无业务输出。"""
    artifact, models, series, finance, config = _campus_inputs()
    doc = json.loads(artifact.canonical_text)
    # 规划变量名（非内部 LP 名，后者含安全规则禁止的 __），取首个
    # 显式单位与上界的变量，上界十倍即不可行。
    plan_vars = doc["planning"]["variables"]
    pinned = sorted(
        name
        for name, spec in plan_vars.items()
        if spec.get("upper_bound") is not None and spec.get("unit")
    )[0]
    spec = plan_vars[pinned]
    pin_lines = (
        "    chain_infeasible_pin:\n"
        "      type: capacity_limit\n"
        f"      expression: \"{pinned} >= {float(spec['upper_bound']) * 10.0} {spec['unit']}\"\n"
    )
    # 文本级插入（保留装配 YAML 规范风格，json/yaml 来回倒换会破坏
    # 风格校验），再走公开装配校验入口得到 artifact。
    source = (SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    anchor = '      expression: "import_meter__period_energy_purchase <= 900000 CNY"\n'
    assert anchor in source, "campus 样本约束锚点变化，pin 插入位置需重审"
    pinned_text = source.replace(anchor, anchor + pin_lines, 1)
    revalidated = validate_assembly_text(pinned_text, package_dir=SAMPLES)
    assert revalidated.ok, [
        (d.code, d.params) for d in revalidated.diagnostics if d.blocking
    ]
    bundle = _generate_bundle((revalidated.artifact, models, series, finance, config))
    receipt, collected = _runtime().run(bundle, _attempt("inf"))
    assert receipt.status == "succeeded"
    assert receipt.exit_code == 0
    result = _adapt(bundle, receipt, collected)
    assert result.execution_status == "completed"
    assert result.math_status == "infeasible"
    assert dict(result.outputs) == {}
    assert result.bundle_id == receipt.bundle_id
    assert result.receipt_id == receipt.receipt_id


def test_chain_policy_rejected_never_executes() -> None:
    """策略拒绝链：执行器不在配额内，不启动进程，无业务输出。"""
    bundle = _generate_bundle(_campus_inputs())
    denied = DeploymentPolicy(
        executables=("ies.executor.unused@1.0.0",),
        environment_vars=(),
        max_timeout_seconds=600,
        max_memory_mib=4096,
        max_cpu_cores=4,
        allow_network=False,
        max_output_files=16,
        max_output_bytes=8_388_608,
    )
    receipt, collected = _runtime(policy=denied).run(bundle, _attempt("rej"))
    assert receipt.status == "policy_rejected"
    assert dict(collected) == {}
    result = _adapt(bundle, receipt, collected)
    assert result.execution_status == "rejected"
    assert result.math_status == "not_solved"
    assert dict(result.outputs) == {}


def _solution_components(solution) -> dict:
    return {name: dict(entry) for name, entry in solution["components"].items()}


def test_chain_success_reports_declared_components() -> None:
    """成功链发布声明财务分量：键/单位/口径与同解交叉核对一致。

    campus 手算关系（发布值均为业务单位）：
    pv1__upfront_capex == 3500 × pv_cap；
    pv1__annual_fixed_om == 35 × pv_cap；
    objective == upfront + annual×1年 + pv1__period_variable_om
      + import_meter__period_energy_purchase。
    """
    bundle = _generate_bundle(_campus_inputs())
    runtime = _runtime()
    receipt, collected = runtime.run(bundle, _attempt("comp"))
    result = _adapt(bundle, receipt, collected)
    assert result.math_status == "solved"
    solution = result.outputs["solution"]
    components = _solution_components(solution)
    assert set(components) == {
        "pv1__upfront_capex",
        "pv1__annual_fixed_om",
        "pv1__period_variable_om",
        "bat1__upfront_capex",
        "bat1__annual_fixed_om",
        "bat1__period_variable_om",
        "hp1__upfront_capex",
        "hp1__annual_fixed_om",
        "hp1__period_variable_om",
        # 计费绑定只发布与价格方向一致的后缀，不双计：
        # import_meter 为购电、export_meter 为售电。
        "import_meter__period_energy_purchase",
        "export_meter__period_energy_sale",
    }
    for name, entry in components.items():
        assert set(entry) == {"value", "unit", "caliber"}
        if name.endswith("__annual_fixed_om"):
            assert entry["unit"] == "CNY/a", name
            assert entry["caliber"] == "annual", name
        else:
            assert entry["unit"] == "CNY", name
            assert entry["caliber"] in ("upfront", "period"), name
        assert math.isfinite(entry["value"])

    by_name = {item["name"]: item for item in solution["variables"]}
    pv_cap = by_name["pv_cap"]["value"]
    upfront = components["pv1__upfront_capex"]["value"]
    annual = components["pv1__annual_fixed_om"]["value"]
    assert upfront == pytest.approx(3500.0 * pv_cap, rel=1e-9)
    assert annual == pytest.approx(35.0 * pv_cap, rel=1e-9)
    expect = (
        upfront
        + annual
        + components["pv1__period_variable_om"]["value"]
        + components["import_meter__period_energy_purchase"]["value"]
    )
    assert solution["objective_value"] == pytest.approx(expect, rel=1e-9)


def _yearly_flows(solution) -> tuple[float, float]:
    """单次真实解 → (t=0 投资取负, 年净流)。

    t=0 投资 = −upfront 口径合计；年净流 = −(annual + period 口径合计)，
    其中 sale 分量已带负号（收益自动抵减成本），purchase 为正成本。
    只用真实声明分量，不引入基线等外部金额。
    """
    components = _solution_components(solution)
    upfront = sum(
        entry["value"] for name, entry in components.items()
        if entry["caliber"] == "upfront"
    )
    annual_cost = sum(
        entry["value"] for name, entry in components.items()
        if entry["caliber"] in ("annual", "period")
    )
    return -upfront, -annual_cost


def test_chain_two_solves_feed_yearly_evaluation() -> None:
    """两份独立真实求解作为评价输入：无复制，指标与明细同源。

    仅为契约测试数据组织，不是产品级多年编排：第二年用不同冻结财务
    （购电价 0.7→0.5 CNY/kWh，经财务公开解析入口重载；降价不破坏原
    约束可行性），运行条件不同但建设方案经断言相同（pv_cap 均为 0，
    upfront 分项一致），t=0 投资一致才保留两年评价；两年年净流必须
    不同，否则即复制单年结果。campus 样例无正净流，如实断言无解/
    未回收，不虚构收入。
    """
    inputs1 = _campus_inputs()
    bundle1 = _generate_bundle(inputs1)
    runtime = _runtime()
    receipt1, collected1 = runtime.run(bundle1, _attempt("yr1"))
    solution1 = _adapt(bundle1, receipt1, collected1).outputs["solution"]

    finance_source = (
        SAMPLES / "finance" / "campus.effective-finance.yaml"
    ).read_text(encoding="utf-8")
    anchor = "      value: '0.7'\n"
    assert finance_source.count(anchor) == 1, "campus 购电价格锚点变化，需重审"
    finance2 = effective_from_yaml(
        finance_source.replace(anchor, "      value: '0.5'\n", 1)
    )
    artifact, models, series, _finance, config = inputs1
    bundle2 = _generate_bundle((artifact, models, series, finance2, config))
    receipt2, collected2 = runtime.run(bundle2, _attempt("yr2"))
    solution2 = _adapt(bundle2, receipt2, collected2).outputs["solution"]

    invest, net1 = _yearly_flows(solution1)
    invest2, net2 = _yearly_flows(solution2)
    # 同一建设方案断言：建设决策变量与建设投入跨两次求解一致，
    # t=0 才可作为同一项目的投资。
    vars1 = {item["name"]: item for item in solution1["variables"]}
    vars2 = {item["name"]: item for item in solution2["variables"]}
    assert vars1["pv_cap"]["value"] == pytest.approx(vars2["pv_cap"]["value"])
    assert invest == pytest.approx(invest2), "建设投入不一致则不是同一方案"
    assert net1 < 0 and net2 < 0, "campus 样例年净流为负（成本大于售电收益）"
    assert net1 != pytest.approx(net2), "两年须为不同真实解，不得复制"
    result = evaluate_project_investment(
        [invest, net1, net2],
        0.08,
        2,
        currency="CNY",
        price_basis="tax_inclusive",
        base_year=2025,
    )
    assert result.evaluation_years == 2
    assert [float(row.net) for row in result.rows] == pytest.approx([invest, net1, net2])
    assert result.npv == result.rows[-1].cumulative_discounted
    assert result.npv < 0
    assert result.irr is None
    assert result.irr_status == IRREvaluationStatus.no_solution
    assert result.static_payback is None
    assert result.static_status == PaybackStatus.not_recovered
    assert result.dynamic_payback is None
    assert result.dynamic_status == PaybackStatus.not_recovered
