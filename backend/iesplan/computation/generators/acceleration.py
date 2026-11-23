"""Verified solver-acceleration metadata extraction for ``SparseModel``.

Enforces all five P0 safe-omission conditions in a single O(nnz) matrix pass
using source provenance and exact numerical matrix structure (never column-name
suffix heuristics).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from iesplan.computation.generators.matrix import SparseModel

ACCELERATION_PATH = "input/acceleration.json"
ACCELERATION_SCHEMA = "ies.solver-acceleration"
ACCELERATION_SCHEMA_VERSION = "1.0.0"

_ROW_Y_RHS_REL_TOL = 1e-12


def _iter_group_col_positions(model: SparseModel, targets: Any):
    if hasattr(targets, "solver_positions"):
        return targets.solver_positions(model, "column")
    return (model.column_position(name) for name in targets)


def _is_nonneg_continuous(model: SparseModel, col_pos: int) -> bool:
    decl = model.column_order[col_pos]
    _name, lower, _upper, kind = model.columns.values_at(decl)
    return (
        kind == "continuous"
        and lower is not None
        and math.isfinite(lower)
        and lower >= 0.0
    )


def extract_verified_technical_mutexes(
    model: SparseModel,
    sources: Mapping[str, Any],
) -> list[list[int]]:
    """Return ``[[bin_col, x_col, y_col, row_x, row_y], ...]`` sorted by ``bin_col``.

    All five conditions are enforced strictly:
    1. ``z`` belongs to a ``column:*`` group whose source has ``kind == "technical"``,
       and ``z`` is ``kind == "binary"`` with bounds ``[0.0, 1.0]``.
    2. Objective coefficient of ``z`` is strictly ``0.0``.
    3. Total nonzero row reference count (degree) of ``z`` across all rows is ``2``.
    4. Those exact 2 rows are ``sense == "le"`` with 2 nonzero terms each:
       ``row_x``: ``1.0 * x - M_x * z <= 0.0`` (``rhs - constant == 0.0``, finite ``M_x > 0``)
       ``row_y``: ``1.0 * y + M_y * z <= M_y``
       (``abs((rhs - constant) - M_y) <= 1e-12 * max(1.0, M_y)``, finite ``M_y > 0``)
       with ``x != z``, ``y != z``, ``y != x``, and continuous ``x, y`` having ``lower >= 0``.
    5. Relaxing those 2 rows & ``z`` preserves ``F_original <= F_relaxed`` with unchanged objective.
    """
    num_cols = len(model.columns)
    num_rows = len(model.rows)
    if num_cols == 0 or num_rows < 2 or not isinstance(sources, Mapping):
        return []

    source_defs = sources.get("sources")
    groups = sources.get("groups")
    if not isinstance(source_defs, Mapping) or not groups:
        return []

    # Condition 1: technical column:* provenance + binary [0.0, 1.0]
    candidates: set[int] = set()
    columns = model.columns
    col_order = model.column_order
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        role = group.get("role")
        if not isinstance(role, str) or not role.startswith("column:"):
            continue
        source_entry = source_defs.get(group.get("source"))
        if not isinstance(source_entry, Mapping) or source_entry.get("kind") != "technical":
            continue
        targets = group.get("targets")
        if targets is None:
            continue
        for col_pos in _iter_group_col_positions(model, targets):
            if not (0 <= col_pos < num_cols):
                continue
            _name, lower, upper, kind = columns.values_at(col_order[col_pos])
            if kind == "binary" and lower == 0.0 and upper == 1.0:
                candidates.add(int(col_pos))

    if not candidates:
        return []

    # Condition 2: strictly zero objective coefficient
    obj_totals: dict[int, float] = {}
    for (name, _offset), coeff in model.objective.terms:
        if coeff == 0.0:
            continue
        col_pos = int(model.column_position(name))
        if col_pos in candidates:
            obj_totals[col_pos] = obj_totals.get(col_pos, 0.0) + coeff
    for col_pos, total in obj_totals.items():
        if not math.isfinite(total) or total != 0.0:
            candidates.discard(col_pos)

    if not candidates:
        return []

    # Conditions 3 & 4: single O(nnz) scan across all model rows
    rows = model.rows
    senses = rows.data.senses
    sense_le = rows.data.SENSES.index("le")
    row_ids = rows.row_ids
    row_rhs = rows.rhs
    row_constants = rows.constants

    degrees: dict[int, int] = {}
    row_x_match: dict[int, tuple[int, int]] = {}
    row_y_match: dict[int, tuple[int, int]] = {}

    def disqualify(col_pos: int) -> None:
        candidates.discard(col_pos)
        row_x_match.pop(col_pos, None)
        row_y_match.pop(col_pos, None)

    for row_pos, row_decl in enumerate(model.row_order):
        nz_count = 0
        col0 = col1 = -1
        val0 = val1 = 0.0
        cand0 = cand1 = -1
        bad_row = False

        for col_pos, coeff in model.row_terms(row_decl):
            if coeff == 0.0:
                continue
            if col_pos in candidates:
                deg = degrees.get(col_pos, 0) + 1
                if deg > 2:
                    disqualify(col_pos)
                    bad_row = True
                else:
                    degrees[col_pos] = deg
                    if cand0 < 0:
                        cand0 = col_pos
                    elif cand1 < 0:
                        cand1 = col_pos
                    else:
                        disqualify(col_pos)
                        bad_row = True
            if nz_count == 0:
                col0, val0 = col_pos, coeff
            elif nz_count == 1:
                col1, val1 = col_pos, coeff
            nz_count += 1

        if cand0 < 0:
            continue

        if (
            bad_row
            or cand1 >= 0
            or nz_count != 2
            or col0 == col1
            or senses[row_ids[row_decl]] != sense_le
        ):
            disqualify(cand0)
            if cand1 >= 0:
                disqualify(cand1)
            continue

        if col0 == cand0:
            z_col, z_val, other_col, other_val = col0, val0, col1, val1
        else:
            z_col, z_val, other_col, other_val = col1, val1, col0, val0

        if other_val != 1.0 or not math.isfinite(z_val) or z_val == 0.0:
            disqualify(z_col)
            continue
        if not _is_nonneg_continuous(model, other_col):
            disqualify(z_col)
            continue

        eff_rhs = row_rhs[row_decl] - row_constants[row_decl]
        if not math.isfinite(eff_rhs):
            disqualify(z_col)
            continue

        if z_val < 0.0:
            mx = -z_val
            if not (mx > 0.0 and eff_rhs == 0.0) or z_col in row_x_match:
                disqualify(z_col)
                continue
            y_info = row_y_match.get(z_col)
            if y_info is not None and y_info[0] == other_col:
                disqualify(z_col)
                continue
            row_x_match[z_col] = (int(other_col), int(row_pos))
        else:
            my = z_val
            if (
                not (my > 0.0 and abs(eff_rhs - my) <= _ROW_Y_RHS_REL_TOL * max(1.0, my))
                or z_col in row_y_match
            ):
                disqualify(z_col)
                continue
            x_info = row_x_match.get(z_col)
            if x_info is not None and x_info[0] == other_col:
                disqualify(z_col)
                continue
            row_y_match[z_col] = (int(other_col), int(row_pos))

    pairs: list[list[int]] = []
    for z_col in sorted(candidates):
        if degrees.get(z_col) != 2:
            continue
        x_info = row_x_match.get(z_col)
        y_info = row_y_match.get(z_col)
        if x_info is None or y_info is None:
            continue
        x_col, row_x = x_info
        y_col, row_y = y_info
        if x_col == y_col or row_x == row_y:
            continue
        pairs.append([int(z_col), x_col, y_col, row_x, row_y])
    return pairs


def build_acceleration_payload(
    model: SparseModel,
    sources: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the ``ies.solver-acceleration`` JSON document for ``input/acceleration.json``."""
    return {
        "schema": ACCELERATION_SCHEMA,
        "schema_version": ACCELERATION_SCHEMA_VERSION,
        "num_columns": len(model.columns),
        "num_rows": len(model.rows),
        "technical_mutex_pairs": extract_verified_technical_mutexes(model, sources),
    }


def acceleration_bytes(
    model: SparseModel,
    sources: Mapping[str, Any],
) -> bytes:
    """Serialize deterministic UTF-8 JSON bytes for ``input/acceleration.json``."""
    payload = build_acceleration_payload(model, sources)
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


__all__ = [
    "ACCELERATION_PATH",
    "ACCELERATION_SCHEMA",
    "ACCELERATION_SCHEMA_VERSION",
    "acceleration_bytes",
    "build_acceleration_payload",
    "extract_verified_technical_mutexes",
]
