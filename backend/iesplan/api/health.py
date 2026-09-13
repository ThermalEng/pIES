"""运维健康聚合 API(STO-07: 独立聚合层, 不驻留在存储路由)。

- GET /api/admin/health: 全系统运维健康视图(存活/就绪/任务/队列/存储),
  由本端点一次转交完整用例 ``application.health.health_view``(只读探针,
  再透传各域/服务/存储公开函数):
  - 存储 → 门面 storage_stats/sample_verify(容量 + 抽样校验);
  - 队列 → 门面 queue_status();
  - 任务/项目/用户计数 → 门面只读统计。
- 存储路由不再实现健康聚合(边界: 存储模块只提供自己的健康结果)。

生产探针(/api/healthz, /api/readyz)由 main._build_health_router 提供,
本端点面向管理员运维界面。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentAdmin
from iesplan.application import health as health_ops
from iesplan.db import get_db

#: 运维健康聚合路由: 挂载前缀 /api/admin(仅管理员)
router = APIRouter(prefix="/api/admin", tags=["admin-health"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("/health", summary="运维健康视图(管理员)")
def admin_health(db: DbSession, _admin: CurrentAdmin) -> dict:
    """运维健康(存活/就绪/指标/队列/存储)——独立聚合层, 非存储路由。

    本端点只做传输适配, 一次转交完整用例 ``health_view``。
    """
    return health_ops.health_view(db)
