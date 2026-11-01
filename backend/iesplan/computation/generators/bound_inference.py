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
        self.equalities: dict[str, tuple[dict[str, float], float]] = {}  # var -> (other_terms, const)
        self.inequalities: list[AffineRelation] = []

        self._init_bounds()
        self._collect_relations()
        self._propagate(budget=budget)

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
            if lo is not None and hi is not None and lo > hi + _TOLERANCE:
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
            sub_terms = {
                k: -v / lead_coeff for k, v in term_dict.items() if k != lead_var
            }
            sub_const = -constant / lead_coeff
            if lead_var not in self.equalities:
                self.equalities[lead_var] = (sub_terms, sub_const)
        else:
            self.inequalities.append(rel)

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
                initial, series, box = {}, {}, {}
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
        """Sparse dependency propagation with deterministic work budget."""
        uses: dict[str, list[int]] = defaultdict(list)
        for idx, rel in enumerate(self.relations):
            for sym, _ in rel.terms:
                uses[sym].append(idx)

        queue = deque(sym for sym, (lo, hi) in self.bounds.items() if lo is not None or hi is not None)
        in_queue = set(queue)

        steps = 0
        while queue and steps < budget:
            steps += 1
            var = queue.popleft()
            in_queue.discard(var)

            for rel_idx in uses.get(var, ()):
                rel = self.relations[rel_idx]
                term_dict = rel.term_dict()

                for target_sym, a_k in rel.terms:
                    if a_k == 0.0:
                        continue

                    # Calculate Rest = -c - sum_{i != target} a_i * x_i
                    min_rest: float | None = -rel.constant
                    max_rest: float | None = -rel.constant

                    for other_sym, a_i in rel.terms:
                        if other_sym == target_sym:
                            continue
                        coeff = -a_i
                        o_lo, o_hi = self.bounds.get(other_sym, (None, None))

                        if coeff > 0:
                            if o_lo is None:
                                min_rest = None
                            elif min_rest is not None:
                                min_rest += coeff * o_lo

                            if o_hi is None:
                                max_rest = None
                            elif max_rest is not None:
                                max_rest += coeff * o_hi
                        elif coeff < 0:
                            if o_hi is None:
                                min_rest = None
                            elif min_rest is not None:
                                min_rest += coeff * o_hi

                            if o_lo is None:
                                max_rest = None
                            elif max_rest is not None:
                                max_rest += coeff * o_lo

                    new_lo: float | None = None
                    new_hi: float | None = None

                    if rel.sense == "eq":
                        if a_k > 0:
                            new_lo = min_rest / a_k if min_rest is not None else None
                            new_hi = max_rest / a_k if max_rest is not None else None
                        else:
                            new_lo = max_rest / a_k if max_rest is not None else None
                            new_hi = min_rest / a_k if min_rest is not None else None
                    elif rel.sense == "le":
                        if a_k > 0:
                            new_hi = max_rest / a_k if max_rest is not None else None
                        else:
                            new_lo = max_rest / a_k if max_rest is not None else None
                    elif rel.sense == "ge":
                        if a_k > 0:
                            new_lo = min_rest / a_k if min_rest is not None else None
                        else:
                            new_hi = min_rest / a_k if min_rest is not None else None

                    # Check tightening
                    cur_lo, cur_hi = self.bounds.get(target_sym, (None, None))
                    tightened = False
                    next_lo = cur_lo
                    next_hi = cur_hi

                    if new_lo is not None:
                        if cur_lo is None or new_lo > cur_lo:
                            next_lo = new_lo
                            tightened = True
                    if new_hi is not None:
                        if cur_hi is None or new_hi < cur_hi:
                            next_hi = new_hi
                            tightened = True

                    if tightened:
                        if next_lo is not None and next_hi is not None and next_lo > next_hi + 1e-9:
                            raise LoweringUnsupportedError(
                                f"关系推导冲突：符号 {target_sym!r} 推导下界 {next_lo} > 上界 {next_hi}",
                                model_source=({"location": asdict(rel.source)} if rel.source else None),
                            )
                        self.bounds[target_sym] = (next_lo, next_hi)
                        cur_lo_src, cur_hi_src = self.provenance.get(target_sym, (None, None))
                        self.provenance[target_sym] = (
                            rel.source if next_lo != cur_lo else cur_lo_src,
                            rel.source if next_hi != cur_hi else cur_hi_src,
                        )
                        if target_sym not in in_queue:
                            queue.append(target_sym)
                            in_queue.add(target_sym)

    def simplify_affine(self, terms: Mapping[str, float], constant: float) -> tuple[dict[str, float], float]:
        """Substitute known equalities into an affine expression to preserve correlations."""
        cur_terms = dict(terms)
        cur_const = constant

        # Repeat substitution passes up to small limit
        for _ in range(5):
            changed = False
            for var in list(cur_terms):
                if var in self.equalities:
                    coeff = cur_terms.pop(var)
                    sub_terms, sub_const = self.equalities[var]
                    cur_const += coeff * sub_const
                    for k, v in sub_terms.items():
                        total = cur_terms.get(k, 0.0) + coeff * v
                        if total == 0.0:
                            cur_terms.pop(k, None)
                        else:
                            cur_terms[k] = total
                    changed = True
                    break
            if not changed:
                break

        return (cur_terms, cur_const)

    def bound_affine(
        self,
        terms: Mapping[str, float],
        constant: float,
        *,
        side: str,
    ) -> float | None:
        """Query lower (min) or upper (max) bound for an affine expression."""
        simp_terms, simp_const = self.simplify_affine(terms, constant)

        # If all terms cancelled, expression is constant!
        if not simp_terms:
            return simp_const

        # A linear relation bounds other affine forms only after accounting
        # for their exact residual. Approximate coefficient matching is unsound
        # at large scales; never treat near-equal ratios as equal.
        best_rel_bound: float | None = None
        for rel in self.inequalities:
            r_dict = rel.term_dict()
            shared = sorted(set(r_dict) & set(simp_terms))
            if not shared:
                continue
            pivot = shared[0]
            factor = simp_terms[pivot] / r_dict[pivot]
            if factor == 0.0 or not math.isfinite(factor):
                continue
            allowed = (
                (factor > 0 and rel.sense == "le" and side == "max")
                or (factor > 0 and rel.sense == "ge" and side == "min")
                or (factor < 0 and rel.sense == "le" and side == "min")
                or (factor < 0 and rel.sense == "ge" and side == "max")
            )
            if not allowed:
                continue
            # Compute the mismatch with high precision, retaining every
            # nonzero term and directing final float conversion outwards.
            from decimal import Decimal, localcontext
            with localcontext() as ctx:
                ctx.prec = 100
                scale = Decimal.from_float(factor)
                residual = {}
                for k in sorted(set(simp_terms) | set(r_dict)):
                    c = Decimal.from_float(simp_terms.get(k, 0.0)) - scale * Decimal.from_float(r_dict.get(k, 0.0))
                    if c:
                        residual[k] = c
                total_dec = Decimal.from_float(simp_const) - scale * Decimal.from_float(rel.constant)
                valid = True
                for k, coeff in residual.items():
                    lo, hi = self.bounds.get(k, (None, None))
                    bound = (hi if coeff > 0 else lo) if side == "max" else (lo if coeff > 0 else hi)
                    if bound is None or not math.isfinite(bound):
                        valid = False
                        break
                    total_dec += coeff * Decimal.from_float(bound)
                if not valid:
                    continue
                val = float(total_dec)
                if not math.isfinite(val):
                    continue
                if side == "max" and Decimal.from_float(val) < total_dec:
                    val = math.nextafter(val, math.inf)
                elif side == "min" and Decimal.from_float(val) > total_dec:
                    val = math.nextafter(val, -math.inf)
                best_rel_bound = (
                    val if best_rel_bound is None
                    else (min(best_rel_bound, val) if side == "max" else max(best_rel_bound, val))
                )

        # Interval evaluation on remaining terms
        total = simp_const
        for sym, coeff in simp_terms.items():
            if coeff == 0.0:
                continue
            lo, hi = self.bounds.get(sym, (None, None))
            val = hi if (coeff > 0.0) == (side == "max") else lo
            if val is None or not math.isfinite(val):
                return best_rel_bound
            total += coeff * val

        if best_rel_bound is not None:
            return min(best_rel_bound, total) if side == "max" else max(best_rel_bound, total)
        return total
