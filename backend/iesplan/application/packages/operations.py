"""项目包用例族(application/packages): 包导出/导入。

编排策略(纠偏 Wave 1 切片 D 收敛后): 经 ``iesplan.package`` 领域公开门面
组合 + 本层拥有事务提交/回滚(旧服务 ``services.package`` 已删除):

- 导出: 权限 → 组装 zip → 对象登记 → 引用 + 审计 → 下载授权；
- 导入提案: 校验 → 暂存对象 → 拟创建项目快照 → 校验报告；
- 确认导入: 分区提交内容(数据集/项目模型/草稿/版本/配置/证据来源) + 提案收尾 + 审计。

``iesplan.package`` 传输编排内部只经领域公开门面访问数据；本层仅增加事务
边界，不新增校验/hash/完整性复核/防御分支。
"""

from __future__ import annotations


from sqlalchemy.orm import Session

from iesplan import package as package_domain
from iesplan import project as project_domain
from iesplan.application.packages.transfers import (
    confirm_import as _confirm_import,
    export_package as _export_package,
    import_proposal as _import_proposal,
)
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


# ---------------------------------------------------------------------------
# HTTP 用例封装导入流程，API 负责流式封顶读取、DTO 和响应映射。
# ---------------------------------------------------------------------------


def propose_import_case(
    db: Session,
    user: UserRecord,
    *,
    file_bytes: bytes,
    idempotency_key: str | None = None,
) -> ImportProposalRecord:
    """校验上传包并暂存导入提案，确认时创建项目。"""
    return propose_import(db, user, file_bytes, idempotency_key=idempotency_key)


def confirm_import_case(db: Session, user: UserRecord, *, proposal_id: int) -> dict:
    """确认导入完整用例: 分区提交 → 返回新项目与导入者角色(与 HTTP 无关, 供 API 组装响应)。"""
    project = confirm_import(db, user, proposal_id)
    return {"project": project, "role": project_domain.get_role(db, user, project.id)}


def confirm_import_case_result(db: Session, user: UserRecord, *, proposal_id: int) -> dict:
    """确认导入响应就绪用例(路由原顺序: 确认导入 → 序列化; 提交由确认用例拥有)。

    ``confirm_import_case`` 的记录契约保持不变; 本用例在其上追加项目序列化,
    供确认导入端点一次转交。返回响应就绪字典。
    """
    result = confirm_import_case(db, user, proposal_id=proposal_id)
    return {
        "project": project_domain.project_to_dict(result["project"]),
        "my_role": result["role"],
    }
