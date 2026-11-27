"""本地模型与算法生态应用用例门面（application/ecosystem）。

外部 API、Worker 及其他应用用例族只经本公开门面消费生态能力。
"""

from __future__ import annotations

from iesplan.application.ecosystem.device_models import (
    export_device_model_case,
    import_device_model_case,
)
from iesplan.application.ecosystem.integration import (
    augment_stage_registries_for_user,
    list_user_enabled_device_documents,
    load_pinned_local_device_documents,
    load_pinned_local_device_records,
    lock_and_load_submission_artifacts,
    lock_and_verify_submission_artifacts,
    resolve_local_evaluator_produces,
    resolve_local_stage_descriptor,
    resolve_user_local_device_revision,
    validate_and_normalize_local_device_pins,
)
from iesplan.application.ecosystem.plugins import (
    approve_artifact_case,
    install_artifact_for_user_case,
    list_catalog_artifacts_case,
    reject_artifact_case,
    set_member_artifact_enabled_case,
    uninstall_artifact_image_case,
    uninstall_member_artifact_case,
    upload_plugin_zip_case,
)
from iesplan.application.ecosystem.settings import (
    get_ecosystem_settings_case,
    update_ecosystem_settings_case,
)

__all__ = [
    "approve_artifact_case",
    "augment_stage_registries_for_user",
    "export_device_model_case",
    "get_ecosystem_settings_case",
    "import_device_model_case",
    "install_artifact_for_user_case",
    "list_catalog_artifacts_case",
    "list_user_enabled_device_documents",
    "load_pinned_local_device_documents",
    "load_pinned_local_device_records",
    "lock_and_load_submission_artifacts",
    "lock_and_verify_submission_artifacts",
    "reject_artifact_case",
    "resolve_local_evaluator_produces",
    "resolve_local_stage_descriptor",
    "resolve_user_local_device_revision",
    "set_member_artifact_enabled_case",
    "uninstall_artifact_image_case",
    "uninstall_member_artifact_case",
    "update_ecosystem_settings_case",
    "upload_plugin_zip_case",
    "validate_and_normalize_local_device_pins",
]
