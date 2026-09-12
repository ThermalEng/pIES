"""审计域公开契约（audit_log，归属 audit）。

- audit_log 不可变：只追加，不提供修改/删除；
- 只含不可变值对象与领域错误；不导入 ORM、Session、services 或 application。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.core.errors import AppError


class AuditError(AppError):
    """审计域错误基类（沿用基类诊断码，不新增码）。"""


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """审计记录（audit_log 表公开视图）。"""

    id: int
    entity_type: str
    entity_id: int
    action: str
    actor_id: int | None = None
    actor_type: str = "user"
    occurred_at: str | None = None
    ip: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    request_id: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionRuleRecord:
    """保留规则（retention_rules 表公开视图；运维诊断消费，只读）。"""

    id: int
    entity_type: str
    object_kind: str
    retention_days: int
    apply_to: str
