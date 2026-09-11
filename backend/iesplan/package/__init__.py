"""项目包域公开门面（import_proposals 表读写，归属 package）。

外部只允许经本门面消费 contract 与 repository 协议；不得导入本域
repository 实现（切片 5 落实）、`iesplan.models` 或 services。
"""

from __future__ import annotations

from iesplan.package.contracts import (
    ImportProposalRecord,
    PackageConflictError,
    PackageNotFoundError,
)
from iesplan.package.repository import PackageRepository

__all__ = [
    "ImportProposalRecord",
    "PackageConflictError",
    "PackageNotFoundError",
    "PackageRepository",
]
