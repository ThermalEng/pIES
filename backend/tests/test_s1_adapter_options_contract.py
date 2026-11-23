"""S1 契约测试：``highs_milp`` acceleration options 与 ``HighsResultAdapter`` 域/验证映射。"""

from __future__ import annotations

import pytest
from bundle_testkit import content_bytes
from test_highs_milp_provider import _campus_inputs
from test_highs_result_adapter import (
    PARAMETERS_PATH,
    PROBLEM_PATH,
    SHIM_INFEASIBLE,
    SHIM_OPTIMAL,
    TINY_MPS,
    V1_TIME_LIMIT,
    _adapt,
    _parameters,
    _receipt,
)

from iesplan.computation import (
    HIGHS_MILP_PROVIDER,
    CalculationConfig,
    ComputationUnavailableError,
    build_registry,
    export_bundle_files,
)
from iesplan.computation.generators.highs_milp import GENERATOR_REF, SOLVER_REF


def _config_with_options(options: dict) -> CalculationConfig:
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


def _with_headers(base_sol: str, headers: dict[str, str]) -> bytes:
    lines = base_sol.splitlines()
    assert lines[0] == "HIGHS_SOLUTION_V1"
    extra = [f"{k}: {v}" for k, v in headers.items()]
    return ("\n".join([lines[0], *extra, *lines[1:]]) + "\n").encode("utf-8")


def test_options_schema_allows_optional_acceleration_and_rejects_invalid() -> None:
    schema = HIGHS_MILP_PROVIDER.descriptor.options_schema
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    accel_schema = schema["properties"]["acceleration"]
    assert accel_schema["type"] == "object"
    assert accel_schema["additionalProperties"] is False
    assert list(accel_schema["properties"]["mode"]["enum"]) == ["off", "on_demand_mutex"]
    assert accel_schema["properties"]["mode"]["default"] == "on_demand_mutex"

    registry = build_registry((HIGHS_MILP_PROVIDER,))
    for valid in (
        {},
        {"acceleration": {}},
        {"acceleration": {"mode": "off"}},
        {"acceleration": {"mode": "on_demand_mutex"}},
    ):
        assert registry.resolve(_config_with_options(valid)) is HIGHS_MILP_PROVIDER

    for invalid in (
        {"unknown": 1},
        {"acceleration": "on_demand_mutex"},
        {"acceleration": {"mode": "bogus"}},
        {"acceleration": {"mode": "off", "extra": True}},
    ):
        with pytest.raises(ValueError, match="计算选项非法"):
            registry.resolve(_config_with_options(invalid))


def test_generate_preserves_bytes_when_acceleration_off_and_appends_flag_when_enabled() -> None:
    artifact, models, materialized, finance, _, evaluation = _campus_inputs(seed=7)
    registry = build_registry((HIGHS_MILP_PROVIDER,))

    cfg_empty = _config_with_options({})
    cfg_omitted_mode = _config_with_options({"acceleration": {}})
    cfg_off = _config_with_options({"acceleration": {"mode": "off"}})
    cfg_on = _config_with_options({"acceleration": {"mode": "on_demand_mutex"}})

    b_empty = registry.resolve(cfg_empty).generate(
        artifact, models, materialized, finance, cfg_empty, evaluation
    )
    b_omitted = registry.resolve(cfg_omitted_mode).generate(
        artifact, models, materialized, finance, cfg_omitted_mode, evaluation
    )
    b_off = registry.resolve(cfg_off).generate(
        artifact, models, materialized, finance, cfg_off, evaluation
    )
    b_on = registry.resolve(cfg_on).generate(
        artifact, models, materialized, finance, cfg_on, evaluation
    )

    bytes_empty = content_bytes(export_bundle_files(b_empty))
    assert content_bytes(export_bundle_files(b_omitted)) == bytes_empty
    assert content_bytes(export_bundle_files(b_on)) == bytes_empty
    assert list(b_off.manifest.command["arguments"]) == [
        "input/problem.mps",
        "--solution_file",
        "output/solution.sol",
        "--time_limit",
        "600",
        "--random_seed",
        "7",
    ]

    assert list(b_on.manifest.command["arguments"]) == [
        "input/problem.mps",
        "--solution_file",
        "output/solution.sol",
        "--time_limit",
        "600",
        "--random_seed",
        "7",
        "--acceleration_mode",
        "on_demand_mutex",
        "--acceleration_file",
        "input/acceleration.json",
    ]
    bytes_off = content_bytes(export_bundle_files(b_off))
    assert "input/acceleration.json" not in bytes_off
    assert "input/acceleration.json" in bytes_empty
    for path in ("input/problem.mps", "input/parameters.json", "input/parameters.jsonl"):
        assert bytes_empty[path] == bytes_off[path]


