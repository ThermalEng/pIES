"""项目域公开门面（projects/drafts/project_versions/version_refs 表，归属 project）。

外部只允许经本门面消费 contract 与 repository 协议；不得导入本域
repository 实现（切片 3 落实）、`iesplan.models` 或 services。
"""

from __future__ import annotations

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

__all__ = [
    "DraftRecord",
    "ProjectConflictError",
    "ProjectNotFoundError",
    "ProjectPage",
    "ProjectRecord",
    "ProjectRepository",
    "ProjectVersionRecord",
    "VersionRefRecord",
]
