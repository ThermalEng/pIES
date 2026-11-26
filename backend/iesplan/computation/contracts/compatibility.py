"""Static consumes/produces and per-stage parameters_schema compatibility checker for 1.2.0."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from iesplan.computation.contracts.common import (
    check_parameters_against_schema,
    validate_parameters_schema,
)
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import CalculationConfig, check_provider_ref, freeze_json_value

PLUGIN_KINDS = ("predictor", "generator", "solver", "evaluator", "algorithm")

STAGE_INPUT_CONTRACT = "ies.stage-input@1.0.0"
PREDICTION_OUTPUT_CONTRACT = "ies.prediction-output@1.0.0"
SOLVER_BUNDLE_MPS_CONTRACT = "ies.solver-bundle.mps@1.0.0"
PRIMAL_SOLUTION_CONTRACT = "ies.primal-solution@1.0.0"
ENGINEERING_SOLUTION_CONTRACT = "ies.engineering-solution@1.0.0"
FINANCIAL_EVALUATION_CONTRACT = "ies.financial-evaluation@1.0.0"
EVALUATION_REPORT_CONTRACT = "ies.evaluation-report@1.0.0"
SUPPORTED_EVALUATOR_OUTPUT_CONTRACTS = frozenset(
    {FINANCIAL_EVALUATION_CONTRACT, EVALUATION_REPORT_CONTRACT}
)


def _freeze_contract_list(values: object, name: str) -> tuple[str, ...]:
    if isinstance(values, Mapping) or not isinstance(values, (list, tuple)):
        raise TypeError(f"{name} 须为契约引用序列，得到 {type(values).__name__}")
    items = tuple(check_provider_ref(item, name) for item in values)
    if len(set(items)) != len(items):
        raise ValueError(f"{name} 契约引用不得重复: {list(items)!r}")
    return items


@dataclass(frozen=True, slots=True)
class StagePluginSpec:
    """Immutable plugin contract specification used for static pipeline compatibility checks."""

    plugin_id: str
    version: str
    kind: str
    parameters_schema: Mapping[str, object]
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    precisions: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    companions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_provider_ref(f"{self.plugin_id}@{self.version}", "plugin")
        if self.kind not in PLUGIN_KINDS:
            raise ValueError(f"插件 kind 非法: {self.kind!r}")
        object.__setattr__(
            self,
            "parameters_schema",
            validate_parameters_schema(self.parameters_schema, "parameters_schema"),
        )
        object.__setattr__(self, "consumes", _freeze_contract_list(self.consumes, "consumes"))
        object.__setattr__(self, "produces", _freeze_contract_list(self.produces, "produces"))
        for attr in ("modes", "outputs"):
            raw = tuple(getattr(self, attr))
            if any(not isinstance(item, str) or not item for item in raw):
                raise ValueError(f"{attr} 条目须为非空字符串")
            object.__setattr__(self, attr, raw)
        precisions = tuple(self.precisions)
        if any(p not in ("low", "medium", "high") for p in precisions):
            raise ValueError(f"precisions 条目非法: {precisions!r}")
        object.__setattr__(self, "precisions", precisions)
        if not isinstance(self.companions, Mapping):
            raise TypeError("companions 须为 Mapping")
        normalized_companions: dict[str, str] = {}
        for role, ref in self.companions.items():
            if role not in ("predictor", "generator", "solver", "evaluator"):
                raise ValueError(f"companions 角色非法: {role!r}")
            normalized_companions[role] = check_provider_ref(ref, f"companions.{role}")
        object.__setattr__(self, "companions", freeze_json_value(normalized_companions))

    @property
    def ref(self) -> str:
        """Canonical '<plugin_id>@<version>' reference."""
        return f"{self.plugin_id}@{self.version}"

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> StagePluginSpec:
        """Build a StagePluginSpec from a validated plugin.yaml / PluginManifest dict."""
        advanced = manifest.get("advanced") or {}
        if not isinstance(advanced, Mapping):
            raise TypeError("manifest.advanced 须为 Mapping")
        extensions = manifest.get("extensions") or {}
        if not isinstance(extensions, Mapping):
            raise TypeError("manifest.extensions 须为 Mapping")
        return cls(
            plugin_id=manifest["plugin_id"],
            version=manifest["version"],
            kind=manifest["kind"],
            parameters_schema=manifest["parameters_schema"],
            consumes=tuple(manifest.get("consumes") or ()),
            produces=tuple(manifest.get("produces") or ()),
            modes=tuple(advanced.get("supported_modes") or ()),
            precisions=tuple(advanced.get("supported_precisions") or ()),
            outputs=tuple(extensions.get("supported_outputs") or ()),
            companions=dict(advanced.get("companions") or {}),
        )


def _resolve_plugin(
    plugins: Mapping[str, StagePluginSpec],
    ref: str,
    expected_kind: str,
) -> StagePluginSpec:
    spec = plugins.get(ref)
    if spec is None:
        raise ComputationUnavailableError(
            f"所选 {expected_kind} 插件未注册或不可用: {ref!r}",
            reason="no-provider",
        )
    if spec.kind != expected_kind:
        raise ComputationUnavailableError(
            f"插件类型不匹配: {ref!r} 为 {spec.kind!r}, 期望 {expected_kind!r}",
            reason="incompatible-kind",
        )
    return spec


def _check_companions(spec: StagePluginSpec, config: CalculationConfig) -> None:
    companions = spec.companions
    if not companions:
        return
    if "generator" in companions and config.generator != companions["generator"]:
        raise ComputationUnavailableError(
            f"{spec.ref!r} 要求 companion generator={companions['generator']!r}, "
            f"实际为 {config.generator!r}",
            reason="incompatible-companion",
        )
    if "solver" in companions and config.solver != companions["solver"]:
        raise ComputationUnavailableError(
            f"{spec.ref!r} 要求 companion solver={companions['solver']!r}, "
            f"实际为 {config.solver!r}",
            reason="incompatible-companion",
        )
    if "predictor" in companions:
        expected_pred = companions["predictor"]
        actual_preds = [str(p["algorithm"]) for p in config.predictions]
        if not actual_preds or any(ref != expected_pred for ref in actual_preds):
            raise ComputationUnavailableError(
                f"{spec.ref!r} 要求 companion predictor={expected_pred!r}",
                reason="incompatible-companion",
            )
    if "evaluator" in companions:
        expected_eval = companions["evaluator"]
        actual_evals = {str(e["evaluator"]) for e in config.evaluators}
        if expected_eval not in actual_evals:
            raise ComputationUnavailableError(
                f"{spec.ref!r} 要求 companion evaluator={expected_eval!r}",
                reason="incompatible-companion",
            )


def _check_mode_and_precision(
    core_spec: StagePluginSpec,
    selected_specs: list[StagePluginSpec],
    config: CalculationConfig,
) -> None:
    if not core_spec.modes:
        raise ComputationUnavailableError(
            f"核心计算插件 {core_spec.ref!r} 未声明支持的 modes",
            reason="incompatible-mode",
        )
    if not core_spec.precisions:
        raise ComputationUnavailableError(
            f"核心计算插件 {core_spec.ref!r} 未声明支持的 precisions",
            reason="incompatible-precision",
        )
    for spec in selected_specs:
        if spec.modes and config.mode not in spec.modes:
            raise ComputationUnavailableError(
                f"插件 {spec.ref!r} 不支持计算模式 {config.mode!r}",
                reason="incompatible-mode",
            )
        if spec.precisions and config.precision not in spec.precisions:
            raise ComputationUnavailableError(
                f"插件 {spec.ref!r} 不支持计算精度 {config.precision!r}",
                reason="incompatible-precision",
            )


def _check_supported_outputs(
    core_spec: StagePluginSpec,
    eval_specs: list[StagePluginSpec],
    config: CalculationConfig,
) -> None:
    if not core_spec.outputs:
        raise ComputationUnavailableError(
            f"核心计算插件 {core_spec.ref!r} 未声明支持的 outputs",
            reason="incompatible-outputs",
        )
    supported_outputs = set(core_spec.outputs)
    for ev_spec in eval_specs:
        if not ev_spec.outputs:
            raise ComputationUnavailableError(
                f"评价器插件 {ev_spec.ref!r} 未声明支持的 outputs",
                reason="incompatible-outputs",
            )
        supported_outputs.update(ev_spec.outputs)
    unsupported = tuple(out for out in config.outputs if out not in supported_outputs)
    if unsupported:
        raise ComputationUnavailableError(
            f"所选插件组合不支持 outputs: {unsupported!r}",
            reason="incompatible-outputs",
        )


def validate_stage_compatibility(
    config: CalculationConfig,
    plugins: Mapping[str, StagePluginSpec],
) -> None:
    """Verify static consumes/produces compatibility, capabilities, and per-stage parameters_schema."""
    selected_specs: list[StagePluginSpec] = []

    # 1. Resolve predictors and validate each prediction entry's contracts & parameters_schema.
    for idx, pred in enumerate(config.predictions):
        pred_ref = str(pred["algorithm"])
        pred_spec = _resolve_plugin(plugins, pred_ref, "predictor")
        selected_specs.append(pred_spec)
        if STAGE_INPUT_CONTRACT not in pred_spec.consumes:
            raise ComputationUnavailableError(
                f"预测插件 {pred_ref!r} 须声明消费阶段输入契约 {STAGE_INPUT_CONTRACT!r}",
                reason="incompatible-contracts",
            )
        if PREDICTION_OUTPUT_CONTRACT not in pred_spec.produces:
            raise ComputationUnavailableError(
                f"预测插件 {pred_ref!r} 须声明产出 {PREDICTION_OUTPUT_CONTRACT!r}",
                reason="incompatible-contracts",
            )
        check_parameters_against_schema(
            pred_spec.parameters_schema,
            pred["parameters"],  # type: ignore[arg-type]
            location_label=f"predictions/{idx}/parameters",
        )

    # 2. Resolve core optimization stage (stepped generator+solver vs unified algorithm).
    if config.is_unified:
        assert config.algorithm is not None
        core_spec = _resolve_plugin(plugins, config.algorithm, "algorithm")
        selected_specs.append(core_spec)
        if STAGE_INPUT_CONTRACT not in core_spec.consumes:
            raise ComputationUnavailableError(
                f"一体化算法 {core_spec.ref!r} 须声明消费阶段输入契约 {STAGE_INPUT_CONTRACT!r}",
                reason="incompatible-contracts",
            )
        if ENGINEERING_SOLUTION_CONTRACT not in core_spec.produces:
            raise ComputationUnavailableError(
                f"一体化算法 {core_spec.ref!r} 须声明产出 {ENGINEERING_SOLUTION_CONTRACT!r}",
                reason="incompatible-contracts",
            )
        check_parameters_against_schema(
            core_spec.parameters_schema,
            config.slot_options("algorithm"),
            location_label="options/algorithm",
        )
        downstream_available_contracts = set(core_spec.produces)
    else:
        assert config.generator is not None and config.solver is not None
        gen_spec = _resolve_plugin(plugins, config.generator, "generator")
        sol_spec = _resolve_plugin(plugins, config.solver, "solver")
        core_spec = gen_spec
        selected_specs.extend([gen_spec, sol_spec])

        if STAGE_INPUT_CONTRACT not in gen_spec.consumes:
            raise ComputationUnavailableError(
                f"生成器 {gen_spec.ref!r} 须声明消费阶段输入契约 {STAGE_INPUT_CONTRACT!r}",
                reason="incompatible-contracts",
            )
        if not gen_spec.produces:
            raise ComputationUnavailableError(
                f"生成器 {gen_spec.ref!r} 未声明 produces 求解包契约",
                reason="incompatible-contracts",
            )
        if not sol_spec.consumes or not (set(gen_spec.produces) & set(sol_spec.consumes)):
            raise ComputationUnavailableError(
                f"生成器 {gen_spec.ref!r} produces={list(gen_spec.produces)!r} "
                f"与求解器 {sol_spec.ref!r} consumes={list(sol_spec.consumes)!r} 不匹配",
                reason="incompatible-contracts",
            )
        if PRIMAL_SOLUTION_CONTRACT not in sol_spec.produces:
            raise ComputationUnavailableError(
                f"求解器 {sol_spec.ref!r} 须声明产出 {PRIMAL_SOLUTION_CONTRACT!r}",
                reason="incompatible-contracts",
            )
        check_parameters_against_schema(
            gen_spec.parameters_schema,
            config.slot_options("generator"),
            location_label="options/generator",
        )
        check_parameters_against_schema(
            sol_spec.parameters_schema,
            config.slot_options("solver"),
            location_label="options/solver",
        )
        # Backend restricted-subprocess ResultAdapter restores primal-solution -> engineering-solution.
        downstream_available_contracts = {ENGINEERING_SOLUTION_CONTRACT}

    # 3. Resolve parallel evaluators and validate consumes/produces & parameters_schema.
    eval_specs: list[StagePluginSpec] = []
    for idx, ev in enumerate(config.evaluators):
        ev_ref = str(ev["evaluator"])
        ev_spec = _resolve_plugin(plugins, ev_ref, "evaluator")
        selected_specs.append(ev_spec)
        eval_specs.append(ev_spec)
        if ENGINEERING_SOLUTION_CONTRACT not in ev_spec.consumes or not (
            set(ev_spec.consumes) & downstream_available_contracts
        ):
            raise ComputationUnavailableError(
                f"评价器 {ev_ref!r} consumes={list(ev_spec.consumes)!r} "
                f"与上游产出 {sorted(downstream_available_contracts)!r} 不匹配",
                reason="incompatible-contracts",
            )
        if not (set(ev_spec.produces) & SUPPORTED_EVALUATOR_OUTPUT_CONTRACTS):
            raise ComputationUnavailableError(
                f"评价器 {ev_ref!r} produces={list(ev_spec.produces)!r} "
                f"不支持标准评价输出契约 ({sorted(SUPPORTED_EVALUATOR_OUTPUT_CONTRACTS)!r})",
                reason="incompatible-contracts",
            )
        check_parameters_against_schema(
            ev_spec.parameters_schema,
            ev["parameters"],  # type: ignore[arg-type]
            location_label=f"evaluators/{idx}/parameters",
        )

    # 4. Check explicit mode, precision, outputs, and optional companions constraints.
    _check_mode_and_precision(core_spec, selected_specs, config)
    _check_supported_outputs(core_spec, eval_specs, config)
    for spec in selected_specs:
        _check_companions(spec, config)