def test_default_original_domain_omits_solver_diagnostics() -> None:
    result = _adapt(None, _receipt(), SHIM_OPTIMAL.encode("utf-8"))
    summary = result.outputs["solver_summary"]
    assert "solver_diagnostics" not in summary
    assert summary["math_status"] == "solved"
    assert summary["best_bound"] == 3001.5
    assert summary["relative_gap"] == 0.0


def test_invalid_domain_booleans_and_subdomain_numbers_fail_protocol() -> None:
    bad_cases = [
        {"solve_domain": "unknown_domain"},
        {"solve_domain": "valid_relaxation", "original_model_verified": "yes"},
        {"solve_domain": "restricted", "original_model_verified": "1"},
        {"solve_domain": "restricted", "original_model_verified": "true", "subdomain_status": ""},
        {"solve_domain": "restricted", "original_model_verified": "true", "subdomain_best_bound": "nan"},
        {"solve_domain": "restricted", "original_model_verified": "true", "subdomain_best_bound": "inf"},
        {"solve_domain": "restricted", "original_model_verified": "true", "subdomain_best_bound": "bad"},
        {"solve_domain": "restricted", "original_model_verified": "true", "subdomain_relative_gap": "-inf"},
        {"solve_domain": "restricted", "original_model_verified": "true", "subdomain_relative_gap": "nan"},
        # feasible solution without original_model_verified=true
        {"solve_domain": "valid_relaxation"},
        {"solve_domain": "valid_relaxation", "original_model_verified": "false"},
        {"solve_domain": "restricted"},
        {"solve_domain": "restricted", "original_model_verified": "false"},
    ]
    for headers in bad_cases:
        raw = _with_headers(SHIM_OPTIMAL, headers)
        with pytest.raises(ComputationUnavailableError) as exc_info:
            _adapt(None, _receipt(), raw)
        assert exc_info.value.reason == "result-protocol-failure", headers


def test_valid_relaxation_verified_maps_optimal_to_solved_and_timelimit_to_feasible() -> None:
    opt_raw = _with_headers(
        SHIM_OPTIMAL,
        {
            "solve_domain": "valid_relaxation",
            "original_model_verified": "true",
            "subdomain_status": "HighsModelStatus.kOptimal",
            "subdomain_best_bound": "3000.0",
            "subdomain_relative_gap": "0.0",
        },
    )
    opt_res = _adapt(None, _receipt(), opt_raw)
    assert opt_res.math_status == "solved"
    opt_summary = opt_res.outputs["solver_summary"]
    assert opt_summary["termination_reason"] == "optimal"
    assert opt_summary["best_bound"] == 3001.5
    assert opt_summary["relative_gap"] == 0.0
    assert dict(opt_summary["solver_diagnostics"]) == {
        "solve_domain": "valid_relaxation",
        "original_model_verified": True,
        "subdomain_status": "HighsModelStatus.kOptimal",
        "subdomain_best_bound": 3001.5,
        "subdomain_relative_gap": 0.0,
    }

    tl_base = (
        SHIM_OPTIMAL.replace("kOptimal", "kTimeLimit")
        .replace("best_bound: 3000.0", "best_bound: 2400.0")
        .replace("relative_gap: 0.0", "relative_gap: 0.2")
    )
    tl_raw = _with_headers(
        tl_base,
        {
            "solve_domain": "valid_relaxation",
            "original_model_verified": "true",
        },
    )
    tl_res = _adapt(None, _receipt(), tl_raw)
    assert tl_res.math_status == "feasible"
    tl_summary = tl_res.outputs["solver_summary"]
    assert tl_summary["termination_reason"] == "time_limit"
    assert tl_summary["best_bound"] == 2401.5
    assert tl_summary["relative_gap"] == 0.2
    assert dict(tl_summary["solver_diagnostics"]) == {
        "solve_domain": "valid_relaxation",
        "original_model_verified": True,
    }

    infeas_raw = _with_headers(SHIM_INFEASIBLE, {"solve_domain": "valid_relaxation"})
    infeas_res = _adapt(None, _receipt(), infeas_raw)
    assert infeas_res.math_status == "infeasible"
    assert infeas_res.outputs["solver_summary"]["termination_reason"] == "infeasible"
    assert dict(infeas_res.outputs["solver_summary"]["solver_diagnostics"]) == {
        "solve_domain": "valid_relaxation",
        "original_model_verified": False,
    }


