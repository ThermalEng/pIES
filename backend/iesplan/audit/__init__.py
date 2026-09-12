"""审计域公开门面（audit_log，归属 audit）。

外部只允许经本门面消费 contract、repository 协议与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.audit import persistence
from iesplan.audit.contracts import AuditError, AuditRecord
from iesplan.audit.repository import AuditRepository

append_entry = persistence.append_entry
list_entries = persistence.list_entries

__all__ = [
    "AuditError",
    "AuditRecord",
    "AuditRepository",
    "append_entry",
    "list_entries",
]
