"""1.2.0 本地生态目录表迁移（ecosystem_settings / local_artifacts / member_artifact_installs）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

MIGRATION_VERSION = "20261127_120_ecosystem"


def upgrade_ecosystem(conn: Connection) -> int:
    """幂等创建生态设置与本地制品/成员安装表，并播种单例默认设置行。"""
    is_pg = conn.dialect.name == "postgresql"
    pk_col = (
        "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"
        if is_pg
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    ts_col = "TIMESTAMPTZ" if is_pg else "TIMESTAMP"
    json_col = "JSONB" if is_pg else "JSON"
    now_iso = datetime.now(UTC).isoformat()

    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS ecosystem_settings (
                id INTEGER PRIMARY KEY,
                market_mode TEXT NOT NULL DEFAULT 'local'
                    CHECK (market_mode IN ('online', 'local')),
                local_upload_requires_approval BOOLEAN NOT NULL DEFAULT TRUE,
                pip_index_url TEXT,
                market_index_url TEXT,
                submission_gateway_url TEXT,
                updated_by BIGINT REFERENCES users(id),
                updated_at {ts_col} NOT NULL
            )
            """
        )
    )
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS local_artifacts (
                id {pk_col},
                kind TEXT NOT NULL
                    CHECK (kind IN (
                        'device_model', 'predictor', 'generator', 'solver', 'evaluator', 'algorithm'
                    )),
                stable_id TEXT NOT NULL,
                version TEXT NOT NULL,
                source_origin TEXT NOT NULL
                    CHECK (source_origin IN ('local_upload', 'offline_import', 'online_market')),
                approval_status TEXT NOT NULL
                    CHECK (approval_status IN ('pending', 'approved', 'rejected')),
                install_status TEXT NOT NULL
                    CHECK (install_status IN (
                        'none', 'pending', 'building', 'testing', 'ready', 'build_failed', 'disabled'
                    )),
                uploaded_by BIGINT NOT NULL REFERENCES users(id),
                approved_by BIGINT REFERENCES users(id),
                approved_at {ts_col},
                object_id TEXT NOT NULL,
                metadata_json {json_col} NOT NULL,
                image_ref TEXT,
                diagnostics {json_col},
                created_at {ts_col} NOT NULL,
                updated_at {ts_col} NOT NULL,
                CONSTRAINT uq_local_artifacts_identity UNIQUE (kind, stable_id, version)
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_local_artifacts_lookup "
            "ON local_artifacts (kind, approval_status, install_status)"
        )
    )
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS member_artifact_installs (
                id {pk_col},
                user_id BIGINT NOT NULL REFERENCES users(id),
                artifact_id BIGINT NOT NULL REFERENCES local_artifacts(id) ON DELETE CASCADE,
                kind TEXT NOT NULL
                    CHECK (kind IN (
                        'device_model', 'predictor', 'generator', 'solver', 'evaluator', 'algorithm'
                    )),
                stable_id TEXT NOT NULL,
                version TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                installed_at {ts_col} NOT NULL,
                updated_at {ts_col} NOT NULL,
                CONSTRAINT uq_member_artifact_install UNIQUE (user_id, artifact_id)
            )
            """
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_member_artifact_installs_lookup "
            "ON member_artifact_installs (user_id, kind, enabled)"
        )
    )
    conn.execute(
        text(
            "INSERT INTO ecosystem_settings "
            "(id, market_mode, local_upload_requires_approval, pip_index_url, "
            "market_index_url, submission_gateway_url, updated_by, updated_at) "
            "VALUES (1, 'local', TRUE, NULL, NULL, NULL, NULL, :updated_at) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"updated_at": now_iso},
    )
    return 1


__all__ = ["MIGRATION_VERSION", "upgrade_ecosystem"]
