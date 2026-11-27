"""生态应用层共享校验与序列化辅助（非公开实现细节，由 __init__ 选择性重导出）。"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from iesplan import identity as identity_domain
from iesplan.core.errors import ForbiddenError
from iesplan.core.namespace import is_valid_namespace, is_valid_slug
from iesplan.ecosystem import (
    ARTIFACT_KINDS,
    SOURCE_ORIGINS,
    EcosystemSettingsRecord,
    EcosystemValidationError,
    LocalArtifactRecord,
    MemberArtifactInstallRecord,
)

LOCAL_SOURCE_ORIGINS: tuple[str, ...] = ("local_upload", "offline_import")

_KIND_TO_TYPE_SEGMENT: dict[str, str] = {
    "device_model": "device",
    "predictor": "predictor",
    "generator": "generator",
    "solver": "solver",
    "evaluator": "evaluator",
    "algorithm": "algorithm",
}
_RESERVED_PUBLISHERS: frozenset[str] = frozenset({"ies", "user", "admin", "system", "official"})
_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def is_admin_user(db: Session, user_id: int) -> bool:
    """检查用户是否拥有全局 admin 角色。"""
    return "admin" in identity_domain.user_roles(db, user_id)


def require_admin(db: Session, user_id: int) -> None:
    """要求全局 admin 角色；非管理员统一抛 403 ForbiddenError。"""
    if not is_admin_user(db, user_id):
        raise ForbiddenError("仅系统管理员可执行该生态管理操作")


def validate_single_http_url(
    raw: str | None,
    *,
    field_name: str,
    reject_pip_flags: bool = False,
) -> str | None:
    """校验单个 HTTP(S) URL；空串归一化为 None，禁止空白/控制字符及 pip 多源注入。"""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise EcosystemValidationError(f"{field_name} 须为字符串或 null", params={"field": field_name})
    if raw == "" or raw.strip() == "":
        return None
    if raw != raw.strip() or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise EcosystemValidationError(
            f"{field_name} 不允许包含空白或控制字符",
            params={"field": field_name},
        )
    if reject_pip_flags and ("--" in raw or "--extra-index-url" in raw):
        raise EcosystemValidationError(
            f"{field_name} 仅允许单个受信任 index URL，禁止附加 pip 参数或 --extra-index-url",
            params={"field": field_name},
        )
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise EcosystemValidationError(
            f"{field_name} 须为合法 http:// 或 https:// URL",
            params={"field": field_name},
        )
    return raw


def verify_external_sha256(raw_bytes: bytes, expected_sha256: str | None) -> None:
    """外部入口三分核对：仅在调用方显式提供期望 sha256 时核对一次，入库后永不保存/重算。"""
    if expected_sha256 is None:
        return
    if not isinstance(expected_sha256, str) or not expected_sha256.strip():
        return
    candidate = expected_sha256.strip().lower()
    if not _SHA256_RE.fullmatch(candidate):
        raise EcosystemValidationError(
            "提供的 expected_sha256 不是合法 64 位十六进制摘要",
            code="ECO-SHA256-INVALID",
            params={"expected_sha256": expected_sha256},
        )
    actual = hashlib.sha256(raw_bytes).hexdigest().lower()
    if actual != candidate:
        raise EcosystemValidationError(
            "外部制品字节与提供的 expected_sha256 不一致",
            code="ECO-SHA256-MISMATCH",
            params={"expected": candidate, "actual": actual},
        )


def validate_artifact_stable_id(stable_id: str, *, expected_kind: str) -> None:
    """校验稳定标识遵循 ies.<type>.<slug> / <publisher>.<type>.<slug> / user.<ns>.<type>.<slug>。"""
    if expected_kind not in ARTIFACT_KINDS:
        raise EcosystemValidationError(
            f"不支持的制品类型: {expected_kind!r}", params={"kind": expected_kind}
        )
    if not isinstance(stable_id, str) or not stable_id:
        raise EcosystemValidationError("制品稳定 ID 不能为空")
    expected_type = _KIND_TO_TYPE_SEGMENT[expected_kind]
    parts = stable_id.split(".")
    if len(parts) == 4 and parts[0] == "user":
        _, pub_ns, art_type, slug = parts
        if not is_valid_namespace(pub_ns):
            raise EcosystemValidationError(
                f"个人命名空间须为 12 位小写 Crockford Base32: {pub_ns!r}",
                params={"stable_id": stable_id},
            )
    elif len(parts) == 3:
        prefix, art_type, slug = parts
        if not _SEGMENT_RE.fullmatch(prefix):
            raise EcosystemValidationError(
                f"发布者前缀非法: {prefix!r}", params={"stable_id": stable_id}
            )
        if prefix != "ies" and prefix in _RESERVED_PUBLISHERS:
            raise EcosystemValidationError(
                f"发布者前缀保留不可用: {prefix!r}", params={"stable_id": stable_id}
            )
    else:
        raise EcosystemValidationError(
            f"稳定 ID 格式非法: {stable_id!r}"
            "（须为 ies.<type>.<slug>、<publisher>.<type>.<slug> 或 user.<ns>.<type>.<slug>）",
            params={"stable_id": stable_id},
        )
    if art_type != expected_type:
        raise EcosystemValidationError(
            f"稳定 ID 类型段 {art_type!r} 与制品 kind={expected_kind!r} 不匹配（期望 {expected_type!r}）",
            params={"stable_id": stable_id, "kind": expected_kind},
        )
    if not is_valid_slug(slug):
        raise EcosystemValidationError(
            f"制品 slug 格式非法: {slug!r}", params={"stable_id": stable_id}
        )


def validate_ingest_source_origin(
    source_origin: str,
    *,
    trusted_market: bool = False,
) -> str:
    """校验导入/上传来源；仅受信任的市场下载内部用例可使用 online_market，禁止用户自报绕过审批。"""
    if source_origin not in SOURCE_ORIGINS:
        raise EcosystemValidationError(
            f"source_origin 非法: {source_origin!r}",
            params={"source_origin": source_origin},
        )
    if source_origin == "online_market" and not trusted_market:
        raise EcosystemValidationError(
            "本地上传或离线导入入口不允许声明 source_origin='online_market'",
            code="ECO-ORIGIN-FORBIDDEN",
            params={"source_origin": source_origin},
        )
    if not trusted_market and source_origin not in LOCAL_SOURCE_ORIGINS:
        raise EcosystemValidationError(
            f"source_origin 非法: {source_origin!r}",
            params={"source_origin": source_origin},
        )
    return source_origin


def initial_approval_status(
    settings_rec: EcosystemSettingsRecord,
    *,
    is_admin: bool,
    source_origin: str,
    trusted_market: bool = False,
) -> str:
    """正交审批判定：仅受信任的市场下载可免审批；本地/离线非管理员按开关强制 pending。"""
    if trusted_market and source_origin == "online_market":
        return "approved"
    if is_admin:
        return "approved"
    if settings_rec.local_upload_requires_approval:
        return "pending"
    return "approved"


def serialize_settings(record: EcosystemSettingsRecord) -> dict[str, Any]:
    return {
        "market_mode": record.market_mode,
        "local_upload_requires_approval": record.local_upload_requires_approval,
        "pip_index_url": record.pip_index_url,
        "market_index_url": record.market_index_url,
        "submission_gateway_url": record.submission_gateway_url,
        "updated_by": record.updated_by,
        "updated_at": record.updated_at,
    }


def serialize_artifact(
    record: LocalArtifactRecord,
    *,
    install: MemberArtifactInstallRecord | None = None,
) -> dict[str, Any]:
    return {
        "id": record.id,
        "kind": record.kind,
        "stable_id": record.stable_id,
        "version": record.version,
        "ref": record.ref,
        "source_origin": record.source_origin,
        "approval_status": record.approval_status,
        "install_status": record.install_status,
        "uploaded_by": record.uploaded_by,
        "approved_by": record.approved_by,
        "approved_at": record.approved_at,
        "object_id": record.object_id,
        "metadata": dict(record.metadata_json),
        "image_ref": record.image_ref,
        "diagnostics": record.diagnostics,
        "installed": install is not None,
        "enabled": install.enabled if install is not None else False,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
