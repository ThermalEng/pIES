"""项目版本用例(application/projects/versions.py, W3-A)。

版本编排薄封装(组合 ``iesplan.project`` 领域公开门面版本编排；旧服务
``services.project`` 已删除，见纠偏 Wave 1 切片 D)：

- 创建版本 / 版本列表 / 版本详情；
- 恢复版本 / 应用结果(返回 ``{"version", "draft"}`` 展示字典，序列化
  沿用旧服务返回形态，与路由契约一致)。

事务：写用例顶层函数拥有提交/回滚(``db.commit`` 收尾，失败
``db.rollback``)；读用例不提交事务。本层不新增校验/hash/完整性
复核/防御分支。

调用方向：``api → application.projects.versions → iesplan.project``；
不导入 ORM、不导入领域内部模块、不调用 ``services.*``。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan import project as project_domain
from iesplan.identity.contracts import UserRecord
from iesplan.project.contracts import ProjectVersionRecord


def create_version(
    db: Session,
    user: UserRecord,
    project_id: int,
    name: str,
    description: str | None = None,
    reason: str = "manual_save",
    parent_version_id: int | None = None,
    source_result_id: str | None = None,
) -> ProjectVersionRecord:
    """事务型从当前草稿创建不可变项目版本；application 层统一提交或回滚。"""
    try:
        version = project_domain.create_project_version(
            db,
            user,
            project_id,
            name,
            description,
            reason,
            parent_version_id=parent_version_id,
            source_result_id=source_result_id,
        )
        db.commit()
        return version
    except Exception:
        db.rollback()
        raise


def list_versions(db: Session, project_id: int) -> list[ProjectVersionRecord]:
    """版本列表(新版本在前；只读，不提交事务)。"""
    return project_domain.list_versions(db, project_id)


def get_version(db: Session, project_id: int, version_id: int) -> ProjectVersionRecord:
    """按 id 获取项目版本(须属于该项目，否则 404；只读，不提交事务)。"""
    return project_domain.require_version(db, project_id, version_id)


def restore_version(
    db: Session,
    user: UserRecord,
    project_id: int,
    version_id: int,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """事务型恢复历史版本(新版本 + 新草稿，不倒写历史)；统一提交或回滚。"""
    try:
        result = project_domain.restore_version(
            db,
            user,
            project_id,
            version_id,
            name=name,
            description=description,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def apply_result(
    db: Session,
    user: UserRecord,
    project_id: int,
    diff_patch: dict,
    *,
    version_id: int | None = None,
    name: str | None = None,
    description: str | None = None,
    source_result_id: str | None = None,
) -> dict[str, Any]:
    """事务型应用选定结果(参数差异补丁→新草稿+新版本)；统一提交或回滚。"""
    try:
        result = project_domain.apply_result(
            db,
            user,
            project_id,
            diff_patch,
            version_id=version_id,
            name=name,
            description=description,
            source_result_id=source_result_id,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


__all__ = [
    "apply_result",
    "create_version",
    "get_version",
    "list_versions",
    "restore_version",
]
