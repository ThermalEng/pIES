"""模型域公开契约（系统图/设备/端口/连接，归属 model）。

- 图/设备/端口/连接一经写入的不可变语义由调用方保证；版本图不可修改；
- 只含不可变值对象与领域错误；不导入 ORM、Session、services 或 application。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.core.errors import ConflictError, NotFoundError


class ModelNotFoundError(NotFoundError):
    """图/设备/端口/连接不存在（沿用基类诊断码，不新增码）。"""


class ModelConflictError(ConflictError):
    """名称重复/并发建图冲突（沿用基类诊断码，不新增码）。"""


@dataclass(frozen=True, slots=True)
class GraphRecord:
    """系统图（system_graphs 表公开视图）。

    工作图挂 draft_id；版本图挂 project_version_id（二者互斥）。
    """

    id: int
    project_id: int
    name: str
    draft_id: int | None = None
    project_version_id: int | None = None
    created_by: int = 0
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    """设备（devices 表公开视图）。"""

    id: int
    graph_id: int
    device_type: str
    kind: str
    name: str
    params: dict[str, Any]
    model_fidelity: str = "medium"
    status: str = "active"
    description: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class PortRecord:
    """端口（ports 表公开视图）。"""

    id: int
    device_id: int
    port_type: str
    direction: str
    name: str
    capacity: float | None = None
    params: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ConnectionRecord:
    """连接（connections 表公开视图）。"""

    id: int
    graph_id: int
    from_port_id: int
    to_port_id: int
    conn_type: str
    loss_rate: float = 0.0
    capacity: float | None = None
    params: dict[str, Any] | None = None


#: 模板生命周期状态（与 model_templates 表 CHECK 约束同语义）。
TEMPLATE_STATUS_DRAFT = "draft"
TEMPLATE_STATUS_PUBLISHED = "published"
TEMPLATE_STATUS_DISABLED = "disabled"
TEMPLATE_STATUSES: tuple[str, ...] = (
    TEMPLATE_STATUS_DRAFT,
    TEMPLATE_STATUS_PUBLISHED,
    TEMPLATE_STATUS_DISABLED,
)

#: 项目模型来源（与 project_models 表 CHECK 约束同语义）。
MODEL_SOURCE_DIRECT = "direct_yaml"
MODEL_SOURCE_TEMPLATE = "template"
MODEL_SOURCES: tuple[str, ...] = (MODEL_SOURCE_DIRECT, MODEL_SOURCE_TEMPLATE)


@dataclass(frozen=True, slots=True)
class ModelTemplateRecord:
    """模板主行（model_templates 表公开视图，不含任何 ORM 状态）。"""

    id: int
    template_id: str
    owner_id: int
    status: str
    description: str | None = None
    slug: str | None = None
    public_namespace: str | None = None
    draft_yaml_object_id: int | None = None
    draft_diagnostics_object_id: int | None = None
    draft_has_inputs: bool | None = None
    draft_revision: int = 0
    draft_updated_at: str | None = None
    current_draft_revision_id: int | None = None
    current_published_revision_id: int | None = None
    published_revision: int = 0
    published_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class ModelTemplateRevisionRecord:
    """模板不可变发布 revision（model_template_revisions 表公开视图）。"""

    id: int
    template_id: int
    revision: int
    schema_version: str
    input_count: int = 0
    diagnostics_object_id: int | None = None
    idempotency_key: str | None = None
    yaml_object_id: int = 0
    receipt_object_id: int = 0
    summary_object_id: int = 0
    published_by: int = 0
    published_at: str | None = None


@dataclass(frozen=True, slots=True)
class TemplateDraftRevisionRecord:
    """模板不可变草稿 revision（model_template_draft_revisions 表公开视图）。"""

    id: int
    entry_id: int
    revision: int
    yaml_object_id: int
    source: str
    created_by: int
    created_at: str | None = None
    diagnostics_object_id: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectModelRecord:
    """项目模型清单行（project_models 表公开视图，不含任何 ORM 状态）。"""

    id: int
    project_id: int
    suffix: int
    base_device_id: str
    device_id: str
    revision: int = 1
    project_revision: int = 0
    model_object_id: int = 0
    receipt_object_id: int = 0
    source: str = MODEL_SOURCE_DIRECT
    template_id: str | None = None
    template_revision: int | None = None
    idempotency_key: str | None = None
    created_by: int = 0
    created_at: str | None = None
