"""ORM 模型包: 全部 41 张表的 SQLAlchemy 2.0 声明。

表域分布(与 01-db-schema.md 章节对应):
- identity.py   第1节身份: users / roles / user_roles / credentials / window_sessions / auth_events
- project.py    第2、3节权限与项目: project_members / ownership_transfers / admin_maintenance_actions /
                projects / drafts / project_versions / version_refs
- model.py      第4节系统模型: system_graphs / devices / ports / connections
- dataset.py    第5节数据集: datasets / dataset_versions / dataset_files
- calc.py       第6、7节配置与任务: calc_configs / calc_snapshots / tasks / task_attempts /
                task_leases / task_progress / task_diagnostics / compute_slots
- result.py     第8节结果: evidence_packages / result_assessments / result_index /
                result_selections / reports
- uncertainty.py 第9节不确定性: uncertainty_snapshots / sample_tasks / sample_records
- audit.py      第10节审计对象: objects / object_refs / audit_log / import_proposals / retention_rules

导入本包即完成全部模型的元数据注册(供 create_all / alembic autogenerate 使用)。
"""

from iesplan.models.audit import (
    AuditLog,
    ImportProposal,
    ObjectRef,
    RetentionRule,
    StoredObject,
)
from iesplan.models.calc import (
    CalcConfig,
    CalcSnapshot,
    ComputeSlot,
    Task,
    TaskAttempt,
    TaskDiagnostic,
    TaskLease,
    TaskProgress,
)
from iesplan.models.dataset import Dataset, DatasetFile, DatasetVersion
from iesplan.models.identity import (
    AuthEvent,
    Credential,
    Role,
    User,
    UserRole,
    WindowSession,
)
from iesplan.models.immutable_triggers import IMMUTABLE_TABLES
from iesplan.models.model import Connection, Device, Port, SystemGraph
from iesplan.models.project import (
    AdminMaintenanceAction,
    Draft,
    OwnershipTransfer,
    Project,
    ProjectMember,
    ProjectVersion,
    VersionRef,
)
from iesplan.models.result import (
    EvidencePackage,
    Report,
    ResultAssessment,
    ResultIndex,
    ResultSelection,
)
from iesplan.models.uncertainty import (
    SampleRecord,
    SampleTask,
    UncertaintySnapshot,
)

__all__ = [
    # 身份
    "User",
    "Role",
    "UserRole",
    "Credential",
    "WindowSession",
    "AuthEvent",
    # 权限
    "ProjectMember",
    "OwnershipTransfer",
    "AdminMaintenanceAction",
    # 项目
    "Project",
    "Draft",
    "ProjectVersion",
    "VersionRef",
    # 系统模型
    "SystemGraph",
    "Device",
    "Port",
    "Connection",
    # 数据集
    "Dataset",
    "DatasetVersion",
    "DatasetFile",
    # 配置与任务
    "CalcConfig",
    "CalcSnapshot",
    "Task",
    "TaskAttempt",
    "TaskLease",
    "TaskProgress",
    "TaskDiagnostic",
    "ComputeSlot",
    # 结果
    "EvidencePackage",
    "ResultAssessment",
    "ResultIndex",
    "ResultSelection",
    "Report",
    # 不确定性
    "UncertaintySnapshot",
    "SampleTask",
    "SampleRecord",
    # 审计与对象
    "StoredObject",
    "ObjectRef",
    "AuditLog",
    "ImportProposal",
    "RetentionRule",
    # 工具常量
    "IMMUTABLE_TABLES",
]
