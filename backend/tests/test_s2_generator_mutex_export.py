"""S2 契约测试：``ies.generator.highs_milp`` 导出严格核验的 ``input/acceleration.json``。"""

from __future__ import annotations

import json
from typing import Any

from bundle_testkit import content_bytes, materialized_file
from test_highs_milp_provider import _campus_inputs

from iesplan.computation import (
    HIGHS_MILP_PROVIDER,
    CalculationConfig,
    build_registry,
    export_bundle_files,
)
from iesplan.computation.generators.acceleration import (
    ACCELERATION_PATH,
    ACCELERATION_SCHEMA,
    ACCELERATION_SCHEMA_VERSION,
    extract_verified_technical_mutexes,
)
from iesplan.computation.generators.highs_milp import GENERATOR_REF, SOLVER_REF
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
)
from iesplan.computation.generators.matrix import SparseModel
from iesplan.computation.generators.project import build_project_model_package


def _config_with_options(options: dict[str, Any]) -> CalculationConfig:
    return CalculationConfig(
        mode="planning",
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        precision="medium",
        time_limit_seconds=600,
        outputs=["solution", "metrics"],
        options=options,
        seed=7,
    )


def _synthetic_mutex_case(
    *,
    bin_name: str = "dev__mutex",
    source_kind: str = "technical",
    role: str = "column:mutex",
    bin_kind: str = "binary",
    bin_bounds: tuple[float | None, float | None] = (0.0, 1.0),
    c_z: float = 0.0,
    x_bounds: tuple[float | None, float | None] = (0.0, 50.0),
    y_bounds: tuple[float | None, float | None] = (0.0, 80.0),
    x_kind: str = "continuous",
    y_kind: str = "continuous",
    x_coeff: float = 1.0,
    y_coeff: float = 1.0,
    zx_coeff: float = -50.0,
    zy_coeff: float = 80.0,
    row_x_const: float = 0.0,
    row_x_rhs: float = 0.0,
    row_y_const: float = 0.0,
    row_y_rhs: float = 80.0,
    row_x_sense: str = "le",
    row_y_sense: str = "le",
    same_xy: bool = False,
    extra_constraints: tuple[LinearConstraint, ...] = (),
    include_second_row: bool = True,
) -> tuple[SparseModel, dict[str, Any]]:
    variables = [
        LinearVariable("x", lower=x_bounds[0], upper=x_bounds[1], kind=x_kind),
        LinearVariable(bin_name, lower=bin_bounds[0], upper=bin_bounds[1], kind=bin_kind),
    ]
    if not same_xy:
        variables.append(LinearVariable("y", lower=y_bounds[0], upper=y_bounds[1], kind=y_kind))
    if extra_constraints:
        extra_vars = {
            name
            for c in extra_constraints
            for (name, _), _ in c.expression.terms
            if name not in {v.name for v in variables}
        }
        for name in sorted(extra_vars):
            variables.append(LinearVariable(name, lower=0.0, upper=100.0, kind="continuous"))

    y_var_name = "x" if same_xy else "y"
    constraints = [
        LinearConstraint(
            name="r_x",
            expression=LinearExpression(
                terms=((("x", 0), x_coeff), ((bin_name, 0), zx_coeff)),
                constant=row_x_const,
            ),
            sense=row_x_sense,
            rhs=row_x_rhs,
        ),
    ]
    if include_second_row:
        constraints.append(
            LinearConstraint(
                name="r_y",
                expression=LinearExpression(
                    terms=(((y_var_name, 0), y_coeff), ((bin_name, 0), zy_coeff)),
                    constant=row_y_const,
                ),
                sense=row_y_sense,
                rhs=row_y_rhs,
            )
        )
    constraints.extend(extra_constraints)

    obj_terms = [(("x", 0), 1.0)]
    if not same_xy:
        obj_terms.append((("y", 0), 2.0))
    if c_z != 0.0:
        obj_terms.append(((bin_name, 0), c_z))

    model = SparseModel.from_public(
        LinearModel(
            variables=tuple(variables),
            constraints=tuple(constraints),
            objective=LinearExpression(terms=tuple(obj_terms), constant=0.0),
            sense="minimize",
        )
    )
    sources = {
        "schema": "ies.model-sources",
        "schema_version": "1.0.0",
        "sources": {
            "src:1": {"kind": source_kind},
        },
        "groups": [
            {
                "source": "src:1",
                "role": role,
                "axis": "step",
                "targets": [bin_name],
                "indices": {"start": 0, "stride": 1, "count": 1},
            }
        ],
        "outputs": [],
    }
    return model, sources


