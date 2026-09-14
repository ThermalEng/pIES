"""项目用例族(application/projects): 公开门面。

跨模块(API/他用例族)只经本门面消费项目用例命令与结果。
"""

# 他族复用的稳定能力(授权门禁/内容对象存取): 叶模块，无 application 内依赖，
# 置于门面顶部，保证 configuration 等族经本门面回引时已绑定。
from iesplan.application.projects.authorization import (
    ensure_access,
)
from iesplan.application.projects.content_objects import (
    load_content_object,
    store_content_object,
)
from iesplan.application.projects.lifecycle import (
    archive_project_case,
    create_project,
    delete_project,
    get_project_view,
    list_all_projects_case,
    list_visible_projects,
    project_to_dict,
    unarchive_project_case,
    update_draft,
    version_to_dict,
)
from iesplan.application.projects.versions import (
    apply_result,
    create_version,
    current_version_matches_draft,
    freeze_snapshot_version,
    get_version,
    list_versions,
    replace_project_model_refs,
    restore_version,
)

__all__ = [
    "apply_result",
    "archive_project_case",
    "create_project",
    "create_version",
    "current_version_matches_draft",
    "delete_project",
    "ensure_access",
    "freeze_snapshot_version",
    "get_project_view",
    "get_version",
    "list_all_projects_case",
    "list_versions",
    "list_visible_projects",
    "load_content_object",
    "project_to_dict",
    "replace_project_model_refs",
    "restore_version",
    "store_content_object",
    "unarchive_project_case",
    "update_draft",
    "version_to_dict",
]
