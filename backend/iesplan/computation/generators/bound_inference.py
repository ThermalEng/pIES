"""P2-S2: relation-based bound inference and expression bounds.

Infers conservative effective bounds from the complete relation inventory:
- Affine equalities, inequalities, capacity limits, bus balances, and state transitions.
- Evaluates affine expressions retaining variable associations (not just independent intervals).
- Handles positive, negative, and zero coefficients, initial states, and step-varying coefficients.
- Deterministic sparse dependency propagation with strict work budget and stopping rules.
- Does not guess Big-M and does not launch a sub-solver.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
import math
from fractions import Fraction
from typing import Any, Mapping

from iesplan.computation.generators.compiler_ir import (
    BoundFact,
    BusBalance,
    CapacityLimit,
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
    LinearConstraint,
    LinearExpression,
    LinearVariable,
    LoweringUnsupportedError,
)
from iesplan.modeling import BinaryNode, RefNode, UnaryNode
from iesplan.computation.generators.templates import bind_technical, compile_technical


_TOLERANCE = 1e-12


@dataclass(frozen=True, slots=True)
class AffineRelation:
    """An exact affine relation: sum(terms[symbol] * symbol) + constant (sense) 0."""

    terms: tuple[tuple[str, float], ...]  # sorted (symbol, coeff) pairs
    constant: float = 0.0
    sense: str = "eq"  # "eq", "le", "ge"
    source: SourceLocation | None = None
    relation_id: str | None = None

    def term_dict(self) -> dict[str, float]:
        return dict(self.terms)


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
    ) -> None:
        self.inventory = inventory
        self.variables = variables
        self.bounds: dict[str, tuple[float | None, float | None]] = {}
        self.provenance: dict[str, tuple[SourceLocation | None, SourceLocation | None]] = {}
        self.relations: list[AffineRelation] = []
        self.equalities: dict[str, tuple[dict[str, Fraction], Fraction]] = {}  # exact affine substitutions
        self.inequalities: list[AffineRelation] = []
        # Sparse reverse index: do not rescan every per-step inequality for
        # each annual VAT / income expression bound request.
        self._inequalities_by_symbol: dict[str, list[int]] = defaultdict(list)

        self._init_bounds()
        self._collect_relations()
        # A contradictory source relation belongs to the exact model. Analysis
        # does not replace the solver's infeasibility diagnosis with a failed
        # generator: if propagation discovers a conflict, retain only declared
        # domain facts and leave ALL rows to the production generator.
        self.conflicts: list[tuple[str, str | None]] = []
        declared_bounds = dict(self.bounds)
        declared_provenance = dict(self.provenance)
        self._propagate(budget=max(budget, min(100_000, 4 * len(self.relations))))
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
    ) -> None:
        filtered = tuple(sorted((k, v) for k, v in terms.items() if v != 0.0))
        if not filtered and constant != 0.0:
            # 0 (sense) -constant
            if sense == "eq" or (sense == "le" and constant > 0) or (sense == "ge" and constant < 0):
                raise LoweringUnsupportedError(
                    f"关系常数冲突：0 {sense} {-constant}",
                    model_source=({"location": asdict(source)} if source else None),
                )
            return
        if not filtered:
            return

        for sym, _ in filtered:
            if sym not in self.bounds:
                # Indexed identities are not interchangeable with their base.
                self.bounds[sym] = (None, None)

        rel = AffineRelation(filtered, constant, sense, source, relation_id)
        self.relations.append(rel)

        # Register direct substitution for equalities
        if sense == "eq":
            term_dict = dict(filtered)
            lead_var = next((k for k, v in term_dict.items() if v == 1.0 or v == -1.0), filtered[0][0])
            lead_coeff = term_dict[lead_var]
            divisor = Fraction.from_float(lead_coeff)
            sub_terms = {
                k: -Fraction.from_float(v) / divisor
                for k, v in term_dict.items() if k != lead_var
            }
            sub_const = -Fraction.from_float(constant) / divisor
            if lead_var not in self.equalities:
                self.equalities[lead_var] = (sub_terms, sub_const)
        else:
            index = len(self.inequalities)
            self.inequalities.append(rel)
            for sym, _ in filtered:
                self._inequalities_by_symbol[sym].append(index)

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
            payload = op.payload
            count = op.domain.count if op.domain.kind == "step" else 1
            if isinstance(payload, CapacityLimit):
                for t in range(count):
                    local = self._column(payload.instance, payload.local, t)
                    self._add_affine(
                        {local: 1.0, payload.planning_symbol: -1.0},
                        0.0, payload.sense, op.source, op.identity,
                    )
            elif isinstance(payload, BusBalance):
                # Existing bus conservation (not the future port-level P3-N).
                for t in range(count):
                    terms: dict[str, float] = {}
                    for inst, local, sign in payload.ports:
                        col = self._column(inst, local, t)
                        terms[col] = terms.get(col, 0.0) + sign
                    self._add_affine(terms, 0.0, "eq", op.source, op.identity)
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
                    self._add_affine(terms, val.constant, "eq", op.source, op.identity)
            elif isinstance(payload, PlanningRule):
                template = payload.template
                for t in range(count):
                    terms: dict[str, float] = {}
                    for (sym, offset), coeff in template.expression.terms:
                        if coeff == 0:
                            continue
                        if payload.step and sym in payload.references:
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
                    )

        # P1's *actual* template/binder performs every arithmetic operation,
        # resolves known coefficients at the correct step and connects t=0
        # lagged references to the same initial declaration as production.
        for family in self.inventory.relations:
            inst = family.source.instance_id
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
            for step in range(count):
                for name, values in series.items():
                    known[name] = values[step]
                def resolve(key: tuple[str, int]) -> tuple[str, int]:
                    name, offset = key
                    return (self._column(inst, name, step + offset), 0)
                try:
                    lowered = bind_technical(
                        template, instance=inst, step=step, known=known,
                        initial=initial, bound=box, affine_column=resolve,
                    )
                except LoweringUnsupportedError:
                    # Non-affine mutex is lowered exactly by the production
                    # consumer, not used to invent an affine analysis relation.
                    continue
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
                    )

    def _propagate(self, budget: int) -> None:
        """Sparse relation worklist with exact arithmetic and directed rounding.

        A source row is examined once, then only when a symbol it uses tightens.
        This never infers identity from a solver-column spelling, and work
        exhaustion retains only established facts without claiming unboundedness.
        """
        from functools import lru_cache

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
            for symbol, _ in rel.terms:
                uses[symbol].append(idx)

        queue = deque(range(len(self.relations)))
        pending = set(queue)
        steps = 0
        while queue and steps < budget:
            steps += 1
            rel_idx = queue.popleft()
            pending.discard(rel_idx)
            rel = self.relations[rel_idx]
            coefficients = [(sym, exact(a)) for sym, a in rel.terms]
            rhs = -exact(rel.constant)
            for target, a_k in coefficients:
                if not a_k:
                    continue
                lower_rest: Fraction | None = rhs
                upper_rest: Fraction | None = rhs
                for other, a_i in coefficients:
                    if other == target:
                        continue
                    coeff = -a_i
                    lo, hi = self.bounds.get(other, (None, None))
                    min_endpoint = lo if coeff > 0 else hi
                    max_endpoint = hi if coeff > 0 else lo
                    if lower_rest is not None:
                        lower_rest = (
                            None if min_endpoint is None
                            else lower_rest + coeff * exact(min_endpoint)
                        )
                    if upper_rest is not None:
                        upper_rest = (
                            None if max_endpoint is None
                            else upper_rest + coeff * exact(max_endpoint)
                        )

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
                    self.conflicts.append((target, rel.relation_id))
                    return
                self.bounds[target] = (next_lo, next_hi)
                prior_lo, prior_hi = self.provenance.get(target, (None, None))
                self.provenance[target] = (
                    rel.source if next_lo != cur_lo else prior_lo,
                    rel.source if next_hi != cur_hi else prior_hi,
                )
                for dependent in uses[target]:
                    if dependent not in pending:
                        queue.append(dependent)
                        pending.add(dependent)

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
