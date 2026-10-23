"""项目设备描述用例: 项目内容精确引用的单一解析来源。

项目模型在保存入口已完成一次完整校验并冻结为规范对象；本模块只按草稿/
版本内容声明的 ``project_models`` 引用（保存/删除模型时由项目域写入）
读取清单行与对象字节，经 ``devices.from_dict`` 还原为不可变文档，供选择器、
预检与装配闸门共用同一份项目设备描述。引用集与装配冻结内容同源：恢复历史
版本后，恢复点之后保存的模型不再进入边界。

不重复 schema/方程/值域校验，不访问其他项目，不做热加载或第二注册表。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from iesplan import model as model_domain
from iesplan.devices import DeviceModelDocument, from_dict
from iesplan.storage import get_object


def _referenced_models(db: Session, content: dict) -> list[model_domain.ProjectModelRecord]:
    """内容精确引用的项目模型清单行。

    引用存在而清单行缺失是真实的数据损坏（如删除模型后恢复旧版本），
    直接透传项目模型域错误，不静默跳过、不伪装成"无此模型"。
    """
    models: list[model_domain.ProjectModelRecord] = []
    for ref in content.get("project_models") or []:
        model = model_domain.get_project_model(db, int(ref["id"]))
        if model is None:
            raise model_domain.ProjectModelNotFoundError(
                "内容引用的项目模型不存在",
                params={"model_id": ref["id"]},
            )
        models.append(model)
    return models


def project_device_type_ids(db: Session, content: dict) -> set[str]:
    """内容精确引用的稳定设备类型 ID（仅供命令门禁判断引用归属）。"""
    return {model.device_id for model in _referenced_models(db, content)}


def load_project_device_documents(
    db: Session, content: dict
) -> dict[str, DeviceModelDocument]:
    """内容精确引用的项目模型 → ``{device_id: 不可变文档}``（纯反序列化）。"""
    documents: dict[str, DeviceModelDocument] = {}
    for model in _referenced_models(db, content):
        raw = get_object(db, model.model_object_id)
        documents[model.device_id] = from_dict(json.loads(raw.decode("utf-8")))
    return documents


def load_project_device_base_ids(db: Session, content: dict) -> dict[str, str]:
    """内容精确引用的项目模型 → ``{最终 device_id: 稳定基础 ID}``。

    设备数据 CSV 的 ``device_id`` 是稳定设备 ID（不含项目作用域 ``_N`` 编号）；
    装配绑定用它核对数据文件属于哪个设备模型，不要求用户猜编号。
    """
    return {
        model.device_id: model.base_device_id
        for model in _referenced_models(db, content)
    }


__all__ = [
    "load_project_device_base_ids",
    "load_project_device_documents",
    "project_device_type_ids",
]
