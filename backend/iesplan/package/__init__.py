"""项目包域公开门面（import_proposals 表读写，归属 package）。

外部只允许经本门面消费 contract、repository 协议与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.package import persistence
from iesplan.package.contracts import (
    ImportProposalRecord,
    PackageConflictError,
    PackageNotFoundError,
)
from iesplan.package.repository import PackageRepository

create_proposal = persistence.create_proposal
get_proposal = persistence.get_proposal
list_proposals = persistence.list_proposals
list_proposals_for_proposer = persistence.list_proposals_for_proposer
set_proposal_review = persistence.set_proposal_review

__all__ = [
    "ImportProposalRecord",
    "PackageConflictError",
    "PackageNotFoundError",
    "PackageRepository",
    "create_proposal",
    "get_proposal",
    "list_proposals",
    "list_proposals_for_proposer",
    "set_proposal_review",
]
