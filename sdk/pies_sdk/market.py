"""Public market index, namespace identity, and external SHA-256 verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from pies_sdk.contracts import check_finite_json, check_relative_path, read_json_file
from pies_sdk.errors import PluginValidationError
from pies_sdk.manifest import parse_semver

MARKET_INDEX_SCHEMA = "ies.market-index"
MARKET_INDEX_VERSION = "1.0.0"

ARTIFACT_KIND_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "device_model": "device",
        "predictor": "predictor",
        "generator": "generator",
        "solver": "solver",
        "evaluator": "evaluator",
        "algorithm": "algorithm",
    }
)
MARKET_ARTIFACT_KINDS: tuple[str, ...] = tuple(ARTIFACT_KIND_TYPES)
RESERVED_PUBLISHERS: frozenset[str] = frozenset({"ies", "user", "admin", "system", "official"})

_SCHEMA_PATH = Path(__file__).parent / "schema" / "market-index-1.0.0.schema.json"
_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CROCKFORD_BASE32_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_PUBLIC_NAMESPACE_RE = re.compile(r"^[0-9a-hjkmnp-tv-z]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONSTRAINT_CLAUSE_RE = re.compile(r"^(>=|<=|>|<|==|!=)?\s*(\d+\.\d+\.\d+)$")


@dataclass(frozen=True, slots=True)
class ParsedArtifactIdentity:
    """Parsed canonical namespace identity for device models and algorithm plugins."""

    id: str
    scope: str
    publisher: str
    public_namespace: str | None
    artifact_type: str
    slug: str


def generate_public_namespace() -> str:
    """Generate a 12-character lowercase Crockford Base32 namespace (60 bits of CSPRNG entropy)."""
    return "".join(secrets.choice(_CROCKFORD_BASE32_ALPHABET) for _ in range(12))


def parse_artifact_id(
    artifact_id: str,
    *,
    kind: str | None = None,
    allow_user_namespace: bool = True,
) -> ParsedArtifactIdentity:
    """Validate and parse `ies.<type>.<slug>`, `user.<ns>.<type>.<slug>`, or `<pub>.<type>.<slug>`."""
    if not isinstance(artifact_id, str) or not artifact_id:
        raise PluginValidationError(f"Artifact id must be a non-empty string: {artifact_id!r}")

    expected_type: str | None = None
    if kind is not None:
        if kind not in ARTIFACT_KIND_TYPES:
            raise PluginValidationError(f"Unsupported artifact kind: {kind!r}")
        expected_type = ARTIFACT_KIND_TYPES[kind]

    parts = artifact_id.split(".")
    if len(parts) == 4 and parts[0] == "user":
        if not allow_user_namespace:
            raise PluginValidationError(
                f"Local user namespace id {artifact_id!r} is forbidden in public market"
            )
        _, pub_ns, art_type, slug = parts
        if not _PUBLIC_NAMESPACE_RE.fullmatch(pub_ns):
            raise PluginValidationError(
                f"Invalid user public_namespace {pub_ns!r}; expected [0-9a-hjkmnp-tv-z]{{12}}"
            )
        scope = "user"
        publisher = f"user.{pub_ns}"
        public_namespace: str | None = pub_ns
    elif len(parts) == 3:
        prefix, art_type, slug = parts
        if not _SEGMENT_RE.fullmatch(prefix):
            raise PluginValidationError(f"Invalid publisher prefix in id {artifact_id!r}: {prefix!r}")
        if prefix == "ies":
            scope = "official"
        elif prefix in RESERVED_PUBLISHERS:
            raise PluginValidationError(
                f"Publisher prefix {prefix!r} in id {artifact_id!r} is reserved"
            )
        else:
            scope = "publisher"
        publisher = prefix
        public_namespace = None
    else:
        raise PluginValidationError(
            f"Invalid artifact id format {artifact_id!r}; expected 'ies.<type>.<slug>', "
            "'user.<ns>.<type>.<slug>', or '<publisher>.<type>.<slug>'"
        )

    if not _SEGMENT_RE.fullmatch(art_type):
        raise PluginValidationError(f"Invalid artifact type segment in {artifact_id!r}: {art_type!r}")
    if expected_type is not None and art_type != expected_type:
        raise PluginValidationError(
            f"Artifact id {artifact_id!r} type {art_type!r} does not match kind {kind!r} "
            f"(expected {expected_type!r})"
        )
    if art_type not in ARTIFACT_KIND_TYPES.values():
        raise PluginValidationError(f"Unknown artifact type segment in {artifact_id!r}: {art_type!r}")
    if not _SEGMENT_RE.fullmatch(slug):
        raise PluginValidationError(f"Invalid slug in artifact id {artifact_id!r}: {slug!r}")

    return ParsedArtifactIdentity(
        id=artifact_id,
        scope=scope,
        publisher=publisher,
        public_namespace=public_namespace,
        artifact_type=art_type,
        slug=slug,
    )


def validate_artifact_id(
    artifact_id: str,
    *,
    kind: str | None = None,
    allow_user_namespace: bool = True,
) -> ParsedArtifactIdentity:
    """Alias for `parse_artifact_id` enforcing canonical namespace and type alignment."""
    return parse_artifact_id(
        artifact_id,
        kind=kind,
        allow_user_namespace=allow_user_namespace,
    )


def verify_external_sha256(
    data: bytes | Path,
    expected_sha256: str | None = None,
    *,
    required: bool = False,
) -> str | None:
    """Verify external artifact SHA-256 according to the 3-way boundary rule.

    - Remote market download (`required=True`): expected digest is mandatory and verified.
    - Offline import with expected digest (`expected_sha256` provided): verified.
    - Offline import without digest (`expected_sha256 is None` and `required=False`): skipped.
    """
    if expected_sha256 is None:
        if required:
            raise PluginValidationError("External sha256 digest is required for market verification")
        return None
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
        raise PluginValidationError(f"Invalid expected sha256 digest: {expected_sha256!r}")
    expected = expected_sha256.lower()

    hasher = hashlib.sha256()
    if isinstance(data, Path):
        if not data.is_file():
            raise PluginValidationError(f"Artifact file does not exist for hashing: {data}")
        with data.open("rb") as fh:
            while chunk := fh.read(65536):
                hasher.update(chunk)
    elif isinstance(data, (bytes, bytearray)):
        hasher.update(bytes(data))
    else:
        raise TypeError(f"data must be bytes or Path, got {type(data).__name__}")

    actual = hasher.hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise PluginValidationError(
            f"External SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return actual


def load_market_index_schema() -> dict[str, Any]:
    """Load the bundled Draft 2020-12 JSON Schema for ies.market-index@1.0.0."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate_compatibility_syntax(compat: Mapping[str, Any]) -> None:
    for target, raw_expr in compat.items():
        clauses = [part.strip() for part in str(raw_expr).split(",")]
        for clause in clauses:
            match = _CONSTRAINT_CLAUSE_RE.fullmatch(clause)
            if not match:
                raise PluginValidationError(
                    f"Invalid compatibility.{target} version constraint: {raw_expr!r}"
                )
            parse_semver(match.group(2))


