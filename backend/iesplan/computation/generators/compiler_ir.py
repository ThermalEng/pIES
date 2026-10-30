"""P1 compiler's *internal*, per-generate declaration and relation inventory.

This is not a second input contract, a global registry, or a persisted Bundle
schema.  Keep original modeling ASTs instead of callbacks or per-step AST copies.
Later slices will add the remaining domains to this same inventory.
"""
from __future__ import annotations

from dataclasses import dataclass, field

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


@dataclass(slots=True)
class CompilerInventory:
    """Local staging only: typed symbols, alias binding, relations and outputs."""

    symbols: dict[str, SymbolDeclaration] = field(default_factory=dict)
    property_bindings: dict[tuple[str, str], str] = field(default_factory=dict)
    coefficients: dict[str, KnownCoefficient] = field(default_factory=dict)
    relations: list[RelationFamily] = field(default_factory=list)
    outputs: list[OutputReference] = field(default_factory=list)
