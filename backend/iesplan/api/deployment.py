"""部署公开初值 API：统一部署配置中允许公开的非秘密默认值。

新建计算配置表单的初值从这里读取，不在前端另写一份常量；用户保存后
的 CalculationConfig revision 与任务快照仍是权威输入，部署配置变动
不得改写它们。本模块只读已装配的类型化配置，不解析文件、不校验配置。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from iesplan.config import settings

deployment_router = APIRouter(prefix="/api/deployment", tags=["deployment"])


@deployment_router.get("/defaults", summary="读取可公开的部署默认值")
def get_deployment_defaults() -> dict[str, Any]:
    """返回新建表单可用的非秘密初值（无需认证；只含公开值）。"""
    return {
        "calculation_defaults": {
            "time_limit_seconds": settings.calc_time_limit_seconds,
        },
    }