def _parse_mps_names(mps_bytes: bytes) -> tuple[list[str], list[str]]:
    row_names: list[str] = []
    col_names: list[str] = []
    seen_cols: set[str] = set()
    section = ""
    for line in mps_bytes.decode("utf-8").splitlines():
        if not line or line.startswith("*"):
            continue
        if not line[0].isspace():
            section = line.strip().split()[0]
            continue
        fields = line.split()
        if section == "ROWS":
            if fields[0] != "N":
                row_names.append(fields[1])
        elif section == "COLUMNS":
            if len(fields) >= 3 and fields[1] == "'MARKER'":
                continue
            col_name = fields[0]
            if col_name not in seen_cols:
                seen_cols.add(col_name)
                col_names.append(col_name)
    return col_names, row_names


def test_campus_exports_verified_technical_mutexes_matching_mps() -> None:
    artifact, models, materialized, finance, _, evaluation = _campus_inputs(seed=7)
    registry = build_registry((HIGHS_MILP_PROVIDER,))
    cfg_on = _config_with_options({"generator": {"acceleration": {"mode": "on_demand_mutex"}}})
    provider = registry.resolve(cfg_on)

    bundle = provider.generate(artifact, models, materialized, finance, cfg_on, evaluation)
    assert any(
        entry == {"path": ACCELERATION_PATH, "media_type": "application/json"}
        for entry in bundle.manifest.inputs
    )
    assert ACCELERATION_PATH in bundle.files

    model, _unit_map, _obj_unit, _facts, _components, _series, sources = (
        build_project_model_package(artifact, models, materialized, finance, cfg_on, evaluation)
    )
    accel = json.loads(materialized_file(bundle, ACCELERATION_PATH).decode("utf-8"))
    assert accel["schema"] == ACCELERATION_SCHEMA
    assert accel["schema_version"] == ACCELERATION_SCHEMA_VERSION
    assert accel["num_columns"] == len(model.columns)
    assert accel["num_rows"] == len(model.rows)

    pairs = accel["technical_mutex_pairs"]
    assert len(pairs) == 17520
    bin_cols = [pair[0] for pair in pairs]
    assert bin_cols == sorted(bin_cols)
    assert len(set(bin_cols)) == len(bin_cols)

    mps_cols, mps_rows = _parse_mps_names(materialized_file(bundle, "input/problem.mps"))
    assert len(mps_cols) == accel["num_columns"]
    assert len(mps_rows) == accel["num_rows"]

    obj_nonzero = {
        model.column_position(name)
        for (name, _), coeff in model.objective.terms
        if coeff != 0.0
    }
    degree = [0] * len(model.columns)
    for decl in model.row_order:
        for col_pos, coeff in model.row_terms(decl):
            if coeff != 0.0:
                degree[col_pos] += 1

    col_names = list(model.column_names())
    row_names = list(model.row_names())
    assert mps_cols == col_names
    assert mps_rows == row_names

    senses = model.rows.data.SENSES
    for bin_col, x_col, y_col, row_x, row_y in pairs:
        assert len({bin_col, x_col, y_col}) == 3
        assert row_x != row_y
        z_name, z_lo, z_hi, z_kind = model.columns.values_at(model.column_order[bin_col])
        assert z_kind == "binary" and z_lo == 0.0 and z_hi == 1.0
        assert bin_col not in obj_nonzero
        assert degree[bin_col] == 2
        assert mps_cols[bin_col] == z_name

        for c_pos in (x_col, y_col):
            _cname, c_lo, _c_hi, c_kind = model.columns.values_at(model.column_order[c_pos])
            assert c_kind == "continuous"
            assert c_lo is not None and c_lo >= 0.0

        rx_decl = model.row_order[row_x]
        assert senses[model.rows.data.senses[model.rows.row_ids[rx_decl]]] == "le"
        rx_terms = [(c, v) for c, v in model.row_terms(rx_decl) if v != 0.0]
        assert dict(rx_terms)[x_col] == 1.0
        mx = -dict(rx_terms)[bin_col]
        assert mx > 0.0
        assert model.rows.rhs[rx_decl] - model.rows.constants[rx_decl] == 0.0

        ry_decl = model.row_order[row_y]
        assert senses[model.rows.data.senses[model.rows.row_ids[ry_decl]]] == "le"
        ry_terms = [(c, v) for c, v in model.row_terms(ry_decl) if v != 0.0]
        assert dict(ry_terms)[y_col] == 1.0
        my = dict(ry_terms)[bin_col]
        assert my > 0.0
        eff_rhs_y = model.rows.rhs[ry_decl] - model.rows.constants[ry_decl]
        assert abs(eff_rhs_y - my) <= 1e-12 * max(1.0, my)


