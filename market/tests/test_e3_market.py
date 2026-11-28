"""Unit and Docker integration tests for standalone pIES Market (`pies_market`)."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pies_sdk import (
    PluginValidationError,
    compute_publisher_email_digest,
    generate_public_namespace,
    validate_market_index,
    validate_publishers_registry,
    verify_external_sha256,
)

from pies_market import (
    MarketValidationError,
    build_market_index,
    check_market_pr,
    load_strict_yaml,
    validate_device_model_file,
    validate_device_model_mapping,
)

MARKET_ROOT = Path(__file__).resolve().parents[1]
PIES_MARKET_DIR = MARKET_ROOT / "pies_market"

_FORBIDDEN_MODULE_PREFIXES = (
    "iesplan",
    "fastapi",
    "sqlalchemy",
    "pydantic",
    "redis",
    "celery",
    "pies_job_runner",
)


def _copy_market(dest: Path) -> Path:
    return shutil.copytree(MARKET_ROOT, dest, ignore=shutil.ignore_patterns("dist", "__pycache__"))


def test_publishers_registry_and_zero_plaintext(tmp_path: Path) -> None:
    pub_file = MARKET_ROOT / "publishers.yaml"
    raw_text = pub_file.read_text(encoding="utf-8")
    assert "@" not in raw_text

    doc = load_strict_yaml(pub_file, label="publishers.yaml")
    validated = validate_publishers_registry(doc)
    assert validated["schema"] == "ies.publishers"
    assert validated["publishers"] == {}

    # Plaintext '@' anywhere in publishers.yaml is rejected by check_pr
    bad_market = _copy_market(tmp_path / "bad_pub_email")
    (bad_market / "publishers.yaml").write_text(
        'schema: ies.publishers\nschema_version: "1.0.0"\n# contact: foo@example.com\npublishers: {}\n',
        encoding="utf-8",
    )
    with pytest.raises(MarketValidationError, match="plaintext '@'"):
        check_market_pr(bad_market)

    # Reserved 'ies' cannot be registered in publishers.yaml
    bad_reserved = _copy_market(tmp_path / "bad_pub_reserved")
    digest = compute_publisher_email_digest("ops@example.org", hmac_secret="test-secret")
    (bad_reserved / "publishers.yaml").write_text(
        (
            'schema: ies.publishers\nschema_version: "1.0.0"\n'
            f'publishers:\n  ies:\n    email_digest: "{digest}"\n    status: active\n'
        ),
        encoding="utf-8",
    )
    with pytest.raises(PluginValidationError, match="Reserved publisher"):
        check_market_pr(bad_reserved)


def test_seed_market_pr_check_and_publisher_ownership_rules(tmp_path: Path) -> None:
    res = check_market_pr(MARKET_ROOT, docker_selftest=False)
    assert res["ok"] is True
    assert res["artifacts_checked"] == 2
    assert res["active_artifacts"] == 2
    assert res["delisted_artifacts"] == 0
    assert res["docker_tests_run"] == 0

    seed_dev_dir = MARKET_ROOT / "artifacts" / "ies" / "ies.device.solar_pv_sample" / "r1"
    seed_meta_text = (seed_dev_dir / "meta.yaml").read_text(encoding="utf-8")
    seed_yaml_text = (seed_dev_dir / "ies.device.solar_pv_sample.device.yaml").read_text(encoding="utf-8")

    # Active community publisher works when registered with explicit status: active
    digest = compute_publisher_email_digest("vendor@acme.org", hmac_secret="secret")
    active_market = _copy_market(tmp_path / "active_pub")
    (active_market / "publishers.yaml").write_text(
        (
            'schema: ies.publishers\nschema_version: "1.0.0"\n'
            f'publishers:\n  acme:\n    email_digest: "{digest}"\n    status: active\n'
        ),
        encoding="utf-8",
    )
    acme_dir = active_market / "artifacts" / "acme" / "acme.device.pump" / "r1"
    acme_dir.mkdir(parents=True)
    (acme_dir / "meta.yaml").write_text(
        seed_meta_text.replace("ies.device.solar_pv_sample", "acme.device.pump").replace(
            "publisher: ies", "publisher: acme"
        ),
        encoding="utf-8",
    )
    (acme_dir / "acme.device.pump.device.yaml").write_text(
        seed_yaml_text.replace("ies.device.solar_pv_sample", "acme.device.pump"),
        encoding="utf-8",
    )
    (acme_dir / "README.md").write_text("# ACME Pump\n", encoding="utf-8")
    assert check_market_pr(active_market)["artifacts_checked"] == 3

    # Missing status in publishers.yaml must be rejected (must not default to active)
    (active_market / "publishers.yaml").write_text(
        (
            'schema: ies.publishers\nschema_version: "1.0.0"\n'
            f'publishers:\n  acme:\n    email_digest: "{digest}"\n'
        ),
        encoding="utf-8",
    )
    with pytest.raises(MarketValidationError, match="explicit status='active'"):
        check_market_pr(active_market)

    # Suspended publisher rejected
    (active_market / "publishers.yaml").write_text(
        (
            'schema: ies.publishers\nschema_version: "1.0.0"\n'
            f'publishers:\n  acme:\n    email_digest: "{digest}"\n    status: suspended\n'
        ),
        encoding="utf-8",
    )
    with pytest.raises(MarketValidationError, match="explicit status='active'"):
        check_market_pr(active_market)

    # Unregistered publisher rejected
    (active_market / "publishers.yaml").write_text(
        'schema: ies.publishers\nschema_version: "1.0.0"\npublishers: {}\n',
        encoding="utf-8",
    )
    with pytest.raises(MarketValidationError, match="Unregistered publisher"):
        check_market_pr(active_market)

    # user.<ns>.* namespace rejected
    ns = generate_public_namespace()
    user_market = _copy_market(tmp_path / "user_ns_market")
    u_dir = user_market / "artifacts" / f"user.{ns}" / f"user.{ns}.device.pv" / "r1"
    u_dir.mkdir(parents=True)
    (u_dir / "meta.yaml").write_text(
        seed_meta_text.replace("ies.device.solar_pv_sample", f"user.{ns}.device.pv").replace(
            "publisher: ies", f"publisher: user.{ns}"
        ),
        encoding="utf-8",
    )
    (u_dir / f"user.{ns}.device.pv.device.yaml").write_text(
        seed_yaml_text.replace("ies.device.solar_pv_sample", f"user.{ns}.device.pv"),
        encoding="utf-8",
    )
    (u_dir / "README.md").write_text("# User PV\n", encoding="utf-8")
    with pytest.raises(PluginValidationError, match="forbidden in public market"):
        check_market_pr(user_market)

    # Missing README.md, LICENSE, or tests directory rejected on plugins
    missing_targets = (
        "plugin/README.md",
        "plugin/LICENSE",
        "plugin/tests/minimal/expected",
    )
    for rel_remove in missing_targets:
        broken = _copy_market(tmp_path / f"broken_{Path(rel_remove).name}")
        target = broken / "artifacts/ies/ies.evaluator.sample_carbon/1.0.0" / rel_remove
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        with pytest.raises(PluginValidationError):
            check_market_pr(broken)


def test_static_check_never_executes_untrusted_plugin_code(tmp_path: Path) -> None:
    mkt = _copy_market(tmp_path / "no_exec_market")
    sentinel = tmp_path / "executed.flag"
    eval_py = mkt / "artifacts/ies/ies.evaluator.sample_carbon/1.0.0/plugin/evaluate.py"
    eval_py.write_text(
        (
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('ran', encoding='utf-8')\n"
            "def evaluate(ctx):\n"
            f"    Path({str(sentinel)!r}).write_text('ran_fn', encoding='utf-8')\n"
        ),
        encoding="utf-8",
    )
    summary = check_market_pr(mkt, docker_selftest=False)
    assert summary["ok"] is True
    assert summary["docker_tests_run"] == 0
    build_market_index(mkt, tmp_path / "dist_no_exec", generated_at="2026-11-28T00:00:00Z")
    assert not sentinel.exists()


def test_device_validator_ast_equations_and_forbidden_fields() -> None:
    seed_dev = (
        MARKET_ROOT / "artifacts/ies/ies.device.solar_pv_sample/r1/ies.device.solar_pv_sample.device.yaml"
    )
    parsed = validate_device_model_file(seed_dev, allow_user_namespace=False)
    assert parsed["device"]["id"] == "ies.device.solar_pv_sample"

    # Valid storage equations with [t], [t-1], step_duration, and scalar initial expression
    storage_doc = {
        "schema": "ies.device-model",
        "schema_version": "1.0.0",
        "device": {
            "id": "ies.device.storage_sample",
            "names": {"en-US": "Sample Storage"},
        },
        "properties": {
            "capacity_kwh": {"value": 100.0, "unit": "kWh"},
            "initial_soc": {"value": 0.5, "unit": "1"},
        },
        "interfaces": {
            "electricity": {
                "type": "bidirectional",
                "carrier": "electricity",
                "unit": "kW",
                "valid_range": {"minimum": None, "maximum": None},
            }
        },
        "equations": {
            "variables": {
                "stored_energy": {
                    "unit": "kWh",
                    "initial": {"expression": "initial_soc * capacity_kwh"},
                },
                "charge": {"unit": "kW"},
                "discharge": {"unit": "kW"},
            },
            "relations": [
                {"id": "power_balance", "expression": "electricity[t] = discharge[t] - charge[t]"},
                {"id": "exclusivity", "expression": "charge[t] * discharge[t] = 0"},
                {
                    "id": "soc_step",
                    "expression": (
                        "stored_energy[t] = stored_energy[t-1] + "
                        "(charge[t] - discharge[t]) * step_duration"
                    ),
                },
            ],
        },
    }
    assert validate_device_model_mapping(storage_doc)["device"]["id"] == "ies.device.storage_sample"

    # Rejects financial / precision / code fields
    bad_price_named = dict(parsed)
    bad_price_named["properties"] = {
        **parsed["properties"],
        "price": {"value": 1.2, "unit": "CNY"},
    }
    with pytest.raises(MarketValidationError, match="financial, precision, or code"):
        validate_device_model_mapping(bad_price_named)

    # Rejects template inputs in published market device YAML
    with_inputs = {**parsed, "inputs": {"rated_capacity_kwp": {"type": "number", "unit": "kWp"}}}
    with pytest.raises(MarketValidationError, match="inputs"):
        validate_device_model_mapping(with_inputs)

    # Rejects broken syntax, attribute calls, undeclared symbols, [t-2], and scalar time indexing
    invalid_expressions = (
        ("electric_out[t] = (", "invalid expression syntax"),
        ("electric_out[t] = os.system('id')", "forbidden expression construct"),
        ("electric_out[t] = eval('1')", "forbidden expression construct"),
        ("electric_out[t] = unknown_var[t]", "undeclared symbol"),
        ("electric_out[t] = solar_irradiance[t-2]", "unsupported time index"),
        ("electric_out[t] = rated_capacity_kwp[t]", "cannot time-index scalar"),
        ("rated_capacity_kwp = 10", "LHS must reference 1 or 2 declared interfaces/variables"),
    )
    for bad_expr, match_text in invalid_expressions:
        doc = {
            **parsed,
            "equations": {"variables": {}, "relations": [{"id": "bad", "expression": bad_expr}]},
        }
        with pytest.raises(MarketValidationError, match=match_text):
            validate_device_model_mapping(doc)

    # Rejects cyclic variable dependency and time-indexed initial.expression
    cyclic_doc = {
        **storage_doc,
        "equations": {
            "variables": {"a": {"unit": "kW"}, "b": {"unit": "kW"}},
            "relations": [
                {"id": "r1", "expression": "a[t] = b[t]"},
                {"id": "r2", "expression": "b[t] = a[t]"},
            ],
        },
    }
    with pytest.raises(MarketValidationError, match="cyclic variable dependency"):
        validate_device_model_mapping(cyclic_doc)

    bad_init_doc = {
        **storage_doc,
        "equations": {
            "variables": {
                "stored_energy": {"unit": "kWh", "initial": {"expression": "electricity[t] * 2"}}
            },
            "relations": [],
        },
    }
    with pytest.raises(MarketValidationError, match="forbids time indexing"):
        validate_device_model_mapping(bad_init_doc)

    # Rejects source/mode on interfaces
    bad_iface = {
        **parsed,
        "interfaces": {
            **parsed["interfaces"],
            "solar_irradiance": {
                "type": "predefined",
                "carrier": "solar",
                "unit": "W/m2",
                "valid_range": {"minimum": 0, "maximum": 2000},
                "source": "constant",
            },
        },
    }
    with pytest.raises(MarketValidationError, match="forbids project-level source"):
        validate_device_model_mapping(bad_iface)


def test_docker_selftest_builds_requirements_sets_limits_and_cleans_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mkt = _copy_market(tmp_path / "req_mkt")
    plugin_dir = mkt / "artifacts/ies/ies.evaluator.sample_carbon/1.0.0/plugin"
    (plugin_dir / "requirements.txt").write_text("jsonschema>=4.0\n", encoding="utf-8")
    plugin_yaml = plugin_dir / "plugin.yaml"
    plugin_yaml.write_text(
        plugin_yaml.read_text(encoding="utf-8").replace(
            '  python: "3.12"',
            '  python: "3.12"\n  requirements: requirements.txt',
        ),
        encoding="utf-8",
    )

    calls: list[tuple[list[str], int | None]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        timeout_arg = kwargs.get("timeout")
        calls.append((list(cmd), int(timeout_arg) if isinstance(timeout_arg, (int, float)) else None))
        if cmd[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=int(timeout_arg or 60))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("PIES_PLUGIN_BASE_IMAGE", "custom-base:1.2.0")

    with pytest.raises(MarketValidationError, match="timed out after 60s"):
        check_market_pr(mkt, docker_selftest=True)

    build_cmds = [c for c, _ in calls if c[:2] == ["docker", "build"]]
    run_cmds = [(c, t) for c, t in calls if c[:2] == ["docker", "run"]]
    rm_cmds = [c for c, _ in calls if c[:3] == ["docker", "rm", "-f"]]
    rmi_cmds = [c for c, _ in calls if c[:3] == ["docker", "rmi", "-f"]]

    assert len(build_cmds) == 1
    assert "--rm" in build_cmds[0]
    ephemeral_tag = build_cmds[0][build_cmds[0].index("-t") + 1]
    assert ephemeral_tag.startswith("pies-market-st-")

    assert len(run_cmds) == 1
    run_cmd, run_timeout = run_cmds[0]
    assert run_timeout == 60
    assert "--network=none" in run_cmd
    assert "--read-only" in run_cmd
    assert "--cpus" in run_cmd and "--memory" in run_cmd and "--pids-limit" in run_cmd
    assert ephemeral_tag in run_cmd
    container_name = run_cmd[run_cmd.index("--name") + 1]

    assert rm_cmds == [["docker", "rm", "-f", container_name]]
    assert rmi_cmds == [["docker", "rmi", "-f", ephemeral_tag]]


def test_build_index_deterministic_sha256_and_delisted_exclusion(tmp_path: Path) -> None:
    dist1 = tmp_path / "dist1"
    dist2 = tmp_path / "dist2"
    ts = "2026-11-28T00:00:00Z"

    idx1 = build_market_index(MARKET_ROOT, dist1, generated_at=ts)
    idx2 = build_market_index(MARKET_ROOT, dist2, generated_at=ts)

    assert validate_market_index(idx1) == idx2
    assert (dist1 / "index.json").read_bytes() == (dist2 / "index.json").read_bytes()
    assert len(idx1["artifacts"]) == 2

    by_id = {entry["id"]: entry for entry in idx1["artifacts"]}
    assert set(by_id) == {"ies.device.solar_pv_sample", "ies.evaluator.sample_carbon"}
    assert by_id["ies.evaluator.sample_carbon"]["consumes"] == ["ies.engineering-solution@1.0.0"]
    assert by_id["ies.evaluator.sample_carbon"]["produces"] == ["ies.evaluation-report@1.0.0"]

    for entry in idx1["artifacts"]:
        pkg_file = dist1 / entry["url"]
        assert pkg_file.is_file()
        assert verify_external_sha256(pkg_file, entry["sha256"], required=True) == entry["sha256"]
        assert pkg_file.stat().st_size == entry["size_bytes"]
        if "readme_url" in entry:
            assert (dist1 / entry["readme_url"]).is_file()

    # Delisted artifact must be excluded and stale children in out_dir cleaned safely
    delisted_market = _copy_market(tmp_path / "delisted_market")
    dev_meta = delisted_market / "artifacts/ies/ies.device.solar_pv_sample/r1/meta.yaml"
    dev_meta.write_text(
        dev_meta.read_text(encoding="utf-8").replace("delisted: false", "delisted: true"),
        encoding="utf-8",
    )
    assert check_market_pr(delisted_market)["delisted_artifacts"] == 1
    dist_delisted = tmp_path / "dist_delisted"
    (dist_delisted / "stale_dir").mkdir(parents=True)
    (dist_delisted / "stale_dir" / "old.txt").write_text("stale", encoding="utf-8")
    (dist_delisted / "stale_file.txt").write_text("stale", encoding="utf-8")
    delisted_idx = build_market_index(delisted_market, dist_delisted, generated_at=ts)
    assert not (dist_delisted / "stale_dir").exists()
    assert not (dist_delisted / "stale_file.txt").exists()
    assert [a["id"] for a in delisted_idx["artifacts"]] == ["ies.evaluator.sample_carbon"]
    assert not (dist_delisted / "artifacts/ies.device.solar_pv_sample-r1.device.yaml").exists()


def test_zero_iesplan_backend_imports() -> None:
    py_files = sorted(PIES_MARKET_DIR.rglob("*.py"))
    assert py_files
    for py_file in py_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    assert root not in _FORBIDDEN_MODULE_PREFIXES, f"{py_file} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", 1)[0]
                assert root not in _FORBIDDEN_MODULE_PREFIXES, f"{py_file} imports {node.module}"


def test_cli_check_pr_and_build_index(tmp_path: Path) -> None:
    chk = subprocess.run(
        [sys.executable, "-m", "pies_market.check_pr", "--market-root", str(MARKET_ROOT), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert chk.returncode == 0, chk.stderr
    assert json.loads(chk.stdout)["artifacts_checked"] == 2

    out_dir = tmp_path / "cli_dist"
    bld = subprocess.run(
        [
            sys.executable,
            "-m",
            "pies_market.build_index",
            "--market-root",
            str(MARKET_ROOT),
            "--out-dir",
            str(out_dir),
            "--generated-at",
            "2026-11-28T00:00:00Z",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bld.returncode == 0, bld.stderr
    assert json.loads(bld.stdout)["artifact_count"] == 2
    assert (out_dir / "index.json").is_file()


@pytest.mark.skipif(
    os.environ.get("PIES_RUN_DOCKER_TESTS") != "1",
    reason="Real Docker --network=none self-test requires PIES_RUN_DOCKER_TESTS=1",
)
def test_real_docker_network_none_selftest(tmp_path: Path) -> None:
    summary = check_market_pr(MARKET_ROOT, docker_selftest=True)
    assert summary["ok"] is True
    assert summary["docker_tests_run"] == 2

    # Real Docker timeout cleanup: sleeping plugin with timeout_seconds=1 is forcibly removed
    slow_mkt = _copy_market(tmp_path / "slow_mkt")
    plugin_dir = slow_mkt / "artifacts/ies/ies.evaluator.sample_carbon/1.0.0/plugin"
    (plugin_dir / "evaluate.py").write_text(
        "import time\ndef evaluate(ctx):\n    time.sleep(10)\n",
        encoding="utf-8",
    )
    pyaml = plugin_dir / "plugin.yaml"
    pyaml.write_text(
        pyaml.read_text(encoding="utf-8").replace("timeout_seconds: 60", "timeout_seconds: 1"),
        encoding="utf-8",
    )
    with pytest.raises(MarketValidationError, match="timed out after 1s"):
        check_market_pr(slow_mkt, docker_selftest=True)
