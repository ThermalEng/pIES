"""P2-S2: relation-based bound inference and expression bounds.

Infers conservative effective bounds from the complete relation inventory:
- Affine equalities, inequalities, capacity limits, bus balances, and state transitions.
- Evaluates affine expressions retaining variable associations (not just independent intervals).
- Handles positive, negative, and zero coefficients, initial states, and step-varying coefficients.
- Deterministic sparse dependency propagation with strict work budget and stopping rules.
- Does not guess Big-M and does not launch a sub-solver.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any

from iesplan.computation.generators.compiler_ir import (
    AmountConstraintRule,
    BoundFact,
    BusBalance,
    CapacityLimit,
    ClosedPort,
    CompilerInventory,
    IndexDomain,
    KnownCoefficient,
    PlanningRule,
    RelationFamily,
    SemanticRelation,
    SourceLocation,
    StateInitial,
    SymbolDeclaration,
)
from iesplan.computation.generators.linear import (
    BoundConflictError,
    LinearConstraint,
    LinearExpression,
    LinearVariable,
    LoweringUnsupportedError,
)
from iesplan.computation.generators.templates import bind_technical, compile_technical
from iesplan.modeling import BinaryNode, RefNode, UnaryNode

_TOLERANCE = 1e-12


@dataclass(frozen=True, slots=True)
class AffineRelation:
    """An exact affine relation: sum(terms[symbol] * symbol) + constant (sense) 0."""

    terms: tuple[tuple[str, float], ...]  # sorted (symbol, coeff) pairs
    constant: float = 0.0
    sense: str = "eq"  # "eq", "le", "ge"
    source: SourceLocation | None = None
    relation_id: str | None = None
    axis: str = "scalar"
    index: int = 0

    def term_dict(self) -> dict[str, float]:
        return dict(self.terms)


@dataclass(frozen=True, slots=True)
class PropagationStats:
    """Observable work performed by one bound-propagation pass.

    The counters describe the analysis pass only. They are intentionally
    separate from mathematical results so performance investigations cannot
    change the conservative bound semantics.
    """

    relation_count: int
    budget: int
    step_limit: int
    steps: int
    bound_updates: int
    requeues: int
    self_requeues: int
    dependent_requeues: int
    max_queue_size: int
    budget_exhausted: bool
    incomplete_symbols: tuple[str, ...]


def _reference_names(node: object) -> set[str]:
    """Collect local names only; P1's compiler owns expression arithmetic."""
    if isinstance(node, RefNode):
        return {node.name}
    if isinstance(node, UnaryNode):
        return _reference_names(node.operand)
    if isinstance(node, BinaryNode):
        return _reference_names(node.left) | _reference_names(node.right)
    return set()