def test_extract_verified_technical_mutexes_accepts_non_suffix_and_offset_constant() -> None:
    model, sources = _synthetic_mutex_case(
        bin_name="aux_switch_0",
        row_x_const=5.0,
        row_x_rhs=5.0,
        row_y_const=-3.0,
        row_y_rhs=77.0,
    )
    pairs = extract_verified_technical_mutexes(model, sources)
    assert pairs == [[
        model.column_position("aux_switch_0"),
        model.column_position("x"),
        model.column_position("y"),
        int(model.row_positions[0]),
        int(model.row_positions[1]),
    ]]


def test_extract_verified_technical_mutexes_negative_cases() -> None:
    # 1. Non-technical source (even if variable is named foo__mutex)
    for bad_kind in ("vat", "planning", "balance", "cost"):
        model, sources = _synthetic_mutex_case(bin_name="foo__mutex", source_kind=bad_kind)
        assert extract_verified_technical_mutexes(model, sources) == []

    # 2. Row-only provenance group (not column:*)
    model, sources = _synthetic_mutex_case(bin_name="foo__mutex", role="row:mutex")
    assert extract_verified_technical_mutexes(model, sources) == []

    # 3. Nonzero objective coefficient on foo__mutex
    for bad_cz in (1e-9, -0.5, 1.0):
        model, sources = _synthetic_mutex_case(bin_name="foo__mutex", c_z=bad_cz)
        assert extract_verified_technical_mutexes(model, sources) == []

    # 4. Degree-3 binary (referenced by a 3rd constraint)
    third_row = LinearConstraint(
        name="r_third",
        expression=LinearExpression(terms=((("foo__mutex", 0), 1.0), (("w", 0), 1.0))),
        sense="le",
        rhs=2.0,
    )
    model, sources = _synthetic_mutex_case(
        bin_name="foo__mutex",
        extra_constraints=(third_row,),
    )
    assert extract_verified_technical_mutexes(model, sources) == []

    # 5. Degree-1 binary
    model, sources = _synthetic_mutex_case(bin_name="foo__mutex", include_second_row=False)
    assert extract_verified_technical_mutexes(model, sources) == []

    # 6. Malformed Big-M RHS / signs / senses / coefficients / bounds / x == y
    malformed_cases = [
        {"row_x_rhs": 1.0},  # row_x rhs - constant != 0
        {"row_x_const": 1.0, "row_x_rhs": 0.0},  # constant shifts eff_rhs to -1
        {"row_y_rhs": 79.0},  # row_y eff_rhs != M_y
        {"row_y_const": 2.0, "row_y_rhs": 80.0},  # row_y eff_rhs == 78 != 80
        {"zx_coeff": 50.0},  # wrong sign in row_x
        {"zy_coeff": -80.0},  # wrong sign in row_y
        {"row_x_sense": "eq"},
        {"row_y_sense": "ge"},
        {"x_coeff": 2.0},
        {"y_coeff": -1.0},
        {"x_bounds": (None, 50.0)},
        {"y_bounds": (-1.0, 80.0)},
        {"x_kind": "binary", "x_bounds": (0.0, 1.0)},
        {"y_kind": "binary", "y_bounds": (0.0, 1.0)},
        {"bin_kind": "continuous"},
        {"bin_bounds": (0.0, 0.0)},
        {"same_xy": True},
    ]
    for overrides in malformed_cases:
        model, sources = _synthetic_mutex_case(bin_name="foo__mutex", **overrides)
        assert extract_verified_technical_mutexes(model, sources) == [], overrides

    # 7. 3rd nonzero term inside row_x
    model, sources = _synthetic_mutex_case(
        bin_name="foo__mutex",
        extra_constraints=(),
    )
    bad_three_term = SparseModel.from_public(
        LinearModel(
            variables=(
                LinearVariable("foo__mutex", lower=0.0, upper=1.0, kind="binary"),
                LinearVariable("w", lower=0.0, upper=10.0, kind="continuous"),
                LinearVariable("x", lower=0.0, upper=50.0, kind="continuous"),
                LinearVariable("y", lower=0.0, upper=80.0, kind="continuous"),
            ),
            constraints=(
                LinearConstraint(
                    name="r_x",
                    expression=LinearExpression(
                        terms=((("x", 0), 1.0), (("foo__mutex", 0), -50.0), (("w", 0), 1.0)),
                    ),
                    sense="le",
                    rhs=0.0,
                ),
                LinearConstraint(
                    name="r_y",
                    expression=LinearExpression(
                        terms=((("y", 0), 1.0), (("foo__mutex", 0), 80.0)),
                    ),
                    sense="le",
                    rhs=80.0,
                ),
            ),
            objective=LinearExpression(terms=((("x", 0), 1.0), (("y", 0), 2.0))),
            sense="minimize",
        )
    )
    assert extract_verified_technical_mutexes(bad_three_term, sources) == []


