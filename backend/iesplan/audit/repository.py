"""审计域 repository 协议（audit_log）。

实现规则：
- 只追加写入与过滤查询；不提供修改/删除；
- 查询、写入、flush 由 repository 完成；绝不 commit/rollback。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from iesplan.audit.contracts import AuditRecord


class AuditRepository(Protocol):
    """审计 repository 协议（无状态方法组，db 由调用方事务拥有）。"""

    def append_entry(
        self,
        db: Session,
        *,
        actor_id: int | None,
        action: str,
        entity_type: str,
        entity_id: int,
        revision: int | None = None,
        result: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        actor_type: str = "user",
        ip: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> AuditRecord:
        """追加不可变审计行；after 由 revision/result/extra 组装。"""
        ...

    def list_entries(
        self,
        db: Session,
        *,
        entity_type: str | None = None,
        entity_id: int | None = None,
        action: str | None = None,
        actor_id: int | None = None,
        actor_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: int | None = None,
        limit: int = 50,
    ) -> list[AuditRecord]:
        """过滤 + 游标分页（id 倒序）；调用方传 limit+1 探测下一页。"""
        ...
