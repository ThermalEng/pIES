"""Catalog of official built-in pIES stage plugins and `/work/in -> /work/out` contracts.

All four official computation stages are packaged in the shared runtime image
``pies-builtin-stages:1.1.5`` and executed by ``backend`` via ``job_runner``
as isolated ephemeral containers (`--network=none --read-only --cap-drop=ALL`):

1. ``ies.predictor.naive@1.0.0`` (`kind="predictor"`):
   - `/work/in/request.json`:
     `{"point_count": int, "seed": int | None, "previous": object | None, "targets": [...]}`
     where each target has `instance`, `interface`, `target_type`, `history_object_id`,
     `parameters`, and either `history` (`[float, ...]`) or `history_csv_rel` (`str`)
   - `/work/out/prediction.json`: `ies.prediction-output@1.0.0` (`predictions`, `artifacts`)
   - `/work/out/artifacts/<instance>__<interface>.json`: deterministic training artifact JSON

2. ``ies.generator.highs_milp@1.0.0`` (`kind="generator"`):
   - `/work/in/request.json` (or split `assembly.json`, `device_models.json`,
     `sequences.json`, `finance.json`, `calculation_config.json`, `evaluation.json`,
     plus optional `/work/in/prediction.json` handed off directly from Stage 1):
     frozen `artifact`, `device_models`, `materialized`, `finance`, `calculation_config`, `evaluation`
   - `/work/out/bundle/`: canonical `ies.solver-bundle@1.0.0` directory
     (`bundle.yaml`, `input/problem.mps`, `input/parameters.json`,
      `input/parameters.jsonl`, optional `input/acceleration.json`)

3. ``ies.solver.highs@1.7.2`` (`kind="solver"`):
   - `/work/in/bundle/`: read-only `ies.solver-bundle@1.0.0` directory (direct handoff
     from Stage 2 `/work/out/bundle/` without re-packing)
   - optional `/work/in/request.json`: `{"threads": int}`
   - `/work/out/solution.sol`: `HIGHS_SOLUTION_V1` text produced by `/usr/local/bin/ies-highs-solver`
   - `/work/out/solver_meta.json`: structured process timing/exit metadata

4. ``ies.result-adapter.highs@1.0.0`` (`kind="evaluator"`):
   - `/work/in/bundle/`: read-only `ies.solver-bundle@1.0.0` directory
   - `/work/in/outputs/` (or `/work/in/solution.*` / `/work/in/bundle/output/solution.*`):
     raw solver output directory handed off directly from Stage 3 `/work/out/`
   - `/work/in/receipt.json` (or `request.json["receipt"]` + `/work/in/outputs/solver_meta.json`)
   - `/work/in/request.json` (or split `finance.json` + `evaluation.json`):
     frozen `finance`, `evaluation`, `profile_id`, `bundle_id`, `receipt_id`,
     `assembly_id`, `calculation_config_id`, `attempt_id`
   - `/work/out/result.json`: `ies.compute-result@1.0.0` (`ComputeResultPayload`) whose
     `outputs` preserves `solver_summary`, `solution`, `series`, `system_series`,
     `financial_facts`, `model_sources`, `metrics`, plus same-run `financial_evaluation`
     (`ies.financial-evaluation@1.0.0`) and `hourly_series` when `solution` is solved/feasible.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

BUILTIN_STAGES_IMAGE = "pies-builtin-stages:1.1.5"
CONTAINER_PLUGINS_ROOT = "/app/plugins"


@dataclass(frozen=True, slots=True)
class BuiltinStageDescriptor:
    """Immutable descriptor for an official built-in pIES container stage plugin."""

    plugin_id: str
    version: str
    kind: str
    image: str
    entrypoint: str
    manifest_rel: str
    capabilities: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        """Canonical `<plugin_id>@<version>` reference."""
        return f"{self.plugin_id}@{self.version}"

    @property
    def container_manifest_path(self) -> str:
        """Absolute path to `plugin.yaml` inside `pies-builtin-stages:1.1.5`."""
        return f"{CONTAINER_PLUGINS_ROOT}/{self.manifest_rel}"

    @property
    def command(self) -> tuple[str, ...]:
        """Container command passed to `job_runner` `start_stage`."""
        return ("python", "-m", "pies_sdk.runner", "--manifest", self.container_manifest_path)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible dictionary copy."""
        return {
            "ref": self.ref,
            "plugin_id": self.plugin_id,
            "version": self.version,
            "kind": self.kind,
            "image": self.image,
            "entrypoint": self.entrypoint,
            "manifest_rel": self.manifest_rel,
            "container_manifest_path": self.container_manifest_path,
            "command": list(self.command),
            "capabilities": list(self.capabilities),
        }


BUILTIN_STAGE_DESCRIPTORS: tuple[BuiltinStageDescriptor, ...] = (
    BuiltinStageDescriptor(
        plugin_id="ies.predictor.naive",
        version="1.0.0",
        kind="predictor",
        image=BUILTIN_STAGES_IMAGE,
        entrypoint="predictor_stage:predict",
        manifest_rel="predictor_naive/plugin.yaml",
        capabilities=(
            "prediction.independent",
            "prediction.any_target_type",
            "prediction.requires_history",
        ),
    ),
    BuiltinStageDescriptor(
        plugin_id="ies.generator.highs_milp",
        version="1.0.0",
        kind="generator",
        image=BUILTIN_STAGES_IMAGE,
        entrypoint="generator_stage:generate",
        manifest_rel="generator_highs_milp/plugin.yaml",
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
    ),
    BuiltinStageDescriptor(
        plugin_id="ies.solver.highs",
        version="1.7.2",
        kind="solver",
        image=BUILTIN_STAGES_IMAGE,
        entrypoint="solver_stage:solve",
        manifest_rel="solver_highs/plugin.yaml",
        capabilities=(
            "solver.milp",
            "solver.lp",
            "solver.highs_solution_v1",
            "solver.on_demand_mutex",
        ),
    ),
    BuiltinStageDescriptor(
        plugin_id="ies.result-adapter.highs",
        version="1.0.0",
        kind="evaluator",
        image=BUILTIN_STAGES_IMAGE,
        entrypoint="evaluator_stage:evaluate",
        manifest_rel="evaluator_highs/plugin.yaml",
        capabilities=(
            "evaluator.highs_solution_v1",
            "evaluator.financial_facts",
            "evaluator.project_investment",
            "evaluator.hourly_series",
        ),
    ),
)

BUILTIN_STAGE_BY_REF: Mapping[str, BuiltinStageDescriptor] = MappingProxyType(
    {item.ref: item for item in BUILTIN_STAGE_DESCRIPTORS}
)


def get_builtin_stage(ref: str) -> BuiltinStageDescriptor:
    """Resolve an official built-in stage plugin descriptor by `<plugin_id>@<version>`."""
    try:
        return BUILTIN_STAGE_BY_REF[ref]
    except KeyError:
        raise KeyError(f"Unknown built-in stage plugin: {ref!r}") from None


def list_builtin_stages(kind: str | None = None) -> tuple[BuiltinStageDescriptor, ...]:
    """Return official built-in stage descriptors, optionally filtered by `kind`."""
    if kind is None:
        return BUILTIN_STAGE_DESCRIPTORS
    return tuple(item for item in BUILTIN_STAGE_DESCRIPTORS if item.kind == kind)
