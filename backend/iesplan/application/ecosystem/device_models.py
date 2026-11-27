"""本地生态设备模型导入与导出用例（ies.device-model@1.0.0）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import ecosystem as ecosystem_domain
from iesplan.application.ecosystem.common import (
    initial_approval_status,
    is_admin_user,
    serialize_artifact,
    validate_artifact_stable_id,
    verify_external_sha256,
)
from iesplan.core.yamlmini import dump as _yaml_dump
from iesplan.devices import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    canonical_bytes,
    parse_candidate_text,
    parse_device_model_v2,
    to_dict,
)
from iesplan.ecosystem import (
    SOURCE_ORIGINS,
    EcosystemArtifactNotFoundError,
    EcosystemConflictError,
    EcosystemValidationError,
)
from iesplan.identity.contracts import UserRecord
from iesplan.storage import attach, get_object, put_object

DEVICE_MODEL_MEDIA_TYPE = "application/json"


def _parse_revision(revision: int) -> int:
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise EcosystemValidationError(
            f"设备模型 revision 须为正整数: {revision!r}",
            params={"field": "revision", "value": revision},
        )
    return revision


def import_device_model_case(
    db: Session,
    user: UserRecord,
    *,
    yaml_text: str | bytes,
    expected_sha256: str | None = None,
    revision: int = 1,
    source_origin: str = "local_upload",
) -> dict[str, Any]:
    """导入/上传 ies.device-model@1.0.0：边界核对可选摘要、严格保留 ID 与 revision、409 防覆盖。"""
    rev = _parse_revision(revision)
    if source_origin not in SOURCE_ORIGINS:
        raise EcosystemValidationError(
            f"source_origin 非法: {source_origin!r}", params={"source_origin": source_origin}
        )
    raw_bytes = yaml_text.encode("utf-8") if isinstance(yaml_text, str) else bytes(yaml_text)
    verify_external_sha256(raw_bytes, expected_sha256)

    try:
        text_str = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EcosystemValidationError(f"设备模型 YAML 非合法 UTF-8 文本: {exc}") from exc

    raw_mapping, failure = parse_candidate_text(text_str)
    if failure is not None or raw_mapping is None:
        raise EcosystemValidationError(
            "设备模型 YAML 解析失败",
            params={"detail": getattr(failure, "detail", "YAML 解析失败")},
        )

    parsed = parse_device_model_v2(raw_mapping)
    if not parsed.ok or parsed.document is None:
        raise EcosystemValidationError(
            "设备模型不满足 ies.device-model@1.0.0 契约",
            params={"diagnostics": [d.to_dict() for d in parsed.diagnostics]},
        )

    doc = parsed.document
    stable_id = doc.device.id
    validate_artifact_stable_id(stable_id, expected_kind="device_model")
    canon_bytes = canonical_bytes(doc)
    version_str = str(rev)

    metadata = {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "revision": rev,
        "names": dict(doc.device.names),
        "interfaces": [
            {"name": name, "type": iface.type, "carrier": iface.carrier, "unit": iface.unit}
            for name, iface in doc.interfaces.items()
        ],
    }

    settings_rec = ecosystem_domain.get_settings(db)
    admin_flag = is_admin_user(db, user.id)
    approval = initial_approval_status(
        settings_rec, is_admin=admin_flag, source_origin=source_origin
    )
    install_status = "ready" if approval == "approved" else "pending"

    try:
        existing = ecosystem_domain.get_artifact_by_identity(
            db, "device_model", stable_id, version_str
        )
        if existing is not None:
            stored = get_object(db, existing.object_id)
            if stored != canon_bytes:
                raise EcosystemConflictError(
                    "同稳定标识与修订号的设备模型已存在且内容不一致，禁止静默覆盖",
                    params={"stable_id": stable_id, "revision": rev},
                )
            install = ecosystem_domain.get_member_install(db, user.id, existing.id)
            if existing.install_status == "ready" and install is None:
                install = ecosystem_domain.upsert_member_install(
                    db, user_id=user.id, artifact=existing, enabled=True
                )
            db.commit()
            payload = serialize_artifact(existing, install=install)
            payload["idempotent"] = True
            return payload

        stored_obj = put_object(
            db,
            canon_bytes,
            DEVICE_MODEL_MEDIA_TYPE,
            "ecosystem_device_model",
        )
        artifact = ecosystem_domain.create_artifact(
            db,
            kind="device_model",
            stable_id=stable_id,
            version=version_str,
            source_origin=source_origin,
            approval_status=approval,
            install_status=install_status,
            uploaded_by=user.id,
            approved_by=user.id if approval == "approved" else None,
            approved_at=datetime.now(UTC) if approval == "approved" else None,
            object_id=stored_obj.oid,
            metadata_json=metadata,
        )
        attach(
            db,
            stored_obj.id,
            "local_artifact",
            artifact.id,
            ref_entity_type="local_artifacts",
            purpose="生态设备模型规范文档",
        )
        install = None
        if install_status == "ready":
            install = ecosystem_domain.upsert_member_install(
                db, user_id=user.id, artifact=artifact, enabled=True
            )
        audit_domain.audit(
            db,
            user.id,
            "ecosystem.device_model_imported",
            "local_artifact",
            artifact.id,
            actor_type="admin" if admin_flag else "user",
            result={"stable_id": stable_id, "revision": rev, "approval_status": approval},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    payload = serialize_artifact(artifact, install=install)
    payload["idempotent"] = False
    return payload


def export_device_model_case(
    db: Session,
    user: UserRecord,
    *,
    artifact_id: int | None = None,
    stable_id: str | None = None,
    revision: int | None = None,
) -> dict[str, Any]:
    """导出纯技术 .device.yaml 正文及发布 revision 元数据（YAML 正文不内嵌版本）。"""
    del user
    record = None
    if artifact_id is not None:
        record = ecosystem_domain.get_artifact(db, artifact_id)
        if record is not None and record.kind != "device_model":
            record = None
    elif stable_id is not None:
        ver_str = str(_parse_revision(revision if revision is not None else 1))
        record = ecosystem_domain.get_artifact_by_identity(
            db, "device_model", stable_id, ver_str
        )
    if record is None:
        raise EcosystemArtifactNotFoundError(
            "本地生态设备模型不存在",
            params={"artifact_id": artifact_id, "stable_id": stable_id, "revision": revision},
        )
    canon = json.loads(get_object(db, record.object_id).decode("utf-8"))
    parsed = parse_device_model_v2(canon)
    if not parsed.ok or parsed.document is None:
        raise EcosystemValidationError("已存储设备模型反序列化失败")
    doc_dict = to_dict(parsed.document)
    yaml_text = _yaml_dump(doc_dict)
    return {
        "artifact_id": record.id,
        "kind": "device_model",
        "stable_id": record.stable_id,
        "revision": int(record.version),
        "filename": f"{record.stable_id}-r{record.version}.device.yaml",
        "yaml_text": yaml_text,
        "metadata": dict(record.metadata_json),
    }
