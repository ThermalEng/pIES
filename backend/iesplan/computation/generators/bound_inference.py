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
    CapacityLimit,
    CompilerInventory,
    IndexDomain,
    KnownCoefficient,
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
from iesplan.modeling import BinaryNode, MathRelation, NumberNode, RefNode, UnaryNode


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


def _node_to_affine(
    node: object,
    inst: str | None,
    inventory: CompilerInventory,
    step: int = 0,
) -> tuple[dict[str, float], float] | None:
    """Convert an AST node into an affine accumulator (terms, constant).

    Returns None if the node contains non-linear expressions (e.g. var * var).
    """
    if isinstance(node, NumberNode):
        val = float(node.value)
        return ({}, val) if math.isfinite(val) else None

    if isinstance(node, RefNode):
        local = node.name
        # Check if local is a known coefficient
        if inst is not None:
            bound_identity = inventory.property_bindings.get((inst, local))
            if bound_identity and bound_identity in inventory.coefficients:
                coeff = inventory.coefficients[bound_identity]
                if coeff.value is not None:
                    return ({}, float(coeff.value))
            # Also check direct local coefficient
            if local in inventory.coefficients:
                coeff = inventory.coefficients[local]
                if coeff.value is not None:
                    return ({}, float(coeff.value))

        if local in inventory.coefficients:
            coeff = inventory.coefficients[local]
            if coeff.value is not None:
                return ({}, float(coeff.value))

        # It is a variable symbol
        symbol_name: str
        if inst is not None:
            symbol_name = inventory.property_bindings.get((inst, local), f"{inst}.{local}")
        else:
            symbol_name = local
        return ({symbol_name: 1.0}, 0.0)

    if isinstance(node, UnaryNode):
        res = _node_to_affine(node.operand, inst, inventory, step)
        if res is None:
            return None
        terms, const = res
        if node.op == "-":
            return ({k: -v for k, v in terms.items()}, -const)
        if node.op == "+":
            return (terms, const)
        return None

    if isinstance(node, BinaryNode):
        left_res = _node_to_affine(node.left, inst, inventory, step)
        right_res = _node_to_affine(node.right, inst, inventory, step)
        if left_res is None or right_res is None:
            return None
        l_terms, l_const = left_res
        r_terms, r_const = right_res

        if node.op == "+":
            merged = dict(l_terms)
            for k, v in r_terms.items():
                total = merged.get(k, 0.0) + v
                if abs(total) < _TOLERANCE:
                    merged.pop(k, None)
                else:
                    merged[k] = total
            return (merged, l_const + r_const)

        if node.op == "-":
            merged = dict(l_terms)
            for k, v in r_terms.items():
                total = merged.get(k, 0.0) - v
                if abs(total) < _TOLERANCE:
                    merged.pop(k, None)
                else:
                    merged[k] = total
            return (merged, l_const - r_const)

        if node.op == "*":
            # Linear if at most one side has variable terms
            if l_terms and r_terms:
                return None  # var * var: non-linear
            if not l_terms:
                # Left is constant: scale right
                factor = l_const
                scaled = {k: v * factor for k, v in r_terms.items() if abs(v * factor) >= _TOLERANCE}
                return (scaled, r_const * factor)
            else:
                # Right is constant: scale left
                factor = r_const
                scaled = {k: v * factor for k, v in l_terms.items() if abs(v * factor) >= _TOLERANCE}
                return (scaled, l_const * factor)

        if node.op == "/":
            # Linear only if right is non-zero constant
            if r_terms or abs(r_const) < _TOLERANCE:
                return None
            factor = 1.0 / r_const
            scaled = {k: v * factor for k, v in l_terms.items() if abs(v * factor) >= _TOLERANCE}
            return (scaled, l_const * factor)

    return None


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
        filtered = tuple(sorted((k, v) for k, v in terms.items() if abs(v) >= _TOLERANCE))
        if not filtered and abs(constant) > _TOLERANCE:
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
                self.bounds[sym] = (None, None)

        rel = AffineRelation(filtered, constant, sense, source, relation_id)
        self.relations.append(rel)

        # Register direct substitution for equalities
        if sense == "eq":
            term_dict = dict(filtered)
            # Find a variable with unit magnitude if possible
            lead_var = next((k for k, v in term_dict.items() if abs(abs(v) - 1.0) < _TOLERANCE), filtered[0][0])
            lead_coeff = term_dict[lead_var]
            sub_terms = {
                k: -v / lead_coeff for k, v in term_dict.items() if k != lead_var
            }
            sub_const = -constant / lead_coeff
            if lead_var not in self.equalities:
                self.equalities[lead_var] = (sub_terms, sub_const)
        else:
            self.inequalities.append(rel)

    def _collect_relations(self) -> None:
        # 1. Collect from SemanticRelations (operations)
        for op in self.inventory.operations:
            payload = op.payload
            if isinstance(payload, CapacityLimit):
                local_sym = f"{payload.instance}.{payload.local}"
                plan_sym = payload.planning_symbol
                # local_sym <= plan_sym (if le) => local_sym - plan_sym <= 0
                if payload.sense == "le":
                    self._add_affine({local_sym: 1.0, plan_sym: -1.0}, 0.0, "le", op.source, op.identity)
                elif payload.sense == "ge":
                    self._add_affine({local_sym: 1.0, plan_sym: -1.0}, 0.0, "ge", op.source, op.identity)

            elif isinstance(payload, StateInitial):
                initial_sym = f"{payload.instance}.{payload.state}.initial"
                val = payload.value
                if isinstance(val, (int, float)):
                    self.bounds[initial_sym] = (float(val), float(val))
                    self.provenance[initial_sym] = (op.source, op.source)
                elif isinstance(val, LinearExpression):
                    terms = {}
                    for (ref, _), coeff in val.terms:
                        if abs(coeff) < _TOLERANCE:
                            continue
                        ref_name = self.inventory.property_bindings.get((payload.instance, ref))
                        if not ref_name:
                            if ref in self.inventory.symbols or ref in self.inventory.bound_facts or ref in self.variables:
                                ref_name = ref
                            else:
                                ref_name = f"{payload.instance}.{ref}"
                        terms[ref_name] = coeff
                    terms[initial_sym] = -1.0
                    self._add_affine(terms, val.constant, "eq", op.source, op.identity)

        # 2. Collect from RelationFamily (technical equations)
        for family in self.inventory.relations:
            inst = family.source.instance_id
            lhs = family.relation.ast.lhs_root
            rhs = family.relation.ast.rhs_root
            lhs_res = _node_to_affine(lhs, inst, self.inventory)
            rhs_res = _node_to_affine(rhs, inst, self.inventory)
            if lhs_res is not None and rhs_res is not None:
                l_terms, l_const = lhs_res
                r_terms, r_const = rhs_res
                diff_terms = dict(l_terms)
                for k, v in r_terms.items():
                    diff_terms[k] = diff_terms.get(k, 0.0) - v
                diff_const = l_const - r_const
                self._add_affine(diff_terms, diff_const, "eq", family.source, family.identity)

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
                    if abs(a_k) < _TOLERANCE:
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
                        if cur_lo is None or new_lo > cur_lo + _TOLERANCE:
                            next_lo = new_lo
                            tightened = True
                    if new_hi is not None:
                        if cur_hi is None or new_hi < cur_hi - _TOLERANCE:
                            next_hi = new_hi
                            tightened = True

                    if tightened:
                        if next_lo is not None and next_hi is not None and next_lo > next_hi + _TOLERANCE:
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
                        if abs(total) < _TOLERANCE:
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

        # Check if proportional to a known inequality
        best_rel_bound: float | None = None
        for rel in self.inequalities:
            r_dict = rel.term_dict()
            if set(r_dict) == set(simp_terms):
                first_k = next(iter(simp_terms))
                factor = r_dict[first_k] / simp_terms[first_k]
                if all(abs(r_dict[k] - factor * simp_terms[k]) < _TOLERANCE for k in simp_terms):
                    # rel: factor * (simp_terms) + rel.constant (sense) 0
                    # factor * simp_expr + (rel.constant - factor * simp_const) (sense) 0
                    if factor > 0:
                        if rel.sense == "le" and side == "max":
                            # factor * simp_terms <= -rel.constant
                            # simp_terms <= -rel.constant / factor
                            val = simp_const - rel.constant / factor
                            best_rel_bound = val if best_rel_bound is None else min(best_rel_bound, val)
                        elif rel.sense == "ge" and side == "min":
                            val = simp_const - rel.constant / factor
                            best_rel_bound = val if best_rel_bound is None else max(best_rel_bound, val)
                    elif factor < 0:
                        if rel.sense == "le" and side == "min":
                            val = simp_const - rel.constant / factor
                            best_rel_bound = val if best_rel_bound is None else max(best_rel_bound, val)
                        elif rel.sense == "ge" and side == "max":
                            val = simp_const - rel.constant / factor
                            best_rel_bound = val if best_rel_bound is None else min(best_rel_bound, val)

        # Interval evaluation on remaining terms
        total = simp_const
        for sym, coeff in simp_terms.items():
            if abs(coeff) < _TOLERANCE:
                continue
            lo, hi = self.bounds.get(sym, (None, None))
            val = hi if (coeff > 0.0) == (side == "max") else lo
            if val is None or not math.isfinite(val):
                # Cannot determine from interval alone
                return best_rel_bound
            total += coeff * val

        if best_rel_bound is not None:
            return min(best_rel_bound, total) if side == "max" else max(best_rel_bound, total)
        return total
