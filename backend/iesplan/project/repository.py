"""项目域 repository 协议（projects/drafts/project_versions/version_refs）。

实现规则（persistence 蓝图，切片 3 落实）：
- 只做查询、写入、flush，必要时用 savepoint 隔离多写；绝不 commit/rollback；
- 唯一冲突映射为 ProjectConflictError，不存在映射为 ProjectNotFoundError；
- 不返回 ORM 或懒加载图；不做权限判定（归 application）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.orm import Session

from iesplan.project.contracts import (
    DraftRecord,
    ProjectPage,
    ProjectRecord,
    ProjectVersionRecord,
    VersionRefRecord,
)


class ProjectRepository(Protocol):
    """项目聚合 repository 协议（无状态方法组，db 由调用方事务拥有）。"""

    def get_project(self, db: Session, project_id: int) -> ProjectRecord | None:
        """取项目主行；不存在返回 None。"""
        ...

    def list_projects(
        self,
        db: Session,
        *,
        owner_id: int | None = None,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
        cursor: int | None = None,
    ) -> ProjectPage:
        """按显式条件列出项目（显式过滤参数不是权限判定；id 倒序，cursor 为末条 id）。"""
        ...

    def count_projects_by_owner(self, db: Session, owner_ids: Sequence[int]) -> dict[int, int]:
        """未删除项目数 read model：owner_id → active + archived 项目数（单条 GROUP BY）。"""
        ...

    def create_project(
        self,
        db: Session,
        *,
        name: str,
        owner_id: int,
        created_by: int,
        description: str | None = None,
        currency: str = "CNY",
        baseline_resolution: str = "1h",
        baseline_leap_year: bool = False,
        baseline_scenario_mode: str = "single",
        schema_version: int = 1,
    ) -> ProjectRecord:
        """创建项目裸行（不含初始草稿，调用方随后补草稿）；重名抛 ProjectConflictError。"""
        ...

    def set_project_status(self, db: Session, project_id: int, status: str) -> ProjectRecord:
        """切换项目状态（active/archived/deleted）；非法转换抛 ProjectConflictError。"""
        ...

    def update_revision_pointers(
        self,
        db: Session,
        project_id: int,
        *,
        finance_profile_id: int | None = None,
        overrides_revision: int | None = None,
        effective_finance_revision: int | None = None,
        planning_revision: int | None = None,
    ) -> ProjectRecord:
        """移动配置 revision 指针（指针可移动，revision 行只追加）。"""
        ...

    def get_current_draft(self, db: Session, project_id: int) -> DraftRecord | None:
        """取项目当前草稿；无草稿返回 None（调用方判数据损坏）。"""
        ...

    def get_draft(self, db: Session, draft_id: int) -> DraftRecord | None:
        """按 id 取草稿行（跨域指针解引用用，不存在返回 None）。"""
        ...

    def get_draft_revision(self, db: Session, project_id: int, revision: int) -> DraftRecord | None:
        """按综合修订号取草稿行。"""
        ...

    def create_draft(
        self,
        db: Session,
        *,
        project_id: int,
        content_object_id: int,
        updated_by: int,
        parent_draft_id: int | None = None,
        make_current: bool = True,
    ) -> DraftRecord:
        """追加草稿行（revision = max + 1 内部计算；make_current 时切换 current 标记
        并移动项目 current_draft_id 指针；并发冲突抛 ProjectConflictError）。"""
        ...

    def update_draft_content_ref(
        self, db: Session, draft_id: int, content_object_id: int
    ) -> DraftRecord | None:
        """维护草稿内容指针（内容语义归调用方域，本方法只换引用；草稿缺失返回 None）。"""
        ...

    def create_version(
        self,
        db: Session,
        *,
        project_id: int,
        name: str,
        reason: str,
        created_by: int,
        content_object_id: int,
        source_draft_id: int | None = None,
        source_draft_revision: int | None = None,
        description: str | None = None,
        parent_version_id: int | None = None,
    ) -> ProjectVersionRecord:
        """追加不可变版本（含 version_no 分配、版本内容对象引用行、
        current_version_id 指针移动；parent 为空时沿用当前指针）。"""
        ...

    def list_versions(self, db: Session, project_id: int) -> list[ProjectVersionRecord]:
        """列出项目版本（version_no 倒序）。"""
        ...

    def get_version(self, db: Session, project_id: int, version_id: int) -> ProjectVersionRecord | None:
        """取单个版本（归属不符返回 None，不抛错）。"""
        ...

    def add_version_ref(
        self,
        db: Session,
        *,
        project_version_id: int,
        ref_type: str,
        object_id: int,
        ref_key: str | None = None,
    ) -> VersionRefRecord:
        """追加版本引用行；重复引用抛 ProjectConflictError。"""
        ...

    def list_version_refs(self, db: Session, project_version_id: int) -> list[VersionRefRecord]:
        """列出版本引用清单。"""
        ...
