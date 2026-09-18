"""模型域公开门面（模板/草稿/项目模型，归属 model）。

外部只允许经本门面消费 contract、repository 协议与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.model import persistence
from iesplan.model.contracts import (
    MODEL_SOURCE_DIRECT,
    MODEL_SOURCE_TEMPLATE,
    MODEL_SOURCES,
    PROJ_MDL_IDENTITY_FAILED,
    PROJ_MDL_VALIDATION_FAILED,
    PROJ_MDL_YAML_PARSE,
    TEMPLATE_STATUS_DISABLED,
    TEMPLATE_STATUS_DRAFT,
    TEMPLATE_STATUS_PUBLISHED,
    TEMPLATE_STATUSES,
    ModelCandidateRejectedError,
    ModelConflictError,
    ModelNotFoundError,
    ModelTemplateRecord,
    ModelTemplateRevisionRecord,
    ProjectModelNotFoundError,
    ProjectModelRecord,
    TemplateDraftRevisionRecord,
)

allocate_project_model_suffix = persistence.allocate_project_model_suffix
create_draft_revision = persistence.create_draft_revision
create_project_model = persistence.create_project_model
create_published_revision = persistence.create_published_revision
create_template = persistence.create_template
delete_project_model = persistence.delete_project_model
delete_template = persistence.delete_template
find_project_model_by_idempotency = persistence.find_project_model_by_idempotency
find_revision_by_idempotency = persistence.find_revision_by_idempotency
get_draft_revision = persistence.get_draft_revision
get_owned_template = persistence.get_owned_template
get_project_model = persistence.get_project_model
get_published_revision = persistence.get_published_revision
list_draft_revisions = persistence.list_draft_revisions
list_owned_templates = persistence.list_owned_templates
list_owned_templates_by_status = persistence.list_owned_templates_by_status
list_project_models = persistence.list_project_models
update_project_model = persistence.update_project_model
update_template = persistence.update_template


__all__ = [
    "MODEL_SOURCES",
    "MODEL_SOURCE_DIRECT",
    "MODEL_SOURCE_TEMPLATE",
    "ModelCandidateRejectedError",
    "ModelConflictError",
    "ModelNotFoundError",
    "ModelTemplateRecord",
    "ModelTemplateRevisionRecord",
    "PROJ_MDL_IDENTITY_FAILED",
    "PROJ_MDL_VALIDATION_FAILED",
    "PROJ_MDL_YAML_PARSE",
    "ProjectModelNotFoundError",
    "ProjectModelRecord",
    "TEMPLATE_STATUSES",
    "TEMPLATE_STATUS_DISABLED",
    "TEMPLATE_STATUS_DRAFT",
    "TEMPLATE_STATUS_PUBLISHED",
    "TemplateDraftRevisionRecord",
    "allocate_project_model_suffix",
    "create_draft_revision",
    "create_project_model",
    "create_published_revision",
    "create_template",
    "delete_project_model",
    "delete_template",
    "find_project_model_by_idempotency",
    "find_revision_by_idempotency",
    "get_draft_revision",
    "get_owned_template",
    "get_project_model",
    "get_published_revision",
    "list_draft_revisions",
    "list_owned_templates",
    "list_owned_templates_by_status",
    "list_project_models",
    "update_project_model",
    "update_template",
]
