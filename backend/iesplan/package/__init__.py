"""项目包域公开门面（import_proposals 表读写 + 包传输编排，归属 package）。

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
from iesplan.package.transfers import (
    DOWNLOAD_TOKEN_TTL_SECONDS,
    EXCEL_MEDIA_TYPE,
    MAX_PACKAGE_BYTES,
    MAX_PACKAGE_ENTRIES,
    MAX_PACKAGE_ENTRY_BYTES,
    MAX_PACKAGE_TOTAL_BYTES,
    PACKAGE_FORMAT_VERSION,
    PACKAGE_MEDIA_TYPE,
    DownloadTokenError,
    ImportValidationError,
    PackageExport,
    PackageSizeError,
    confirm_import,
    create_download_token,
    export_excel,
    export_package,
    import_proposal,
    verify_download_token,
)

create_proposal = persistence.create_proposal
get_proposal = persistence.get_proposal
list_proposals = persistence.list_proposals
list_proposals_for_proposer = persistence.list_proposals_for_proposer
set_proposal_review = persistence.set_proposal_review

__all__ = [
    "DOWNLOAD_TOKEN_TTL_SECONDS",
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
    "PackageRepository",
    "PackageSizeError",
    "DownloadTokenError",
    "confirm_import",
    "create_download_token",
    "create_proposal",
    "export_excel",
    "export_package",
    "get_proposal",
    "import_proposal",
    "list_proposals",
    "list_proposals_for_proposer",
    "set_proposal_review",
    "verify_download_token",
]
