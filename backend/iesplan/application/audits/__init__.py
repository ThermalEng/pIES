"""审计用例族(application/audits): 审计查询与管理端维护审计。

审计查询与管理员维护审计的薄封装(组合 ``iesplan.audit`` 域公开门面；
纠偏 Wave 1 切片 C，旧 services.audit 已删除)：

- ``query_audit``：过滤 + 游标分页(只读，不拥有事务)；
- ``record_unlock_audit``：管理员解锁任务审计(只 INSERT/flush，提交由
  调用方事务边界控制——解锁端点的 ORM 写尚无领域归属，整体事务仍在
  路由层，见 ``iesplan.api.admin``)。

本层不新增校验/hash/完整性复核/防御分支。

调用方向：``api → application.audits.service → iesplan.audit(域门面)``；
不导入 ORM、不导入 services、不导入领域内部模块。
"""

from __future__ import annotations

from iesplan.application.audits.service import query_audit, record_unlock_audit

__all__ = [
    "query_audit",
    "record_unlock_audit",
]