class BoundInferenceEngine:
    """Deterministic bound derivation and associated expression query engine."""

    def __init__(
        self,
        inventory: CompilerInventory,
        variables: Mapping[str, LinearVariable],
        *,
        budget: int = 5000,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        self.inventory = inventory
        self.variables = variables
        self.checkpoint = checkpoint
        # References to the final affine rows already bound for analysis.
        # Production reuses these objects; no second mathematical model or
        # additional copy of their coefficients is constructed.
        self.technical_rows: dict[str, list[tuple[LinearConstraint, ...] | None]] = {}
        self.bounds: dict[str, tuple[float | None, float | None]] = {}
        self.provenance: dict[str, tuple[SourceLocation | None, SourceLocation | None]] = {}
        self.relations: list[AffineRelation] = []
        self.equalities: dict[str, tuple[dict[str, Fraction], Fraction]] = {}  # exact affine substitutions
        self.inequalities: list[AffineRelation] = []
        self.conflicts: list[dict[str, Any]] = []
        self.budget_exhausted = False
        self.incomplete_symbols: frozenset[str] = frozenset()
        self.propagation_stats = PropagationStats(
            relation_count=0,
            budget=0,
            step_limit=0,
            steps=0,
            bound_updates=0,
            requeues=0,
            self_requeues=0,
            dependent_requeues=0,
            max_queue_size=0,
            budget_exhausted=False,
            incomplete_symbols=(),
        )
        self._indexes_ready = False
        # Sparse reverse index: do not rescan every per-step inequality for
        # each annual VAT / income expression bound request.
        self._inequalities_by_symbol: dict[str, list[int]] = defaultdict(list)

        self._init_bounds()
        self._collect_relations()
        # A contradictory source relation belongs to the exact model. Analysis
        # keeps all rows for the solver and only reports the conflict if a
        # downstream exact lowering would otherwise misreport a missing bound.
        declared_bounds = dict(self.bounds)
        declared_provenance = dict(self.provenance)
        self._propagate(budget=max(budget, 4 * len(self.relations)))
        if self.conflicts:
            self.bounds = declared_bounds
            self.provenance = declared_provenance

    def _init_bounds(self) -> None:
        # Load from inventory.bound_facts
        for sym, fact in self.inventory.bound_facts.items():
            self.bounds[sym] = (fact.lower, fact.upper)
            self.provenance[sym] = (fact.lower_source, fact.upper_source)

        # Materialize every explicit time slot's *own* domain. The same
        # declared box may be reused at each time, but derived bounds cannot.
        for (inst, local), columns in self.inventory.step_columns.items():
            fact = self.inventory.bound_facts.get(f"{inst}.{local}")
            if fact is None:
                continue
            for column in columns:
                self.bounds[column] = (fact.lower, fact.upper)
                self.provenance[column] = (fact.lower_source, fact.upper_source)

        # Load from variables
        for name, var in self.variables.items():
            cur_lo, cur_hi = self.bounds.get(name, (None, None))
            lo = cur_lo if var.lower is None else (var.lower if cur_lo is None else max(cur_lo, var.lower))
            hi = cur_hi if var.upper is None else (var.upper if cur_hi is None else min(cur_hi, var.upper))
            self.bounds[name] = (lo, hi)
            # Check for immediate contradiction
            if lo is not None and hi is not None and lo > hi:
                raise LoweringUnsupportedError(
                    f"变量 {name!r} 初始界冲突 (lower={lo} > upper={hi})",
                    model_source={
                        "source": f"variable:{name}",
                        "role": "bound_conflict",
                        "axis": "scalar",
                        "index": 0,
                    },
                )

    def _add_affine(
        self,
        terms: Mapping[str, float],
        constant: float,
        sense: str = "eq",
        source: SourceLocation | None = None,
        relation_id: str | None = None,
        *,
        axis: str = "scalar",
        index: int = 0,
    ) -> None:
        filtered = tuple(sorted((k, v) for k, v in terms.items() if v != 0.0))
        if not filtered and constant != 0.0:
            # 0 (sense) -constant
            if sense == "eq" or (sense == "le" and constant > 0) or (sense == "ge" and constant < 0):
                rel = AffineRelation((), constant, sense, source, relation_id, axis, index)
                self.relations.append(rel)
                self._record_conflict("<constant>", rel)
            return
        if not filtered:
            return

        for sym, _ in filtered:
            if sym not in self.bounds:
                # Indexed identities are not interchangeable with their base.
                self.bounds[sym] = (None, None)

        rel = AffineRelation(filtered, constant, sense, source, relation_id, axis, index)
        self.relations.append(rel)
        if self._indexes_ready:
            self._index_relation(rel)

    def _index_relations(self) -> None:
        """Build deterministic equality and inequality indexes after collection."""
        source_keys: dict[int, str] = {}

        def key(rel: AffineRelation) -> tuple:
            if self.checkpoint is not None:
                self.checkpoint()
            source = ""
            if rel.source is not None:
                source = source_keys.get(id(rel.source))
                if source is None:
                    source = repr(asdict(rel.source))
                    source_keys[id(rel.source)] = source
            if rel.relation_id is not None:
                # A relation family is already expanded in its declared domain
                # order. Keep that order within the family: sorting solver
                # column names lexically puts t10 before t2 and can turn a
                # single forward pass into thousands of worklist retries.
                return (0, rel.relation_id, source)
            return (1, rel.terms, rel.sense, rel.constant, source)

        self.equalities.clear()
        self.inequalities.clear()
        self._inequalities_by_symbol.clear()
        for rel in sorted(self.relations, key=key):
            if self.checkpoint is not None:
                self.checkpoint()
            self._index_relation(rel)
        self._indexes_ready = True

    def _index_relation(self, rel: AffineRelation) -> None:
        if not rel.terms:
            return
        if rel.sense == "eq":
            lead_var = next(
                (name for name, coeff in rel.terms if coeff in (1.0, -1.0)),
                rel.terms[0][0],
            )
            lead_coeff = next(coeff for name, coeff in rel.terms if name == lead_var)
            divisor = Fraction.from_float(lead_coeff)
            sub_terms = {
                name: -Fraction.from_float(coeff) / divisor
                for name, coeff in rel.terms if name != lead_var
            }
            sub_const = -Fraction.from_float(rel.constant) / divisor
            current = self.equalities.get(lead_var)
            candidate = (sub_terms, sub_const)
            if current is None or repr(candidate) < repr(current):
                self.equalities[lead_var] = candidate
        else:
            index = len(self.inequalities)
            self.inequalities.append(rel)
            for sym, _ in rel.terms:
                self._inequalities_by_symbol[sym].append(index)

    @staticmethod
    def _source_dict(source: SourceLocation | None) -> dict[str, Any] | None:
        return ({key: value for key, value in asdict(source).items() if value is not None}
                if source is not None else None)

    def _record_conflict(
        self, symbol: str, relation: AffineRelation,
        prior_sources: tuple[SourceLocation | None, SourceLocation | None] = (None, None),
    ) -> None:
        self.conflicts.append({
            "symbol": symbol,
            "relation_id": relation.relation_id,
            "source": relation.source,
            "axis": relation.axis,
            "index": relation.index,
            "prior_sources": prior_sources,
        })

    def _column(self, inst: str, local: str, step: int) -> str:
        """Resolve the very same typed instance/step slot as the generator."""
        scalar = self.inventory.property_bindings.get((inst, local))
        if scalar is not None:
            return scalar
        columns = self.inventory.step_columns.get((inst, local))
        if columns is not None:
            return columns[step]
        # Standalone scalar inventory tests have no declared time slots.
        return f"{inst}.{local}"

    def _collect_relations(self) -> None:
        # Every operation consumes the sealed P1 source identities and index
        # domain. Never infer a time reference by parsing a solver column name.
        for op in self.inventory.operations:
            if self.checkpoint is not None:
                self.checkpoint()
            payload = op.payload
            count = op.domain.count if op.domain.kind == "step" else 1
            if isinstance(payload, CapacityLimit):
                for t in range(count):
                    if self.checkpoint is not None:
                        self.checkpoint()
                    local = self._column(payload.instance, payload.local, t)
                    self._add_affine(
                        {local: 1.0, payload.planning_symbol: -1.0},
                        0.0, payload.sense, op.source, op.identity,
                        axis=op.domain.kind, index=t,
                    )
            elif isinstance(payload, BusBalance):
                # Actual port-bus conservation from the shared network partition.
                for t in range(count):
                    if self.checkpoint is not None:
                        self.checkpoint()
                    terms: dict[str, float] = {}
                    for inst, local, sign in payload.ports:
                        col = self._column(inst, local, t)
                        terms[col] = terms.get(col, 0.0) + sign
                    self._add_affine(
                        terms, 0.0, "eq", op.source, op.identity,
                        axis=op.domain.kind, index=t,
                    )
            elif isinstance(payload, ClosedPort):
                for t in range(count):
                    if self.checkpoint is not None:
                        self.checkpoint()
                    self._add_affine(
                        {self._column(payload.instance, payload.interface, t): 1.0},
                        0.0, "eq", op.source, op.identity,
                        axis=op.domain.kind, index=t,
                    )
            elif isinstance(payload, StateInitial):
                initial_sym = f"{payload.instance}.{payload.state}.initial"
                val = payload.value
                if isinstance(val, (int, float)):
                    self.bounds[initial_sym] = (float(val), float(val))
                    self.provenance[initial_sym] = (op.source, op.source)
                elif isinstance(val, LinearExpression):
                    terms: dict[str, float] = {initial_sym: -1.0}
                    for (ref, offset), coeff in val.terms:
                        if coeff:
                            # P1 initial expressions may directly reference a
                            # global planning decision rather than a local
                            # device property; preserve that binding.
                            if (ref in self.inventory.bound_facts
                                    or ref in self.inventory.symbols
                                    or ref in self.variables):
                                local = ref
                            else:
                                local = self._column(payload.instance, ref, offset)
                            terms[local] = terms.get(local, 0.0) + coeff
                    self._add_affine(
                        terms, val.constant, "eq", op.source, op.identity,
                        axis=op.domain.kind, index=0,
                    )
            elif isinstance(payload, (PlanningRule, AmountConstraintRule)):
                if isinstance(payload, AmountConstraintRule) and payload.bound is None:
                    # Conditional money/peak relations remain in the exact model;
                    # only an already affine binding contributes proven bounds.
                    continue
                template = payload.bound if isinstance(payload, AmountConstraintRule) else payload.template
                for t in range(count):
                    if self.checkpoint is not None:
                        self.checkpoint()
                    terms: dict[str, float] = {}
                    for (sym, offset), coeff in template.expression.terms:
                        if coeff == 0:
                            continue
                        if isinstance(payload, PlanningRule) and payload.step and sym in payload.references:
                            ref = payload.references[sym]
                            col = self._column(ref["instance"], ref["interface"], t + offset)
                        elif op.source.instance_id:
                            col = self._column(op.source.instance_id, sym, t + offset)
                        else:
                            col = sym
                        terms[col] = terms.get(col, 0.0) + coeff
                    self._add_affine(
                        terms, template.expression.constant - template.rhs,
                        template.sense, op.source, op.identity,
                        axis=op.domain.kind, index=t,
                    )

        # P1's *actual* template/binder performs every arithmetic operation,
        # resolves known coefficients at the correct step and connects t=0
        # lagged references to the same initial declaration as production.
        for family in self.inventory.relations:
            inst = family.scope_id or family.source.instance_id
            if inst is None:
                continue
            context = self.inventory.technical_contexts.get(family.identity)
            if context is not None:
                template, constants, initial, series, symbols = context
                count = family.domain.count
                box = {name: (entry[1], entry[2]) for name, entry in symbols.items()
                       if entry[0] == "var"}
            else:
                # Scalar inventories in isolated tests share P1's binder too.
                refs = (_reference_names(family.relation.ast.lhs_root)
                        | _reference_names(family.relation.ast.rhs_root))
                constants: dict[str, float] = {}
                roles: dict[str, str] = {}
                for name in refs:
                    identity = self.inventory.property_bindings.get((inst, name), f"{inst}.{name}")
                    coefficient = (self.inventory.coefficients.get(identity)
                                   or self.inventory.coefficients.get(name))
                    if coefficient is not None and coefficient.value is not None:
                        roles[name] = "known"
                        constants[name] = float(coefficient.value)
                    else:
                        roles[name] = "var"
                template = compile_technical(family.relation, roles)
                initial, series = {}, {}
                box = {}
                for name in refs:
                    if roles[name] == "var":
                        identity = self.inventory.property_bindings.get(
                            (inst, name), f"{inst}.{name}"
                        )
                        fact = self.inventory.bound_facts.get(identity)
                        if fact is not None:
                            box[name] = (fact.lower, fact.upper)
                        elif identity in self.variables:
                            var = self.variables[identity]
                            box[name] = (var.lower, var.upper)
                        else:
                            box[name] = (None, None)
                count = family.domain.count if family.domain.kind == "step" else 1
            known = dict(constants)
            reusable = template.fast_lhs is not None and template.fast_rhs is not None
            cached_rows = [] if reusable else None
            if cached_rows is not None:
                self.technical_rows[family.identity] = cached_rows
            for step in range(count):
                if self.checkpoint is not None:
                    self.checkpoint()
                for name, values in series.items():
                    known[name] = values[step]
                def resolve(key: tuple[str, int]) -> tuple[str, int]:
                    name, offset = key
                    return (self._column(inst, name, step + offset), 0)
                try:
                    lowered = bind_technical(
                        template, instance=inst, step=step, known=known,
                        initial=initial, bound=box, affine_column=resolve,
                        affine_row_name=f"{inst}__{family.relation.id}__t{step}",
                    )
                except LoweringUnsupportedError:
                    # Non-affine mutex is lowered exactly by the production
                    # consumer, not used to invent an affine analysis relation.
                    if cached_rows is not None:
                        cached_rows.append(None)
                    continue
                if cached_rows is not None:
                    cached_rows.append(lowered.constraints)
                if lowered.variables:
                    continue
                for row in lowered.constraints:
                    terms: dict[str, float] = {}
                    for (name, offset), coeff in row.expression.terms:
                        if offset:
                            raise RuntimeError("bound analysis expects bound solver columns")
                        terms[name] = terms.get(name, 0.0) + coeff
                    self._add_affine(
                        terms, row.expression.constant - row.rhs,
                        row.sense, family.source, family.identity,
                        axis=family.domain.kind, index=step,
                    )

    def _propagate(self, budget: int) -> None:
        """Sparse relation worklist with exact arithmetic and directed rounding.

        A source row is examined once, then only when a symbol it uses tightens.
        This never infers identity from a solver-column spelling, and work
        exhaustion retains only established facts without claiming unboundedness.
        """
        from functools import lru_cache

        self._index_relations()
        self.budget_exhausted = False
        self.incomplete_symbols = frozenset()

        @lru_cache(maxsize=8192)
        def exact(value: float) -> Fraction:
            return Fraction.from_float(float(value))

        def outward(value: Fraction, upper: bool) -> float | None:
            try:
                rounded = float(value)
            except OverflowError:
                return None
            if not math.isfinite(rounded):
                return None
            rounded_exact = exact(rounded)
            if upper and rounded_exact < value:
                rounded = math.nextafter(rounded, math.inf)
            elif not upper and rounded_exact > value:
                rounded = math.nextafter(rounded, -math.inf)
            return rounded if math.isfinite(rounded) else None

        uses: dict[str, list[int]] = defaultdict(list)
        for idx, rel in enumerate(self.relations):
            if self.checkpoint is not None:
                self.checkpoint()
            for symbol, _ in rel.terms:
                uses[symbol].append(idx)

        initial_count = len(self.relations)
        initial = set(range(initial_count))
        queue = deque(range(initial_count))
        pending = set(queue)
        steps = 0
        bound_updates = 0
        requeues = 0
        self_requeues = 0
        dependent_requeues = 0
        max_queue_size = len(queue)
        step_limit = initial_count + budget

        def finish() -> None:
            self.propagation_stats = PropagationStats(
                relation_count=initial_count,
                budget=budget,
                step_limit=step_limit,
                steps=steps,
                bound_updates=bound_updates,
                requeues=requeues,
                self_requeues=self_requeues,
                dependent_requeues=dependent_requeues,
                max_queue_size=max_queue_size,
                budget_exhausted=self.budget_exhausted,
                incomplete_symbols=tuple(sorted(self.incomplete_symbols)),
            )

        while queue and steps < initial_count + budget:
            if self.checkpoint is not None:
                self.checkpoint()
            steps += 1
            rel_idx = queue.popleft()
            pending.discard(rel_idx)
            initial.discard(rel_idx)
            rel = self.relations[rel_idx]
            coefficients = [(sym, exact(a)) for sym, a in rel.terms]
            rhs = -exact(rel.constant)
            # Sum the interval of -a*x once. Removing the target contribution
            # gives exactly the previous nested-loop calculation. Update the
            # totals after each tightening to retain within-row propagation.
            # A count of unknown endpoints handles single-sided/free variables
            # without ever subtracting infinities.
            endpoints: dict[str, tuple[Fraction | None, Fraction | None]] = {}
            lower_sum = upper_sum = rhs
            lower_missing = upper_missing = 0
            for symbol, coefficient in coefficients:
                if self.checkpoint is not None:
                    self.checkpoint()
                lo, hi = self.bounds.get(symbol, (None, None))
                low, high = (lo, hi) if coefficient < 0 else (hi, lo)
                low_term = None if low is None else -coefficient * exact(low)
                high_term = None if high is None else -coefficient * exact(high)
                endpoints[symbol] = (low_term, high_term)
                if low_term is None:
                    lower_missing += 1
                else:
                    lower_sum += low_term
                if high_term is None:
                    upper_missing += 1
                else:
                    upper_sum += high_term
            for target, a_k in coefficients:
                if self.checkpoint is not None:
                    self.checkpoint()
                if not a_k:
                    continue
                old_low_term, old_high_term = endpoints[target]
                lower_rest = (lower_sum - (old_low_term or 0)
                              if lower_missing == (old_low_term is None) else None)
                upper_rest = (upper_sum - (old_high_term or 0)
                              if upper_missing == (old_high_term is None) else None)

                new_lo: float | None = None
                new_hi: float | None = None
                if rel.sense == "eq":
                    if a_k > 0:
                        new_lo = (outward(lower_rest / a_k, False)
                                  if lower_rest is not None else None)
                        new_hi = (outward(upper_rest / a_k, True)
                                  if upper_rest is not None else None)
                    else:
                        new_lo = (outward(upper_rest / a_k, False)
                                  if upper_rest is not None else None)
                        new_hi = (outward(lower_rest / a_k, True)
                                  if lower_rest is not None else None)
                elif rel.sense == "le":
                    if a_k > 0 and upper_rest is not None:
                        new_hi = outward(upper_rest / a_k, True)
                    elif a_k < 0 and upper_rest is not None:
                        new_lo = outward(upper_rest / a_k, False)
                elif rel.sense == "ge":
                    if a_k > 0 and lower_rest is not None:
                        new_lo = outward(lower_rest / a_k, False)
                    elif a_k < 0 and lower_rest is not None:
                        new_hi = outward(lower_rest / a_k, True)

                cur_lo, cur_hi = self.bounds.get(target, (None, None))
                next_lo = (new_lo if cur_lo is None else max(cur_lo, new_lo)
                           ) if new_lo is not None else cur_lo
                next_hi = (new_hi if cur_hi is None else min(cur_hi, new_hi)
                           ) if new_hi is not None else cur_hi
                if next_lo == cur_lo and next_hi == cur_hi:
                    continue
                if next_lo is not None and next_hi is not None and next_lo > next_hi:
                    # A source-level inconsistent system must keep all exact
                    # production rows and let the solver report infeasibility.
                    self._record_conflict(
                        target, rel, self.provenance.get(target, (None, None)),
                    )
                    finish()
                    return
                self.bounds[target] = (next_lo, next_hi)
                low, high = (next_lo, next_hi) if a_k < 0 else (next_hi, next_lo)
                low_term = None if low is None else -a_k * exact(low)
                high_term = None if high is None else -a_k * exact(high)
                lower_missing += (low_term is None) - (old_low_term is None)
                upper_missing += (high_term is None) - (old_high_term is None)
                lower_sum += (low_term or 0) - (old_low_term or 0)
                upper_sum += (high_term or 0) - (old_high_term or 0)
                endpoints[target] = (low_term, high_term)
                bound_updates += 1
                prior_lo, prior_hi = self.provenance.get(target, (None, None))
                self.provenance[target] = (
                    rel.source if next_lo != cur_lo else prior_lo,
                    rel.source if next_hi != cur_hi else prior_hi,
                )
                for dependent in uses[target]:
                    # Tightening a bound from one affine row cannot improve
                    # that same row again: its opposite-side interval already
                    # used the other variables' endpoints. Only a DIFFERENT
                    # relation can provide new information. Avoid an otherwise
                    # redundant second visit for every state-transition row.
                    if dependent == rel_idx:
                        continue
                    if dependent not in initial and dependent not in pending:
                        queue.append(dependent)
                        pending.add(dependent)
                        requeues += 1
                        dependent_requeues += 1
                max_queue_size = max(max_queue_size, len(queue))
        if queue:
            self.budget_exhausted = True
            self.incomplete_symbols = frozenset(
                symbol
                for rel_idx in pending
                for symbol, _ in self.relations[rel_idx].terms
            )
        finish()

    def conflict_error(self) -> BoundConflictError:
        conflict = self.conflicts[0]
        source = conflict["source"]
        symbol = conflict["symbol"]
        model_source = {
            "source": f"relation:{conflict['relation_id']}",
            "role": "relation_conflict",
            "axis": conflict["axis"],
            "index": conflict["index"],
            "location": {
                "symbol": symbol,
                "relation": self._source_dict(source),
                "prior_sources": [self._source_dict(item) for item in conflict["prior_sources"]],
            },
        }
        return BoundConflictError(
            ("已证明模型约束冲突：存在恒假的约束" if symbol == "<constant>"
             else f"已证明模型约束冲突：变量 {symbol!r} 的有效上下界矛盾"),
            model_source=model_source,
        )

    def simplify_affine(
        self, terms: Mapping[str, float], constant: float,
    ) -> tuple[dict[str, Fraction], Fraction]:
        """Algebraically exact substitutions of the original float inputs.

        No tolerance-based cancellation or float coefficient loss is permitted.
        """
        current = {
            k: Fraction.from_float(float(v))
            for k, v in terms.items() if v != 0
        }
        offset = Fraction.from_float(float(constant))
        # A worklist preserves dict-insertion order (including replacement
        # keys) without repeatedly scanning a year-sized expression for the
        # next substitutable variable. Each equality lead is visited at most
        # once, as in the previous implementation.
        seen: set[str] = set()
        queue = deque(k for k in current if k in self.equalities)
        limit = min(len(self.equalities), 1000)
        while queue and len(seen) < limit:
            lead = queue.popleft()
            if lead in seen or lead not in current:
                continue
            seen.add(lead)
            multiplier = current.pop(lead)
            replacement, base = self.equalities[lead]
            offset += multiplier * base
            for k, coeff in replacement.items():
                existed = k in current
                value = current.get(k, Fraction(0)) + multiplier * coeff
                if value:
                    current[k] = value
                    if not existed and k in self.equalities and k not in seen:
                        queue.append(k)
                else:
                    current.pop(k, None)
        return current, offset

    def bound_affine(
        self,
        terms: Mapping[str, float],
        constant: float,
        *,
        side: str,
    ) -> float | None:
        """Conservatively bound a correlated linear form, without guesswork.

        The expression may contain an entire year of hourly variables. Scan
        only inequalities sharing a nonzero symbol, and adjust only each
        sparse row's coefficients. Rebuilding the whole annual expression
        for every hourly capacity row would be quadratic in time slots.
        Exact Fraction math, full cancellation of unbounded terms, and
        outward rounding of the final result are retained.
        """
        coeffs, base = self.simplify_affine(terms, constant)
        if side not in ("min", "max"):
            raise ValueError(side)

        def as_float(value: Fraction) -> float | None:
            try:
                result = float(value)
            except OverflowError:
                return None
            if not math.isfinite(result):
                return None
            exact_result = Fraction.from_float(result)
            if side == "max" and exact_result < value:
                return math.nextafter(result, math.inf)
            if side == "min" and exact_result > value:
                return math.nextafter(result, -math.inf)
            return result

        def endpoint_term(sym: str, coeff: Fraction) -> Fraction | None:
            if not coeff:
                return Fraction(0)
            lo, hi = self.bounds.get(sym, (None, None))
            endpoint = (
                (hi if coeff > 0 else lo) if side == "max"
                else (lo if coeff > 0 else hi)
            )
            if endpoint is None or not math.isfinite(endpoint):
                return None
            return coeff * Fraction.from_float(endpoint)

        # Store the finite part even when some symbols are unbounded. An
        # inequality may cancel all missing endpoints (e.g. x-y <= 5 when
        # neither x nor y has a finite upper bound).
        per_symbol = {
            sym: endpoint_term(sym, coeff)
            for sym, coeff in coeffs.items()
        }
        base_sum = base
        missing = 0
        for contribution in per_symbol.values():
            if contribution is None:
                missing += 1
            else:
                base_sum += contribution
        candidate = base_sum if missing == 0 else None

        # In a two-year model, thousands of per-step capacity limits coexist
        # with 8,760-term annual tax bases. Query the inverse source index,
        # rather than scanning all rows and allocating a dense remainder dict
        # for each one. Sorting retains prior source-order tie behavior.
        relevant = sorted({
            index
            for sym in coeffs
            for index in self._inequalities_by_symbol.get(sym, ())
        })
        for index in relevant:
            rel = self.inequalities[index]
            # rel.terms is already sorted; the first shared key is exactly the
            # same pivot chosen by the previous dense algorithm.
            pivot = next((sym for sym, _ in rel.terms if sym in coeffs), None)
            if pivot is None:
                continue
            pivot_coeff = next(a for sym, a in rel.terms if sym == pivot)
            factor = coeffs[pivot] / Fraction.from_float(pivot_coeff)
            allowed = (
                (factor > 0 and rel.sense == "le" and side == "max")
                or (factor > 0 and rel.sense == "ge" and side == "min")
                or (factor < 0 and rel.sense == "le" and side == "min")
                or (factor < 0 and rel.sense == "ge" and side == "max")
            )
            if not allowed:
                continue

            # The new expression is base - factor*row. Every symbol outside
            # the sparse row has an unchanged contribution. Update only row
            # symbols, and keep the exact count of unbounded endpoints.
            total = base_sum - factor * Fraction.from_float(rel.constant)
            unbounded = missing
            for sym, a in rel.terms:
                previous = per_symbol.get(sym, Fraction(0))
                if previous is None:
                    unbounded -= 1
                else:
                    total -= previous
                coeff = coeffs.get(sym, Fraction(0)) - factor * Fraction.from_float(a)
                adjusted = endpoint_term(sym, coeff)
                if adjusted is None:
                    unbounded += 1
                else:
                    total += adjusted
            if unbounded:
                continue
            if candidate is None:
                candidate = total
            elif side == "max":
                candidate = min(candidate, total)
            else:
                candidate = max(candidate, total)
        return None if candidate is None else as_float(candidate)
