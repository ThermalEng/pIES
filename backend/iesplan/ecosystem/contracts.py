"""本地模型与算法生态领域公开契约（归属 ecosystem 域）。

只包含不可变值对象、域枚举与域异常；不含 ORM、Session、HTTP 或容器驱动逻辑。
严禁在本域持久化或运行期重算 ``sha256`` 摘要（宪法 §2.6 三分边界规则）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from iesplan.core.errors import AppError, ConflictError, NotFoundError

MARKET_MODES: tuple[str, ...] = ("online", "local")
PLUGIN_KINDS: tuple[str, ...] = ("predictor", "generator", "solver", "evaluator", "algorithm")
ARTIFACT_KINDS: tuple[str, ...] = ("device_model",) + PLUGIN_KINDS
SOURCE_ORIGINS: tuple[str, ...] = ("local_upload", "offline_import", "online_market")
APPROVAL_STATUSES: tuple[str, ...] = ("pending", "approved", "rejected")
INSTALL_STATUSES: tuple[str, ...] = (
    "none",
    "pending",
    "building",
    "testing",
    "ready",
    "build_failed",
    "disabled",
)


class EcosystemArtifactNotFoundError(NotFoundError):
    """生态制品不存在（HTTP 404）。"""

    code = "ECO-NF-001"
    message_key = "ies.diag.ecosystem.artifact_not_found"


class EcosystemConflictError(ConflictError):
    """生态制品唯一版本冲突或状态冲突（HTTP 409）。"""

    code = "ECO-CONFLICT-001"
    message_key = "ies.diag.ecosystem.conflict"


class EcosystemUninstallInUseError(ConflictError):
    """仍有在途任务引用该精确版本，无状态拒绝物理清理镜像（HTTP 409）。"""

    code = "ECO-UNINSTALL-IN-USE"
    message_key = "ies.diag.ecosystem.uninstall_in_use"


class EcosystemValidationError(AppError):
    """生态导入、清单或参数配置校验失败（HTTP 400）。"""

    code = "ECO-VAL-001"
    http_status = 400
    severity = "error"
    message_key = "ies.diag.ecosystem.validation_failed"


@dataclass(frozen=True, slots=True)
class EcosystemSettingsRecord:
    """实例级正交生态设置（ecosystem_settings 单例行）。"""

    id: int
    market_mode: str = "local"
    local_upload_requires_approval: bool = True
    pip_index_url: str | None = None
    market_index_url: str | None = None
    submission_gateway_url: str | None = None
    updated_by: int | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class LocalArtifactRecord:
    """实例级共享本地制品目录记录（按 (kind, stable_id, version) 去重）。

    遵守宪法 §2.6：对象入库后一律不存储、不重算 ``sha256``。
    """

    id: int
    kind: str
    stable_id: str
    version: str
    source_origin: str
    approval_status: str
    install_status: str
    uploaded_by: int
    object_id: str
    metadata_json: dict[str, Any] = field(default_factory=dict)
    approved_by: int | None = None
    approved_at: str | None = None
    image_ref: str | None = None
    diagnostics: Any | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.stable_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class MemberArtifactInstallRecord:
    """成员级多版本并存安装与启用状态记录（member_artifact_installs）。"""

    id: int
    user_id: int
    artifact_id: int
    kind: str
    stable_id: str
    version: str
    enabled: bool = True
    installed_at: str | None = None
    updated_at: str | None = None


__all__ = [
    "APPROVAL_STATUSES",
    "ARTIFACT_KINDS",
    "EcosystemArtifactNotFoundError",
    "EcosystemConflictError",
    "EcosystemSettingsRecord",
    "EcosystemUninstallInUseError",
    "EcosystemValidationError",
    "INSTALL_STATUSES",
    "LocalArtifactRecord",
    "MARKET_MODES",
    "MemberArtifactInstallRecord",
    "PLUGIN_KINDS",
    "SOURCE_ORIGINS",
]
