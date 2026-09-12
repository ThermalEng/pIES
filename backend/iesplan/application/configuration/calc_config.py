"""计算配置用例(application/configuration)。

``api/config.py`` 经本模块访问计算配置，依赖方向
``api → application.configuration.calc_config → {configuration, project,
model, audit} 域公开门面 + application.projects.content_objects``；
不导入 ORM、不导入其他域内部模块、不调用 ``services.*``。

职责（与旧 ``services.config`` 一致）：
- 工作图加载、默认配置生成、配置读写（读用例不提交）；
- ``save_config`` 为顶层事务拥有者（成功 ``db.commit``，失败
  ``db.rollback``；保存同时同步当前草稿内容的 ``calc_config`` 节，
  任务级参数 ``task_params`` 原样保留）；
- ``validate_config`` / ``parameter_metadata`` / ``list_algorithms_meta``
  与 ``row_to_config`` 直用 configuration 域唯一实现（本模块不保留从
  service 复制的映射/校验表）。

本模块不新增校验/哈希/回退。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import configuration as configuration_domain
from iesplan import model as model_domain
from iesplan import project as project_domain
from iesplan.application.projects.content_objects import (
    load_content_object,
    store_content_object,
)
from iesplan.configuration import row_to_config
from iesplan.configuration.contracts import CalcConfigRecord
from iesplan.core.errors import ConflictError, NotFoundError
from iesplan.project.contracts import ProjectRecord

__all__ = [
    "get_config",
    "get_default_config",
    "list_algorithms_meta",
    "load_work_graph",
    "parameter_metadata",
    "row_to_config",
    "save_config",
    "validate_config",
]


def load_work_graph(db: Session, project_id: int) -> dict:
    """加载项目当前工作图(设备清单)。

    优先取 current_draft_id 关联的工作图; 未关联时取该项目最近一张工作图;
    无任何图返回空设备清单(默认配置仍可生成)。
    """
    graph = None
    if project_id is not None:
        proj = project_domain.get_project(db, project_id)
        if proj is not None and proj.current_draft_id is not None:
            graph = model_domain.find_graph_by_draft(db, project_id, proj.current_draft_id)
        if graph is None:
            graph = model_domain.find_latest_working_graph(db, project_id)
    if graph is None:
        return {"devices": []}
    rows = model_domain.list_devices(db, graph.id)
    return {
        "devices": [
            {
                "id": d.id,
                "device_type": d.device_type,
                "kind": d.kind,
                "name": d.name,
                "params": dict(d.params or {}),
            }
            for d in rows
        ]
    }


def get_default_config(db: Session, project_id: int) -> dict:
    """重新生成默认计算配置（不保存；币种按项目币种，缺省 CNY）。"""
    proj = project_domain.get_project(db, project_id)
    currency = proj.currency if proj is not None and proj.currency else "CNY"
    graph = load_work_graph(db, project_id)
    return configuration_domain.build_default_config(graph, currency)


def validate_config(config: dict, graph: dict) -> list:
    """校验计算配置（不保存），直用 configuration 域唯一实现。"""
    return configuration_domain.validate_config(config, graph)


def parameter_metadata(graph: dict) -> dict:
    """参数元数据（单位/范围/帮助键），直用 configuration 域唯一实现。"""
    return configuration_domain.parameter_metadata(graph)


def list_algorithms_meta() -> list[dict]:
    """算法注册表列表，直用 configuration 域唯一实现。"""
    return configuration_domain.list_algorithms_meta()


def _current_draft_revision(db: Session, project_id: int) -> int:
    """当前草稿修订号; 项目尚无草稿时按 1 处理(领域模型 §项目聚合 初始草稿 revision=1)。"""
    proj = project_domain.get_project(db, project_id)
    if proj is None:
        raise NotFoundError(
            f"项目不存在: {project_id}",
            code="RES-MISS-003",
            message_key="ies.diag.res.not_found",
            params={"project_id": project_id},
        )
    if proj.current_draft_id is not None:
        draft = project_domain.get_draft(db, proj.current_draft_id)
        if draft is not None:
            return draft.revision
    return 1


def _sync_draft_config(
    db: Session,
    proj: ProjectRecord,
    config: dict,
    row: CalcConfigRecord,
) -> None:
    """把已保存配置同步进当前草稿内容的 calc_config 节(不递增草稿修订)。

    计算快照装配与项目包导出以草稿内容为权威输入; 若不同步, 保存的配置
    不会进入快照/导出包(配置语义丢失)。任务级参数(task_params)属于任务
    提交时的覆盖项, 原样保留不覆盖。
    """
    if proj.current_draft_id is None:
        return
    draft = project_domain.get_draft(db, proj.current_draft_id)
    if draft is None:
        return
    # 内容对象缺失或损坏时直接抛出加载原错误, 不回退初始骨架
    # (宪法 §13: 对象缺失或不可读返回实际错误, 禁止旧副本回退)。
    content = load_content_object(db, draft.content_object_id)
    old_calc = content.get("calc_config") or {}
    content["calc_config"] = {
        "params": dict(config.get("parameters") or {}),
        "variables": list(config.get("variables") or []),
        "objectives": list(config.get("objectives") or []),
        "constraints": list(config.get("constraints") or []),
        "algorithm": dict(config.get("algorithm") or {}),
        "solver": row.solver,
        "tolerances": dict(config.get("tolerances") or {}),
        "random_seed": config.get("random_seed"),
        "irr_floor": config.get("irr_floor"),
    }
    if isinstance(old_calc.get("task_params"), dict):
        content["calc_config"]["task_params"] = old_calc["task_params"]
    content_object_id = store_content_object(db, content)
    project_domain.update_draft_content_ref(db, draft.id, content_object_id)


def save_config(
    db: Session,
    project_id: int,
    config: dict,
    expected_revision: int,
    *,
    user_id: int | None = None,
) -> CalcConfigRecord:
    """保存计算配置(与草稿修订绑定, 乐观锁; 冻结行则新建版本行, 经 configuration 域)。

    参数:
        db: 数据库会话。
        project_id: 项目 id。
        config: 计算配置 dict。
        expected_revision: 期望的草稿修订号; 与实际修订不符抛 ConflictError。
        user_id: 修改者; None 时回退到项目 owner(认证接线前的兼容路径)。

    返回:
        保存后的 CalcConfig 行。
    """
    proj = project_domain.get_project(db, project_id)
    if proj is None:
        raise NotFoundError(
            f"项目不存在: {project_id}",
            code="RES-MISS-003",
            message_key="ies.diag.res.not_found",
            params={"project_id": project_id},
        )
    current_revision = _current_draft_revision(db, project_id)
    if expected_revision != current_revision:
        raise ConflictError(
            f"草稿修订冲突: 期望 {expected_revision}, 当前 {current_revision}",
            params={
                "expected_revision": expected_revision,
                "current_revision": current_revision,
            },
        )
    config = configuration_domain.normalize_config(config)
    existing = [
        c
        for c in configuration_domain.list_calc_configs(db, project_id)
        if c.name == configuration_domain.DEFAULT_CONFIG_NAME
    ]
    latest = max(existing, key=lambda c: c.version, default=None)
    if latest is not None and latest.status == "frozen":
        latest = None  # 冻结行不可修改(01 §6.1 触发器语义), 新建版本行
    algo_mode = config.get("algorithm", {}).get("mode", "auto")
    algo_name = config.get("algorithm", {}).get("name")
    values = {
        "params": config["parameters"],
        "variables": config["variables"],
        "objectives": config["objectives"],
        "constraints": config["constraints"],
        "min_irr": config.get("irr_floor"),
        "algorithm": None
        if algo_mode == "auto"
        else configuration_domain.ALGO_DB_CLASS.get(algo_name or "", algo_name),
        "solver": configuration_domain.SOLVER_ID,
        "tolerances": config.get("tolerances", {}),
        "random_seed": config.get("random_seed"),
    }
    actor = user_id or proj.owner_id
    try:
        if latest is None:
            row = configuration_domain.create_calc_config(
                db,
                project_id=project_id,
                name=configuration_domain.DEFAULT_CONFIG_NAME,
                params=values["params"],
                variables=values["variables"],
                objectives=values["objectives"],
                constraints=values["constraints"],
                tolerances=values["tolerances"],
                updated_by=actor,
                min_irr=values["min_irr"],
                algorithm=values["algorithm"],
                solver=values["solver"],
                random_seed=values["random_seed"],
            )
        else:
            row = configuration_domain.update_calc_config(
                db, latest.id, values=values, updated_by=actor
            )
        # 0.2.0 B4: 配置保存属"项目/数据/计算配置"关键变更(宪法 §16), 保留不可变
        # 最小化脱敏审计(只记版本/变量数/目标/算法, 不复制完整配置)
        audit_domain.append_entry(
            db,
            actor_id=user_id or proj.owner_id,
            action="config.saved",
            entity_type="calc_config",
            entity_id=row.id,
            actor_type="user",
            extra={
                "project_id": project_id,
                "version": row.version,
                "status": row.status,
                "variables": len(config.get("variables") or []),
                "objectives": len(config.get("objectives") or []),
                "constraints": len(config.get("constraints") or []),
                "algorithm": row.algorithm,
                "random_seed": config.get("random_seed"),
            },
        )
        # 同步当前草稿内容的 calc_config 节(快照装配/项目包导出以草稿内容为
        # 权威输入, 不更新则保存的配置不进入计算快照与导出包)
        _sync_draft_config(db, proj, config, row)
        db.commit()
    except Exception:
        db.rollback()
        raise
    # row 为 configuration 域记录(已物化值对象), 无需 ORM refresh。
    return row


def get_config(db: Session, project_id: int) -> dict:
    """读取当前计算配置(未保存时返回生成的默认配置, 不带版本)。

    返回:
        {"config": dict, "meta": dict, "version": int|None, "status": str, "updated_at": str|None}
    """
    if project_domain.get_project(db, project_id) is None:
        raise NotFoundError(
            f"项目不存在: {project_id}",
            code="RES-MISS-003",
            message_key="ies.diag.res.not_found",
            params={"project_id": project_id},
        )
    graph = load_work_graph(db, project_id)
    meta = configuration_domain.parameter_metadata(graph)
    configs = [
        c
        for c in configuration_domain.list_calc_configs(db, project_id)
        if c.name == configuration_domain.DEFAULT_CONFIG_NAME
    ]
    row = max(configs, key=lambda c: c.version, default=None)
    if row is None:
        return {
            "config": get_default_config(db, project_id),
            "meta": meta,
            "version": None,
            "status": "draft",
            "updated_at": None,
        }
    return {
        "config": configuration_domain.row_to_config(row),
        "meta": meta,
        "version": row.version,
        "status": row.status,
        "updated_at": row.updated_at,
    }
