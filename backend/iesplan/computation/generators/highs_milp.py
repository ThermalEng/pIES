"""首个真实 GeneratorProvider：``ies.generator.highs_milp@1.0.0`` (1.2.0 契约解耦版)。

纯内存确定性转换：直接输入（装配产物、设备数学贡献、物化序列、
有效财务快照、13 字段计算配置、冻结评价确认）→ ``SolverBundle`` 完整聚合
(解耦 manifest ``ies.solver-bundle@1.0.0`` + ``ies.variable-map@1.0.0`` + MPS + 分块参数)。
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from functools import partial
from itertools import islice
from typing import Any

from iesplan.assembly.contracts import ValidatedAssemblyArtifact
from iesplan.computation.bundle import PRIMAL_SOLUTION_OUTPUT_PATH
from iesplan.computation.contracts.compatibility import SOLVER_BUNDLE_MPS_CONTRACT
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators.acceleration import ACCELERATION_PATH, acceleration_bytes
from iesplan.computation.generators.descriptor import GeneratorDescriptor
from iesplan.computation.generators.matrix import SparseModel
from iesplan.computation.generators.mps import iter_mps_chunks
from iesplan.computation.generators.parameters import RECORD_ITEMS, RECORDS_PATH
from iesplan.computation.generators.project import build_project_model_package
from iesplan.computation.protocols import (
    CalculationConfig,
    SolverBundle,
    SolverBundleManifest,
    StopProbe,
    read_stop_reason,
)
from iesplan.computation.sequences import MaterializedSet
from iesplan.core.content import GeneratedContent, MemoryContent
from iesplan.core.units import AFFINE_UNITS, UnitError, dims_of, lookup, normalize_unit, unit_meta
from iesplan.finance import EffectiveFinanceConfig, EvaluationInput
from iesplan.modeling import DeviceMathContribution

GENERATOR_ID = "ies.generator.highs_milp"
GENERATOR_VERSION = "1.0.0"
GENERATOR_REF = f"{GENERATOR_ID}@{GENERATOR_VERSION}"
SOLVER_REF = "ies.solver.highs@1.7.2"
EXECUTOR_REF = "ies.executor.container@1.0.0"
EVALUATOR_FINANCE_REF = "ies.evaluator.finance@1.0.0"
ASSEMBLY_SCHEMA_VERSION = "1.0.0"

#: seed 为 null 时 descriptor 明示的固定种子。
FIXED_SEED = 0

#: 本 provider 支持的输出请求。
SUPPORTED_OUTPUTS = ("solution", "metrics")

PROBLEM_PATH = "input/problem.mps"
PARAMETERS_PATH = "input/parameters.json"
EPIGRAPHS_PATH = "input/epigraphs.json"
DEFAULT_ACCELERATION_MODE = "on_demand_mutex"

VARIABLE_MAP_SCHEMA = "ies.variable-map"
VARIABLE_MAP_VERSION = "1.0.0"

SOLVER_EPIGRAPH_SCHEMA = "ies.solver-epigraph"
SOLVER_EPIGRAPH_VERSION = "1.0.0"

_OPTIONS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "acceleration": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["off", "on_demand_mutex"],
                    "default": DEFAULT_ACCELERATION_MODE,
                },
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

DESCRIPTOR = GeneratorDescriptor(
    generator_id=GENERATOR_ID,
    version=GENERATOR_VERSION,
    options_schema=dict(_OPTIONS_SCHEMA),
    outputs=SUPPORTED_OUTPUTS,
    assembly_schema_version=ASSEMBLY_SCHEMA_VERSION,
    modes=("planning",),
    precisions=("medium",),
    capabilities=(
        "affine",
        "mutex_bigm",
        "finance.vat",
        "finance.construction_input_vat",
        "finance.income_tax",
        "finance.credit_pools",
        "finance.financial_facts",
        "finance.frozen_baseline",
        "finance.cashflow_difference",
        "finance.cash_movements",
        "finance.assets",
        "finance.derived_tax",
        "finance.monthly_demand",
        "finance.affine_maintenance",
        "finance.monthly_daily_prices",
        "finance.affine_annual_om",
        "finance.subcomponent_vat",
        "finance.maintenance_depreciation",
        "finance.ratio_residual",
        "finance.net_cashflows",
        "technical.step_constraints",
        "network.closed_ports",
        "modeling.system_equations",
        "outputs.system_series",
        "planning.construction_activation",
        "finance.cost_occurrence",
        "finance.scoped_amount_sources",
        "finance.named_items",
        "finance.amount_references",
        "finance.amount_constraints",
    ),
    solvers=(SOLVER_REF,),
    executors=(EXECUTOR_REF,),
    equation_contracts=("ies.equation.ast",),
    evaluators=(),
    deterministic=True,
    resource_limits={},
    unit_system="SI",
    media_types=("application/mps", "application/json", "application/x-ndjson"),
    seed_semantics="config.seed 直接固定进变量映射；为 null 时使用固定种子 0；不读取随机全局状态",
)


def linear_conversion_for_unit(unit: str) -> tuple[float, float]:
    """Return explicit affine conversion ``(scale, offset)`` from internal SI/currency to ``unit``."""
    if not isinstance(unit, str) or not unit:
        raise ComputationUnavailableError(f"变量单位非法: {unit!r}", reason="bad-unit")
    try:
        dims = dims_of(unit)
        if dims.get("currency") == 1:
            factor = float(unit_meta(unit)["to_si"])
            return 1.0 / factor, 0.0
        canonical = normalize_unit(unit)
        if canonical in AFFINE_UNITS:
            spec = lookup(canonical)
            to_si_val = float(spec.to_si)
            return 1.0 / to_si_val, -float(spec.offset) / to_si_val
        factor = float(unit_meta(canonical)["to_si"])
        return 1.0 / factor, 0.0
    except (UnitError, ZeroDivisionError, ValueError) as exc:
        raise ComputationUnavailableError(
            f"无法构建单位 {unit!r} 的线性换算系数: {exc}",
            reason="bad-unit",
        ) from exc


def _batches(values: Any) -> Iterator[list[Any]]:
    it = iter(values)
    while batch := list(islice(it, RECORD_ITEMS)):
        yield batch


def _expression_refs(node: object, register_expr: Any) -> object:
    if isinstance(node, Mapping):
        if "constant" in node and "terms" in node:
            return {"expression": register_expr(node)}
        return {key: _expression_refs(val, register_expr) for key, val in node.items()}
    if isinstance(node, (tuple, list)):
        return [_expression_refs(val, register_expr) for val in node]
    return node


def _attach_scale_entry_conversions(entries: Sequence[dict[str, Any]]) -> None:
    """Attach linear unit scale and offset metadata to scale entries in place."""
    for sc_entry in entries:
        sc_val, off_val = linear_conversion_for_unit(sc_entry["unit"])
        sc_entry["scale"] = sc_val
        sc_entry["offset"] = off_val


def _attach_scale_conversions(fact_refs: dict[str, Any]) -> dict[str, Any]:
    for unit_entry in fact_refs.get("construction_units", ()):
        _attach_scale_entry_conversions(unit_entry["scales"])
    _attach_scale_entry_conversions(fact_refs.get("planning_variables", ()))
    return fact_refs


def target_parameter_records(
    model: SparseModel,
    unit_map: Mapping[str, str],
    components: Mapping[str, Mapping[str, Any]],
    series: Sequence[Mapping[str, Any]],
    facts: Mapping[str, Any],
    sources: Mapping[str, Any],
    epigraphs: Sequence[tuple[str, tuple[str, ...]]] = (),
) -> Iterator[list[Any]]:
    """Yield ordered ``ies.variable-map@1.0.0`` NDJSON records with 0-based column positions."""
    expressions: list[Mapping[str, Any]] = []
    identities: dict[tuple[int, str], int] = {}

    for batch in _batches(model.column_names()):
        yield ["column_names", batch]
    for batch in _batches(model.row_names()):
        yield ["row_names", batch]
    for peak_name, flow_names in epigraphs:
        yield ["epigraph", model.column_position(peak_name)]
        for batch in _batches(flow_names):
            yield ["epigraph_flows", [model.column_position(name) for name in batch]]
        yield ["epigraph_end"]

    def register_expr(node: Mapping[str, Any]) -> int:
        identity = (id(node["terms"]), float(node["constant"]).hex())
        if identity not in identities:
            identities[identity] = len(expressions)
            expressions.append(node)
        return identities[identity]

    component_refs = [
        (name, decl["unit"], decl["caliber"], register_expr(decl))
        for name, decl in sorted(components.items())
    ]
    fact_refs = _attach_scale_conversions(_expression_refs(facts, register_expr))  # type: ignore[arg-type]
    for number, node in enumerate(expressions):
        yield ["expression", number, float(node["constant"])]
        for batch in _batches(node["terms"].items()):
            yield ["terms", [[model.column_position(name), float(coeff)] for name, coeff in batch]]
        yield ["expression_end"]

    unit_entries = (
        [model.column_position(name), unit, *linear_conversion_for_unit(unit)]
        for name, unit in sorted(unit_map.items())
    )
    for batch in _batches(unit_entries):
        yield ["units", batch]

    for entry in component_refs:
        yield ["component", *entry]
    yield ["facts", fact_refs]

    for entry in series:
        scale, offset = linear_conversion_for_unit(str(entry["unit"]))
        meta = {key: value for key, value in entry.items() if key != "columns"}
        meta["scale"] = scale
        meta["offset"] = offset
        yield ["series", meta]
        for batch in _batches(entry["columns"]):
            yield ["positions", [model.column_position(name) for name in batch]]
        yield ["series_end"]

    yield ["sources", {key: value for key, value in sources.items() if key != "groups"}]
    for group in sources["groups"]:
        indices = group["indices"]
        yield [
            "source_group",
            {key: value for key, value in group.items() if key not in ("targets", "indices")},
            dict(indices) if isinstance(indices, Mapping) else None,
        ]
        for batch in _batches(group["targets"].solver_positions(model, group["role"].partition(":")[0])):
            yield ["targets", batch]
        if not isinstance(indices, Mapping):
            for batch in _batches(indices):
                yield ["indices", batch]
        yield ["source_group_end"]


def target_parameter_chunks(
    model: SparseModel,
    unit_map: Mapping[str, str],
    components: Mapping[str, Mapping[str, Any]],
    series: Sequence[Mapping[str, Any]],
    facts: Mapping[str, Any],
    sources: Mapping[str, Any],
    epigraphs: Sequence[tuple[str, tuple[str, ...]]] = (),
    *,
    stop: StopProbe | None = None,
) -> Iterator[bytes]:
    """Stream deterministic NDJSON bytes for ``input/parameters.jsonl``."""
    for record in target_parameter_records(
        model, unit_map, components, series, facts, sources, epigraphs=epigraphs
    ):
        reason = read_stop_reason(stop)
        if reason is not None:
            raise ComputationUnavailableError("生成阶段已停止", reason=reason)
        yield (
            json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
            .encode("utf-8")
            + b"\n"
        )


def epigraphs_bytes(
    model: SparseModel,
    epigraphs: Sequence[tuple[str, tuple[str, ...]]],
) -> bytes:
    """Serialize 0-based demand epigraph groups for solver-internal tightness checking."""
    payload = {
        "schema": SOLVER_EPIGRAPH_SCHEMA,
        "schema_version": SOLVER_EPIGRAPH_VERSION,
        "num_columns": len(model.columns),
        "epigraphs": [
            {
                "peak_col": model.column_position(peak_name),
                "flow_cols": [model.column_position(name) for name in flow_names],
            }
            for peak_name, flow_names in epigraphs
        ],
    }
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2).encode("utf-8")
        + b"\n"
    )


def _check_tariffs(artifact: ValidatedAssemblyArtifact, finance: EffectiveFinanceConfig) -> None:
    doc = json.loads(artifact.canonical_text)
    bindings = doc.get("tariff_bindings") or {}
    for binding_id in sorted(bindings):
        price_id = bindings[binding_id]["price"]
        kind = finance.energy_prices[price_id].kind
        if kind not in ("constant", "time_series", "monthly_daily"):
            raise ComputationUnavailableError(
                f"本 provider 只支持 constant/time_series/monthly_daily 计费价格，计费绑定 "
                f"{binding_id!r} 的价格 {price_id!r} 为 {kind!r}",
                reason="unsupported-tariff",
            )


def _check_stop(stop: StopProbe | None) -> None:
    reason = read_stop_reason(stop)
    if reason is not None:
        raise ComputationUnavailableError("生成阶段已停止", reason=reason)


def _variable_map_header_bytes(
    *,
    artifact: ValidatedAssemblyArtifact,
    generator_ref: str,
    raw_offset: float,
    objective_unit: str,
    seed: int,
    config: CalculationConfig,
    column_count: int,
) -> bytes:
    doc = json.loads(artifact.canonical_text)
    assembly = doc.get("assembly") or {}
    obj_scale, obj_const_offset = linear_conversion_for_unit(objective_unit)
    objective_offset = obj_scale * float(raw_offset) + obj_const_offset
    if not math.isfinite(obj_scale) or not math.isfinite(objective_offset):
        raise ComputationUnavailableError("目标换算参数非有限", reason="bad-unit")
    metadata = {
        "schema": VARIABLE_MAP_SCHEMA,
        "schema_version": VARIABLE_MAP_VERSION,
        "assembly": {
            "id": assembly.get("id"),
            "schema_version": doc.get("schema_version"),
        },
        "generator": generator_ref,
        "records": RECORDS_PATH,
        "column_count": column_count,
        "objective_offset": objective_offset,
        "objective_scale": obj_scale,
        "objective_unit": objective_unit,
        "outputs": list(config.outputs),
        "precision": config.precision,
        "seed": seed,
    }
    return (
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2).encode("utf-8")
        + b"\n"
    )


def generate_highs_bundle(
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    materialized: MaterializedSet,
    finance: EffectiveFinanceConfig,
    config: CalculationConfig,
    evaluation: EvaluationInput,
    *,
    generator_ref: str = GENERATOR_REF,
    bundle_contract: str = SOLVER_BUNDLE_MPS_CONTRACT,
    stop: StopProbe | None = None,
) -> SolverBundle:
    """Compile frozen stage inputs into a contract-decoupled 1.2.0 ``SolverBundle``."""
    if not isinstance(config, CalculationConfig):
        raise TypeError(f"config 须为 CalculationConfig，得到 {type(config).__name__}")
    _check_tariffs(artifact, finance)
    _check_stop(stop)

    seed = config.seed if config.seed is not None else FIXED_SEED
    epigraphs: list[tuple[str, tuple[str, ...]]] = []
    model, unit_map, objective_unit, facts, components, series, sources = build_project_model_package(
        artifact,
        models,
        materialized,
        finance,
        config,  # type: ignore[arg-type]
        evaluation,
        stop=stop,
        epigraphs=epigraphs,
    )
    _check_stop(stop)

    mps_content = GeneratedContent(partial(iter_mps_chunks, model, stop=stop))
    raw_offset = float(model.objective.constant)
    header_bytes = _variable_map_header_bytes(
        artifact=artifact,
        generator_ref=generator_ref,
        raw_offset=raw_offset,
        objective_unit=objective_unit,
        seed=seed,
        config=config,
        column_count=len(model.columns),
    )
    _check_stop(stop)

    inputs: list[dict[str, str]] = [
        {"path": PROBLEM_PATH, "media_type": "application/mps"},
        {"path": PARAMETERS_PATH, "media_type": "application/json"},
        {"path": RECORDS_PATH, "media_type": "application/x-ndjson"},
    ]
    files: dict[str, Any] = {
        PROBLEM_PATH: mps_content,
        PARAMETERS_PATH: MemoryContent(header_bytes),
        RECORDS_PATH: GeneratedContent(
            partial(
                target_parameter_chunks,
                model,
                unit_map,
                components,
                series,
                facts,
                sources,
                epigraphs=tuple(epigraphs),
                stop=stop,
            )
        ),
    }
    if epigraphs:
        _check_stop(stop)
        inputs.append({"path": EPIGRAPHS_PATH, "media_type": "application/json"})
        files[EPIGRAPHS_PATH] = MemoryContent(epigraphs_bytes(model, epigraphs))

    gen_opts = config.slot_options("generator")
    acceleration = gen_opts.get("acceleration")
    accel_mode = (
        acceleration.get("mode", DEFAULT_ACCELERATION_MODE)
        if isinstance(acceleration, Mapping)
        else DEFAULT_ACCELERATION_MODE
    )
    if accel_mode == "on_demand_mutex":
        _check_stop(stop)
        inputs.append({"path": ACCELERATION_PATH, "media_type": "application/json"})
        files[ACCELERATION_PATH] = MemoryContent(acceleration_bytes(model, sources))

    manifest = SolverBundleManifest(
        bundle={"generator": generator_ref, "contract": bundle_contract},
        inputs=inputs,
        outputs=[
            {
                "path": PRIMAL_SOLUTION_OUTPUT_PATH,
                "media_type": "application/json",
                "required": True,
            }
        ],
        extensions={},
    )
    return SolverBundle(manifest=manifest, files=files)


class HighsMilpProvider:
    """HiGHS MILP 生成器实现(无状态纯转换，输出 1.2.0 契约解耦 SolverBundle)。"""

    __slots__ = ()

    @property
    def descriptor(self) -> GeneratorDescriptor:
        """公开能力描述符（与 ``ref`` 单一来源）。"""
        return DESCRIPTOR

    @property
    def ref(self) -> str:
        """生成器精确引用（即描述符引用，不另存第二份）。"""
        return DESCRIPTOR.ref

    def available(self) -> bool:
        """纯内存实现恒可用。"""
        return True

    def generate(
        self,
        artifact: ValidatedAssemblyArtifact,
        models: Mapping[str, DeviceMathContribution],
        materialized: MaterializedSet,
        finance: EffectiveFinanceConfig,
        config: CalculationConfig,
        evaluation: EvaluationInput,
        *,
        stop: StopProbe | None = None,
    ) -> SolverBundle:
        """由直接输入生成完整 1.2.0 契约解耦 SolverBundle 聚合。"""
        return generate_highs_bundle(
            artifact,
            models,
            materialized,
            finance,
            config,
            evaluation,
            generator_ref=self.ref,
            stop=stop,
        )

    def generate_with_stop(
        self,
        artifact: ValidatedAssemblyArtifact,
        models: Mapping[str, DeviceMathContribution],
        materialized: MaterializedSet,
        finance: EffectiveFinanceConfig,
        config: CalculationConfig,
        evaluation: EvaluationInput,
        *,
        stop: StopProbe,
    ) -> SolverBundle:
        """生成器自愿提供的可停止扩展，不改变稳定 generate 协议。"""
        return self.generate(
            artifact, models, materialized, finance, config, evaluation, stop=stop,
        )


#: 内置真实生成器实例（组合根显式注册，无动态发现）。
HIGHS_MILP_PROVIDER = HighsMilpProvider()

__all__ = [
    "ASSEMBLY_SCHEMA_VERSION",
    "DEFAULT_ACCELERATION_MODE",
    "DESCRIPTOR",
    "EPIGRAPHS_PATH",
    "EVALUATOR_FINANCE_REF",
    "EXECUTOR_REF",
    "FIXED_SEED",
    "GENERATOR_ID",
    "GENERATOR_REF",
    "GENERATOR_VERSION",
    "HIGHS_MILP_PROVIDER",
    "PARAMETERS_PATH",
    "PROBLEM_PATH",
    "RECORDS_PATH",
    "SOLVER_EPIGRAPH_SCHEMA",
    "SOLVER_EPIGRAPH_VERSION",
    "SOLVER_REF",
    "SUPPORTED_OUTPUTS",
    "VARIABLE_MAP_SCHEMA",
    "VARIABLE_MAP_VERSION",
    "HighsMilpProvider",
    "epigraphs_bytes",
    "generate_highs_bundle",
    "linear_conversion_for_unit",
    "target_parameter_chunks",
    "target_parameter_records",
]
