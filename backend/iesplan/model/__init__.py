"""模型域公开门面（系统图/设备/端口/连接 + 模板/草稿/项目模型，归属 model）。

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
    ConnectionRecord,
    DeviceRecord,
    GraphRecord,
    ModelCandidateRejectedError,
    ModelConflictError,
    ModelNotFoundError,
    ModelTemplateRecord,
    ModelTemplateRevisionRecord,
    PortRecord,
    ProjectModelNotFoundError,
    ProjectModelRecord,
    TemplateDraftRevisionRecord,
)

allocate_project_model_suffix = persistence.allocate_project_model_suffix
create_connection = persistence.create_connection
create_device = persistence.create_device
create_draft_revision = persistence.create_draft_revision
create_graph = persistence.create_graph
create_ports = persistence.create_ports
create_project_model = persistence.create_project_model
create_published_revision = persistence.create_published_revision
create_template = persistence.create_template
delete_connection = persistence.delete_connection
delete_device_cascade = persistence.delete_device_cascade
delete_project_model = persistence.delete_project_model
delete_template = persistence.delete_template
find_connection = persistence.find_connection
find_device_by_name = persistence.find_device_by_name
find_graph_by_draft = persistence.find_graph_by_draft
find_latest_working_graph = persistence.find_latest_working_graph
find_project_model_by_idempotency = persistence.find_project_model_by_idempotency
find_revision_by_idempotency = persistence.find_revision_by_idempotency
find_working_graph = persistence.find_working_graph
get_connection = persistence.get_connection
get_device = persistence.get_device
get_draft_revision = persistence.get_draft_revision
get_graph = persistence.get_graph
get_owned_template = persistence.get_owned_template
get_port = persistence.get_port
get_project_model = persistence.get_project_model
get_published_revision = persistence.get_published_revision
list_connections = persistence.list_connections
list_devices = persistence.list_devices
list_draft_revisions = persistence.list_draft_revisions
list_owned_templates = persistence.list_owned_templates
list_owned_templates_by_status = persistence.list_owned_templates_by_status
list_ports = persistence.list_ports
list_ports_by_device = persistence.list_ports_by_device
list_project_models = persistence.list_project_models
resync_device_ports = persistence.resync_device_ports
update_connection = persistence.update_connection
update_device = persistence.update_device
update_project_model = persistence.update_project_model
update_template = persistence.update_template

#: 领域不可变表清单(唯一真相归 persistence 所有, 本门面只做引用重导出; 本域当前为空)。
IMMUTABLE_TABLES = persistence.IMMUTABLE_TABLES


def install_tables() -> None:
    """公开生命周期钩子: 导入本域 persistence 即完成 Base.metadata 表注册(幂等, 无其他副作用)。"""
    persistence.install_tables()


def install_triggers() -> tuple[str, ...]:
    """公开生命周期钩子: 返回本域触发器部署语句(按执行序, 供组合根编排收集)。"""
    return persistence.install_triggers()


__all__ = [
    "ConnectionRecord",
    "DeviceRecord",
    "GraphRecord",
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
    "PortRecord",
    "ProjectModelNotFoundError",
    "ProjectModelRecord",
    "TEMPLATE_STATUSES",
    "TEMPLATE_STATUS_DISABLED",
    "TEMPLATE_STATUS_DRAFT",
    "TEMPLATE_STATUS_PUBLISHED",
    "TemplateDraftRevisionRecord",
    "allocate_project_model_suffix",
    "create_connection",
    "create_device",
    "create_draft_revision",
    "create_graph",
    "create_ports",
    "create_project_model",
    "create_published_revision",
    "create_template",
    "delete_connection",
    "delete_device_cascade",
    "delete_project_model",
    "delete_template",
    "find_connection",
    "find_device_by_name",
    "find_graph_by_draft",
    "find_latest_working_graph",
    "find_project_model_by_idempotency",
    "find_revision_by_idempotency",
    "find_working_graph",
    "get_connection",
    "get_device",
    "get_draft_revision",
    "get_graph",
    "get_owned_template",
    "get_port",
    "get_project_model",
    "get_published_revision",
    "list_connections",
    "list_devices",
    "list_draft_revisions",
    "list_owned_templates",
    "list_owned_templates_by_status",
    "list_ports",
    "list_ports_by_device",
    "list_project_models",
    "resync_device_ports",
    "update_connection",
    "update_device",
    "update_project_model",
    "update_template",
    "IMMUTABLE_TABLES",
    "install_tables",
    "install_triggers",
]
