"""本地模型与算法生态公开门面（ecosystem_settings / local_artifacts / member_artifact_installs）。

外部模块只经本门面消费不可变契约与持久化操作，不得直接导入 ``persistence`` 或 ORM。
"""

from __future__ import annotations

from iesplan.ecosystem import persistence
from iesplan.ecosystem.contracts import (
    APPROVAL_STATUSES,
    ARTIFACT_KINDS,
    INSTALL_STATUSES,
    MARKET_MODES,
    PLUGIN_KINDS,
    SOURCE_ORIGINS,
    EcosystemArtifactNotFoundError,
    EcosystemConflictError,
    EcosystemSettingsRecord,
    EcosystemUninstallInUseError,
    EcosystemValidationError,
    LocalArtifactRecord,
    MemberArtifactInstallRecord,
)

IMMUTABLE_TABLES = persistence.IMMUTABLE_TABLES

create_artifact = persistence.create_artifact
delete_member_install = persistence.delete_member_install
get_artifact = persistence.get_artifact
get_artifact_by_identity = persistence.get_artifact_by_identity
get_member_install = persistence.get_member_install
get_settings = persistence.get_settings
install_triggers = persistence.install_triggers
list_artifacts = persistence.list_artifacts
list_enabled_artifacts_for_user = persistence.list_enabled_artifacts_for_user
list_member_installs = persistence.list_member_installs
lock_artifacts_for_submission = persistence.lock_artifacts_for_submission
set_member_install_enabled = persistence.set_member_install_enabled
update_artifact = persistence.update_artifact
upsert_member_install = persistence.upsert_member_install
upsert_settings = persistence.upsert_settings

__all__ = [
    "APPROVAL_STATUSES",
    "ARTIFACT_KINDS",
    "EcosystemArtifactNotFoundError",
    "EcosystemConflictError",
    "EcosystemSettingsRecord",
    "EcosystemUninstallInUseError",
    "EcosystemValidationError",
    "IMMUTABLE_TABLES",
    "INSTALL_STATUSES",
    "LocalArtifactRecord",
    "MARKET_MODES",
    "MemberArtifactInstallRecord",
    "PLUGIN_KINDS",
    "SOURCE_ORIGINS",
    "create_artifact",
    "delete_member_install",
    "get_artifact",
    "get_artifact_by_identity",
    "get_member_install",
    "get_settings",
    "install_triggers",
    "list_artifacts",
    "list_enabled_artifacts_for_user",
    "list_member_installs",
    "lock_artifacts_for_submission",
    "set_member_install_enabled",
    "update_artifact",
    "upsert_member_install",
    "upsert_settings",
]