def validate_market_index(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an ies.market-index@1.0.0 payload against schema and uniqueness rules."""
    if not isinstance(data, Mapping):
        raise PluginValidationError("Market index must be a JSON object")
    check_finite_json(data, "market_index")

    validator = Draft202012Validator(load_market_index_schema())
    errors = sorted(validator.iter_errors(dict(data)), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise PluginValidationError(f"Market index invalid [{loc}]: {first.message}")

    generated_at = data["generated_at"]
    if not isinstance(generated_at, str) or "T" not in generated_at or not generated_at.endswith("Z"):
        raise PluginValidationError(
            f"Market index generated_at must be a UTC ISO-8601 string ending in 'Z': {generated_at!r}"
        )
    try:
        datetime.fromisoformat(f"{generated_at[:-1]}+00:00")
    except ValueError as exc:
        raise PluginValidationError(f"Invalid market index generated_at: {generated_at!r}") from exc

    seen_device_revs: set[tuple[str, int]] = set()
    seen_plugin_vers: set[tuple[str, str]] = set()
    for idx, entry in enumerate(data["artifacts"]):
        kind = entry["kind"]
        art_id = entry["id"]
        parsed = parse_artifact_id(art_id, kind=kind, allow_user_namespace=False)
        if entry["publisher"] != parsed.publisher:
            raise PluginValidationError(
                f"artifacts[{idx}].publisher {entry['publisher']!r} does not match id {art_id!r}"
            )
        check_relative_path(entry["url"], f"artifacts[{idx}].url")
        if "readme_url" in entry:
            check_relative_path(entry["readme_url"], f"artifacts[{idx}].readme_url")
        _validate_compatibility_syntax(entry["compatibility"])

        if kind == "device_model":
            rev = entry["revision"]
            if (art_id, rev) in seen_device_revs:
                raise PluginValidationError(
                    f"Duplicate device_model entry in market index: ({art_id!r}, revision={rev})"
                )
            seen_device_revs.add((art_id, rev))
        else:
            ver = entry["version"]
            if (art_id, ver) in seen_plugin_vers:
                raise PluginValidationError(
                    f"Duplicate plugin entry in market index: ({art_id!r}, version={ver!r})"
                )
            seen_plugin_vers.add((art_id, ver))

    return dict(data)


def load_market_index(path: str | Path) -> dict[str, Any]:
    """Read and validate a market index JSON file."""
    raw = read_json_file(Path(path))
    return validate_market_index(raw)