def test_restricted_domain_forces_feasible_and_nulls_global_bounds_even_on_koptimal() -> None:
    from iesplan.application import worker as worker_app
    from iesplan.metrics import OptimalityValidity
    from iesplan.results import check_optimality

    raw = _with_headers(
        SHIM_OPTIMAL,
        {
            "solve_domain": "restricted",
            "original_model_verified": "true",
            "subdomain_status": "HighsModelStatus.kOptimal",
            "subdomain_best_bound": "3000.0",
            "subdomain_relative_gap": "0.0",
        },
    )
    res = _adapt(None, _receipt(), raw)
    assert res.execution_status == "completed"
    assert res.math_status == "feasible"
    summary = res.outputs["solver_summary"]
    assert summary["termination_reason"] == "optimal"
    assert summary["math_status"] == "feasible"
    assert summary["has_feasible_solution"] is True
    assert summary["objective_value"] == 3001.5
    assert summary["best_bound"] is None
    assert summary["relative_gap"] is None
    assert dict(summary["solver_diagnostics"]) == {
        "solve_domain": "restricted",
        "original_model_verified": True,
        "subdomain_status": "HighsModelStatus.kOptimal",
        "subdomain_best_bound": 3001.5,
        "subdomain_relative_gap": 0.0,
    }
    assert res.outputs["solution"]["objective_value"] == 3001.5
    assert worker_app.business_outcome_for(res) == "restricted_results"
    validity, _ = check_optimality({"compute_result": {"outputs": {"solver_summary": dict(summary)}}})
    assert validity == OptimalityValidity.restricted

    # Unit conversion applies to subdomain_best_bound while global best_bound stays None
    phys_sol = (
        SHIM_OPTIMAL.replace("objective_value: 3000.0", "objective_value: 7200000.0")
        .replace("best_bound: 3000.0", "best_bound: 7200000.0")
        .replace("0: 3000.0", "0: 3600000.0", 1)
    )
    phys_raw = _with_headers(
        phys_sol,
        {
            "solve_domain": "restricted",
            "original_model_verified": "true",
            "subdomain_status": "HighsModelStatus.kOptimal",
            "subdomain_best_bound": "3600000.0",
            "subdomain_relative_gap": "0.5",
        },
    )
    phys_files = {
        PROBLEM_PATH: TINY_MPS.encode("utf-8"),
        PARAMETERS_PATH: _parameters(
            unit_map={"x": "kWh", "y": "kWh"},
            offset=0.0,
            objective_unit="kWh",
        ),
    }
    phys_res = _adapt(phys_files, _receipt(), phys_raw)
    assert phys_res.math_status == "feasible"
    phys_summary = phys_res.outputs["solver_summary"]
    assert phys_summary["objective_value"] == 2.0
    assert phys_summary["best_bound"] is None
    assert phys_summary["relative_gap"] is None
    assert phys_summary["solver_diagnostics"]["subdomain_best_bound"] == 1.0
    assert phys_summary["solver_diagnostics"]["subdomain_relative_gap"] == 0.5


def test_restricted_without_feasible_solution_maps_not_solved() -> None:
    tl_raw = _with_headers(
        V1_TIME_LIMIT,
        {
            "solve_domain": "restricted",
            "subdomain_status": "HighsModelStatus.kTimeLimit",
            "subdomain_best_bound": "none",
            "subdomain_relative_gap": "none",
        },
    )
    tl_res = _adapt(None, _receipt(), tl_raw)
    assert tl_res.math_status == "not_solved"
    tl_summary = tl_res.outputs["solver_summary"]
    assert tl_summary["termination_reason"] == "time_limit"
    assert tl_summary["best_bound"] is None
    assert tl_summary["relative_gap"] is None
    assert dict(tl_summary["solver_diagnostics"]) == {
        "solve_domain": "restricted",
        "original_model_verified": False,
        "subdomain_status": "HighsModelStatus.kTimeLimit",
        "subdomain_best_bound": None,
        "subdomain_relative_gap": None,
    }

    infeas_raw = _with_headers(SHIM_INFEASIBLE, {"solve_domain": "restricted"})
    infeas_res = _adapt(None, _receipt(), infeas_raw)
    assert infeas_res.math_status == "not_solved"
    assert infeas_res.outputs["solver_summary"]["termination_reason"] == "not_solved"

    # Unverified feasible solution rejected on original domain too; unverified empty primal fails cleanly
    orig_unverified_feasible = _with_headers(
        SHIM_OPTIMAL, {"solve_domain": "original", "original_model_verified": "false"}
    )
    with pytest.raises(ComputationUnavailableError) as exc_info:
        _adapt(None, _receipt(), orig_unverified_feasible)
    assert exc_info.value.reason == "result-protocol-failure"

    unverified_empty_opt = _with_headers(
        SHIM_INFEASIBLE.replace("kInfeasible", "kOptimal"),
        {"solve_domain": "original", "original_model_verified": "false"},
    )
    res_num_fail = _adapt(None, _receipt(), unverified_empty_opt)
    assert res_num_fail.math_status == "numeric_failure"
    assert res_num_fail.outputs["solver_summary"]["solver_diagnostics"]["original_model_verified"] is False

    unverified_empty_tl = _with_headers(
        V1_TIME_LIMIT, {"solve_domain": "original", "original_model_verified": "false"}
    )
    res_tl_fail = _adapt(None, _receipt(), unverified_empty_tl)
    assert res_tl_fail.math_status == "not_solved"
    assert res_tl_fail.outputs["solver_summary"]["termination_reason"] == "time_limit"
