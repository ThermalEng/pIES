"""Tests for `pies-sdk validate` and `pies-sdk pack` across all 5 plugin kinds and safety gates."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from pies_sdk import (
    PluginValidationError,
    generate_public_namespace,
    pack_plugin_package,
    validate_plugin_package,
    write_json_file,
)
from pies_sdk.cli import main as cli_main

SDK_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = SDK_ROOT / "examples"


def _write_dir_plugin(
    root: Path,
    *,
    plugin_id: str,
    kind: str,
    consumes: list[str],
    produces: list[str],
    main_code: str,
    test_in_files: dict[str, object],
    test_expected_files: dict[str, object],
    with_market_docs: bool = True,
    requirements: str | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    req_line = f'  requirements: "{requirements}"\n' if requirements else ""
    consumes_yaml = "".join(f"  - {c}\n" for c in consumes)
    produces_yaml = "".join(f"  - {p}\n" for p in produces)
    (root / "plugin.yaml").write_text(
        (
            "schema: ies.algorithm-plugin\n"
            'schema_version: "1.0.0"\n'
            f"plugin_id: {plugin_id}\n"
            'version: "1.0.0"\n'
            f"kind: {kind}\n"
            "name:\n"
            f"  en_us: {plugin_id}\n"
            f"description: Offline {kind} fixture.\n"
            "license: MPL-2.0\n"
            "compatibility:\n"
            '  pies: ">=1.2.0,<2.0.0"\n'
            '  sdk: ">=1.2.0,<2.0.0"\n'
            f"consumes:\n{consumes_yaml}"
            f"produces:\n{produces_yaml}"
            "runtime:\n"
            "  language: python\n"
            '  python: "3.12"\n'
            f"{req_line}"
            "entrypoint: main:run\n"
            "parameters_schema:\n"
            "  type: object\n"
            "  additionalProperties: false\n"
            "  properties: {}\n"
            "resources:\n"
            "  default: {cpu: 1, memory_mb: 128, timeout_seconds: 30, process_limit: 4}\n"
            "  maximum: {cpu: 2, memory_mb: 256, timeout_seconds: 60, process_limit: 8}\n"
            "tests:\n"
            "  - id: dir_case\n"
            "    in: tests/dir_case/in\n"
            "    expected: tests/dir_case/expected\n"
        ),
        encoding="utf-8",
    )
    (root / "main.py").write_text(main_code, encoding="utf-8")
    if with_market_docs:
        (root / "README.md").write_text(f"# {plugin_id}\n", encoding="utf-8")
        (root / "LICENSE").write_text("MPL-2.0\n", encoding="utf-8")
    if requirements:
        (root / requirements).write_text("jsonschema>=4.0\n", encoding="utf-8")

    for rel, content in test_in_files.items():
        target = root / "tests" / "dir_case" / "in" / rel
        if isinstance(content, str):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        else:
            write_json_file(target, content)
    for rel, content in test_expected_files.items():
        target = root / "tests" / "dir_case" / "expected" / rel
        if isinstance(content, str):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        else:
            write_json_file(target, content)
    return root


def test_validate_and_pack_reference_examples_and_cli(tmp_path: Path) -> None:
    for example_name in ("reference_algorithm", "iteration_loop"):
        example_src = EXAMPLES_DIR / example_name
        res = validate_plugin_package(example_src, run_tests=True, require_market_fields=False)
        assert res["ok"] is True
        assert res["tests_run"] == 1

        out_zip = tmp_path / f"{example_name}.algorithm.zip"
        packed = pack_plugin_package(
            example_src,
            output_path=out_zip,
            run_validate=True,
            require_market_fields=False,
        )
        assert Path(packed["archive_path"]).is_file()
        assert validate_plugin_package(out_zip, run_tests=True)["ok"] is True

    # CLI validate & pack --json work both directly and via module/script
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pies_sdk.cli",
            "validate",
            str(EXAMPLES_DIR / "reference_algorithm"),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["plugin_id"] == "acme.algorithm.reference_dispatch"


def test_validate_supports_all_five_plugin_kinds_directory_and_file_modes(tmp_path: Path) -> None:
    # 1) predictor (directory mode)
    pred_out = {
        "schema": "ies.prediction-output",
        "schema_version": "1.0.0",
        "predictions": [{"instance": "pv1", "interface": "power_out", "values": [1.0, 2.0]}],
        "artifacts": [],
    }
    pred_dir = _write_dir_plugin(
        tmp_path / "pred",
        plugin_id="acme.predictor.stub",
        kind="predictor",
        consumes=["ies.stage-input@1.0.0"],
        produces=["ies.prediction-output@1.0.0"],
        main_code=(
            "from pies_sdk import PredictionOutputPayload, StageContext\n"
            "def run(ctx: StageContext) -> None:\n"
            "    req = ctx.read_request()\n"
            "    ctx.write_prediction(PredictionOutputPayload(predictions=[\n"
            "        {'instance': 'pv1', 'interface': 'power_out', 'values': req['vals']}\n"
            "    ]))\n"
        ),
        test_in_files={"request.json": {"vals": [1.0, 2.0]}},
        test_expected_files={"prediction.json": pred_out},
    )
    assert validate_plugin_package(pred_dir, run_tests=True, require_market_fields=True)["ok"] is True

    # 2) generator (directory mode -> bundle/)
    bundle_manifest = {
        "schema": "ies.solver-bundle",
        "schema_version": "1.0.0",
        "bundle": {"generator": "acme.generator.stub@1.0.0", "contract": "ies.solver-bundle.mps@1.0.0"},
        "inputs": [{"path": "input/problem.mps", "media_type": "application/mps"}],
        "outputs": [
            {"path": "output/primal_solution.json", "media_type": "application/json", "required": True}
        ],
        "extensions": {},
    }
    gen_dir = _write_dir_plugin(
        tmp_path / "gen",
        plugin_id="acme.generator.stub",
        kind="generator",
        consumes=["ies.stage-input@1.0.0"],
        produces=["ies.solver-bundle.mps@1.0.0"],
        main_code=(
            "from pies_sdk import StageContext\n"
            "def run(ctx: StageContext) -> None:\n"
            "    req = ctx.read_request()\n"
            "    ctx.write_bundle(req['manifest'], {'input/problem.mps': 'NAME TEST\\nENDATA\\n'})\n"
        ),
        test_in_files={"request.json": {"manifest": bundle_manifest}},
        test_expected_files={"bundle/input/problem.mps": "NAME TEST\nENDATA\n"},
    )
    assert validate_plugin_package(gen_dir, run_tests=True, require_market_fields=True)["ok"] is True

    # 3) solver (directory mode -> primal_solution.json)
    primal_doc = {
        "schema": "ies.primal-solution",
        "schema_version": "1.0.0",
        "math_status": "solved",
        "termination_reason": "optimal",
        "has_feasible_solution": True,
        "column_count": 2,
        "objective_value": 5.0,
        "best_bound": 5.0,
        "relative_gap": 0.0,
        "values": [2.0, 3.0],
        "receipt": {"solver": "acme.solver.stub@1.0.0"},
    }
    solver_dir = _write_dir_plugin(
        tmp_path / "solver",
        plugin_id="acme.solver.stub",
        kind="solver",
        consumes=["ies.solver-bundle.mps@1.0.0"],
        produces=["ies.primal-solution@1.0.0"],
        main_code=(
            "from pies_sdk import StageContext, read_solver_bundle\n"
            "def run(ctx: StageContext) -> None:\n"
            "    read_solver_bundle(ctx.work_in)\n"
            "    ctx.write_primal_solution({\n"
            "        'schema': 'ies.primal-solution', 'schema_version': '1.0.0',\n"
            "        'math_status': 'solved', 'termination_reason': 'optimal',\n"
            "        'has_feasible_solution': True, 'column_count': 2,\n"
            "        'objective_value': 5.0, 'best_bound': 5.0, 'relative_gap': 0.0,\n"
            "        'values': [2.0, 3.0], 'receipt': {'solver': 'acme.solver.stub@1.0.0'},\n"
            "    })\n"
        ),
        test_in_files={
            "bundle/bundle.yaml": (
                "schema: ies.solver-bundle\n"
                'schema_version: "1.0.0"\n'
                "bundle:\n"
                "  generator: acme.generator.stub@1.0.0\n"
                "  contract: ies.solver-bundle.mps@1.0.0\n"
                "inputs:\n"
                "  - path: input/problem.mps\n"
                "    media_type: application/mps\n"
                "outputs:\n"
                "  - path: output/primal_solution.json\n"
                "    media_type: application/json\n"
                "    required: true\n"
            ),
            "bundle/input/problem.mps": "NAME TEST\nENDATA\n",
        },
        test_expected_files={"primal_solution.json": primal_doc},
    )
    assert validate_plugin_package(solver_dir, run_tests=True, require_market_fields=True)["ok"] is True

    # 4) evaluator (directory mode -> evaluation_report.json)
    report_doc = {
        "schema": "ies.evaluation-report",
        "schema_version": "1.0.0",
        "title": "Carbon Report",
        "evaluator": "acme.evaluator.carbon@1.0.0",
        "metrics": [{"id": "co2", "label": "CO2", "value": 1.5, "unit": "t"}],
        "charts": [],
        "tables": [],
    }
    eval_dir = _write_dir_plugin(
        tmp_path / "eval",
        plugin_id="acme.evaluator.carbon",
        kind="evaluator",
        consumes=["ies.engineering-solution@1.0.0"],
        produces=["ies.evaluation-report@1.0.0"],
        main_code=(
            "from pies_sdk import StageContext\n"
            "def run(ctx: StageContext) -> None:\n"
            "    sol = ctx.read_engineering_solution()\n"
            "    val = float(sol.solver_summary.objective_value or 0.0)\n"
            "    ctx.write_evaluation_report({\n"
            "        'schema': 'ies.evaluation-report', 'schema_version': '1.0.0',\n"
            "        'title': 'Carbon Report', 'evaluator': 'acme.evaluator.carbon@1.0.0',\n"
            "        'metrics': [{'id': 'co2', 'label': 'CO2', 'value': val, 'unit': 't'}],\n"
            "        'charts': [], 'tables': [],\n"
            "    })\n"
        ),
        test_in_files={
            "solution.json": {
                "schema": "ies.engineering-solution",
                "schema_version": "1.0.0",
                "solver_summary": {
                    "math_status": "solved",
                    "termination_reason": "optimal",
                    "has_feasible_solution": True,
                    "objective_value": 1.5,
                    "objective_unit": "CNY",
                    "best_bound": 1.5,
                    "relative_gap": 0.0,
                },
                "construction_units": [],
                "series": {},
                "system_series": {},
            }
        },
        test_expected_files={"evaluation_report.json": report_doc},
    )
    assert validate_plugin_package(eval_dir, run_tests=True, require_market_fields=True)["ok"] is True


def test_pack_is_deterministic_and_excludes_cache_git_pyc(tmp_path: Path) -> None:
    src = tmp_path / "src_algo"
    shutil.copytree(EXAMPLES_DIR / "reference_algorithm", src)
    (src / "__pycache__").mkdir(exist_ok=True)
    (src / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"bytecode")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (src / "extra.pyc").write_bytes(b"pyc")

    out1 = tmp_path / "a1.algorithm.zip"
    out2 = tmp_path / "a2.algorithm.zip"
    r1 = pack_plugin_package(src, output_path=out1, run_validate=True)
    r2 = pack_plugin_package(src, output_path=out2, run_validate=True)
    assert r1["sha256"] == r2["sha256"]
    assert out1.read_bytes() == out2.read_bytes()

    with zipfile.ZipFile(out1, "r") as zf:
        names = zf.namelist()
        assert "plugin.yaml" in names
        assert not any("__pycache__" in n or ".git" in n or n.endswith(".pyc") for n in names)


def test_validate_market_mode_and_no_tests_with_requirements(tmp_path: Path) -> None:
    # reference_algorithm omits README.md / LICENSE -> passes default, fails --market
    with pytest.raises(PluginValidationError, match="README.md"):
        validate_plugin_package(EXAMPLES_DIR / "reference_algorithm", require_market_fields=True)

    # Local user.<ns>.* passes non-market validate, fails --market
    local_dir = tmp_path / "local_user"
    shutil.copytree(EXAMPLES_DIR / "reference_algorithm", local_dir)
    (local_dir / "README.md").write_text("# Local\n", encoding="utf-8")
    (local_dir / "LICENSE").write_text("MPL-2.0\n", encoding="utf-8")
    ns = generate_public_namespace()
    text = (local_dir / "plugin.yaml").read_text(encoding="utf-8").replace(
        "acme.algorithm.reference_dispatch",
        f"user.{ns}.algorithm.my_dispatch",
    )
    (local_dir / "plugin.yaml").write_text(text, encoding="utf-8")
    assert validate_plugin_package(local_dir, run_tests=True, require_market_fields=False)["ok"] is True
    with pytest.raises(PluginValidationError, match="public market"):
        validate_plugin_package(local_dir, run_tests=False, require_market_fields=True)

    # --no-tests validates AST entrypoint even when runtime.requirements imports uninstalled pkg
    req_dir = tmp_path / "req_algo"
    shutil.copytree(EXAMPLES_DIR / "reference_algorithm", req_dir)
    (req_dir / "requirements.txt").write_text("nonexistent-pypi-pkg==1.0.0\n", encoding="utf-8")
    req_yaml = (req_dir / "plugin.yaml").read_text(encoding="utf-8").replace(
        'python: "3.12"',
        'python: "3.12"\n  requirements: requirements.txt',
    )
    (req_dir / "plugin.yaml").write_text(req_yaml, encoding="utf-8")
    (req_dir / "main.py").write_text(
        "import nonexistent_third_party_pkg\ndef solve(ctx):\n    pass\n",
        encoding="utf-8",
    )
    assert cli_main(["validate", str(req_dir), "--no-tests", "--json"]) == 0

    # Re-exported public callable (from impl import run) allowed by validate (--no-tests & runtime)
    reexport_dir = _write_dir_plugin(
        tmp_path / "reexport",
        plugin_id="acme.algorithm.reexport_demo",
        kind="algorithm",
        consumes=["ies.stage-input@1.0.0"],
        produces=["ies.engineering-solution@1.0.0"],
        main_code="from impl import run\n",
        test_in_files={"request.json": {}},
        test_expected_files={
            "solution.json": {
                "schema": "ies.engineering-solution",
                "schema_version": "1.0.0",
                "solver_summary": {
                    "math_status": "infeasible",
                    "termination_reason": "infeasible",
                    "has_feasible_solution": False,
                    "objective_value": None,
                    "objective_unit": None,
                    "best_bound": None,
                    "relative_gap": None,
                },
            }
        },
    )
    (reexport_dir / "impl.py").write_text(
        "from pies_sdk import EngineeringSolutionPayload, SolverSummary, StageContext\n"
        "def run(ctx: StageContext) -> None:\n"
        "    ctx.write_engineering_solution(EngineeringSolutionPayload(\n"
        "        solver_summary=SolverSummary(\n"
        "            math_status='infeasible', termination_reason='infeasible',\n"
        "            has_feasible_solution=False, objective_value=None,\n"
        "            objective_unit=None, best_bound=None, relative_gap=None,\n"
        "        )\n"
        "    ))\n",
        encoding="utf-8",
    )
    assert validate_plugin_package(reexport_dir, run_tests=False)["ok"] is True
    assert validate_plugin_package(reexport_dir, run_tests=True)["ok"] is True

    # Empty in/ directory preserved across pack -> validate roundtrip + unsafe test.id rejected cleanly
    empty_in_dir = _write_dir_plugin(
        tmp_path / "empty_in",
        plugin_id="acme.algorithm.empty_in",
        kind="algorithm",
        consumes=["ies.stage-input@1.0.0"],
        produces=["ies.engineering-solution@1.0.0"],
        main_code=(
            "from pies_sdk import EngineeringSolutionPayload, SolverSummary, StageContext\n"
            "def run(ctx: StageContext) -> None:\n"
            "    ctx.write_engineering_solution(EngineeringSolutionPayload(\n"
            "        solver_summary=SolverSummary(\n"
            "            math_status='infeasible', termination_reason='infeasible',\n"
            "            has_feasible_solution=False, objective_value=None,\n"
            "            objective_unit=None, best_bound=None, relative_gap=None,\n"
            "        )\n"
            "    ))\n"
        ),
        test_in_files={},
        test_expected_files={
            "solution.json": {
                "schema": "ies.engineering-solution",
                "schema_version": "1.0.0",
                "solver_summary": {
                    "math_status": "infeasible",
                    "termination_reason": "infeasible",
                    "has_feasible_solution": False,
                    "objective_value": None,
                    "objective_unit": None,
                    "best_bound": None,
                    "relative_gap": None,
                },
            }
        },
    )
    (empty_in_dir / "tests" / "dir_case" / "in").mkdir(parents=True, exist_ok=True)
    empty_in_zip = tmp_path / "empty_in.algorithm.zip"
    pack_plugin_package(empty_in_dir, output_path=empty_in_zip, run_validate=True)
    assert validate_plugin_package(empty_in_zip, run_tests=True)["ok"] is True

    # Traversal-like test.id cannot escape temp dir when running offline tests
    weird_id_dir = tmp_path / "weird_id"
    shutil.copytree(empty_in_dir, weird_id_dir)
    weird_yaml = (weird_id_dir / "plugin.yaml").read_text(encoding="utf-8").replace(
        "id: dir_case", "id: ../../weird_case"
    )
    (weird_id_dir / "plugin.yaml").write_text(weird_yaml, encoding="utf-8")
    assert validate_plugin_package(weird_id_dir, run_tests=True)["test_ids"] == ["../../weird_case"]


def test_validate_rejects_archive_and_directory_security_violations(tmp_path: Path) -> None:
    base_zip = tmp_path / "base.algorithm.zip"
    pack_plugin_package(EXAMPLES_DIR / "reference_algorithm", output_path=base_zip)

    # 1) Wrapper directory
    wrapped_zip = tmp_path / "wrapped.algorithm.zip"
    with zipfile.ZipFile(base_zip, "r") as src_z, zipfile.ZipFile(wrapped_zip, "w") as dst_z:
        for info in src_z.infolist():
            dst_z.writestr(f"nested/{info.filename}", src_z.read(info))
    with pytest.raises(PluginValidationError, match="root"):
        validate_plugin_package(wrapped_zip, run_tests=False)

    # 2) Path traversal (..)
    trav_zip = tmp_path / "trav.algorithm.zip"
    with zipfile.ZipFile(base_zip, "r") as src_z, zipfile.ZipFile(trav_zip, "w") as dst_z:
        for info in src_z.infolist():
            dst_z.writestr(info, src_z.read(info))
        dst_z.writestr("../escape.txt", b"bad")
    with pytest.raises(PluginValidationError, match="Unsafe path"):
        validate_plugin_package(trav_zip, run_tests=False)

    # 3) Absolute path
    abs_zip = tmp_path / "abs.algorithm.zip"
    with zipfile.ZipFile(base_zip, "r") as src_z, zipfile.ZipFile(abs_zip, "w") as dst_z:
        for info in src_z.infolist():
            dst_z.writestr(info, src_z.read(info))
        dst_z.writestr("/etc/passwd", b"bad")
    with pytest.raises(PluginValidationError, match="Unsafe path"):
        validate_plugin_package(abs_zip, run_tests=False)

    # 4) Case-insensitive collision
    coll_zip = tmp_path / "coll.algorithm.zip"
    with zipfile.ZipFile(base_zip, "r") as src_z, zipfile.ZipFile(coll_zip, "w") as dst_z:
        for info in src_z.infolist():
            dst_z.writestr(info, src_z.read(info))
        dst_z.writestr("Main.py", b"x = 1\n")
    with pytest.raises(PluginValidationError, match="collision"):
        validate_plugin_package(coll_zip, run_tests=False)

    # 4b) Dotted alias collision (main.py + ./main.py) rejected before extraction
    dot_coll_zip = tmp_path / "dot_coll.algorithm.zip"
    with zipfile.ZipFile(base_zip, "r") as src_z, zipfile.ZipFile(dot_coll_zip, "w") as dst_z:
        for info in src_z.infolist():
            dst_z.writestr(info, src_z.read(info))
        dst_z.writestr("./main.py", b"x = 1\n")
    with pytest.raises(PluginValidationError, match="collision"):
        validate_plugin_package(dot_coll_zip, run_tests=False)

    # 5) Symlink inside ZIP
    sym_zip = tmp_path / "sym.algorithm.zip"
    with zipfile.ZipFile(base_zip, "r") as src_z, zipfile.ZipFile(sym_zip, "w") as dst_z:
        for info in src_z.infolist():
            dst_z.writestr(info, src_z.read(info))
        sym_info = zipfile.ZipInfo("link.txt")
        sym_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        dst_z.writestr(sym_info, b"main.py")
    with pytest.raises(PluginValidationError, match="Symlink"):
        validate_plugin_package(sym_zip, run_tests=False)

    # 6) Encrypted entry inside ZIP (set general-purpose bit 0 in central directory header)
    enc_zip = tmp_path / "enc.algorithm.zip"
    raw_zip = bytearray(base_zip.read_bytes())
    cd_pos = raw_zip.find(b"PK\x01\x02")
    assert cd_pos != -1
    raw_zip[cd_pos + 8] |= 0x01
    enc_zip.write_bytes(bytes(raw_zip))
    with pytest.raises(PluginValidationError, match="Encrypted"):
        validate_plugin_package(enc_zip, run_tests=False)

    # 7) Dockerfile / .dockerignore in ZIP & directory
    for bad_name in ("Dockerfile", ".dockerignore"):
        bad_dir = tmp_path / f"dir_{bad_name}"
        shutil.copytree(EXAMPLES_DIR / "reference_algorithm", bad_dir)
        (bad_dir / bad_name).write_text("FROM alpine\n", encoding="utf-8")
        with pytest.raises(PluginValidationError, match="Forbidden build file"):
            validate_plugin_package(bad_dir, run_tests=False)

    # 8) Symlink & hardlink in directory
    sym_dir = tmp_path / "sym_dir"
    shutil.copytree(EXAMPLES_DIR / "reference_algorithm", sym_dir)
    os.symlink("main.py", sym_dir / "alias.py")
    with pytest.raises(PluginValidationError, match="Symlink"):
        validate_plugin_package(sym_dir, run_tests=False)

    hard_dir = tmp_path / "hard_dir"
    shutil.copytree(EXAMPLES_DIR / "reference_algorithm", hard_dir)
    os.link(hard_dir / "main.py", hard_dir / "hard_alias.py")
    with pytest.raises(PluginValidationError, match="Hardlinked"):
        validate_plugin_package(hard_dir, run_tests=False)

    # 9) Expected test output mismatch caught cleanly (exit code 2 in CLI)
    bad_exp_dir = tmp_path / "bad_exp"
    shutil.copytree(EXAMPLES_DIR / "reference_algorithm", bad_exp_dir)
    exp_path = bad_exp_dir / "tests" / "minimal" / "expected.json"
    exp_data = json.loads(exp_path.read_text(encoding="utf-8"))
    exp_data["metrics"]["total_cost"] = 999999.0
    exp_path.write_text(json.dumps(exp_data), encoding="utf-8")
    assert cli_main(["validate", str(bad_exp_dir), "--json"]) == 2
