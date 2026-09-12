"""项目域公开契约(projects/drafts/project_versions/version_refs 表，归属 project)。

- 只含不可变值对象与领域错误；不导入 ORM、Session、services 或 application；
- 时间字段一律 ISO 8601 UTC 字符串；ORM datetime → 本类型的映射由
  repository 实现负责（解耦重构切片 3）；
- 权限判定不在此层（归 application 用例，切片 6）。
"""

from __future__ import annotations

from dataclasses import dataclass

from iesplan.core.errors import ConflictError, NotFoundError


class ProjectNotFoundError(NotFoundError):
    """项目/草稿/版本不存在（沿用基类诊断码，不新增码）。"""


class ProjectConflictError(ConflictError):
    """项目状态/指针/版本冲突（沿用基类诊断码，不新增码）。"""


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """项目主行（projects 表公开视图，不含任何 ORM 状态）。"""

    id: int
    name: str
    description: str | None
    status: str
    owner_id: int
    currency: str
    baseline_resolution: str
    baseline_leap_year: bool
    baseline_scenario_mode: str
    schema_version: int
    finance_profile_id: int | None = None
    overrides_revision: int | None = None
    effective_finance_revision: int | None = None
    planning_revision: int | None = None
    current_draft_id: int | None = None
    current_version_id: int | None = None
    created_by: int = 0
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class DraftRecord:
    """工作草稿行（drafts 表公开视图；内容正文在对象存储，不在此）。"""

    id: int
    project_id: int
    revision: int
    content_object_id: int
    parent_draft_id: int | None = None
    is_current: bool = False
    updated_by: int = 0
    updated_at: str | None = None
    # drafts 表无 created_at 列（以 updated_at 为准）；恒为 None，仅为兼容既有
    # API 输出形状（旧 ORM 缺失属性读值为 None）。
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectVersionRecord:
    """项目版本行（project_versions 表公开视图，不可变）。"""

    id: int
    project_id: int
    version_no: int
    name: str
    description: str | None = None
    created_by: int = 0
    parent_version_id: int | None = None
    source_draft_id: int | None = None
    source_draft_revision: int | None = None
    reason: str = ""
    baseline_resolution: str = "1h"
    baseline_leap_year: bool = False
    baseline_scenario_mode: str = "single"
    currency: str | None = None
    schema_version: int = 1
    content_object_id: int = 0
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class VersionRefRecord:
    """版本引用清单行（version_refs 表公开视图，不可变）。"""

    id: int
    project_version_id: int
    ref_type: str
    object_id: int
    ref_key: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectPage:
    """项目列表一页（cursor 为末条 id；空 cursor 表示无更多）。"""

    items: tuple[ProjectRecord, ...] = ()
    next_cursor: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
