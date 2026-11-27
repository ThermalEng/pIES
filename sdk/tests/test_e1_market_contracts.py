"""Unit tests for E1 market index, artifact namespace identity, and external SHA-256."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from pies_sdk import (
    MARKET_INDEX_SCHEMA,
    MARKET_INDEX_VERSION,
    PluginValidationError,
    StageIOError,
    generate_public_namespace,
    load_market_index,
    parse_artifact_id,
    validate_artifact_id,
    validate_market_index,
    verify_external_sha256,
    write_json_file,
)


def _sample_market_index() -> dict:
    return {
        "schema": MARKET_INDEX_SCHEMA,
        "schema_version": MARKET_INDEX_VERSION,
        "generated_at": "2026-11-27T12:00:00Z",
        "artifacts": [
            {
                "id": "acme.device.chp",
                "kind": "device_model",
                "revision": 1,
                "name": {"zh_cn": "燃气热电联产机组", "en_us": "Gas CHP Unit"},
                "description": "Declarative CHP device model.",
                "publisher": "acme",
                "license": "MPL-2.0",
                "compatibility": {"pies": ">=1.2.0,<2.0.0"},
                "url": "artifacts/acme.device.chp-r1.device.yaml",
                "readme_url": "artifacts/acme.device.chp-r1.README.md",
                "sha256": "e" * 64,
                "size_bytes": 4096,
            },
            {
                "id": "acme.device.chp",
                "kind": "device_model",
                "revision": 2,
                "name": {"en_us": "Gas CHP Unit r2"},
                "description": "Updated CHP device model.",
                "publisher": "acme",
                "license": "MPL-2.0",
                "compatibility": {"pies": ">=1.2.0,<2.0.0"},
                "url": "artifacts/acme.device.chp-r2.device.yaml",
                "sha256": "f" * 64,
                "size_bytes": 4200,
            },
            {
                "id": "ies.predictor.naive",
                "kind": "predictor",
                "version": "1.0.0",
                "name": {"en_us": "Naive Predictor"},
                "description": "Official predictor.",
                "publisher": "ies",
                "license": "MPL-2.0",
                "compatibility": {"pies": ">=1.2.0,<2.0.0"},
                "consumes": ["ies.stage-input@1.0.0"],
                "produces": ["ies.prediction-output@1.0.0"],
                "url": "artifacts/ies.predictor.naive-1.0.0.algorithm.zip",
                "sha256": "a" * 64,
                "size_bytes": 8192,
            },
            {
                "id": "acme.solver.cbc_mps",
                "kind": "solver",
                "version": "1.0.0",
                "name": {"zh_cn": "CBC MPS 求解器", "en_us": "CBC MPS Solver"},
                "description": "CBC solver plugin.",
                "publisher": "acme",
                "license": "MPL-2.0",
                "compatibility": {"pies": ">=1.2.0,<2.0.0", "sdk": ">=1.2.0,<2.0.0"},
                "consumes": ["ies.solver-bundle.mps@1.0.0"],
                "produces": ["ies.primal-solution@1.0.0"],
                "url": "artifacts/acme.solver.cbc_mps-1.0.0.algorithm.zip",
                "readme_url": "artifacts/acme.solver.cbc_mps-1.0.0.README.md",
                "sha256": "0" * 64,
                "size_bytes": 102400,
            },
        ],
    }


def test_market_index_roundtrip_and_multi_version(tmp_path: Path) -> None:
    payload = _sample_market_index()
    validated = validate_market_index(payload)
    assert len(validated["artifacts"]) == 4

    index_file = write_json_file(tmp_path / "index.json", payload)
    loaded = load_market_index(index_file)
    assert loaded == validated


def test_market_index_rejects_invalid_versions_duplicates_and_user_ns() -> None:
    # device_model forbids version and requires int revision >= 1
    bad_dev_ver = _sample_market_index()
    bad_dev_ver["artifacts"][0]["version"] = "1.0.0"
    with pytest.raises(PluginValidationError):
        validate_market_index(bad_dev_ver)

    bad_dev_rev = _sample_market_index()
    bad_dev_rev["artifacts"][0]["revision"] = 0
    with pytest.raises(PluginValidationError):
        validate_market_index(bad_dev_rev)

    # plugin forbids revision and requires semver version
    bad_plug_rev = _sample_market_index()
    bad_plug_rev["artifacts"][3]["revision"] = 1
    with pytest.raises(PluginValidationError):
        validate_market_index(bad_plug_rev)

    # duplicate (id, revision) and duplicate (id, version) rejected
    dup_rev = _sample_market_index()
    dup_rev["artifacts"][1]["revision"] = 1
    with pytest.raises(PluginValidationError, match="Duplicate device_model"):
        validate_market_index(dup_rev)

    dup_ver = _sample_market_index()
    dup_ver["artifacts"].append(deepcopy(dup_ver["artifacts"][3]))
    with pytest.raises(PluginValidationError, match="Duplicate plugin"):
        validate_market_index(dup_ver)

    # user.<ns> forbidden in public market index
    user_entry = _sample_market_index()
    ns = generate_public_namespace()
    user_entry["artifacts"][0]["id"] = f"user.{ns}.device.chp"
    user_entry["artifacts"][0]["publisher"] = f"user.{ns}"
    with pytest.raises(PluginValidationError):
        validate_market_index(user_entry)

    # unknown field rejected
    extra_field = _sample_market_index()
    extra_field["extra"] = True
    with pytest.raises(PluginValidationError, match="extra"):
        validate_market_index(extra_field)

    # non-finite rejected
    non_finite = _sample_market_index()
    non_finite["artifacts"][0]["bad_num"] = float("nan")
    with pytest.raises(StageIOError, match="non-finite"):
        validate_market_index(non_finite)


def test_namespace_identity_and_kind_matching() -> None:
    ns = generate_public_namespace()
    assert len(ns) == 12
    assert set(ns) <= set("abcdefghijklmnopqrstuvwxyz234567")
    assert ns != generate_public_namespace()

    official = parse_artifact_id("ies.solver.highs", kind="solver")
    assert official.scope == "official"
    assert official.publisher == "ies"
    assert official.public_namespace is None

    local_user = validate_artifact_id(f"user.{ns}.device.custom_hp", kind="device_model")
    assert local_user.scope == "user"
    assert local_user.public_namespace == ns
    assert local_user.artifact_type == "device"

    pub = parse_artifact_id("acme.evaluator.carbon", kind="evaluator", allow_user_namespace=False)
    assert pub.scope == "publisher"
    assert pub.publisher == "acme"

    # Reserved publishers rejected
    for reserved in ("admin", "system", "official"):
        with pytest.raises(PluginValidationError, match="reserved"):
            parse_artifact_id(f"{reserved}.solver.foo", kind="solver")

    # user.* forbidden when allow_user_namespace=False
    with pytest.raises(PluginValidationError, match="forbidden in public market"):
        parse_artifact_id(f"user.{ns}.algorithm.my_algo", kind="algorithm", allow_user_namespace=False)

    # Invalid public_namespace (not 12-char Base32)
    with pytest.raises(PluginValidationError, match="public_namespace"):
        parse_artifact_id("user.too_short.device.hp", kind="device_model")

    # Kind-type mismatch rejected
    with pytest.raises(PluginValidationError, match="does not match kind"):
        parse_artifact_id("acme.solver.cbc", kind="evaluator")


def test_verify_external_sha256_three_way_boundary(tmp_path: Path) -> None:
    payload = b"hello pies market"
    digest = hashlib.sha256(payload).hexdigest()
    pkg = tmp_path / "pkg.zip"
    pkg.write_bytes(payload)

    # 1) Offline import without expected digest -> skipped (returns None)
    assert verify_external_sha256(pkg, None, required=False) is None
    assert verify_external_sha256(payload, None, required=False) is None

    # 2) Offline import with expected digest -> verified
    assert verify_external_sha256(pkg, digest.upper(), required=False) == digest
    with pytest.raises(PluginValidationError, match="mismatch"):
        verify_external_sha256(pkg, "0" * 64, required=False)

    # 3) Market download -> digest mandatory
    with pytest.raises(PluginValidationError, match="required"):
        verify_external_sha256(pkg, None, required=True)
    assert verify_external_sha256(pkg, digest, required=True) == digest
