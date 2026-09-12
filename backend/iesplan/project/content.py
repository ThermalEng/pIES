"""项目内容文档（草稿/版本内容对象，归属 project 域）。

本模块为纯函数：初始内容骨架、内容字典 ↔ 规范化字节、字节 → 内容字典
解析与缺失/损坏错误构造。对象存储 IO（put/get/attach）由调用方承担：
services 层内部直接经 storage 公开门面读写，application 调用方经
application/projects/content_objects.py 用例读写。本模块不导入 storage、
不持有会话、不做提交。
"""

from __future__ import annotations

import json

from iesplan.core.diagnostics import SEVERITY_ERROR, SYS_STORE_CORRUPT
from iesplan.core.errors import AppError
from iesplan.core.jsonutil import canonical_json


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


def content_to_bytes(content: dict) -> bytes:
    """内容字典 → 规范化 JSON 字节（与既有对象存储写入字节一致）。"""
    return canonical_json(content).encode("utf-8")


def corrupt_error(message: str, *, object_id: int | None = None) -> AppError:
    """内容对象缺失/损坏错误构造（错误码/诊断键不变）。

    缺失/读取失败携带对象定位；解析/结构错误不带定位（与既有行为一致）。
    """
    return AppError(
        message,
        code=SYS_STORE_CORRUPT,
        severity=SEVERITY_ERROR,
        message_key="ies.diag.store.corrupt",
        location=(
            {"object_type": "object", "object_id": object_id}
            if object_id is not None
            else None
        ),
    )


def parse_content_object(raw: bytes) -> dict:
    """规范化字节 → 内容字典（解析失败/结构非法一律按数据损坏明确报错）。"""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise corrupt_error("内容对象解析失败(数据损坏)") from exc
    if not isinstance(parsed, dict):
        raise corrupt_error("内容对象结构非法(数据损坏)")
    return parsed
