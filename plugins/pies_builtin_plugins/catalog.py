"""Catalog of official built-in pIES stage plugins and `/work/in -> /work/out` contracts.

All four official computation stages are packaged in the shared runtime image
``pies-builtin-stages:1.2.0`` and executed by ``backend`` via ``job_runner``
as isolated ephemeral containers (`--network=none --read-only --cap-drop=ALL`):

1. ``ies.predictor.naive@1.0.0`` (`kind="predictor"`):
   - `/work/in/request.json`:
     `{"point_count": int, "seed": int | None, "targets": [...]}`
     where each target has `instance`, `interface`, `target_type`, `history_object_id`,
     `parameters`, and either `history` (`[float, ...]`) or `history_csv_rel` (`str`)
   - `/work/out/prediction.json`: `ies.prediction-output@1.0.0` (`predictions`, `artifacts`)
   - `/work/out/artifacts/<instance>__<interface>.json`: deterministic training artifact JSON

2. ``ies.generator.highs_milp@1.0.0`` (`kind="generator"`):
   - `/work/in/request.json` (or split `assembly.json`, `device_models.json`,
     `sequences.json` / `frozen_series_inputs`, `finance.json`, `calculation_config.json`,
     `evaluation.json`, plus optional `/work/in/prediction.json`):
     frozen `artifact`, `device_models`, `materialized`/`frozen_series_inputs`,
     `finance`, `calculation_config`, `evaluation`
   - `/work/out/bundle/`: canonical `ies.solver-bundle@1.0.0` directory (`ies.solver-bundle.mps@1.0.0`)
     (`bundle.yaml`, `input/problem.mps`, `input/parameters.json`,
      `input/parameters.jsonl`, optional `input/acceleration.json`, `input/epigraphs.json`)

3. ``ies.solver.highs@1.7.2`` (`kind="solver"`):
   - `/work/in/bundle/`: read-only `ies.solver-bundle@1.0.0` directory (`ies.solver-bundle.mps@1.0.0`)
   - optional `/work/in/request.json` / `calculation_config.json` (`time_limit_seconds`, `seed`, `threads`)
   - `/work/out/primal_solution.json`: strictly 0-based `ies.primal-solution@1.0.0`

4. ``ies.evaluator.finance@1.0.0`` (`kind="evaluator"`):
   - `/work/in/solution.json`: canonical `ies.engineering-solution@1.0.0`
   - `/work/in/{assembly,device_models,sequences/frozen_series_inputs,finance,evaluation}.json`:
     frozen read-only context hardlinked by `backend`
   - `/work/out/financial_evaluation.json`: canonical `ies.financial-evaluation@1.0.0` JSON.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

BUILTIN_STAGES_IMAGE = "pies-builtin-stages:1.2.0"
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
        """Absolute path to `plugin.yaml` inside `pies-builtin-stages:1.2.0`."""
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
            "solver.primal_solution",
            "solver.on_demand_mutex",
        ),
    ),
    BuiltinStageDescriptor(
        plugin_id="ies.evaluator.finance",
        version="1.0.0",
        kind="evaluator",
        image=BUILTIN_STAGES_IMAGE,
        entrypoint="finance_evaluator_stage:evaluate",
        manifest_rel="evaluator_finance/plugin.yaml",
        capabilities=(
            "evaluator.engineering_solution",
            "evaluator.financial_evaluation",
            "evaluator.project_investment",
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
