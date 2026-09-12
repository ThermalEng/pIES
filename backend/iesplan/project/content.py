"""项目内容文档（草稿/版本内容对象，归属 project 域）。

草稿与版本的内容正文存于对象存储（storage），本模块拥有其读写实现：
内容字典 ↔ 对象存储对象（规范化 JSON + owner 引用），以及初始内容骨架。
services.project 的同名公开函数为薄委托（Wave 3 前保留调用方兼容），
application 与其他调用方应直接经 project 域门面消费。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from iesplan.core.diagnostics import SEVERITY_ERROR, SYS_STORE_CORRUPT
from iesplan.core.errors import AppError, NotFoundError
from iesplan.core.jsonutil import canonical_json
from iesplan.storage import ObjectCorruptError, attach, get_object, put_object


def initial_content(language: str = "zh-CN") -> dict:
    """初始草稿内容骨架（空模型/布局/绑定/配置 + 空受控扩展清单）。"""
    return {
        "schema_version": 1,
        "language": language,
        "unit_system": "si",
        "extensions": {},
        "model": {"devices": [], "ports": [], "connections": []},
        "layout": {},
        "dataset_bindings": [],
        "calc_config": {
            "params": {},
            "variables": [],
            "objectives": [],
            "constraints": [],
            "algorithm": None,
            "solver": None,
            "tolerances": {},
            "random_seed": None,
        },
        "applied_commands": {},
    }


def store_content_object(db: Session, content: dict) -> int:
    """内容字典 → 对象存储对象并建立草稿内容引用，返回对象 id（每次写入新行）。

    规范化 JSON → 对象存储对象（架构宪法 §10/§12、domain-model §对象生命周期）；
    storage_path 的解释/分桶/临时文件全部由 iesplan.storage 内部实现，
    本模块不拼路径、不导入 StoredObject ORM。
    """
    raw = canonical_json(content)
    handle = put_object(
        db,
        raw.encode("utf-8"),
        "application/json",
        source_category="project_content",
    )
    # owner 引用（对象生命周期权威事实）：草稿/版本内容对象不可清理；
    # 引用键为稳定的对象 id（重复写入幂等复用同一引用行）。
    attach(
        db,
        handle.id,
        "draft_content",
        handle.id,
        ref_entity_type="drafts",
        purpose="草稿内容文档",
    )
    return handle.id


def load_content_bytes(db: Session, content_object_id: int) -> bytes:
    """按对象 id 读取内容字节（对象缺失/损坏抛 AppError，供字节级一致性比对）。"""
    try:
        return get_object(db, content_object_id)
    except NotFoundError as exc:
        raise AppError(
            "内容对象缺失(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": content_object_id},
        ) from exc
    except ObjectCorruptError as exc:
        raise AppError(
            "内容对象读取失败(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": content_object_id},
        ) from exc


def load_content_object(db: Session, content_object_id: int) -> dict:
    """按对象 id 读取内容对象（缺失/损坏/结构非法一律按数据损坏明确报错）。"""
    raw = load_content_bytes(db, content_object_id)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppError(
            "内容对象解析失败(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
        ) from exc
    if not isinstance(parsed, dict):
        raise AppError(
            "内容对象结构非法(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
        )
    return parsed
