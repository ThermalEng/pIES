"""项目模型应用门面（2.0 候选校验/原子保存 + 设备选择器）。

旧 normalized 系统图编排（设备/端口/连接 CRUD、拓扑校验、草稿镜像）已
随 S2c 删除；项目建模唯一事实为草稿内容 model 节（名键设备实例 + 真实
端点连接），经项目草稿命令写入、版本冻结、装配构造器消费。
"""

from __future__ import annotations

from iesplan.application.models import model_save, selector
from iesplan.application.models.model_save import (
    MODEL_MEDIA_TYPE,
    delete_project_model,
    get_project_models,
    save_project_model,
    save_project_model_in_transaction,
    validate_candidate,
)
from iesplan.application.models.project_devices import (
    load_project_device_base_ids,
    load_project_device_documents,
    project_device_type_ids,
)
from iesplan.application.models.selector import (
    list_device_types,
    list_project_device_types,
)

__all__ = [
    "MODEL_MEDIA_TYPE",
    "delete_project_model",
    "get_project_models",
    "list_device_types",
    "list_project_device_types",
    "load_project_device_base_ids",
    "load_project_device_documents",
    "model_save",
    "project_device_type_ids",
    "save_project_model",
    "save_project_model_in_transaction",
    "selector",
    "validate_candidate",
]
