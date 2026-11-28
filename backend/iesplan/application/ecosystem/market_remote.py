"""阶段目录汇总、在线公共市场索引/安装与无状态投稿代理用例。

核心规则：
- ``local`` 模式下禁止任何在线公共市场或投稿网关出站请求（统一 403 ECO-MARKET-LOCAL-MODE）。
- 远端下载严格遵循外部边界三分校验（``verify_external_sha256(..., required=True)``），入库标记
  ``source_origin="online_market", trusted_market=True`` 自动免审批，
  不影响 ``local_upload_requires_approval``。
- 无状态投稿代理绝不持久化、审计或打印任何明文邮箱。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy.orm import Session

from iesplan import ecosystem as ecosystem_domain
from iesplan.application.ecosystem.common import (
    validate_artifact_stable_id,
    verify_external_sha256,
)
from iesplan.application.ecosystem.device_models import import_device_model_case
from iesplan.application.ecosystem.integration import augment_stage_registries_for_user
from iesplan.application.ecosystem.plugins import (
    install_artifact_for_user_case,
    upload_plugin_zip_case,
)
from iesplan.computation.config import (
    BUILTIN_EVALUATOR_FINANCE_SPEC,
    BUILTIN_SOLVER_HIGHS_SPEC,
    build_algorithm_registry,
    build_evaluator_registry,
    build_solver_registry,
    build_stage_plugin_map,
)
from iesplan.computation.generators import HIGHS_MILP_PROVIDER
from iesplan.computation.generators import build_registry as build_generator_registry
from iesplan.computation.predictors import NAIVE_PREDICTOR, build_predictor_registry
from iesplan.computation.protocols import thaw_json_value
from iesplan.core.errors import AppError, ForbiddenError, http_error
from iesplan.ecosystem import (
    ARTIFACT_KINDS,
    PLUGIN_KINDS,
    EcosystemArtifactNotFoundError,
    EcosystemSettingsRecord,
    EcosystemValidationError,
    LocalArtifactRecord,
)
from iesplan.identity.contracts import UserRecord

_MARKET_INDEX_SCHEMA = "ies.market-index"
_MARKET_INDEX_VERSION = "1.0.0"
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTRACT_REF_RE = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)+@[0-9]+\.[0-9]+\.[0-9]+$")
_COMPAT_CLAUSE_RE = re.compile(r"^(>=|<=|>|<|==|!=)?\s*(\d+\.\d+\.\d+)$")

_DEVICE_REQUIRED_KEYS = frozenset(
    {
        "id",
        "kind",
        "revision",
        "name",
        "description",
        "publisher",
        "license",
        "compatibility",
        "url",
        "sha256",
        "size_bytes",
    }
)
_DEVICE_ALLOWED_KEYS = _DEVICE_REQUIRED_KEYS | frozenset({"readme_url"})
_PLUGIN_REQUIRED_KEYS = frozenset(
    {
        "id",
        "kind",
        "version",
        "name",
        "description",
        "publisher",
        "license",
        "compatibility",
        "url",
        "sha256",
        "size_bytes",
    }
)
_PLUGIN_ALLOWED_KEYS = _PLUGIN_REQUIRED_KEYS | frozenset({"consumes", "produces", "readme_url"})

_BUILTIN_STAGE_DESCRIPTIONS: dict[str, str] = {
    "ies.predictor.naive@1.0.0": "末值携载(全确定性, 种子只追溯不参与计算)",
    "ies.generator.highs_milp@1.0.0": "内置 MILP 规划与运行约束装配生成器",
    "ies.solver.highs@1.7.2": "内置 HiGHS 开源混合整数线性规划求解器",
    "ies.evaluator.finance@1.0.0": "内置全生命周期财务评价与税务清分计算器",
}


def build_http_client(*, timeout: float = 30.0) -> httpx.Client:
    """构建远端市场与投稿网关 HTTP 客户端（测试可注入替代）。"""
    return httpx.Client(timeout=timeout)


def _reject_non_finite_json(token: str) -> Any:
    raise EcosystemValidationError(f"远端市场 JSON 包含非法非有限常量: {token!r}")


def _check_finite_json(value: Any, label: str = "payload") -> None:
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise EcosystemValidationError(f"{label} 禁止非有限浮点数: {value!r}")
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise EcosystemValidationError(f"{label} 键须为字符串")
            _check_finite_json(v, f"{label}.{k}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _check_finite_json(item, f"{label}[{idx}]")


def _check_relative_path(path: Any, field_name: str) -> str:
    if not isinstance(path, str) or not path:
        raise EcosystemValidationError(f"{field_name} 须为非空相对路径")
    if "\\" in path or path.startswith("/") or ".." in path.split("/"):
        raise EcosystemValidationError(f"{field_name} 不允许路径越界或绝对路径: {path!r}")
    return path


def _parse_semver_tuple(raw: Any, label: str) -> tuple[int, int, int]:
    if not isinstance(raw, str) or not _SEMVER_RE.fullmatch(raw):
        raise EcosystemValidationError(f"{label} 须为 X.Y.Z 语义化版本: {raw!r}")
    major, minor, patch = raw.split(".")
    return int(major), int(minor), int(patch)


def _require_online_settings(db: Session) -> EcosystemSettingsRecord:
    settings_rec = ecosystem_domain.get_settings(db)
    if settings_rec.market_mode != "online":
        raise ForbiddenError(
            "当前实例处于 local 离线模式，禁止访问在线公共市场或投稿网关",
            code="ECO-MARKET-LOCAL-MODE",
            message_key="ies.diag.ecosystem.market_local_mode",
            params={"market_mode": settings_rec.market_mode},
        )
    return settings_rec


def _require_market_index_url(settings_rec: EcosystemSettingsRecord) -> str:
    if not settings_rec.market_index_url:
        raise EcosystemValidationError(
            "在线模式下尚未配置公共市场索引地址 market_index_url",
            code="ECO-MARKET-URL-MISSING",
            params={"field": "market_index_url"},
        )
    return settings_rec.market_index_url


def _require_gateway_url(settings_rec: EcosystemSettingsRecord) -> str:
    if not settings_rec.submission_gateway_url:
        raise EcosystemValidationError(
            "在线模式下尚未配置无状态投稿网关地址 submission_gateway_url",
            code="ECO-SUBMISSION-GATEWAY-MISSING",
            params={"field": "submission_gateway_url"},
        )
    return settings_rec.submission_gateway_url


def _default_stage_registries(
    *,
    predictor_registry: Any | None = None,
    generator_registry: Any | None = None,
    solver_registry: Any | None = None,
    evaluator_registry: Any | None = None,
    algorithm_registry: Any | None = None,
) -> tuple[Any, Any, Any, Any, Any]:
    pred_reg = (
        predictor_registry
        if predictor_registry is not None
        else build_predictor_registry((NAIVE_PREDICTOR,))
    )
    gen_reg = (
        generator_registry
        if generator_registry is not None
        else build_generator_registry((HIGHS_MILP_PROVIDER,))
    )
    solv_reg = (
        solver_registry
        if solver_registry is not None
        else build_solver_registry((BUILTIN_SOLVER_HIGHS_SPEC,))
    )
    eval_reg = (
        evaluator_registry
        if evaluator_registry is not None
        else build_evaluator_registry((BUILTIN_EVALUATOR_FINANCE_SPEC,))
    )
    algo_reg = (
        algorithm_registry
        if algorithm_registry is not None
        else build_algorithm_registry(())
    )
    return pred_reg, gen_reg, solv_reg, eval_reg, algo_reg


def _registry_refs(registry: Any) -> set[str]:
    return {desc.ref for desc in registry.snapshot()}


def get_stage_catalog_case(
    db: Session,
    user: UserRecord,
    *,
    predictor_registry: Any | None = None,
    generator_registry: Any | None = None,
    solver_registry: Any | None = None,
    evaluator_registry: Any | None = None,
    algorithm_registry: Any | None = None,
) -> dict[str, Any]:
    """返回当前成员可见的动态阶段算法目录（内置 + 已安装启用且 ready 的生态插件）。"""
    base_pred, base_gen, base_solv, base_eval, base_algo = _default_stage_registries(
        predictor_registry=predictor_registry,
        generator_registry=generator_registry,
        solver_registry=solver_registry,
        evaluator_registry=evaluator_registry,
        algorithm_registry=algorithm_registry,
    )
    builtin_refs = (
        _registry_refs(base_pred)
        | _registry_refs(base_gen)
        | _registry_refs(base_solv)
        | _registry_refs(base_eval)
        | _registry_refs(base_algo)
    )
    enabled_plugins = ecosystem_domain.list_enabled_artifacts_for_user(db, user.id, kinds=PLUGIN_KINDS)
    eco_by_ref: dict[str, LocalArtifactRecord] = {rec.ref: rec for rec in enabled_plugins}

    aug_pred, aug_gen, aug_solv, aug_eval, aug_algo = augment_stage_registries_for_user(
        db,
        user_id=user.id,
        predictor_registry=base_pred,
        generator_registry=base_gen,
        solver_registry=base_solv,
        evaluator_registry=base_eval,
        algorithm_registry=base_algo,
    )
    spec_map = build_stage_plugin_map(
        predictor_registry=aug_pred,
        generator_registry=aug_gen,
        solver_registry=aug_solv,
        evaluator_registry=aug_eval,
        algorithm_registry=aug_algo,
    )
    pred_descs: dict[str, str] = {}
    for desc in aug_pred.snapshot():
        pred_descs[desc.ref] = str(getattr(desc, "description", "") or "")

    buckets: dict[str, list[dict[str, Any]]] = {
        "predictors": [],
        "generators": [],
        "solvers": [],
        "evaluators": [],
        "algorithms": [],
    }
    kind_to_bucket = {
        "predictor": "predictors",
        "generator": "generators",
        "solver": "solvers",
        "evaluator": "evaluators",
        "algorithm": "algorithms",
    }

    for ref in sorted(spec_map):
        spec = spec_map[ref]
        bucket_key = kind_to_bucket.get(spec.kind)
        if bucket_key is None:
            continue
        source = "builtin" if ref in builtin_refs else "ecosystem"
        eco_rec = eco_by_ref.get(ref)
        if eco_rec is not None and source == "ecosystem":
            description = str(eco_rec.metadata_json.get("description") or "")
        elif spec.kind == "predictor":
            description = pred_descs.get(ref) or _BUILTIN_STAGE_DESCRIPTIONS.get(ref, "")
        else:
            description = _BUILTIN_STAGE_DESCRIPTIONS.get(ref, "")
        buckets[bucket_key].append(
            {
                "ref": spec.ref,
                "stable_id": spec.plugin_id,
                "version": spec.version,
                "description": description,
                "consumes": list(spec.consumes),
                "produces": list(spec.produces),
                "parameters_schema": thaw_json_value(spec.parameters_schema),
                "source": source,
            }
        )
    return buckets


def _validate_localized_name(name: Any, idx: int) -> None:
    if not isinstance(name, Mapping) or not name:
        raise EcosystemValidationError(f"market index artifacts[{idx}].name 须为非空多语言映射")
    unknown = set(name) - {"zh_cn", "en_us"}
    if unknown:
        raise EcosystemValidationError(f"market index artifacts[{idx}].name 存在非法键: {sorted(unknown)}")
    for lang_key, val in name.items():
        if not isinstance(val, str) or not val.strip():
            raise EcosystemValidationError(f"market index artifacts[{idx}].name.{lang_key} 须为非空字符串")


def _validate_compatibility(compat: Any, idx: int) -> None:
    if not isinstance(compat, Mapping) or not compat:
        raise EcosystemValidationError(f"market index artifacts[{idx}].compatibility 须为非空映射")
    unknown = set(compat) - {"pies", "sdk"}
    if unknown:
        raise EcosystemValidationError(
            f"market index artifacts[{idx}].compatibility 存在非法目标: {sorted(unknown)}"
        )
    for target, expr in compat.items():
        if not isinstance(expr, str) or not expr.strip():
            raise EcosystemValidationError(
                f"market index artifacts[{idx}].compatibility.{target} 须为非空约束字符串"
            )
        for clause in (part.strip() for part in expr.split(",")):
            match = _COMPAT_CLAUSE_RE.fullmatch(clause)
            if not match:
                raise EcosystemValidationError(
                    f"market index artifacts[{idx}].compatibility.{target} 约束语法非法: {expr!r}"
                )


def _validate_market_index_payload(raw_data: Any) -> dict[str, Any]:
    if not isinstance(raw_data, Mapping):
        raise EcosystemValidationError("公共市场 index.json 顶层须为 JSON 对象")
    _check_finite_json(raw_data, "market_index")
    expected_top = {"schema", "schema_version", "generated_at", "artifacts"}
    if set(raw_data) != expected_top:
        raise EcosystemValidationError(
            f"公共市场 index.json 顶层字段非法: {sorted(raw_data)!r}"
        )
    if (
        raw_data["schema"] != _MARKET_INDEX_SCHEMA
        or raw_data["schema_version"] != _MARKET_INDEX_VERSION
    ):
        schema_val = raw_data.get("schema")
        ver_val = raw_data.get("schema_version")
        raise EcosystemValidationError(
            f"公共市场 index.json schema 不匹配: {schema_val!r}@{ver_val!r}"
        )
    gen_at = raw_data["generated_at"]
    if not isinstance(gen_at, str) or "T" not in gen_at or not gen_at.endswith("Z"):
        raise EcosystemValidationError(f"market index generated_at 须为 UTC ISO-8601 字符串: {gen_at!r}")
    try:
        datetime.fromisoformat(f"{gen_at[:-1]}+00:00")
    except ValueError as exc:
        raise EcosystemValidationError(f"market index generated_at 格式非法: {gen_at!r}") from exc

    artifacts = raw_data["artifacts"]
    if not isinstance(artifacts, list):
        raise EcosystemValidationError("market index artifacts 须为数组")

    seen_device_revs: set[tuple[str, int]] = set()
    seen_plugin_vers: set[tuple[str, str]] = set()
    for idx, entry in enumerate(artifacts):
        if not isinstance(entry, Mapping):
            raise EcosystemValidationError(f"market index artifacts[{idx}] 须为对象")
        kind = entry.get("kind")
        if kind not in ARTIFACT_KINDS:
            raise EcosystemValidationError(f"market index artifacts[{idx}].kind 非法: {kind!r}")
        if kind == "device_model":
            missing = sorted(_DEVICE_REQUIRED_KEYS - set(entry))
            extra = sorted(set(entry) - _DEVICE_ALLOWED_KEYS)
        else:
            missing = sorted(_PLUGIN_REQUIRED_KEYS - set(entry))
            extra = sorted(set(entry) - _PLUGIN_ALLOWED_KEYS)
        if missing or extra:
            raise EcosystemValidationError(
                f"market index artifacts[{idx}] 字段不满足契约 (missing={missing}, extra={extra})"
            )
        art_id = str(entry["id"])
        if art_id.startswith("user."):
            raise EcosystemValidationError(
                f"公共市场 index.json 禁止包含私有 user.* 命名空间制品: {art_id!r}"
            )
        validate_artifact_stable_id(art_id, expected_kind=kind)
        publisher = str(entry["publisher"])
        if publisher != art_id.split(".", 1)[0]:
            raise EcosystemValidationError(
                f"market index artifacts[{idx}].publisher {publisher!r} 与 id {art_id!r} 不匹配"
            )
        for text_field in ("description", "license"):
            if not isinstance(entry[text_field], str) or not entry[text_field].strip():
                raise EcosystemValidationError(f"market index artifacts[{idx}].{text_field} 须为非空字符串")
        _validate_localized_name(entry["name"], idx)
        _validate_compatibility(entry["compatibility"], idx)
        _check_relative_path(entry["url"], f"artifacts[{idx}].url")
        if "readme_url" in entry and entry["readme_url"] is not None:
            _check_relative_path(entry["readme_url"], f"artifacts[{idx}].readme_url")
        sha = entry["sha256"]
        if not isinstance(sha, str) or not _SHA256_HEX_RE.fullmatch(sha):
            raise EcosystemValidationError(f"market index artifacts[{idx}].sha256 须为 64 位小写 hex")
        size_bytes = entry["size_bytes"]
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
            raise EcosystemValidationError(f"market index artifacts[{idx}].size_bytes 须为正整数")

        if kind == "device_model":
            rev = entry["revision"]
            if isinstance(rev, bool) or not isinstance(rev, int) or rev < 1:
                raise EcosystemValidationError(f"market index artifacts[{idx}].revision 须为正整数")
            if (art_id, rev) in seen_device_revs:
                raise EcosystemValidationError(f"market index 存在重复设备条目: ({art_id!r}, {rev})")
            seen_device_revs.add((art_id, rev))
        else:
            ver = entry["version"]
            _parse_semver_tuple(ver, f"artifacts[{idx}].version")
            for contract_field in ("consumes", "produces"):
                if contract_field in entry and entry[contract_field] is not None:
                    vals = entry[contract_field]
                    if not isinstance(vals, list) or len(vals) != len(set(vals)):
                        raise EcosystemValidationError(
                            f"market index artifacts[{idx}].{contract_field} 须为去重字符串数组"
                        )
                    for item in vals:
                        if not isinstance(item, str) or not _CONTRACT_REF_RE.fullmatch(item):
                            raise EcosystemValidationError(
                                f"market index artifacts[{idx}].{contract_field} 契约引用非法: {item!r}"
                            )
            if (art_id, ver) in seen_plugin_vers:
                raise EcosystemValidationError(f"market index 存在重复插件条目: ({art_id!r}, {ver!r})")
            seen_plugin_vers.add((art_id, ver))

    return dict(raw_data)


def _fetch_market_index_doc(market_index_url: str, *, client: httpx.Client) -> dict[str, Any]:
    try:
        resp = client.get(market_index_url)
    except httpx.HTTPError as exc:
        raise http_error(
            502,
            "ECO-MARKET-FETCH-FAILED",
            "ies.diag.ecosystem.market_fetch_failed",
            detail=str(exc),
        ) from exc
    if resp.status_code >= 400:
        raise http_error(
            502,
            "ECO-MARKET-FETCH-FAILED",
            "ies.diag.ecosystem.market_fetch_failed",
            upstream_status=resp.status_code,
        )
    try:
        raw_doc = json.loads(resp.content.decode("utf-8"), parse_constant=_reject_non_finite_json)
    except EcosystemValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EcosystemValidationError(f"公共市场 index.json 解析失败: {exc}") from exc
    return _validate_market_index_payload(raw_doc)


def _is_update_available(entry: Mapping[str, Any], local: LocalArtifactRecord | None) -> bool:
    if local is None:
        return False
    if entry["kind"] == "device_model":
        if not local.version.isdigit():
            return False
        return int(entry["revision"]) > int(local.version)
    try:
        remote_t = _parse_semver_tuple(entry["version"], "remote_version")
        local_t = _parse_semver_tuple(local.version, "local_version")
    except EcosystemValidationError:
        return False
    return remote_t > local_t


def _resolve_market_item_url(index_url: str, relative_path: str) -> str:
    resolved = urljoin(index_url, relative_path)
    parsed = urlsplit(resolved)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise EcosystemValidationError(f"远端制品下载地址非法: {resolved!r}")
    return resolved


def get_market_index_case(
    db: Session,
    user: UserRecord,
    *,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """在线模式下拉取并校验远端公共市场 index.json，并叠加当前成员的三态视图。"""
    settings_rec = _require_online_settings(db)
    index_url = _require_market_index_url(settings_rec)

    if http_client is not None:
        index_doc = _fetch_market_index_doc(index_url, client=http_client)
    else:
        with build_http_client() as client:
            index_doc = _fetch_market_index_doc(index_url, client=client)

    local_artifacts = ecosystem_domain.list_artifacts(db)
    local_by_id: dict[tuple[str, str], LocalArtifactRecord] = {
        (rec.kind, rec.stable_id): rec for rec in local_artifacts
    }
    installs = {
        rec.artifact_id: rec for rec in ecosystem_domain.list_member_installs(db, user.id)
    }

    items: list[dict[str, Any]] = []
    for entry in index_doc["artifacts"]:
        kind = str(entry["kind"])
        art_id = str(entry["id"])
        local = local_by_id.get((kind, art_id))
        install = installs.get(local.id) if local is not None else None
        if local is not None and local.approval_status == "approved" and local.install_status == "ready":
            member_state = (
                "local_installed" if install is not None and install.enabled else "local_uninstalled"
            )
        else:
            member_state = "remote"
        readme_rel = entry.get("readme_url")
        items.append(
            {
                "id": art_id,
                "kind": kind,
                "version": str(entry["revision"]) if kind == "device_model" else str(entry["version"]),
                "revision": int(entry["revision"]) if kind == "device_model" else None,
                "name": dict(entry["name"]),
                "description": str(entry["description"]),
                "publisher": str(entry["publisher"]),
                "license": str(entry["license"]),
                "compatibility": dict(entry["compatibility"]),
                "consumes": list(entry.get("consumes") or ()),
                "produces": list(entry.get("produces") or ()),
                "sha256": str(entry["sha256"]),
                "size_bytes": int(entry["size_bytes"]),
                "readme_url": _resolve_market_item_url(index_url, str(readme_rel)) if readme_rel else None,
                "member_state": member_state,
                "local_artifact_id": local.id if local is not None else None,
                "local_version": local.version if local is not None else None,
                "update_available": _is_update_available(entry, local),
            }
        )

    return {
        "items": items,
        "generated_at": str(index_doc["generated_at"]),
        "market_index_url": index_url,
    }


def _select_market_entry(
    index_doc: Mapping[str, Any],
    *,
    artifact_id: str,
    kind: str,
    version_or_revision: str | int | None,
) -> Mapping[str, Any]:
    candidates = [
        entry
        for entry in index_doc["artifacts"]
        if entry["id"] == artifact_id and entry["kind"] == kind
    ]
    if not candidates:
        raise EcosystemArtifactNotFoundError(
            "公共市场索引中未找到指定制品",
            code="ECO-MARKET-ARTIFACT-NOT-FOUND",
            params={"artifact_id": artifact_id, "kind": kind},
        )
    if version_or_revision is not None:
        target = str(version_or_revision).strip()
        for entry in candidates:
            entry_ver = str(entry["revision"]) if kind == "device_model" else str(entry["version"])
            if entry_ver == target:
                return entry
        raise EcosystemArtifactNotFoundError(
            "公共市场索引中未找到指定版本或修订的制品",
            code="ECO-MARKET-ARTIFACT-NOT-FOUND",
            params={
                "artifact_id": artifact_id,
                "kind": kind,
                "version_or_revision": target,
            },
        )
    if kind == "device_model":
        return max(candidates, key=lambda e: int(e["revision"]))
    return max(candidates, key=lambda e: _parse_semver_tuple(e["version"], "market.version"))


def install_from_market_case(
    db: Session,
    user: UserRecord,
    *,
    artifact_id: str,
    kind: str,
    version_or_revision: str | int | None = None,
    scheduler: Any | None = None,
    job_runner_client: Any | None = None,
    session_factory: Any | None = None,
    plugin_source_root: Any | None = None,
    stage_root: Any | None = None,
    wait_for_build: bool = True,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """从在线公共市场下载制品、强制校验权威 SHA-256 并以 trusted_market=True 免审批入库安装。"""
    settings_rec = _require_online_settings(db)
    index_url = _require_market_index_url(settings_rec)
    if kind not in ARTIFACT_KINDS:
        raise EcosystemValidationError(f"不支持的制品 kind: {kind!r}", params={"kind": kind})
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise EcosystemValidationError("artifact_id 不能为空", params={"field": "artifact_id"})

    def _download_from_client(client: httpx.Client) -> tuple[Mapping[str, Any], bytes]:
        index_doc = _fetch_market_index_doc(index_url, client=client)
        entry = _select_market_entry(
            index_doc,
            artifact_id=artifact_id.strip(),
            kind=kind,
            version_or_revision=version_or_revision,
        )
        download_url = _resolve_market_item_url(index_url, str(entry["url"]))
        try:
            resp = client.get(download_url)
        except httpx.HTTPError as exc:
            raise http_error(
                502,
                "ECO-MARKET-FETCH-FAILED",
                "ies.diag.ecosystem.market_fetch_failed",
                detail=str(exc),
            ) from exc
        if resp.status_code >= 400:
            raise http_error(
                502,
                "ECO-MARKET-FETCH-FAILED",
                "ies.diag.ecosystem.market_fetch_failed",
                upstream_status=resp.status_code,
            )
        return entry, bytes(resp.content)

    if http_client is not None:
        entry, raw_bytes = _download_from_client(http_client)
    else:
        with build_http_client() as client:
            entry, raw_bytes = _download_from_client(client)

    expected_sha256 = str(entry["sha256"])
    verify_external_sha256(raw_bytes, expected_sha256, required=True)

    if kind == "device_model":
        artifact_payload = import_device_model_case(
            db,
            user,
            yaml_text=raw_bytes,
            expected_sha256=expected_sha256,
            revision=int(entry["revision"]),
            source_origin="online_market",
            trusted_market=True,
        )
    else:
        artifact_payload = upload_plugin_zip_case(
            db,
            user,
            zip_bytes=raw_bytes,
            expected_sha256=expected_sha256,
            source_origin="online_market",
            trusted_market=True,
            scheduler=scheduler,
            job_runner_client=job_runner_client,
            session_factory=session_factory,
            plugin_source_root=plugin_source_root,
            stage_root=stage_root,
            wait_for_build=wait_for_build,
        )
        if artifact_payload.get("install_status") == "ready" and not artifact_payload.get("enabled"):
            artifact_payload = install_artifact_for_user_case(
                db,
                user,
                int(artifact_payload["id"]),
                enabled=True,
            )

    return {
        "artifact": artifact_payload,
        "installed": bool(artifact_payload.get("installed", False)),
    }


def _gateway_error(resp: httpx.Response, *, code: str) -> AppError:
    detail = f"submission gateway returned HTTP {resp.status_code}"
    try:
        payload = resp.json()
        if isinstance(payload, Mapping) and isinstance(payload.get("detail"), str):
            detail = payload["detail"]
    except ValueError:
        pass
    status = resp.status_code if 400 <= resp.status_code < 500 else 502
    return http_error(
        status,
        code,
        "ies.diag.ecosystem.gateway_error",
        upstream_status=resp.status_code,
        detail=detail,
    )


def _gateway_url(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def request_market_otp_case(
    db: Session,
    user: UserRecord,
    *,
    email: str,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """无状态代理：向投稿网关请求邮箱 OTP 挑战（不存储、不审计、不记录任何明文邮箱）。"""
    del user
    settings_rec = _require_online_settings(db)
    gateway_url = _require_gateway_url(settings_rec)
    if not isinstance(email, str) or not email.strip():
        raise EcosystemValidationError("email 不能为空", params={"field": "email"})

    endpoint = _gateway_url(gateway_url, "/v1/otp/request")
    try:
        if http_client is not None:
            resp = http_client.post(endpoint, json={"email": email})
        else:
            with build_http_client() as client:
                resp = client.post(endpoint, json={"email": email})
    except httpx.HTTPError as exc:
        raise http_error(
            502,
            "ECO-MARKET-GATEWAY-ERROR",
            "ies.diag.ecosystem.gateway_error",
            detail=type(exc).__name__,
        ) from exc

    if resp.status_code >= 400:
        raise _gateway_error(resp, code="ECO-MARKET-OTP-REQUEST-FAILED")
    data = resp.json()
    return {
        "challenge_token": str(data["challenge_token"]),
        "expires_in": int(data["expires_in"]),
    }


def verify_market_otp_case(
    db: Session,
    user: UserRecord,
    *,
    email: str,
    otp_code: str,
    challenge_token: str,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """无状态代理：向投稿网关校验 OTP 并换取短期投稿令牌（零明文邮箱留存）。"""
    del user
    settings_rec = _require_online_settings(db)
    gateway_url = _require_gateway_url(settings_rec)
    if not isinstance(email, str) or not email.strip():
        raise EcosystemValidationError("email 不能为空", params={"field": "email"})
    if not isinstance(otp_code, str) or not otp_code.strip():
        raise EcosystemValidationError("otp_code 不能为空", params={"field": "otp_code"})
    if not isinstance(challenge_token, str) or not challenge_token.strip():
        raise EcosystemValidationError("challenge_token 不能为空", params={"field": "challenge_token"})

    endpoint = _gateway_url(gateway_url, "/v1/otp/verify")
    payload = {"email": email, "otp_code": otp_code, "challenge_token": challenge_token}
    try:
        if http_client is not None:
            resp = http_client.post(endpoint, json=payload)
        else:
            with build_http_client() as client:
                resp = client.post(endpoint, json=payload)
    except httpx.HTTPError as exc:
        raise http_error(
            502,
            "ECO-MARKET-GATEWAY-ERROR",
            "ies.diag.ecosystem.gateway_error",
            detail=type(exc).__name__,
        ) from exc

    if resp.status_code >= 400:
        raise _gateway_error(resp, code="ECO-MARKET-OTP-VERIFY-FAILED")
    data = resp.json()
    return {
        "submission_token": str(data["submission_token"]),
        "expires_in": int(data["expires_in"]),
    }


def submit_to_market_case(
    db: Session,
    user: UserRecord,
    *,
    submission_token: str,
    kind: str,
    file_bytes: bytes,
    filename: str,
    artifact_id: str | None = None,
    version_or_revision: str | None = None,
    publisher: str | None = None,
    license_name: str | None = None,
    description: str | None = None,
    readme_markdown: str | None = None,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """无状态代理：持 submission_token 向投稿网关转发制品包与元数据生成市场 PR。"""
    del user
    settings_rec = _require_online_settings(db)
    gateway_url = _require_gateway_url(settings_rec)
    if kind not in ARTIFACT_KINDS:
        raise EcosystemValidationError(f"不支持的制品 kind: {kind!r}", params={"kind": kind})
    if not isinstance(submission_token, str) or not submission_token.strip():
        raise EcosystemValidationError("submission_token 不能为空", params={"field": "submission_token"})
    if not file_bytes:
        raise EcosystemValidationError("投稿制品文件不能为空", params={"field": "file"})

    form_data: dict[str, str] = {"kind": kind}
    if kind == "device_model" and version_or_revision is not None and str(version_or_revision).strip():
        raw_rev = str(version_or_revision).strip()
        if raw_rev.startswith("r") and raw_rev[1:].isdigit():
            raw_rev = raw_rev[1:]
        if not raw_rev.isdigit() or int(raw_rev) < 1:
            raise EcosystemValidationError(
                f"device_model 投稿 revision 须为正整数: {version_or_revision!r}",
                params={"field": "version_or_revision"},
            )
        form_data["revision"] = str(int(raw_rev))
    if description is not None and description.strip():
        form_data["description"] = description.strip()
    if license_name is not None and license_name.strip():
        form_data["license"] = license_name.strip()
    if readme_markdown is not None and readme_markdown.strip():
        form_data["readme_markdown"] = readme_markdown.strip()
    if publisher is not None and publisher.strip() and artifact_id is not None and artifact_id.strip():
        if not artifact_id.strip().startswith(f"{publisher.strip()}."):
            raise EcosystemValidationError(
                "publisher 与 artifact_id 前缀不一致",
                params={"artifact_id": artifact_id, "publisher": publisher},
            )

    endpoint = _gateway_url(gateway_url, "/v1/submissions")
    headers = {"Authorization": f"Bearer {submission_token.strip()}"}
    files = {
        "file": (
            filename or ("artifact.device.yaml" if kind == "device_model" else "plugin.zip"),
            bytes(file_bytes),
            "application/octet-stream",
        )
    }
    try:
        if http_client is not None:
            resp = http_client.post(endpoint, headers=headers, data=form_data, files=files)
        else:
            with build_http_client() as client:
                resp = client.post(endpoint, headers=headers, data=form_data, files=files)
    except httpx.HTTPError as exc:
        raise http_error(
            502,
            "ECO-MARKET-GATEWAY-ERROR",
            "ies.diag.ecosystem.gateway_error",
            detail=type(exc).__name__,
        ) from exc

    if resp.status_code >= 400:
        raise _gateway_error(resp, code="ECO-MARKET-SUBMIT-FAILED")
    data = resp.json()
    return {
        "duplicate": bool(data.get("duplicate", False)),
        "pr_number": int(data["pr_number"]),
        "pr_url": str(data["pr_url"]),
        "branch": str(data["branch"]),
        "artifact_id": str(data.get("artifact_id") or artifact_id or ""),
        "kind": kind,
    }
