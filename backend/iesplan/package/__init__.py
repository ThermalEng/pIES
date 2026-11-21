"""项目包域公开门面（import_proposals 表读写 + 包格式纯函数，归属 package）。

跨域传输编排归属 ``application.packages.transfers``。外部只允许经本门面
消费 contract、repository 协议与 repository 实现函数；不得导入
`iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.package import persistence
from iesplan.package.contracts import (
    ImportProposalRecord,
    PackageConflictError,
    PackageNotFoundError,
)
from iesplan.package.transfers import (
    EXCEL_MEDIA_TYPE,
    MAX_PACKAGE_BYTES,
    MAX_PACKAGE_ENTRIES,
    MAX_PACKAGE_ENTRY_BYTES,
    MAX_PACKAGE_TOTAL_BYTES,
    PACKAGE_FORMAT_VERSION,
    PACKAGE_MEDIA_TYPE,
    ImportValidationError,
    PackageExport,
    PackageSizeError,
    bound_dataset_ids,
    media_file_kind,
    parse_evidence_content,
    parse_package,
)

claim_proposal_for_confirm = persistence.claim_proposal_for_confirm
create_proposal = persistence.create_proposal
get_proposal = persistence.get_proposal
list_proposals = persistence.list_proposals
list_proposals_for_proposer = persistence.list_proposals_for_proposer
set_proposal_review = persistence.set_proposal_review


__all__ = [
    "EXCEL_MEDIA_TYPE",
    "ImportProposalRecord",
    "ImportValidationError",
    "MAX_PACKAGE_BYTES",
    "MAX_PACKAGE_ENTRIES",
    "MAX_PACKAGE_ENTRY_BYTES",
    "MAX_PACKAGE_TOTAL_BYTES",
    "PACKAGE_FORMAT_VERSION",
    "PACKAGE_MEDIA_TYPE",
    "PackageConflictError",
    "PackageExport",
    "PackageNotFoundError",
    "PackageSizeError",
    "bound_dataset_ids",
    "claim_proposal_for_confirm",
    "media_file_kind",
    "create_proposal",
    "get_proposal",
    "list_proposals",
    "list_proposals_for_proposer",
    "parse_evidence_content",
    "parse_package",
    "set_proposal_review",
]