def test_bundle_bytes_identical_when_off_and_mps_parameters_identical_when_on() -> None:
    artifact, models, materialized, finance, _, evaluation = _campus_inputs(seed=7)
    registry = build_registry((HIGHS_MILP_PROVIDER,))

    cfg_empty = _config_with_options({"generator": {}})
    cfg_omitted = _config_with_options({"generator": {"acceleration": {}}})
    cfg_off = _config_with_options({"generator": {"acceleration": {"mode": "off"}}})
    cfg_on = _config_with_options({"generator": {"acceleration": {"mode": "on_demand_mutex"}}})

    b_empty = registry.resolve(cfg_empty).generate(
        artifact, models, materialized, finance, cfg_empty, evaluation
    )
    b_omitted = registry.resolve(cfg_omitted).generate(
        artifact, models, materialized, finance, cfg_omitted, evaluation
    )
    b_off = registry.resolve(cfg_off).generate(
        artifact, models, materialized, finance, cfg_off, evaluation
    )
    b_on_1 = registry.resolve(cfg_on).generate(
        artifact, models, materialized, finance, cfg_on, evaluation
    )
    b_on_2 = registry.resolve(cfg_on).generate(
        artifact, models, materialized, finance, cfg_on, evaluation
    )

    bytes_empty = content_bytes(export_bundle_files(b_empty))
    assert content_bytes(export_bundle_files(b_omitted)) == bytes_empty
    bytes_on_1 = content_bytes(export_bundle_files(b_on_1))
    bytes_on_2 = content_bytes(export_bundle_files(b_on_2))
    assert bytes_empty == bytes_on_1 == bytes_on_2
    assert ACCELERATION_PATH in bytes_empty
    assert any(entry["path"] == ACCELERATION_PATH for entry in b_empty.manifest.inputs)

    bytes_off = content_bytes(export_bundle_files(b_off))
    assert ACCELERATION_PATH not in bytes_off
    assert all(entry["path"] != ACCELERATION_PATH for entry in b_off.manifest.inputs)
    for path in ("input/problem.mps", "input/parameters.json", "input/parameters.jsonl"):
        assert bytes_on_1[path] == bytes_off[path]
