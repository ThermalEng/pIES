"""项目域公开门面（projects/drafts/project_versions/version_refs 表，归属 project）。

外部只允许经本门面消费 contract 与 repository 协议；不得导入本域
repository 实现（切片 3 落实）、`iesplan.models` 或 services。
"""

from __future__ import annotations

from iesplan.project import content as _content
from iesplan.project import persistence
from iesplan.project.access import (
    OWNER_CAPABILITIES,
    InvalidRequestError,
    get_role,
)
from iesplan.project.contracts import (
    DraftRecord,
    MaintenanceActionRecord,
    ProjectConflictError,
    ProjectNotFoundError,
    ProjectPage,
    ProjectRecord,
    ProjectVersionRecord,
    VersionRefRecord,
)
from iesplan.project.versions import (
    build_version_content,
    draft_to_dict,
    project_to_dict,
    require_current_draft,
    require_project,
    require_version,
    version_to_dict,
)

add_version_ref = persistence.add_version_ref
content_to_bytes = _content.content_to_bytes
corrupt_error = _content.corrupt_error
count_projects_by_owner = persistence.count_projects_by_owner
create_draft = persistence.create_draft
create_project = persistence.create_project
create_version = persistence.create_version
initial_content = _content.initial_content
parse_content_object = _content.parse_content_object
get_current_draft = persistence.get_current_draft
get_draft = persistence.get_draft
get_draft_revision = persistence.get_draft_revision
get_project = persistence.get_project
get_project_version_content_id = persistence.get_project_version_content_id
get_version = persistence.get_version
list_maintenance_actions = persistence.list_maintenance_actions
list_projects = persistence.list_projects
list_version_refs = persistence.list_version_refs
list_versions = persistence.list_versions
project_name_exists = persistence.project_name_exists
record_maintenance_action = persistence.record_maintenance_action
set_project_status = persistence.set_project_status
update_draft_content_ref = persistence.update_draft_content_ref
update_revision_pointers = persistence.update_revision_pointers

#: 领域不可变表清单(唯一真相归 persistence 所有, 本门面只做引用重导出)。
IMMUTABLE_TABLES = persistence.IMMUTABLE_TABLES


def install_tables() -> None:
    """公开生命周期钩子: 导入本域 persistence 即完成 Base.metadata 表注册(幂等, 无其他副作用)。"""
    persistence.install_tables()


def install_triggers() -> tuple[str, ...]:
    """公开生命周期钩子: 返回本域触发器部署语句(按执行序, 供组合根编排收集)。"""
    return persistence.install_triggers()


__all__ = [
    "DraftRecord",
    "MaintenanceActionRecord",
    "OWNER_CAPABILITIES",
    "ProjectConflictError",
    "ProjectNotFoundError",
    "ProjectPage",
    "ProjectRecord",
    "ProjectVersionRecord",
    "VersionRefRecord",
    "add_version_ref",
    "content_to_bytes",
    "corrupt_error",
    "count_projects_by_owner",
    "build_version_content",
    "create_draft",
    "create_project",
    "create_version",
    "draft_to_dict",
    "get_current_draft",
    "get_draft",
    "get_draft_revision",
    "get_project",
    "get_project_version_content_id",
    "get_role",
    "get_version",
    "initial_content",
    "InvalidRequestError",
    "list_maintenance_actions",
    "list_projects",
    "list_version_refs",
    "list_versions",
    "parse_content_object",
    "project_name_exists",
    "project_to_dict",
    "record_maintenance_action",
    "require_current_draft",
    "require_project",
    "require_version",
    "set_project_status",
    "update_draft_content_ref",
    "update_revision_pointers",
    "version_to_dict",
    "IMMUTABLE_TABLES",
    "install_tables",
    "install_triggers",
]
