"""项目包用例族(application/packages): 包导出/导入。

编排策略(纠偏 Wave 1 切片 D 收敛后): 经 ``iesplan.package`` 领域公开门面
组合 + 本层拥有事务提交/回滚(旧服务 ``services.package`` 已删除):

- 导出: 权限 → 组装 zip → 对象登记 → 引用 + 审计 → 下载授权；
- 导入提案: 校验 → 暂存对象 → 拟创建项目快照 → 校验报告；
- 确认导入: 分区提交内容(数据集/草稿/版本/配置/证据来源) + 提案收尾 + 审计。

``iesplan.package`` 传输编排内部只经领域公开门面访问数据；本层仅增加事务
边界，不新增校验/hash/完整性复核/防御分支。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan import package as package_domain
from iesplan.application.packages.transfers import confirm_import as _confirm_import
from iesplan.application.packages.transfers import export_package as _export_package
from iesplan.application.packages.transfers import import_proposal as _import_proposal
from iesplan.identity.contracts import UserRecord
from iesplan.package.contracts import ImportProposalRecord
from iesplan.project.contracts import ProjectRecord

#: 项目包字节上限(取自 package 域常量；路由层流式读取封顶用，本层不新增校验)。
MAX_PACKAGE_BYTES: int = package_domain.MAX_PACKAGE_BYTES

# ---------------------------------------------------------------------------
# 包导出(组合 package 域 export_package; 顶层拥有事务)
# ---------------------------------------------------------------------------


def export_package(db: Session, user: UserRecord, project_id: int):
    """导出完整项目包用例(仅所有者); 本层拥有事务提交/回滚。"""
    try:
        result = _export_package(db, user, project_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 包导入(组合 package 域 import_proposal/confirm_import; 顶层拥有事务)
# ---------------------------------------------------------------------------


def propose_import(
    db: Session,
    user: UserRecord,
    file_bytes: bytes,
    idempotency_key: str | None = None,
) -> ImportProposalRecord:
    """创建导入提案用例(校验 → 暂存 → 拟创建项目快照); 本层拥有事务提交/回滚。"""
    try:
        result = _import_proposal(
            db, user, file_bytes, idempotency_key=idempotency_key
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def confirm_import(db: Session, user: UserRecord, proposal_id: int) -> ProjectRecord:
    """确认导入用例(分区提交 + 提案收尾 + 审计); 本层拥有事务提交/回滚。"""
    try:
        result = _confirm_import(db, user, proposal_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def create_download_token(
    object_id: int,
    kind: str,
    *,
    project_id: int,
    user_id: int,
    ttl_seconds: int = package_domain.DOWNLOAD_TOKEN_TTL_SECONDS,
) -> str:
    """签发短期单对象下载授权(纯签名, 无 DB 写, 不拥有事务)。"""
    return package_domain.create_download_token(
        object_id, kind, project_id=project_id, user_id=user_id, ttl_seconds=ttl_seconds
    )


def verify_download_token(token: str, *, expected_kind: str | None = None) -> dict[str, Any]:
    """校验下载授权 token(纯校验, 无 DB 写, 不拥有事务)。"""
    return package_domain.verify_download_token(token, expected_kind=expected_kind)
