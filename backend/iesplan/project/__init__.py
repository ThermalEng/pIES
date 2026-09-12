"""项目域公开门面（projects/drafts/project_versions/version_refs 表，归属 project）。

外部只允许经本门面消费 contract 与 repository 协议；不得导入本域
repository 实现（切片 3 落实）、`iesplan.models` 或 services。
"""

from __future__ import annotations

from iesplan.project import persistence
from iesplan.project.contracts import (
    DraftRecord,
    ProjectConflictError,
    ProjectNotFoundError,
    ProjectPage,
    ProjectRecord,
    ProjectVersionRecord,
    VersionRefRecord,
)
from iesplan.project.repository import ProjectRepository

add_version_ref = persistence.add_version_ref
count_projects_by_owner = persistence.count_projects_by_owner
create_draft = persistence.create_draft
create_project = persistence.create_project
create_version = persistence.create_version
get_current_draft = persistence.get_current_draft
get_draft = persistence.get_draft
get_draft_revision = persistence.get_draft_revision
get_project = persistence.get_project
get_version = persistence.get_version
list_projects = persistence.list_projects
list_version_refs = persistence.list_version_refs
list_versions = persistence.list_versions
project_name_exists = persistence.project_name_exists
set_project_status = persistence.set_project_status
update_draft_content_ref = persistence.update_draft_content_ref
update_revision_pointers = persistence.update_revision_pointers

__all__ = [
    "DraftRecord",
    "ProjectConflictError",
    "ProjectNotFoundError",
    "ProjectPage",
    "ProjectRecord",
    "ProjectRepository",
    "ProjectVersionRecord",
    "VersionRefRecord",
    "add_version_ref",
    "count_projects_by_owner",
    "create_draft",
    "create_project",
    "create_version",
    "get_current_draft",
    "get_draft",
    "get_draft_revision",
    "get_project",
    "get_version",
    "list_projects",
    "list_version_refs",
    "list_versions",
    "project_name_exists",
    "set_project_status",
    "update_draft_content_ref",
    "update_revision_pointers",
]
