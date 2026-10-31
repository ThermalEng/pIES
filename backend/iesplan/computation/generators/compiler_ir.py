"""P1 compiler's *internal*, per-generate declaration and relation inventory.

This is not a second input contract or a global registry. Keep original
modeling ASTs instead of callbacks or per-step AST copies. Only compact
source references leave this inventory in the paired Bundle metadata.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
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
    data_object_refs: tuple[str, ...] | None = None


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
class CostAmountTerm:
    """One compiled finance driver term before solver-column expansion."""

    driver: str
    local: str
    symbol: str
    target_kind: str  # property or interface
    aggregation: str
    coefficient: float


@dataclass(frozen=True, slots=True)
class CostComponent:
    instance: str
    component: str
    finance_type: str
    fixed_amount: float | None
    terms: tuple[CostAmountTerm, ...]


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
class VatRuleSource:
    rule_id: str
    rule: object


@dataclass(frozen=True, slots=True)
class VatPoolOperation:
    pool_id: str
    rules: Mapping[str, tuple]
    pool: object | None = None


@dataclass(frozen=True, slots=True)
class FinancialAmountTerm:
    """Period-bound reference to an already collected semantic amount source."""

    role: str
    source_relation: str
    coefficient: float
    start: int | None = None
    end: int | None = None
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class VatPeriodConnection:
    """One declared VAT month before conditional rows are lowered."""

    pool_id: str
    month: int
    start: int
    end: int
    opening_credit: float
    refund_claim: float
    refund_arrival: float
    amount_terms: tuple[FinancialAmountTerm, ...] = ()


@dataclass(frozen=True, slots=True)
class IncomeTaxOperation:
    rule_id: str
    rule: object


@dataclass(frozen=True, slots=True)
class IncomeYearConnection:
    """One declared income-tax year and its prior-year state edge."""

    rule_id: str
    year: int
    start: int | None
    end: int | None
    amount_terms: tuple[FinancialAmountTerm, ...] = ()


@dataclass(frozen=True, slots=True)
class IncomeLossClaim:
    """Potential ordered loss-vintage claim, declared before lowering."""

    rule_id: str
    use_year: int
    origin_kind: str  # opening or generated
    origin_index: int


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
    | TariffCharge | DemandPeak | MaintenancePayment | VatRuleSource | VatPoolOperation
    | VatPeriodConnection | IncomeTaxOperation | IncomeYearConnection | IncomeLossClaim
    | DepreciationCharge | NetCashPayment
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


@dataclass(frozen=True, slots=True)
class BoundFact:
    """Declared, intersected, or derived bound fact with provenance."""

    symbol: str
    lower: float | None = None
    upper: float | None = None
    lower_source: SourceLocation | None = None
    upper_source: SourceLocation | None = None
    model_domain: tuple[float | None, float | None] | None = None
    model_source: SourceLocation | None = None
    project_boundary: tuple[float | None, float | None] | None = None
    project_source: SourceLocation | None = None


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
        # Dependency edges describe the order of *compilation outputs*, not
        # the simultaneous physics equations; cycles in algebra are legal.
        self.dependencies = {
            op.identity: tuple(op.depends_on) for op in inventory.operations
        }
        for identity, dependencies in self.dependencies.items():
            for dependency in dependencies:
                if dependency not in self._by_id:
                    raise RuntimeError(
                        f"semantic relation {identity!r} lacks dependency {dependency!r}"
                    )
        self.uses = {name: frozenset(ids) for name, ids in uses.items()}
        self.bound_requests: list[BoundRequest] = []

    def relations(self, kind: str) -> tuple[SemanticRelation, ...]:
        return self._groups.get(kind, ())

    def operation(self, identity: str) -> SemanticRelation:
        return self._by_id[identity]

    def conversion_phases(self) -> tuple[str, ...]:
        """Resolve existing conditional compilation phases from source edges.

        A phase is scheduled once even when it contains many month/year
        relations. Source-level dependencies contribute ordering constraints;
        technical equations are intentionally excluded from this DAG.
        """
        kinds = (
            "demand", "planning_scalar", "maintenance", "vat", "income",
            "net_cash", "objective",
        )
        # Domain operations which produce the amount consumed by another
        # conversion phase but are not themselves row-generating phases.
        source_phase = {
            "annual_cash": "objective", "construction_payment": "objective",
        }
        edges = {kind: set() for kind in kinds}
        for relation in self.inventory.operations:
            kind = relation.kind
            if kind not in edges:
                continue
            for dependency in self.dependencies[relation.identity]:
                parent = self._by_id[dependency].kind
                parent = source_phase.get(parent, parent)
                if parent in edges and parent != kind and kind != "objective":
                    edges[kind].add(parent)
        # Explicit quantity dependencies are part of the existing algorithm:
        # tax bases consume the demand and maintenance amounts, cash output
        # consumes the tax states and net payments, planning cost expressions
        # may consume peak charge amounts.
        edges["planning_scalar"].add("demand")
        edges["vat"].update(("demand", "maintenance"))
        edges["income"].update(("demand", "maintenance", "vat"))
        edges["net_cash"].update(("maintenance", "income"))
        edges["objective"].update(set(kinds) - {"objective"})
        result: list[str] = []
        remaining = set(kinds)
        while remaining:
            ready = next((kind for kind in kinds
                          if kind in remaining and not (edges[kind] & remaining)), None)
            if ready is None:
                raise RuntimeError(f"conditional compilation dependencies cycle: {edges}")
            result.append(ready)
            remaining.remove(ready)
        return tuple(result)

    def bound_fact(self, name: str) -> BoundFact | None:
        """Query unified bound fact and provenance for a symbol."""
        if name in self.inventory.bound_facts:
            return self.inventory.bound_facts[name]
        if "." in name:
            inst, _, prop = name.partition(".")
            bound_name = self.inventory.property_bindings.get((inst, prop))
            if bound_name and bound_name in self.inventory.bound_facts:
                return self.inventory.bound_facts[bound_name]
        var = self.variables.get(name)
        if var is not None:
            return BoundFact(
                symbol=name,
                lower=var.lower,
                upper=var.upper,
            )
        return None

    def variable_bounds(
        self, name: str, *, relation: SemanticRelation | None = None,
        period: int | None = None,
    ) -> tuple[float | None, float | None]:
        variable = self.variables.get(name)
        if variable is None:
            resolved_name = name
            if "." in name:
                inst, _, prop = name.partition(".")
                resolved_name = self.inventory.property_bindings.get((inst, prop), name)
                variable = self.variables.get(resolved_name)
            if variable is None:
                fact = self.inventory.bound_facts.get(resolved_name) or self.inventory.bound_facts.get(name)
                if fact is not None:
                    result = (fact.lower, fact.upper)
                else:
                    raise LoweringUnsupportedError(f"查询未知求解变量 {name!r}")
            else:
                result = (variable.lower, variable.upper)
        else:
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
                    f"{label}缺有限界：变量 {name!r} 界不足，无法精确",
                    model_source=({
                        "source": f"relation:{relation.identity}", "role": "bound",
                        "axis": relation.domain.kind, "index": period or 0,
                        "location": {k: v for k, v in asdict(relation.source).items() if v is not None},
                    } if relation is not None else None),
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
    bound_facts: dict[str, BoundFact] = field(default_factory=dict)
    sealed: bool = False

    def collect(self, operation: SemanticRelation) -> None:
        if self.sealed:
            raise RuntimeError("no source relations after collection barrier")
        self.operations.append(operation)

    def register_bound_fact(self, fact: BoundFact) -> None:
        if self.sealed:
            raise RuntimeError("no bound facts after collection barrier")
        self.bound_facts[fact.symbol] = fact

    def seal(self) -> None:
        self.sealed = True

    @property
    def all_relations(self) -> tuple[RelationFamily | SemanticRelation, ...]:
        return (*self.relations, *self.operations)
