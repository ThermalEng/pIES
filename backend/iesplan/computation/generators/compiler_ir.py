"""P1 compiler's *internal*, per-generate declaration and relation inventory.

This is not a second input contract, a global registry, or a persisted Bundle
schema.  Keep original modeling ASTs instead of callbacks or per-step AST copies.
Later slices will add the remaining domains to this same inventory.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
import math

from iesplan.computation.generators.linear import (
    LinearConstraint, LinearExpression, LinearVariable, LoweringUnsupportedError,
    LoweredRelation, lower_mutex,
)
from iesplan.core.contracts.planning_expression import PlanningExpression

from iesplan.modeling import MathRelation


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Separate the assembly instance from its reusable model definition."""

    field_path: str
    instance_id: str | None = None
    model_id: str | None = None
    model_revision: str | None = None
    relation_id: str | None = None
    binding_id: str | None = None
    data_object_ref: str | None = None


@dataclass(frozen=True, slots=True)
class IndexDomain:
    """The original relation is stored once, not expanded for every step."""

    kind: str  # scalar, step, month, or year
    count: int = 1


@dataclass(frozen=True, slots=True)
class SymbolDeclaration:
    identity: str
    role: str  # property, planning, flow, state, or internal
    domain: IndexDomain
    unit: str
    source: SourceLocation


@dataclass(frozen=True, slots=True)
class KnownCoefficient:
    """Known data source, never a substitute for a planning decision."""

    identity: str
    mode: str  # fixed_property, step_duration, or materialized_series
    source: SourceLocation
    value: float | None = None


@dataclass(frozen=True, slots=True)
class RelationFamily:
    """Inspectable mathematical AST and its source/dependencies.

    The output and referenced identities are local model symbols; instance
    binding resolves them to declared symbols before matrix expansion.
    """

    identity: str
    relation: MathRelation
    domain: IndexDomain
    referenced_symbols: tuple[str, ...]
    output_symbol: str | None
    known_coefficients: tuple[str, ...]
    source: SourceLocation


@dataclass(frozen=True, slots=True)
class OutputReference:
    identity: str
    symbol_id: str
    domain: IndexDomain
    source: SourceLocation



# These are *source* relations, not post-lowering MPS rows and not callbacks.
# In particular, tax and month-peak operations are semantic relations with
# ordered period / amount references. Their exact auxiliary columns are
# produced only after the collection has been sealed and analyzed.
@dataclass(frozen=True, slots=True)
class CapacityLimit:
    instance: str
    local: str
    planning_symbol: str
    sense: str


@dataclass(frozen=True, slots=True)
class StateInitial:
    instance: str
    state: str
    value: float | LinearExpression


@dataclass(frozen=True, slots=True)
class BusBalance:
    carrier: str
    bus_index: int
    ports: tuple[tuple[str, str, float], ...]


