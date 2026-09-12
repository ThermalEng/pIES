"""模型用例族(application/models)。

W2-B 由 services/model.py 搬入系统模型写入全部能力(设备/端口/连接写入、
拓扑校验与草稿内容同步), 以及由 application/projects/model_save.py 搬入的
「保存项目模型」用例。用例顶层函数拥有事务提交, 数据读写只经
model/project/audit/storage 域公开门面。
"""

from iesplan.application.models.model_save import (
    FINAL_OWNER_NAMESPACE,
    ModelCandidateRejectedError,
    delete_project_model,
    get_project_models,
    project_model_to_dict,
    save_project_model,
    validate_candidate,
)
from iesplan.application.models.service import (
    CARRIER_PORT_TYPE,
    CONN_CROSS_PROJECT,
    CONN_DIRECTION_INVALID,
    CONN_DUPLICATE,
    CONN_ENERGY_MISMATCH,
    CONN_SELF_LOOP,
    CONN_TYPE_BY_PORT,
    ModelValidationError,
    connect,
    create_device,
    delete_device,
    disconnect,
    get_device_ports,
    get_graph,
    get_or_create_working_graph,
    serialize_connection,
    serialize_device,
    serialize_port,
    sync_draft_content,
    update_connection,
    update_device,
    validate_device_params,
    validate_project_model,
    validate_topology,
)
from iesplan.core.errors import NotFoundError

__all__ = [
    "CARRIER_PORT_TYPE",
    "CONN_CROSS_PROJECT",
    "CONN_DIRECTION_INVALID",
    "CONN_DUPLICATE",
    "CONN_ENERGY_MISMATCH",
    "CONN_SELF_LOOP",
    "CONN_TYPE_BY_PORT",
    "FINAL_OWNER_NAMESPACE",
    "ModelCandidateRejectedError",
    "ModelValidationError",
    "NotFoundError",
    "connect",
    "create_device",
    "delete_device",
    "delete_project_model",
    "disconnect",
    "get_device_ports",
    "get_graph",
    "get_or_create_working_graph",
    "get_project_models",
    "project_model_to_dict",
    "save_project_model",
    "serialize_connection",
    "serialize_device",
    "serialize_port",
    "sync_draft_content",
    "update_connection",
    "update_device",
    "validate_candidate",
    "validate_device_params",
    "validate_project_model",
    "validate_topology",
]
