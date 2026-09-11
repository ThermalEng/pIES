"""项目包域公开契约(import_proposals 表读写，归属 package)。

- 导出侧只组合其他域的公开 record 与对象 id，不拥有新表；
- 导入提议是外部数据入库前的评审记录，状态机由本域 repository 维护；
- 审计日志（audit_log）的写入归 audit facade/application（切片 11），不在此；
- 只含不可变值对象与领域错误；不导入 ORM、Session、services 或 application。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.core.errors import ConflictError, NotFoundError


class PackageNotFoundError(NotFoundError):
    """导入提议不存在（沿用基类诊断码，不新增码）。"""


class PackageConflictError(ConflictError):
    """导入提议状态冲突（沿用基类诊断码，不新增码）。"""


@dataclass(frozen=True, slots=True)
class ImportProposalRecord:
    """导入提议（import_proposals 表公开视图）。"""

    id: int
    project_id: int
    proposer_id: int
    source_type: str
    status: str
    source_object_id: int | None = None
    source_path: str | None = None
    review_summary: dict[str, Any] | None = None
    review_errors: dict[str, Any] | None = None
    decided_by: int | None = None
    decided_at: str | None = None
