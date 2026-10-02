"""任务用例族(application/tasks): 提交/取消/重试/租约。

任务提交与状态机编排的唯一实现(已收敛原 ``services.tasks`` +
``services.queue`` 语义，旧服务已删除):

- 提交: 权限 → 项目状态 → 类型/幂等键必填与父任务校验 → 幂等命中 →
  存储门禁 → 快照装配(含草稿固化, 相同输入复用快照) → 任务行 → 诊断 → 入队；
- 取消: 终态拒绝 → queued 直接取消 → running 转 cancelling + 取消信号 +
  批量子任务传播；确认取消收拢尝试/租约/槽；
- 重试: 仅终态可重试(计算类须有快照) → 回 queued + 入队；
- 租约: 占槽 + 建尝试 + 建租约(fencing token) + running + 出队。

事务: 顶层用例拥有提交/回滚(``db.commit`` 收尾, 失败 ``db.rollback``);
内部步骤只经领域公开门面写入 + flush, 不提交。队列(可重建视图)的入队/
出队/信号与旧服务同一位置触发, 行为一致。

数据访问只经领域公开门面(tasks/project/dataset/audit/storage/identity/
configuration, 含 tasks 域队列可重建视图); 不导入 ``models.*``。
幂等键格式改接任务域常量唯一权威(``iesplan.tasks.contracts.IDEMPOTENCY_KEY_RE``)。
任务类型/状态机/业务结局映射与任务错误唯一权威归 tasks 域
(``iesplan.tasks`` 门面), 本模块只做提交/幂等/快照/事务编排, 直接复用。
快照固化时的版本内容规则(含财务/规划引用闭合)经
``application.projects.versions.freeze_snapshot_version`` 项目用例拥有
(语义与旧 services.project 一致), 本模块只编排提交/幂等/快照/事务。
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from iesplan.finance import EffectiveFinanceConfig

from sqlalchemy.orm import Session

from iesplan import __version__
from iesplan import configuration as configuration_domain
from iesplan import dataset as dataset_domain
from iesplan import identity as identity_domain
from iesplan import modeling as modeling_domain
from iesplan import project as project_domain
from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.projects import (
    current_version_matches_draft as _current_version_matches_draft,
)
from iesplan.application.projects import (
    ensure_access,
    load_content_object,
)
from iesplan.application.projects import (
    freeze_snapshot_version as _freeze_snapshot_version,
)
from iesplan.assembly import (
    AssemblyValidationError,
    AssemblyValidationResult,
    validate_project_export,
)
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.config import settings
from iesplan.core.diagnostics import (
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SYS_STORE_CORRUPT,
    TASK_DATA_SNAPSHOT_MISSING,
    TASK_QUEUED,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.idgen import new_id
from iesplan.core.jsonutil import jsonable
from iesplan.identity.contracts import UserRecord
from iesplan.project.contracts import ProjectRecord, ProjectVersionRecord
from iesplan.storage import (
    get_object,
    object_info,
    orphaned_stats,
    usage_summary,
)
from iesplan.tasks.contracts import (
    COMPUTE_TYPES,
    LEASE_TTL_SECONDS,
    POOL_BY_TYPE,
    SNAPSHOT_SCHEMA_VERSION,
    TASK_TYPES,
    TERMINAL_STATUSES,
    CalcSnapshotRecord,
    CancelDeniedError,
    ComputeSlotRecord,
    InvalidRequestError,
    StorageQuotaError,
    TaskAttemptRecord,
    TaskRecord,
    TaskStateError,
)
from iesplan.tasks.contracts import (
    IDEMPOTENCY_KEY_RE as _IDEMPOTENCY_KEY_RE,
)

#: 任务类型/池/状态机/结局映射与任务错误唯一权威见 tasks 域
#: (``iesplan.tasks`` 门面, 本模块顶层导入复用); 此处不复制。
#: 逐时结果每行估算字节(~1 KB)
_HOURLY_BYTES_PER_ROW = 1024
#: 中间文件系数(默认 0.5)
_INTERMEDIATE_FACTOR = 0.5
#: 证据包系数(默认 0.1)
_EVIDENCE_FACTOR = 0.1
#: 幂等键格式唯一权威见 tasks.contracts(顶层导入 _IDEMPOTENCY_KEY_RE)。

#: 项目所有者能力集唯一权威: iesplan.project.OWNER_CAPABILITIES(经 project_domain 取用, 此处不复制)。


@dataclass(frozen=True, slots=True)
class Claim:
    """一次领取结果(尝试 + 租约 + fencing token)。"""

    task_id: int
    attempt_id: int
    attempt_no: int
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class StorageEstimate:
    """存储门禁估算结果。"""

    need: int
    avail: int
    blocked: bool
    suggestions: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "need_bytes": self.need,
            "avail_bytes": self.avail,
            "blocked": self.blocked,
            "suggestions": self.suggestions,
        }


# ---------------------------------------------------------------------------
# 项目/权限/内容读取胶水(复制 services.project 对应语义, 经领域公开门面)
# ---------------------------------------------------------------------------


def require_project(db: Session, project_id: int) -> ProjectRecord:
    """按 id 取项目; 不存在一律 404。"""
    project = project_domain.get_project(db, project_id)
    if project is None:
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    return project


def _get_current_draft(db: Session, project: ProjectRecord):
    """取项目当前草稿; 缺失视为数据损坏。"""
    draft = project_domain.get_current_draft(db, project.id)
    if draft is None:
        raise AppError(
            "项目缺少当前草稿(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "project", "object_id": project.id},
        )
    return draft






# ---------------------------------------------------------------------------
# 项目/草稿/版本解析(复制 services.tasks._resolve_project_inputs 语义)
# ---------------------------------------------------------------------------


def _resolve_project_inputs(
    db: Session, project: ProjectRecord, actor: UserRecord, *, freeze: bool
) -> tuple[ProjectVersionRecord | None, dict]:
    """解析任务输入: 项目版本(或当前草稿内容, 需要时固化)。

    返回 (version, content): version 为 None 表示未固化(仅读草稿内容)。
    已有版本但草稿领域内容已变更时版本不再新鲜, 必须按当前草稿重新固化,
    否则后续任务会静默运行旧版本输入。
    """
    version = None
    if project.current_version_id is not None:
        version = project_domain.get_version(db, project.id, project.current_version_id)
        if version is None:
            raise AppError(
                "项目版本指针缺失(数据损坏)",
                code=SYS_STORE_CORRUPT,
                severity=SEVERITY_ERROR,
                message_key="ies.diag.store.corrupt",
                location={"object_type": "project", "object_id": project.id},
            )
        if not _current_version_matches_draft(db, project):
            version = None  # 草稿已变更: 需重新固化
    if version is not None:
        return version, load_content_object(db, version.content_object_id)
    draft = _get_current_draft(db, project)
    content = load_content_object(db, draft.content_object_id)
    if not freeze:
        return None, content
    # 草稿固化: 创建不可变项目版本(计算输入固定)。闸门必须消费密封后
    # 的版本内容(含 project_baseline 等项目固化字段), 不得直接使用原始
    # 草稿内容, 否则与已有版本路径的输入口径不一致。
    version = _freeze_snapshot_version(db, actor, project, draft)
    return version, load_content_object(db, version.content_object_id)






def _bound_dataset_ids(content: dict) -> list[int]:
    """从项目内容取出绑定的数据集版本 id 清单。"""
    return [
        int(binding["dataset_version_id"])
        for binding in content.get("dataset_bindings", [])
        if binding.get("dataset_version_id") is not None
    ]


def _derive_random_seed(version_id: int) -> int:
    """随机种子强制非 NULL: 配置缺省时取项目版本 id(48 bit 非负种子)。

    版本 id 为明确标识(非内容摘要): 同一版本重提 → 同一种子(可复现);
    不同版本 → 不同种子。不做摘要派生。
    """
    return int(version_id) % (1 << 48)


# ---------------------------------------------------------------------------
# 快照装配(复制 services.tasks.assemble_snapshot 语义)
# ---------------------------------------------------------------------------


def _snapshot_inputs_equal(
    snapshot: CalcSnapshotRecord,
    *,
    dataset_ids: list[int],
    calc_config: dict,
    schema_version: str,
    result_adapter: str,
    executor: str,
    program_version: str,
    extensions: dict,
    random_seed: int,
    canonical_text: str | None,
    receipt: dict,
    device_models: dict,
) -> bool:
    """快照输入一致判定: 全部快照内容字段逐项相等(不使用内容摘要)。

    复用身份包含 schema_version、执行引用与逐实例数学贡献: 不同 schema、
    不同配套 adapter/executor 或不同设备贡献的快照永不复用。
    容差唯一权威是 calc_config 正文的 convergence, 此处不另设 tolerances。
    """
    return (
        list(snapshot.dataset_version_ids or []) == list(dataset_ids)
        and jsonable(snapshot.calc_config_snapshot or {}) == jsonable(calc_config)
        and (snapshot.schema_version or "") == schema_version
        and (snapshot.result_adapter or "") == result_adapter
        and (snapshot.executor or "") == executor
        and (snapshot.program_version or "") == program_version
        and jsonable(snapshot.extension_versions or {}) == jsonable(extensions)
        and snapshot.random_seed == random_seed
        and (snapshot.canonical_assembly_text or "") == (canonical_text or "")
        and jsonable(snapshot.assembly_receipt or {}) == jsonable(receipt)
        and jsonable(snapshot.device_models or {}) == jsonable(device_models)
    )


def _resolve_pinned_calculation_config(
    db: Session, project_id: int, config: dict[str, Any] | None
) -> tuple[dict[str, Any], int]:
    """解析任务提交钉选的计算配置 revision → (正文副本, revision)。

    只接受任务级 config 中的 ``config_revision``(正整数); 缺失或形态非法
    (含布尔值) → 400 TASK-REQ-006。按 (project_id, version) 精确解析已保存
    行, 正文原样取行内容, 不重复校验(输入入口唯一校验); 查无 → 404, 不读
    最新指针, 不回退草稿 calc_config、默认配置或空字典。
    """
    raw = (config or {}).get("config_revision")
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise InvalidRequestError(
            "计算类任务须在 config 中明确引用已保存配置 revision(config_revision 正整数)",
            code="TASK-REQ-006",
            params={"project_id": project_id, "config_revision": raw},
            location={"object_type": "project", "object_id": project_id, "field": "config_revision"},
        )
    row = configuration_domain.get_calculation_config_revision(db, project_id, raw)
    if row is None:
        raise NotFoundError(
            "指定的计算配置 revision 在本项目不存在",
            params={"project_id": project_id, "config_revision": raw},
            location={"object_type": "calculation_config", "object_id": project_id, "field": "config_revision"},
        )
    return dict(row.content), row.version


def _resolve_execution_refs(
    body: Mapping[str, Any],
    generator_registry: Any | None,
    project_id: int,
) -> tuple[str, str]:
    """解析并冻结执行引用(0.12-A3): generator → descriptor → 配套 adapter/executor。

    只做引用解析, 不做 mode/精度/options 能力门禁(那是生成时
    ``GeneratorRegistry.resolve`` 的职责, 此处不重复校验):
    - 目录未装配(None) → 500(服务端装配缺失, 无回退);
    - generator 未注册(含旧 ``ies.algo.*`` 引用, 不隐式映射) → 400
      TASK-REQ-007;
    - solver 不在 descriptor.solvers → 400 TASK-REQ-008;
    - executors 非恰好一个 → 400 TASK-REQ-009(不按首项/注册顺序猜测)。
    返回 (result_adapter 精确引用, executor 精确引用)。
    """
    generator_ref = body.get("generator")
    solver_ref = body.get("solver")
    if generator_registry is None:
        raise AppError(
            "生成器目录未装配, 计算任务提交不可用",
            params={"project_id": project_id},
        )
    if not isinstance(generator_ref, str) or not generator_ref:
        raise InvalidRequestError(
            "计算配置引用的生成器不是合法精确引用",
            code="TASK-REQ-007",
            params={"project_id": project_id, "generator": generator_ref},
            location={"object_type": "calculation_config", "object_id": project_id, "field": "generator"},
        )
    try:
        provider = generator_registry.get(generator_ref)
    except ComputationUnavailableError as exc:
        raise InvalidRequestError(
            "计算配置引用的生成器未注册(旧算法引用不隐式映射到新生成器)",
            code="TASK-REQ-007",
            params={"project_id": project_id, "generator": generator_ref},
            location={"object_type": "calculation_config", "object_id": project_id, "field": "generator"},
        ) from exc
    descriptor = provider.descriptor
    if solver_ref not in descriptor.solvers:
        raise InvalidRequestError(
            "计算配置引用的求解器不在所选生成器兼容清单内",
            code="TASK-REQ-008",
            params={"project_id": project_id, "generator": generator_ref, "solver": solver_ref},
            location={"object_type": "calculation_config", "object_id": project_id, "field": "solver"},
        )
    executors = tuple(descriptor.executors or ())
    if len(executors) != 1:
        raise InvalidRequestError(
            "所选生成器未声明唯一执行器(无公开选择规则, 不猜测)",
            code="TASK-REQ-009",
            params={"project_id": project_id, "generator": generator_ref, "executors": list(executors)},
            location={"object_type": "calculation_config", "object_id": project_id, "field": "generator"},
        )
    return descriptor.result_adapter, executors[0]


def _freeze_device_models(artifact: Any, device_documents: Mapping[str, Any]) -> dict[str, Any]:
    """逐装配实例数学贡献冻结(``{instance_id: contribution dict}``)。

    实例 → 类型经规范文本 ``devices.<inst>.definition.id`` 唯一确定(闸门
    已阻断未知类型)；贡献由本次闸门实际解析的设备文档构建，与各校验阶段
    同一次解析，不二次读取当前目录。构建失败(方程语义阻断)即闸门失败，
    不建快照、不补默认。同一输入恒得同一字节(``contribution_to_dict``
    稳定键序)。
    """
    doc = json.loads(artifact.canonical_text)
    devices = doc.get("devices") or {}
    frozen: dict[str, Any] = {}
    for inst in sorted(devices):
        # definition.id 与设备文档由闸门保证：未知类型已阻断，此处直接使用。
        type_id = (devices[inst].get("definition") or {}).get("id")
        built = modeling_domain.build_math_contribution(device_documents[type_id])
        if not built.ok or built.contribution is None:
            raise AssemblyValidationError(
                list(built.diagnostics),
                message=f"设备数学贡献构建失败: {inst!r}",
            )
        frozen[inst] = modeling_domain.contribution_to_dict(built.contribution)
    return frozen


def _assemble_snapshot(
    db: Session,
    project_id: int,
    task_type: str,
    config: dict[str, Any] | None = None,
    user: UserRecord | None = None,
    calculation_config: tuple[dict[str, Any], int] | None = None,
    generator_registry: Any | None = None,
) -> CalcSnapshotRecord:
    """组装不可变计算快照(相同输入复用)。

    绑定: 项目版本(无版本时固化当前草稿)、数据集版本 id 清单、钉选的计算
    配置正文 + 来源 revision/项目身份、程序版本、受控扩展版本、随机种子
    (正文 seed 缺失时按版本派生, 强制非 NULL)、规范装配文本/回执; 相同输入
    复用既有快照(快照内容字段逐项相等判定, 含 config_ref, 故不同 revision
    永不复用)。任务级 config(除绑定键 config_revision 外)并入快照的
    calc_config_snapshot.task_params, 不覆盖配置正文。
    """
    project = require_project(db, project_id)
    actor = user or identity_domain.get_user(db, project.owner_id)
    if actor is None:
        raise InvalidRequestError("无法确定快照创建者", params={"project_id": project_id})
    version, content = _resolve_project_inputs(db, project, actor, freeze=True)
    assert version is not None
    body, revision = calculation_config if calculation_config is not None else ({}, 0)
    calc_config: dict[str, Any] = dict(body)
    task_params = {k: v for k, v in (config or {}).items() if k != "config_revision"}
    if task_params:
        calc_config["task_params"] = jsonable(task_params)
    if revision:
        calc_config["config_ref"] = {"project_id": project_id, "revision": revision}
    random_seed = calc_config.get("seed")
    if not isinstance(random_seed, int) or isinstance(random_seed, bool):
        random_seed = _derive_random_seed(version.id)
    dataset_ids = _bound_dataset_ids(content)
    extensions = content.get("extensions") or {}
    # 执行引用与快照同事务冻结: 同一已解析 generator 派生同一配套对。
    result_adapter, executor = _resolve_execution_refs(
        calc_config, generator_registry, project_id
    )

    # 统一生产闸门: 先签发规范文本/回执二件套, 失败不创建快照或任务。
    # 回执与逐实例数学贡献进入快照身份; 快照去重使用内容字段逐项相等判定,
    # 文本仅校验字头。贡献由本次闸门同一次设备解析冻结，不二次读目录。
    gate = _assembly_gate(db, project_id, content, task_type)
    assert gate.artifact is not None
    artifact = gate.artifact
    receipt = artifact.receipt.to_dict()
    device_models = _freeze_device_models(artifact, gate.device_documents)
    for candidate in tasks_domain.list_snapshots_for_version(db, version.id):
        if _snapshot_inputs_equal(
            candidate,
            dataset_ids=dataset_ids,
            calc_config=calc_config,
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            result_adapter=result_adapter,
            executor=executor,
            program_version=__version__,
            extensions=extensions,
            random_seed=random_seed,
            canonical_text=artifact.canonical_text,
            receipt=receipt,
            device_models=device_models,
        ):
            return candidate  # 相同输入复用既有快照(不可变, 复用安全)

    return tasks_domain.create_snapshot(
        db,
        project_version_id=version.id,
        dataset_version_ids=dataset_ids,
        calc_config_snapshot=calc_config,
        random_seed=random_seed,
        created_by=actor.id,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        result_adapter=result_adapter,
        executor=executor,
        device_models=device_models,
        program_version=__version__,
        extension_versions=extensions,
        canonical_assembly_text=artifact.canonical_text,
        assembly_receipt=receipt,
    )


def _export_finance_for(
    db: Session, project_id: int, *, profile_id: str, revision: int
) -> tuple[dict[str, Any], dict[str, EffectiveFinanceConfig]]:
    """项目当前 Effective 快照 → (finance 精确引用节, 密封快照显式注入)。

    只解析传入的精确引用 {profile_id, revision}；不读项目当前指针，
    不 fallback。引用身份为签发时写入对象存储的真实内容对象 oid(经行
    content_object_id 非空列解析)；密封对象由行内容类型化恢复后注入，
    装配直接消费，不重验字段。钉选 revision 不存在/档案不符 →
    AppError(与配置域 PROJ-FIN-003 同语义，可见失败，不回退)。
    finance 节携带 revision，装配回执记录冻结血缘(oid/profile/revision)。
    """
    from iesplan.finance import EffectiveFinanceConfig
    from iesplan.storage import object_info

    row = configuration_domain.get_effective_revision(db, project_id, revision)
    if row is None or row.profile_id != profile_id:
        raise AppError(
            "冻结版本引用的 Effective 财务快照不存在(先引用财务 Profile 后提交计算)",
            code="PROJ-FIN-003",
            params={"project_id": project_id, "profile_id": profile_id, "revision": revision},
        )
    effective = EffectiveFinanceConfig.from_dict(row.content)
    object_id = str(object_info(db, row.content_object_id)["oid"])
    finance = {
        "ref": {
            "kind": "object",
            "object_id": object_id,
            "media_type": "application/yaml",
        },
        "profile_id": row.profile_id,
        "revision": row.revision,
    }
    return finance, {object_id: effective}


def _project_planning_for(db: Session, project_id: int, *, revision: int) -> dict[str, Any]:
    """冻结版本钉选的 planning_config revision → 确定性投影。

    经 configuration 公开门面取对应不可变行，PlanningConfig.from_dict
    类型化恢复(持久化反序列化边界)后 to_dict() 序列化即装配 planning 节；
    装配不查库、不重验可信内容。钉选 revision 不存在 → AppError
    (与配置域 PROJ-PLAN-004 同语义，可见失败，不回退)。
    """
    from iesplan.core.contracts import PlanningConfig

    row = configuration_domain.get_planning_revision(db, project_id, revision)
    if row is None:
        raise AppError(
            "冻结版本引用的规划配置不存在(先保存规划配置后提交计算)",
            code="PROJ-PLAN-004",
            params={"project_id": project_id, "revision": revision},
        )
    return PlanningConfig.from_dict(row.content).to_dict()


def _frozen_ref(content: Mapping, project_id: int, key: str, field: str) -> Mapping:
    """版本内容冻结引用块(缺失/形态非法 → InvalidRequestError，不读当前指针)。"""
    block = content.get(key)
    if not isinstance(block, Mapping) or field not in block:
        raise InvalidRequestError(
            f"版本内容缺少冻结引用 {key}.{field}(先固定对应配置后提交计算)",
            params={"project_id": project_id},
        )
    return block


def _assembly_gate(db: Session, project_id: int, content: dict, task_type: str) -> AssemblyValidationResult:
    """计算任务统一装配闸门: 完整四阶段校验后签发规范二件套。

    阻断诊断抛 AssemblyValidationError(HTTP 422)。签发即受信，不做重复复核。
    成功结果同时携带本次校验实际解析的设备文档(同一次解析，供快照冻结
    逐实例数学贡献，调用方不得另行二次解析当前目录)。
    finance/planning 只消费传入版本内容已冻结的精确引用
    (effective_finance {profile_id, overrides_revision, revision} /
    planning_config {revision})，
    按钉选 revision 经公开门面取不可变行；不读项目 latest/current 指针，
    不 fallback，缺席/档案不符明确失败。finance_binding/tariff_bindings
    只透传项目内容已声明的节(起草命令写入)，缺席即对应装配节缺失阻断，
    不猜测、不补默认。
    """
    if task_type not in COMPUTE_TYPES:
        raise InvalidRequestError("仅计算类任务可装配计算快照", params={"task_type": task_type})
    frozen_finance = _frozen_ref(content, project_id, "effective_finance", "revision")
    profile_id = frozen_finance.get("profile_id")
    revision = frozen_finance.get("revision")
    if (
        not isinstance(profile_id, str)
        or not profile_id
        or not isinstance(revision, int)
        or isinstance(revision, bool)
    ):
        raise InvalidRequestError(
            "版本内容冻结财务引用非法(先引用财务 Profile 后提交计算)",
            params={"project_id": project_id},
        )
    frozen_planning = _frozen_ref(content, project_id, "planning_config", "revision")
    planning_revision = frozen_planning.get("revision")
    if not isinstance(planning_revision, int) or isinstance(planning_revision, bool):
        raise InvalidRequestError(
            "版本内容冻结规划引用非法(先保存规划配置后提交计算)",
            params={"project_id": project_id},
        )
    finance, finance_snapshots = _export_finance_for(
        db, project_id, profile_id=profile_id, revision=revision
    )
    planning = _project_planning_for(db, project_id, revision=planning_revision)
    export_content = dict(content)
    # 装配名使用确定性项目标识（slug 要求 ASCII，项目名可能是中文）；
    # 不设 graph_id 兼容键。项目不存在即 404，不编造。
    require_project(db, project_id)
    export_content["name"] = f"project_{project_id}"
    contents = _dataset_contents_for(db, content)
    result = validate_project_export(
        export_content,
        dataset_contents=contents,
        finance=finance,
        finance_binding=content.get("finance_binding"),
        tariff_bindings=content.get("tariff_bindings"),
        planning=planning,
        finance_snapshots=finance_snapshots,
    )
    if result.artifact is None:
        raise AssemblyValidationError(result.diagnostics)
    return result


def _dataset_contents_for(
    db: Session, content: dict,
) -> dict[str, dict]:
    """项目绑定数据集版本 → 装配显式注入({合成路径: 对象字节})。

    只通过 dataset 域与 storage 公开门面读取。绑定由 dataset.bind
    输入边界写入类型化整数 id(不存在/跨项目当场拒绝)，此处直接信任：
    每个版本选择 ``file_kind=data`` 的权威数据本体；缺数据本体或
    对象不可读是门面的真实错误(直接透传)，一律不跳过、不伪装成
    装配引用缺失。
    """
    from iesplan.assembly import dataset_export_path

    contents: dict[str, dict] = {}
    for binding in content.get("dataset_bindings", []) or []:
        vid = binding["dataset_version_id"]
        data_file = next(
            (f for f in dataset_domain.list_files(db, vid) if f.file_kind == "data"),
            None,
        )
        if data_file is None:
            raise AppError(
                "数据集版本缺少权威数据本体(file_kind=data)",
                params={"dataset_version_id": vid},
            )
        raw = get_object(db, data_file.object_id)
        object_meta: dict = object_info(db, data_file.object_id)
        media_type = object_meta.get("media_type")
        if not media_type:
            media_type = {
                "csv": "text/csv; charset=utf-8",
                "parquet": "application/vnd.apache.parquet",
                "json": "application/json",
            }.get(data_file.format, "application/octet-stream")
        path = dataset_export_path(vid)
        contents[path] = {
            "object_id": str(data_file.object_id),
            "media_type": media_type,
            "data": raw,
        }
    return contents


# ---------------------------------------------------------------------------
# 存储门禁(复制 services.tasks.estimate_storage 语义)
# ---------------------------------------------------------------------------


def _resolve_storage_estimate_inputs(
    db: Session, project: ProjectRecord, actor: UserRecord
) -> tuple[dict, list[int]]:
    """只读解析估算输入(版本或草稿内容, 不固化不写库)。"""
    _version, content = _resolve_project_inputs(db, project, actor, freeze=False)
    return content, _bound_dataset_ids(content)


def list_cleanup_suggestions(db: Session) -> list[dict[str, Any]]:
    """清理建议(按"最安全 → 最激进"排序, 每项含对象清单与预计释放量)。"""
    suggestions: list[dict[str, Any]] = []

    orphaned = orphaned_stats(db)
    suggestions.append(
        {
            "action": "cleanup_orphaned_objects",
            "message_key": "ies.fix.store.orphaned",
            "count": orphaned["count"],
            "estimate_bytes": orphaned["total_bytes"],
        }
    )

    report_count = results_domain.count_reports(db)
    suggestions.append(
        {
            "action": "archive_old_reports",
            "message_key": "ies.fix.store.reports",
            "count": int(report_count),
            "estimate_bytes": 0,
        }
    )

    terminal_tasks = tasks_domain.count_tasks_by_statuses(db, TERMINAL_STATUSES)
    suggestions.append(
        {
            "action": "cleanup_terminal_task_files",
            "message_key": "ies.fix.store.task_files",
            "count": int(terminal_tasks),
            "estimate_bytes": 0,
        }
    )

    suggestions.append(
        {
            "action": "archive_project_versions",
            "message_key": "ies.fix.store.versions",
            "count": 0,
            "estimate_bytes": 0,
        }
    )
    suggestions.append(
        {
            "action": "reduce_samples_or_horizon",
            "message_key": "ies.fix.store.business_throttle",
            "count": 0,
            "estimate_bytes": 0,
        }
    )
    return suggestions


def estimate_storage(
    db: Session, project_id: int, task_type: str, config: dict[str, Any] | None = None
) -> StorageEstimate:
    """提交前存储需求估算与安全阈值检查。

    S_need = S_snap + S_inter + S_hourly(+S_samples) + S_evid;
    S_avail = min(配额余额, 卷空闲空间); 配额未配置时仅以卷空闲空间为准。
    只读, 不拥有事务。
    """
    project = require_project(db, project_id)
    actor = identity_domain.get_user(db, project.owner_id)
    if actor is None:
        raise NotFoundError("项目所有者不存在", params={"project_id": project_id})
    content, dataset_ids = _resolve_storage_estimate_inputs(db, project, actor)
    params = config or {}

    # 快照与输入: 数据集版本对象大小之和
    snap_bytes = 0
    for dvid in dataset_ids:
        snap_bytes += sum(f.size_bytes or 0 for f in dataset_domain.list_files(db, dvid))

    # 逐时结果: 行数 × ~1 KB(Y = 规划年数; 多目标解点 × 解点数)
    years = int(params.get("horizon_years", 1) or 1)
    n_solutions = int(params.get("n_solutions", 1) or 1)
    hourly_rows = 8760 * max(years, 1) * max(n_solutions, 1)
    hourly_bytes = hourly_rows * _HOURLY_BYTES_PER_ROW
    # 样本结果(uncertainty 批次: 样本数 × 逐时规模)
    sample_bytes = 0
    if task_type == "uncertainty":
        n_samples = int(params.get("n_samples", 0) or 0)
        sample_bytes = max(n_samples, 0) * hourly_bytes
    result_bytes = hourly_bytes + sample_bytes
    inter_bytes = int(_INTERMEDIATE_FACTOR * result_bytes)
    evid_bytes = int(_EVIDENCE_FACTOR * (snap_bytes + result_bytes))
    need = snap_bytes + inter_bytes + result_bytes + evid_bytes

    # 可用空间: min(配额余额, 卷空闲空间); 配额未配置视为无限
    usage = usage_summary(db)
    volume_free = shutil.disk_usage(settings.data_dir).free
    if int(usage["quota_bytes"] or 0) > 0:
        avail = max(int(usage["quota_bytes"]) - int(usage["used_bytes"]), 0)
    else:
        avail = volume_free
    avail = min(avail, volume_free)

    blocked = need > avail - settings.storage_min_free_bytes
    suggestions = list_cleanup_suggestions(db) if blocked else []
    return StorageEstimate(need=need, avail=avail, blocked=blocked, suggestions=suggestions)


# ---------------------------------------------------------------------------
# 任务提交(复制 services.tasks.create_task 语义; 顶层拥有事务)
# ---------------------------------------------------------------------------


def _get_task(db: Session, task_id: int) -> TaskRecord:
    """按 id 取任务; 不存在 404。"""
    task = tasks_domain.get_task(db, task_id)
    if task is None:
        raise NotFoundError(
            "任务不存在",
            params={"task_id": task_id},
            location={"object_type": "task", "object_id": task_id},
        )
    return task


def ensure_task_belongs(db: Session, project_id: int, task_id: int) -> TaskRecord:
    """任务必须属于该项目(否则 404, 不泄露其他项目任务存在性)。只读, 不拥有事务。"""
    task = _get_task(db, task_id)
    if task.project_id != project_id:
        raise NotFoundError(
            "任务不存在",
            params={"task_id": task_id, "project_id": project_id},
            location={"object_type": "task", "object_id": task_id},
        )
    return task


def _write_diagnostic(
    db: Session,
    task_id: int,
    *,
    level: str,
    code: str,
    message: str,
    attempt_id: int | None = None,
    stack_trace: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """写入任务诊断(不可变, 只 INSERT; 经 tasks 域)。"""
    tasks_domain.append_diagnostic(
        db,
        task_id=task_id,
        level=level,
        message=message,
        attempt_id=attempt_id,
        code=code,
        stack_trace=stack_trace,
        context=context,
    )


def _submit_task(
    db: Session,
    user: UserRecord,
    project_id: int,
    task_type: str,
    config: dict[str, Any] | None = None,
    *,
    idempotency_key: str,
    parent_task_id: int | None = None,
    generator_registry: Any | None = None,
) -> tuple[TaskRecord, dict[str, bool]]:
    """创建任务(幂等检查 → 门禁 → 快照 → INSERT → 入队)。

    幂等键必填(项目作用域, 宪法 §5.5); 缺失或格式非法直接拒绝。
    返回 (任务记录, 标记): 标记仅含 replay。内部步骤只 flush,
    由顶层 submit_task 提交。
    generator_registry 为组合根已发布的生成器目录(计算任务解析冻结
    执行引用用; io 任务不消费)。
    """
    ensure_access(db, user, project_id, "edit")
    project = require_project(db, project_id)
    if project.status != "active":
        raise ConflictError("项目已归档或已删除, 不能提交任务", params={"project_id": project_id})
    if task_type not in TASK_TYPES:
        raise InvalidRequestError("未知任务类型", code="TASK-REQ-002", params={"task_type": task_type})
    if task_type == "analysis":
        # 批量分析必须有扫描规格(task_params.sweeps)
        sweeps = (config or {}).get("sweeps")
        if not isinstance(sweeps, list) or not sweeps:
            raise InvalidRequestError(
                "analysis 任务缺少扫描规格(sweeps)",
                params={"task_type": task_type, "expected": "task_config.sweeps 非空数组"},
            )
    if not idempotency_key or not re.fullmatch(_IDEMPOTENCY_KEY_RE, idempotency_key):
        raise InvalidRequestError(
            "任务提交必须提供幂等键(项目作用域, 须匹配 ^[A-Za-z0-9._:-]{1,128}$)",
            code="TASK-REQ-003",
            params={"idempotency_key": idempotency_key},
        )
    if parent_task_id is not None:
        parent = _get_task(db, parent_task_id)
        if parent.project_id != project_id:
            raise InvalidRequestError(
                "父任务不属于该项目",
                code="TASK-REQ-004",
                params={"parent_task_id": parent_task_id, "project_id": project_id},
            )

    # 1) 幂等命中(限定项目范围; 同键返回同一任务, 不再解析 revision)
    existing = tasks_domain.get_task_by_idempotency(db, project_id, idempotency_key)
    if existing is not None:
        return existing, {"replay": True}

    snapshot: CalcSnapshotRecord | None = None
    if task_type in COMPUTE_TYPES:
        # 1b) 计算任务必须明确引用本项目已保存 CalculationConfig revision。
        # 精确引用只解析传入号码, 不读最新指针, 不回退草稿/默认/空字典;
        # 查无(未保存/超出现有/跨项目号码) → 404, 缺失或形态非法 → 400。
        pinned = _resolve_pinned_calculation_config(db, project_id, config)
        # 2) 存储门禁(门禁失败不创建快照不创建任务)
        estimate = estimate_storage(db, project_id, task_type, config)
        if estimate.blocked:
            raise StorageQuotaError(
                "存储空间不足, 任务提交被拒绝",
                params={
                    "need_bytes": estimate.need,
                    "avail_bytes": estimate.avail,
                    "min_pad_bytes": settings.storage_min_free_bytes,
                    "suggestions": estimate.suggestions,
                },
                location={"object_type": "project", "object_id": project_id},
            )
        # 3) 快照装配(相同输入复用既有快照, 见 _assemble_snapshot)
        snapshot = _assemble_snapshot(
            db, project_id, task_type, config=config, user=user,
            calculation_config=pinned, generator_registry=generator_registry,
        )
    # io 类任务(report/export/import/dataset_build): 无快照, 仅幂等键命中复用

    priority = 0
    deadline: datetime | None = None
    if config:
        try:
            priority = int(config.get("priority", 0))
        except (TypeError, ValueError):
            raise InvalidRequestError("priority 须为整数", code="TASK-REQ-005") from None
        raw_deadline = config.get("deadline")
        if raw_deadline is not None:
            try:
                deadline = datetime.fromisoformat(str(raw_deadline).replace("Z", "+00:00"))
            except ValueError:
                raise InvalidRequestError("deadline 须为 ISO 时间", code="TASK-REQ-005") from None

    pool = POOL_BY_TYPE[task_type]
    trace_id = new_id("trc-")
    task = tasks_domain.create_task(
        db,
        project_id=project_id,
        type=task_type,
        requested_by=user.id,
        idempotency_key=idempotency_key,
        calc_snapshot_id=snapshot.id if snapshot is not None else None,
        priority=priority,
        deadline=deadline,
    )
    _write_diagnostic(
        db,
        task.id,
        level=SEVERITY_INFO,
        code=TASK_QUEUED,
        message="任务已排队",
        context={
            "trace_id": trace_id,
            "queue": pool,
            "snapshot_id": task.calc_snapshot_id,
            "parent_task_id": parent_task_id,
        },
    )
    # 5) 入队(可重建视图; 权威事实 = tasks.status='queued')
    tasks_domain.enqueue(
        task.id,
        pool,
        task_type=task_type,
        snapshot_id=task.calc_snapshot_id,
        priority=priority,
        trace_id=trace_id,
    )
    return task, {"replay": False}


def submit_task(
    db: Session,
    user: UserRecord,
    project_id: int,
    task_type: str,
    config: dict[str, Any] | None = None,
    *,
    idempotency_key: str,
    parent_task_id: int | None = None,
    generator_registry: Any | None = None,
) -> tuple[TaskRecord, dict[str, bool]]:
    """提交任务用例(幂等键必填); 本层拥有事务提交/回滚。"""
    try:
        result = _submit_task(
            db, user, project_id, task_type,
            config=config, idempotency_key=idempotency_key, parent_task_id=parent_task_id,
            generator_registry=generator_registry,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 并发槽与领取/租约(复制 services.tasks 语义; 顶层拥有事务)
# ---------------------------------------------------------------------------


def _ensure_slots(db: Session) -> None:
    """惰性初始化槽表(每池 capacity 行、每行容量 1, 幂等)。"""
    tasks_domain.ensure_slots(db, {"compute": settings.compute_slots, "io": settings.io_slots})


def acquire_slot(db: Session, pool_name: str) -> ComputeSlotRecord | None:
    """占用一个并发槽; 无空槽返回 None。只 flush, 不拥有事务。"""
    _ensure_slots(db)
    return tasks_domain.acquire_slot(db, pool_name)


def release_slot(db: Session, attempt_id: int) -> None:
    """释放绑定的并发槽。只 flush, 不拥有事务。"""
    tasks_domain.release_slot(db, attempt_id)


def _claim_task(db: Session, task_id: int, worker_id: str) -> Claim | None:
    """领取任务: 占槽 + 建尝试 + 建租约(发 fencing token) + 任务 running。

    task_attempts(running) / task_leases(active, UUID token) /
    tasks.status='running' / compute_slots.in_use+1 同事务完成。无空槽或任务
    非 queued 时返回 None(任务保持排队)。
    """
    _ensure_slots(db)
    task = _get_task(db, task_id)
    if task.status != "queued":
        return None
    pool = POOL_BY_TYPE.get(task.type, "compute")
    slot = acquire_slot(db, pool)
    if slot is None:
        return None
    attempt = tasks_domain.create_attempt(db, task_id=task.id, worker_id=worker_id)
    token = uuid4()
    tasks_domain.acquire_lease(
        db,
        attempt_id=attempt.id,
        lease_token=str(token),
        acquired_by=worker_id,
        ttl_seconds=LEASE_TTL_SECONDS,
    )
    tasks_domain.set_task_status(db, task.id, "running")
    tasks_domain.bind_slot_attempt(db, slot.id, attempt.id)
    tasks_domain.remove(task.id, pool)  # 领取后出队(视图)
    return Claim(
        task_id=task.id,
        attempt_id=attempt.id,
        attempt_no=attempt.attempt_no,
        lease_token=token,
    )


def claim_task(db: Session, task_id: int, worker_id: str) -> Claim | None:
    """领取任务用例(租约编排); 本层拥有事务提交/回滚。"""
    try:
        result = _claim_task(db, task_id, worker_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _finish_attempt(
    db: Session, task: TaskRecord, status: str, stop_reason: str | None
) -> TaskAttemptRecord | None:
    """收尾当前运行尝试: 尝试终态 + 租约释放/吊销 + 槽释放。"""
    attempt = tasks_domain.get_running_attempt(db, task.id)
    if attempt is None:
        return None
    finished = tasks_domain.finish_attempt(db, attempt.id, status, stop_reason=stop_reason)
    lease = tasks_domain.get_active_lease_for_attempt(db, attempt.id)
    if lease is not None:
        tasks_domain.release_lease(
            db,
            attempt.id,
            lease.lease_token,
            status="released" if status == "succeeded" else "revoked",
        )
    release_slot(db, attempt.id)
    return finished


# ---------------------------------------------------------------------------
# 取消(复制 services.tasks.cancel_task/acknowledge_cancel 语义; 顶层拥有事务)
# ---------------------------------------------------------------------------


def _cancel_task(
    db: Session, task_id: int, reason: str = "user_cancel", actor_id: int | None = None
) -> TaskRecord:
    """取消任务。

    - 终态 → 409 CancelDeniedError;
    - queued(未运行) → 直接 cancelled, 出队;
    - running → cancelling(权威) + 广播取消信号; 批量父任务传播到未完成子任务
      (queued 子任务直接取消, running 子任务进 cancelling);
    - cancelling → 幂等返回。
    """
    task = _get_task(db, task_id)
    if task.status in TERMINAL_STATUSES:
        raise CancelDeniedError(
            "终态任务不可取消",
            params={"task_id": task_id, "status": task.status},
            location={"object_type": "task", "object_id": task_id},
        )
    if task.status == "queued":
        task = tasks_domain.set_task_status(db, task.id, "cancelled")
        tasks_domain.remove(task.id, POOL_BY_TYPE[task.type])
        return task
    if task.status == "cancelling":
        return task  # 取消已发起, 幂等

    task = tasks_domain.set_task_status(db, task.id, "cancelling")
    tasks_domain.set_cancel(task.id, reason)
    # 批量传播: uncertainty 父任务 → 未完成子任务
    children = tasks_domain.list_child_tasks(db, task_id)
    for child in children:
        if child.status in TERMINAL_STATUSES:
            continue
        if child.status == "queued":
            tasks_domain.set_task_status(db, child.id, "cancelled")  # 未运行的子任务直接取消
            tasks_domain.remove(child.id, POOL_BY_TYPE[child.type])
        else:
            tasks_domain.set_task_status(db, child.id, "cancelling")  # 运行中子任务由 Worker 收拢
            tasks_domain.set_cancel(child.id, reason)
    return task


def cancel_task(
    db: Session, task_id: int, reason: str = "user_cancel", actor_id: int | None = None
) -> TaskRecord:
    """取消任务用例; 本层拥有事务提交/回滚。"""
    try:
        result = _cancel_task(db, task_id, reason=reason, actor_id=actor_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _acknowledge_cancel(db: Session, task_id: int) -> TaskRecord:
    """确认取消(Worker 终止子进程后调用; cancelling → cancelled)。

    已产出的样本/证据保留不回滚; 批量父任务在部分子任务已完成时记
    business_outcome=partial_batch。
    """
    task = _get_task(db, task_id)
    if task.status == "cancelled":
        return task
    if task.status != "cancelling":
        raise TaskStateError(
            "任务不在取消中",
            params={"task_id": task_id, "status": task.status},
            location={"object_type": "task", "object_id": task_id},
        )
    attempt = _finish_attempt(db, task, status="stopped", stop_reason="cancelled")
    outcome: str | None = None
    if task.type == "uncertainty":
        completed_children = sum(
            1 for child in tasks_domain.list_child_tasks(db, task.id) if child.status == "completed"
        )
        if completed_children > 0:
            outcome = "partial_batch"
    task = tasks_domain.set_task_status(db, task.id, "cancelled", business_outcome=outcome)
    tasks_domain.clear_cancel(task.id)
    _write_diagnostic(
        db,
        task.id,
        level=SEVERITY_INFO,
        code=TASK_QUEUED,
        message="任务已取消",
        attempt_id=attempt.id if attempt else None,
        context={"business_outcome": outcome},
    )
    return task


def acknowledge_cancel(db: Session, task_id: int) -> TaskRecord:
    """确认取消用例; 本层拥有事务提交/回滚。"""
    try:
        result = _acknowledge_cancel(db, task_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 手动重试(复制 services.tasks.retry_task 语义; 顶层拥有事务)
# ---------------------------------------------------------------------------


def _retry_task(db: Session, user: UserRecord, task_id: int) -> TaskRecord:
    """手动重试(仅终态任务; 计算类须存在快照, 缺失 → TASK-DATA-001 blocking)。

    重试不改变 idempotency_key 语义、不改变快照; 新尝试在下次领取时创建
    (attempt_no 递增, 新租约新 token)。
    """
    task = _get_task(db, task_id)
    ensure_access(db, user, task.project_id, "edit")
    if task.status not in TERMINAL_STATUSES:
        raise TaskStateError(
            "仅终态任务可手动重试",
            params={"task_id": task_id, "status": task.status},
            location={"object_type": "task", "object_id": task_id},
        )
    if task.type in COMPUTE_TYPES and task.calc_snapshot_id is None:
        raise AppError(
            "计算快照缺失, 任务不可复现, 无法重试",
            code=TASK_DATA_SNAPSHOT_MISSING,
            severity=SEVERITY_BLOCKING,
            message_key="ies.diag.task.snapshot_missing",
            params={"task_id": task_id},
            location={"object_type": "task", "object_id": task_id},
        )
    pool = POOL_BY_TYPE[task.type]
    trace_id = new_id("trc-")
    task = tasks_domain.set_task_status(db, task.id, "queued", business_outcome=None)
    _write_diagnostic(
        db,
        task.id,
        level=SEVERITY_INFO,
        code=TASK_QUEUED,
        message="手动重试已排队",
        context={"trace_id": trace_id, "queue": pool, "snapshot_id": task.calc_snapshot_id, "retry": True},
    )
    tasks_domain.enqueue(
        task.id,
        pool,
        task_type=task.type,
        snapshot_id=task.calc_snapshot_id,
        priority=task.priority,
        trace_id=trace_id,
    )
    return task


def retry_task(db: Session, user: UserRecord, task_id: int) -> TaskRecord:
    """手动重试用例; 本层拥有事务提交/回滚。"""
    try:
        result = _retry_task(db, user, task_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