@dataclass(frozen=True, slots=True)
class PlanningRule:
    name: str
    template: LinearConstraint
    step: bool
    references: Mapping[str, Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class CostComponent:
    instance: str
    component: str
    finance_type: str
    value: object


@dataclass(frozen=True, slots=True)
class TariffCharge:
    binding_id: str
    instance: str
    interface: str
    aggregation: str
    price_id: str


@dataclass(frozen=True, slots=True)
class DemandPeak:
    billing_id: str
    instance: str
    interface: str
    charge_id: str
    month: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class MaintenancePayment:
    year: int
    position: int
    entry: object


@dataclass(frozen=True, slots=True)
class VatPoolOperation:
    pool_id: str
    rules: Mapping[str, list]


@dataclass(frozen=True, slots=True)
class IncomeTaxOperation:
    rule_id: str
    rule: object


@dataclass(frozen=True, slots=True)
class DepreciationCharge:
    rule_id: str
    entry: object


@dataclass(frozen=True, slots=True)
class NetCashPayment:
    cash_id: str
    entry: object


@dataclass(frozen=True, slots=True)
class ConstructionPayment:
    year: int
    ratio: float


@dataclass(frozen=True, slots=True)
class AnnualCash:
    year: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ProjectObjective:
    expression: str
    compiled: PlanningExpression | None


SemanticPayload = (
    CapacityLimit | StateInitial | BusBalance | PlanningRule | CostComponent
    | TariffCharge | DemandPeak | MaintenancePayment | VatPoolOperation
    | IncomeTaxOperation | DepreciationCharge | NetCashPayment
    | ConstructionPayment | AnnualCash | ProjectObjective
)


@dataclass(frozen=True, slots=True)
class SemanticRelation:
    identity: str
    kind: str
    domain: IndexDomain
    payload: SemanticPayload
    source: SourceLocation
    input_symbols: tuple[str, ...] = ()
    output_symbols: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    requires_bounds: bool = False


@dataclass(frozen=True, slots=True)
class BoundRequest:
    """An actual conditional-lowering request, not global bound propagation."""

    relation_id: str
    source: SourceLocation
    period: int | None
    side: str
    symbols: tuple[str, ...]
    value: float | None


class CompilerAnalysis:
    """One read-only relation/usage view and one existing-box-bound entrypoint.

    Physics is a simultaneous constraint system, not an expression DAG.
    Only conversion dependencies order auxiliary operations.
    P2 can replace the interval logic here without changing the consumers.
    """

    def __init__(self, inventory: CompilerInventory, variables: Mapping[str, LinearVariable]):
        if not inventory.sealed:
            raise RuntimeError("relation collection must be complete before analysis")
        self.inventory = inventory
        self.variables = variables
        groups: dict[str, list[SemanticRelation]] = defaultdict(list)
        uses: dict[str, set[str]] = defaultdict(set)
        for family in inventory.relations:
            inst = family.source.instance_id
            for local in family.referenced_symbols:
                name = inventory.property_bindings.get((inst, local), f"{inst}.{local}")
                uses[name].add(family.identity)
        for operation in inventory.operations:
            groups[operation.kind].append(operation)
            for symbol in operation.input_symbols:
                uses[symbol].add(operation.identity)
        self._groups = {key: tuple(value) for key, value in groups.items()}
        self._by_id = {op.identity: op for op in inventory.operations}
        self.uses = {name: frozenset(ids) for name, ids in uses.items()}
        self.bound_requests: list[BoundRequest] = []

    def relations(self, kind: str) -> tuple[SemanticRelation, ...]:
        return self._groups.get(kind, ())

    def operation(self, identity: str) -> SemanticRelation:
        return self._by_id[identity]

    def variable_bounds(
        self, name: str, *, relation: SemanticRelation | None = None,
        period: int | None = None,
    ) -> tuple[float | None, float | None]:
        variable = self.variables.get(name)
        if variable is None:
            raise LoweringUnsupportedError(f"查询未知求解变量 {name!r}")
        result = (variable.lower, variable.upper)
        if relation is not None:
            self.bound_requests.extend(
                BoundRequest(relation.identity, relation.source, period, side,
                             (name,), value)
                for side, value in zip(("min", "max"), result)
            )
        return result

    def affine_side(
        self, affine: object, *, side: str, label: str,
        relation: SemanticRelation | None = None, period: int | None = None,
    ) -> float:
        # Both the income and VAT lowerers share exactly this existing
        # coefficient-sign box interval; no invented Big-M / global propagation.
        total = affine.constant
        for name, coeff in affine.terms.items():
            if coeff == 0.0:
                continue
            lo, hi = self.variable_bounds(name)
            value = hi if (coeff > 0.0) == (side == "max") else lo
            if value is None or not math.isfinite(value):
                raise LoweringUnsupportedError(
                    f"{label}缺有限界：变量 {name!r} 界不足，无法精确"
                )
            total += coeff * value
        if relation is not None:
            self.bound_requests.append(BoundRequest(
                relation.identity, relation.source, period, side,
                tuple(sorted(k for k, v in affine.terms.items() if v)), total,
            ))
        return total

    def modeling_symbols(
        self, instance: str, step: int, symbols: Mapping[str, tuple],
    ) -> dict[str, tuple]:
        """Use the same bound authority for technical mutex at each step."""
        result = dict(symbols)
        for local, entry in symbols.items():
            if entry[0] != "var":
                continue
            global_name = self.inventory.property_bindings.get((instance, local))
            name = global_name or f"{instance}__{local}__t{step}"
            if name in self.variables:
                lo, hi = self.variable_bounds(name)
                result[local] = ("var", lo, hi)
        return result

    def mutex(
        self, name: str, x: str, y: str,
        *, relation: SemanticRelation, period: int | None = None,
    ) -> LoweredRelation:
        lo_x, hi_x = self.variable_bounds(x, relation=relation, period=period)
        lo_y, hi_y = self.variable_bounds(y, relation=relation, period=period)
        return lower_mutex(
            name, (x, 0), (y, 0),
            {x: ("var", lo_x, hi_x), y: ("var", lo_y, hi_y)},
        )


@dataclass(slots=True)
class CompilerInventory:
    """Local staging only: typed symbols, alias binding, relations and outputs."""

    symbols: dict[str, SymbolDeclaration] = field(default_factory=dict)
    property_bindings: dict[tuple[str, str], str] = field(default_factory=dict)
    coefficients: dict[str, KnownCoefficient] = field(default_factory=dict)
    relations: list[RelationFamily] = field(default_factory=list)
    outputs: list[OutputReference] = field(default_factory=list)
    operations: list[SemanticRelation] = field(default_factory=list)
    sealed: bool = False

    def collect(self, operation: SemanticRelation) -> None:
        if self.sealed:
            raise RuntimeError("no source relations after collection barrier")
        self.operations.append(operation)

    def seal(self) -> None:
        self.sealed = True

    @property
    def all_relations(self) -> tuple[RelationFamily | SemanticRelation, ...]:
        return (*self.relations, *self.operations)
