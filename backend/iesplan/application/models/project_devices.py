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
from iesplan.core.errors import AppError
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


def load_project_device_data_bindings(
    db: Session, content: dict
) -> dict[str, list[dict]]:
    """项目模型回执中的模板数据绑定 → ``{device_id: [绑定]}``。

    模板数据输入在模型保存入口已解析为项目数据集版本并校验；装配构造器
    据此为使用该模型的设备实例补齐 ``predefined_interfaces`` 来源声明。
    回执缺失/损坏是真实的数据损坏，直接透传错误，不静默跳过。
    """
    bindings: dict[str, list[dict]] = {}
    for model in _referenced_models(db, content):
        raw = get_object(db, model.receipt_object_id)
        receipt = json.loads(raw.decode("utf-8"))
        if not isinstance(receipt, dict):
            raise AppError(
                "项目模型回执对象损坏",
                code="SYS-STORE-001",
                message_key="ies.diag.store.corrupt",
                location={"object_type": "project_model", "object_id": str(model.id)},
            )
        items = receipt.get("data_bindings")
        if isinstance(items, list) and items:
            bindings[model.device_id] = [dict(item) for item in items if isinstance(item, dict)]
    return bindings


__all__ = [
    "load_project_device_data_bindings",
    "load_project_device_documents",
    "project_device_type_ids",
]
