"""对象管理用例(application/objects): 管理端存储视图/清理/恢复/回收编排。

``iesplan.api.objects`` 的路由层编排整体下沉(Wave 4): 路由只做 HTTP 适配
(请求模型/依赖注入/响应信封), 本用例拥有存储编排与事务边界。

- 读用例(不提交事务): ``get_storage_view`` / ``preview_cleanup`` /
  ``list_pending`` / ``get_storage_health``(直接委托 storage 公开门面);
- 写用例(顶层拥有提交/回滚): ``execute_cleanup``(软删标记) /
  ``restore_object``(保留期恢复) / ``purge``(过期物理回收)。

本层不新增校验/hash/完整性复核/防御分支, 只做参数透传与事务收尾。

调用方向: ``api → application.objects.service → storage 公开门面``;
不导入 ORM、不导入领域内部模块。
"""

from __future__ import annotations

from iesplan.application.objects.service import (
    execute_cleanup,
    get_storage_health,
    get_storage_view,
    list_pending,
    preview_cleanup,
    purge,
    restore_object,
)

__all__ = [
    "execute_cleanup",
    "get_storage_health",
    "get_storage_view",
    "list_pending",
    "preview_cleanup",
    "purge",
    "restore_object",
]
