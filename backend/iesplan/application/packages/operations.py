"""项目包用例族(application/packages): 包导出/导入。

编排策略(见 Wave 2 W2-C): 不复制 ``services.package`` 内部实现, 经其现有
函数组合 + 本层拥有事务提交/回滚(旧服务只读保留, 待 Wave 3 API 迁移后由
协调者删除):

- 导出: 权限 → 组装 zip → 对象登记 → 引用 + 审计 → 下载授权；
- 导入提案: 校验 → 暂存对象 → 拟创建项目快照 → 校验报告；
- 确认导入: 分区提交内容(数据集/草稿/版本/配置/证据来源) + 提案收尾 + 审计。

``services.package`` 内部已只经领域公开门面访问数据；本层仅增加事务边界，
不新增校验/hash/完整性复核/防御分支。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan.identity.contracts import UserRecord
from iesplan.package.contracts import ImportProposalRecord
from iesplan.project.contracts import ProjectRecord
from iesplan.services import package as package_service

#: 项目包字节上限(透传 services.package 常量；路由层流式读取封顶用，
#: 本层不新增校验)。
MAX_PACKAGE_BYTES: int = package_service.MAX_PACKAGE_BYTES

# ---------------------------------------------------------------------------
# 包导出(组合 services.package.export_package; 顶层拥有事务)
# ---------------------------------------------------------------------------


def export_package(db: Session, user: UserRecord, project_id: int):
    """导出完整项目包用例(仅所有者); 本层拥有事务提交/回滚。"""
    try:
        result = package_service.export_package(db, user, project_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 包导入(组合 services.package.import_proposal/confirm_import; 顶层拥有事务)
# ---------------------------------------------------------------------------


def propose_import(
    db: Session,
    user: UserRecord,
    file_bytes: bytes,
    idempotency_key: str | None = None,
) -> ImportProposalRecord:
    """创建导入提案用例(校验 → 暂存 → 拟创建项目快照); 本层拥有事务提交/回滚。"""
    try:
        result = package_service.import_proposal(
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
        result = package_service.confirm_import(db, user, proposal_id)
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
    ttl_seconds: int = package_service.DOWNLOAD_TOKEN_TTL_SECONDS,
) -> str:
    """签发短期单对象下载授权(纯签名, 无 DB 写, 不拥有事务)。"""
    return package_service.create_download_token(
        object_id, kind, project_id=project_id, user_id=user_id, ttl_seconds=ttl_seconds
    )


def verify_download_token(token: str, *, expected_kind: str | None = None) -> dict[str, Any]:
    """校验下载授权 token(纯校验, 无 DB 写, 不拥有事务)。"""
    return package_service.verify_download_token(token, expected_kind=expected_kind)
