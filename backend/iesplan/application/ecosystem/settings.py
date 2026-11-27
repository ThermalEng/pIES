"""实例级正交生态设置管理用例。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import ecosystem as ecosystem_domain
from iesplan.application.ecosystem.common import (
    require_admin,
    serialize_settings,
    validate_single_http_url,
)
from iesplan.ecosystem import MARKET_MODES, EcosystemValidationError
from iesplan.identity.contracts import UserRecord


def get_ecosystem_settings_case(db: Session, user: UserRecord | None = None) -> dict[str, Any]:
    """读取实例级正交生态设置；普通成员也可读取当前生效模式与开关。"""
    del user
    record = ecosystem_domain.get_settings(db)
    return serialize_settings(record)


def update_ecosystem_settings_case(
    db: Session,
    admin_user: UserRecord,
    *,
    market_mode: str,
    local_upload_requires_approval: bool,
    pip_index_url: str | None = None,
    market_index_url: str | None = None,
    submission_gateway_url: str | None = None,
) -> dict[str, Any]:
    """管理员更新正交生态开关与受信任单源 pip_index_url（写审计，提交事务）。"""
    require_admin(db, admin_user.id)
    if market_mode not in MARKET_MODES:
        raise EcosystemValidationError(
            f"market_mode 须为 {MARKET_MODES!r} 之一，得到 {market_mode!r}",
            params={"field": "market_mode", "value": market_mode},
        )
    if not isinstance(local_upload_requires_approval, bool):
        raise EcosystemValidationError(
            "local_upload_requires_approval 须为显式布尔值",
            params={"field": "local_upload_requires_approval"},
        )
    clean_pip_url = validate_single_http_url(
        pip_index_url, field_name="pip_index_url", reject_pip_flags=True
    )
    clean_market_url = validate_single_http_url(market_index_url, field_name="market_index_url")
    clean_gateway_url = validate_single_http_url(
        submission_gateway_url, field_name="submission_gateway_url"
    )
    try:
        updated = ecosystem_domain.upsert_settings(
            db,
            market_mode=market_mode,
            local_upload_requires_approval=local_upload_requires_approval,
            pip_index_url=clean_pip_url,
            market_index_url=clean_market_url,
            submission_gateway_url=clean_gateway_url,
            updated_by=admin_user.id,
        )
        audit_domain.audit(
            db,
            admin_user.id,
            "ecosystem.settings_updated",
            "ecosystem_settings",
            1,
            actor_type="admin",
            result=serialize_settings(updated),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return serialize_settings(updated)
