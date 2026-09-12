"""项目校验用例(Wave 2 W2-A: application/validations)。

从 ``iesplan.services.validation`` 复制的 U07 项目校验流程（计算前完整预检
与财务基准确认），旧服务保留未删（待 Wave 3 接入、Wave 5 删除）。

复制来源（基线 5c40b01 ``services/validation.py``）：
- ``validate_project``（聚合模型完整性/参数变量/数据绑定与质量/配置兼容与
  IRR 硬约束/财务基准确认/计算就绪，一次返回全部诊断）；
- ``mark_baseline_confirmed``（财务基准确认审计追加）；
- ``store_validation_report`` / ``get_latest_validation_report``
  （校验报告对象存储持久化与读取）。

改接说明（行为一致，仅换调用方向）：
- 旧 ``dataset_service.put_object/add_object_ref/get_object_bytes`` 改经
  storage 公开门面（``put_object`` / ``add_ref`` / ``get_object``）；
- 旧 ``project_service.get_current_draft_content`` 改经 project 域公开门面
 （``get_current_draft``）+ storage 门面读内容文档；
- 其余 project / dataset / audit 域调用本就经域公开门面，保持不变。

遗留 services 调用（待协调者统一改接，见模块末尾 ``LEGACY_SERVICE_CALLS``）：
- ``services.model.validate_project_model`` / ``services.model.get_graph``
  （模型能力，W2-B 搬入 application/models 后改接）；
- ``services.config.get_config`` / ``services.config.load_work_graph`` /
  ``services.config.validate_config``（计算配置读写与校验能力，仍在旧
  services.config，为避免在应用层重复实现其领域校验逻辑，待后续波次搬移后改接）。

事务：写用例（``mark_baseline_confirmed`` / ``store_validation_report``）
顶层函数拥有提交/回滚；``validate_project`` 与读取函数为只读，不提交事务。

调用方向：``api → application.validations.precheck → {project, dataset,
audit, storage} 域公开门面 + devices 公开门面``；不导入 ORM、不导入其他域
内部模块。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import dataset as dataset_domain
from iesplan import project as project_domain
from iesplan.application import models as model_service
from iesplan.audit.contracts import AuditRecord
from iesplan.core.diagnostics import (
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import AppError, NotFoundError
from iesplan.devices import DeviceModelDocument as DeviceTypeSpec
from iesplan.devices import get_device as get_device_type
from iesplan.identity.contracts import UserRecord
from iesplan.project.contracts import ProjectRecord
from iesplan.services import config as config_service
from iesplan.storage import add_ref, find_refs_by_owner, get_object, put_object

#: 遗留 services 调用点：
#: - services.config.get_config / services.config.load_work_graph /
#:   services.config.validate_config → 待计算配置能力搬移（后续波次）。
#: （模型能力已改接 application.models，见 Wave 2 集成。）
LEGACY_SERVICE_CALLS: tuple[str, ...] = (
    "iesplan.services.config.get_config",
    "iesplan.services.config.load_work_graph",
    "iesplan.services.config.validate_config",
)

# ---------------------------------------------------------------------------
# 诊断码(本单元新增, 导入时登记; 04 目录未登记, 见 NEW_DIAG_CODES 扩展模式)
# ---------------------------------------------------------------------------

VALID_MODEL_NO_GRID = "VALID-MODEL-001"  # 模型缺少电网连接
VALID_MODEL_NO_LOAD = "VALID-MODEL-002"  # 模型缺少负荷
VALID_MODEL_NO_SUPPLY = "VALID-MODEL-003"  # 有负荷的载体缺少供给设备
VALID_CONFIG_NOT_SAVED = "VALID-CONFIG-001"  # 计算配置未保存(使用默认配置)
VALID_CONFIG_NO_IRR = "VALID-CONFIG-002"  # 缺少最低 IRR 硬约束
VALID_DATA_NO_BINDING = "VALID-DATA-001"  # 未绑定任何数据集版本
VALID_DATA_VERSION_INVALID = "VALID-DATA-002"  # 数据集版本质量阻断
VALID_DATA_BINDING_BROKEN = "VALID-DATA-004"  # 绑定的版本缺失/已删除/不属于本项目
VALID_FIN_NO_CONFIRM = "VALID-FIN-001"  # 缺少财务基准确认证据
VALID_FIN_STALE = "VALID-FIN-002"  # 财务基准确认内容与当前配置不一致
VALID_READY_NOT_ASSEMBLABLE = "VALID-READY-001"  # 计算快照不可组装


def _register_diag_codes() -> None:
    """登记本单元新增诊断码到共享诊断目录(幂等, 与 U05 数据集单元同模式)。"""
    from iesplan.core import diagnostics as diag_mod

    codes = {
        VALID_MODEL_NO_GRID: "模型缺少电网连接(至少 1 个)",
        VALID_MODEL_NO_LOAD: "模型缺少负荷(至少 1 个)",
        VALID_MODEL_NO_SUPPLY: "有负荷的载体缺少供给设备",
        VALID_CONFIG_NOT_SAVED: "计算配置未保存, 预检使用自动生成的默认配置",
        VALID_CONFIG_NO_IRR: "缺少最低 IRR 硬约束(REQ-CALC-006)",
        VALID_DATA_NO_BINDING: "项目未绑定任何数据集版本",
        VALID_DATA_VERSION_INVALID: "所选数据集版本存在阻断性质量问题",
        VALID_DATA_BINDING_BROKEN: "绑定的数据集版本缺失/已删除或不属于本项目",
        VALID_FIN_NO_CONFIRM: "缺少财务基准确认证据(架构宪法 §16 安全与审计)",
        VALID_FIN_STALE: "财务基准确认内容与当前配置不一致, 需重新确认",
        VALID_READY_NOT_ASSEMBLABLE: "计算快照不可组装",
    }
    for code, desc in codes.items():
        diag_mod.NEW_DIAG_CODES.setdefault(code, desc)
    for code, key in {
        VALID_MODEL_NO_GRID: "ies.diag.valid.model_no_grid",
        VALID_MODEL_NO_LOAD: "ies.diag.valid.model_no_load",
        VALID_MODEL_NO_SUPPLY: "ies.diag.valid.model_no_supply",
        VALID_CONFIG_NOT_SAVED: "ies.diag.valid.config_not_saved",
        VALID_CONFIG_NO_IRR: "ies.diag.valid.config_no_irr",
        VALID_DATA_NO_BINDING: "ies.diag.valid.data_no_binding",
        VALID_DATA_VERSION_INVALID: "ies.diag.valid.data_version_invalid",
        VALID_DATA_BINDING_BROKEN: "ies.diag.valid.data_binding_broken",
        VALID_FIN_NO_CONFIRM: "ies.diag.valid.fin_no_confirm",
        VALID_FIN_STALE: "ies.diag.valid.fin_stale",
        VALID_READY_NOT_ASSEMBLABLE: "ies.diag.valid.ready_not_assemblable",
    }.items():
        diag_mod.DIAG_MESSAGE_KEYS.setdefault(code, key)
    for code, hint in {
        VALID_MODEL_NO_GRID: "ies.fix.valid.model_add_grid",
        VALID_MODEL_NO_LOAD: "ies.fix.valid.model_add_load",
        VALID_MODEL_NO_SUPPLY: "ies.fix.valid.model_add_supply",
        VALID_CONFIG_NOT_SAVED: "ies.fix.valid.config_save",
        VALID_CONFIG_NO_IRR: "ies.fix.valid.config_set_irr",
        VALID_DATA_NO_BINDING: "ies.fix.valid.data_bind",
        VALID_DATA_VERSION_INVALID: "ies.fix.valid.data_fix_version",
        VALID_DATA_BINDING_BROKEN: "ies.fix.valid.data_fix_binding",
        VALID_FIN_NO_CONFIRM: "ies.fix.valid.fin_confirm",
        VALID_FIN_STALE: "ies.fix.valid.fin_reconfirm",
        VALID_READY_NOT_ASSEMBLABLE: "ies.fix.valid.ready_check",
    }.items():
        diag_mod.DIAG_FIX_HINT_KEYS.setdefault(code, hint)


_register_diag_codes()

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 财务基准确认审计动作(追加式, 不可覆盖; 架构宪法 §16 安全与审计 + domain-model §对象生命周期 确认证据)
BASELINE_ACTION: str = "project.baseline_confirmed"

#: 电网连接注册表类型 id(模型完整性检查)
GRID_TYPE_ID: str = "ies.device.grid_connection"

#: 载体 → 端口类型(与 application.models.CARRIER_PORT_TYPE 同值，本地声明避免跨层导入)
_CARRIER_PORT_TYPE: dict[str, str] = {
    "electricity": "electric",
    "heat": "thermal",
    "cool": "cooling",
}

#: 校验报告对象媒体类型(01 §10.1)
_REPORT_MEDIA_TYPE: str = "application/json"


# ---------------------------------------------------------------------------
# 报告结构
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ValidationReport:
    """项目预检报告(REQ-CALC-007 校验门禁输出)。

    status: 'ok' | 'warnings' | 'blocked'(存在阻断错误时 blocked);
    blocks_submit: 是否禁止提交(任一 error/blocking 诊断即 True);
    diagnostics: 全部诊断(一次返回全部问题, warning 不降级阻断)。
    """

    status: str
    diagnostics: list[Diagnostic]
    blocks_submit: bool
    project_id: str = ""
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    source: str = "iesplan.services.validation"

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容字典(诊断字段与 04 §5.4 对齐)。"""
        summary = {"blocking": 0, "error": 0, "warning": 0, "info": 0}
        for d in self.diagnostics:
            if d.blocking:
                summary["blocking"] += 1
            elif d.severity == SEVERITY_ERROR:
                summary["error"] += 1
            elif d.severity == SEVERITY_WARNING:
                summary["warning"] += 1
            elif d.severity == SEVERITY_INFO:
                summary["info"] += 1
        return {
            "status": self.status,
            "blocks_submit": self.blocks_submit,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "summary": summary,
            "project_id": self.project_id,
            "generated_at": self.generated_at,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# 完整预检
# ---------------------------------------------------------------------------


def validate_project(db: Session, project_id: int, include_data: bool = True) -> ValidationReport:
    """执行项目完整预检(REQ-CALC-007 校验门禁), 一次返回全部问题。

    聚合检查: 模型完整性/拓扑、参数与变量、数据绑定与质量、配置兼容与 IRR 硬约束、
    财务基准确认、计算就绪(快照可组装)。

    参数:
        db: 数据库会话。
        project_id: 项目 id(不存在或已删除抛 NotFoundError)。
        include_data: 是否包含数据检查(任意方案评价等场景可跳过大数据项)。

    返回:
        ValidationReport: 诊断一次返回全部; error/blocking 级阻断提交,
        warning 不降级阻断。
    """
    project = _require_project(db, project_id)
    # 配置与设备图各只读一次(未保存时配置为生成的默认配置), 供检查项共用
    config_data = config_service.get_config(project_id, db)
    graph = config_service.load_work_graph(db, project_id)
    diags: list[Diagnostic] = []
    _check_model(db, project_id, diags)
    _check_config(project_id, config_data, graph, diags)
    if include_data:
        _check_data(db, project, diags)
    _check_financial_baseline(db, project, config_data, diags)
    _check_readiness(project, diags)
    # 统一标记: 阻断错误保持阻断(与严重度一致), 警告不降级也不升级。
    # Diagnostic 为深度不可变类型, 上下文/阻断标记经 replace 生成新对象。
    marked: list[Diagnostic] = []
    for d in diags:
        updated = d.with_context(project_id=str(project_id))
        if d.severity in (SEVERITY_BLOCKING, SEVERITY_ERROR) and not updated.blocking:
            updated = updated.replace(blocking=True)
        marked.append(updated)
    diags = marked
    blockers = [d for d in diags if d.blocking]
    if blockers:
        status = "blocked"
    elif any(d.severity == SEVERITY_WARNING for d in diags):
        status = "warnings"
    else:
        status = "ok"
    return ValidationReport(
        status=status,
        diagnostics=diags,
        blocks_submit=bool(blockers),
        project_id=str(project_id),
    )


def _require_project(db: Session, project_id: int) -> ProjectRecord:
    """按 id 取项目; 不存在或已删除(软删)一律 404(与 U03 语义一致)。"""
    project = project_domain.get_project(db, project_id)
    if project is None:
        raise NotFoundError(
            f"项目不存在: {project_id}",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": str(project_id)},
        )
    return project


def _device_spec(dev: dict) -> DeviceTypeSpec | None:
    """设备序列化 → 注册表规格(设备类型未注册返回 None, 供完整性判断)。"""
    type_id = dev.get("device_type")
    if not isinstance(type_id, str) or not type_id:
        return None
    try:
        return get_device_type(type_id)
    except NotFoundError:
        return None


# ---------------------------------------------------------------------------
# 检查项 a: 模型完整性 + 拓扑 + 设备参数
# ---------------------------------------------------------------------------


def _check_model(db: Session, project_id: int, diags: list[Diagnostic]) -> None:
    """模型完整性: 拓扑/设备参数(U04 诊断) + 电网连接/负荷/载体供给(本单元检查)。"""
    # 拓扑 + 每台设备参数校验(复用 U04; error 级即阻断)
    diags.extend(model_service.validate_project_model(db, project_id))
    graph = model_service.get_graph(db, project_id)
    devices = graph.get("devices", [])
    ports = graph.get("ports", [])
    loc = {"object_type": "project", "object_id": str(project_id)}
    # 至少 1 个电网连接(供能接口, 架构宪法 §12 快照任务与结果 + domain-model §项目聚合)
    if not any(d.get("device_type") == GRID_TYPE_ID for d in devices):
        diags.append(
            make_diag(
                VALID_MODEL_NO_GRID,
                severity=SEVERITY_ERROR,
                params={"project_id": project_id},
                location=loc,
            )
        )
    # 至少 1 个负荷
    has_load = False
    for d in devices:
        spec = _device_spec(d)
        if spec is not None and any(i.type == "predefined" for i in spec.interfaces.values()):
            has_load = True
            break
    if not has_load:
        diags.append(
            make_diag(
                VALID_MODEL_NO_LOAD,
                severity=SEVERITY_ERROR,
                params={"project_id": project_id},
                location=loc,
            )
        )
    # 电/热/冷每个有负荷的载体必须有供给设备(出方向端口, 双向不计)
    load_carriers: set[str] = set()
    for d in devices:
        spec = _device_spec(d)
        if spec is not None and any(i.type == "predefined" for i in spec.interfaces.values()):
            load_carriers.update(
                i.carrier for i in spec.interfaces.values() if i.carrier in _CARRIER_PORT_TYPE
            )
    supply_carriers: set[str] = set()
    for p in ports:
        if p.get("direction") == "out":
            for carrier, ptype in _CARRIER_PORT_TYPE.items():
                if p.get("port_type") == ptype:
                    supply_carriers.add(carrier)
    for carrier in sorted(load_carriers - supply_carriers):
        diags.append(
            make_diag(
                VALID_MODEL_NO_SUPPLY,
                severity=SEVERITY_ERROR,
                params={"carrier": carrier, "project_id": project_id},
                location={"object_type": "system_graph", "field": f"carrier:{carrier}"},
            )
        )


# ---------------------------------------------------------------------------
# 检查项 b/d: 参数/变量/目标/约束/算法兼容(复用 U06)
# ---------------------------------------------------------------------------


def _check_config(project_id: int, config_data: dict, graph: dict, diags: list[Diagnostic]) -> None:
    """计算配置: 参数当前值/变量初始值与界内/目标/约束/算法兼容(validate_config)。

    参数:
        project_id: 项目 id(诊断定位)。
        config_data: get_config 的返回 {"config", "meta", "version", "status", ...};
            由 validate_project 一次性读取, 避免重复加载。
        graph: load_work_graph 的设备清单(validate_config 的参数/变量校验输入)。
    """
    config = config_data.get("config") or {}
    loc = {"object_type": "config", "object_id": ""}
    if config_data.get("version") is None:
        # 未保存过配置: 预检使用自动生成的默认配置, 给出警告
        diags.append(
            make_diag(
                VALID_CONFIG_NOT_SAVED,
                severity=SEVERITY_WARNING,
                params={"project_id": project_id, "reason_code": "using_defaults"},
                location=loc,
            )
        )
    # 最低 IRR 硬约束(REQ-CALC-006: 独立顶层字段, 不可被目标权重抵消)
    if config.get("irr_floor") is None:
        diags.append(
            make_diag(
                VALID_CONFIG_NO_IRR,
                severity=SEVERITY_ERROR,
                params={"project_id": project_id},
                location={**loc, "field": "irr_floor"},
            )
        )
    # 参数/变量/目标/约束/算法能力(REQ-CALC-002/004/005: 错误级阻断)
    diags.extend(config_service.validate_config(config, graph))


# ---------------------------------------------------------------------------
# 检查项 c: 数据绑定与版本质量
# ---------------------------------------------------------------------------


def _check_data(db: Session, project: ProjectRecord, diags: list[Diagnostic]) -> None:
    """数据集: 当前草稿至少绑定一个版本; 绑定版本有效且属于本项目; UTC 偏移一致。

    绑定来源以当前草稿内容文档的 dataset_bindings 为权威(U03 dataset.bind),
    不推断"项目名下数据集的最新版本"——那可能与项目实际计算输入不一致。
    质量报告缺失/结构损坏一律按阻断处理(fail-closed), 不产生 500。
    """
    bindings = _load_dataset_bindings(db, project.id)
    if not bindings:
        diags.append(
            make_diag(
                VALID_DATA_NO_BINDING,
                severity=SEVERITY_ERROR,
                params={"project_id": project.id},
                location={
                    "object_type": "project",
                    "object_id": str(project.id),
                    "field": "dataset_bindings",
                },
            )
        )
        return
    version_ids = [b["dataset_version_id"] for b in bindings if isinstance(b, dict)]
    rows = dataset_domain.list_versions_by_ids(db, version_ids)
    by_id = {v.id: v for v in rows}
    project_dataset_ids = set(dataset_domain.list_dataset_ids(db, project.id))
    for binding in bindings:
        if not isinstance(binding, dict) or not isinstance(binding.get("dataset_version_id"), int):
            continue  # 内容损坏的绑定条目由草稿内容校验负责, 不在此重复报
        version_id = binding["dataset_version_id"]
        version = by_id.get(version_id)
        loc = {
            "object_type": "dataset_version",
            "object_id": str(version_id),
            "field": "quality_report",
        }
        if version is None:
            diags.append(
                make_diag(
                    VALID_DATA_BINDING_BROKEN,
                    severity=SEVERITY_ERROR,
                    params={
                        "dataset_version_id": version_id,
                        "reason_code": "version_missing",
                    },
                    location=loc,
                )
            )
            continue
        if version.dataset_id not in project_dataset_ids:
            diags.append(
                make_diag(
                    VALID_DATA_BINDING_BROKEN,
                    severity=SEVERITY_ERROR,
                    params={
                        "dataset_version_id": version_id,
                        "reason_code": "foreign_version",
                        "dataset_id": version.dataset_id,
                    },
                    location=loc,
                )
            )
            continue
        # 质量报告缺失/结构损坏 → fail-closed 阻断(无质量证据视为无效输入)
        report = version.quality_report
        if not isinstance(report, dict):
            diags.append(
                make_diag(
                    VALID_DATA_VERSION_INVALID,
                    severity=SEVERITY_ERROR,
                    params={
                        "dataset_id": version.dataset_id,
                        "version_no": version.version_no,
                        "reason_code": "quality_report_missing",
                    },
                    location=loc,
                )
            )
            continue
        blocking_codes = _quality_blocking_codes(report)
        if report.get("has_blocking_errors") or blocking_codes:
            diags.append(
                make_diag(
                    VALID_DATA_VERSION_INVALID,
                    severity=SEVERITY_ERROR,
                    params={
                        "dataset_id": version.dataset_id,
                        "version_no": version.version_no,
                        "codes": blocking_codes,
                    },
                    location=loc,
                )
            )


def _current_draft_content(db: Session, project_id: int) -> dict:
    """当前草稿内容文档(经 project 域公开门面取草稿 + storage 门面读对象)。

    旧 services.project.get_current_draft_content 的同语义改接实现；
    命令簿记不外泄。
    """
    project = _require_project(db, project_id)
    draft = project_domain.get_current_draft(db, project.id)
    if draft is None:
        raise AppError(
            "项目缺少当前草稿(数据损坏)",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
            location={"object_type": "project", "object_id": project.id},
        )
    raw = get_object(db, draft.content_object_id)
    try:
        content = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppError(
            "内容对象解析失败(数据损坏)",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
        ) from exc
    if not isinstance(content, dict):
        raise AppError(
            "内容对象结构非法(数据损坏)",
            code="SYS-STORE-001",
            message_key="ies.diag.store.corrupt",
        )
    content.pop("applied_commands", None)
    return content


def _load_dataset_bindings(db: Session, project_id: int) -> list[dict]:
    """当前草稿内容文档的 dataset_bindings(U03 dataset.bind 写入的权威绑定来源)。"""
    content = _current_draft_content(db, project_id)
    bindings = content.get("dataset_bindings")
    if not isinstance(bindings, list):
        return []
    return [b for b in bindings if isinstance(b, dict)]


def _quality_blocking_codes(report: dict) -> list[str]:
    """质量报告 → 阻断诊断码列表(severity 为 error/blocking 或 blocking 标志均计入)。"""
    entries = report.get("diagnostics")
    if not isinstance(entries, list):
        return ["QUALITY-REPORT-STRUCT"]
    codes: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            codes.append("QUALITY-ENTRY-INVALID")
            continue
        severity = entry.get("severity")
        if entry.get("blocking") or severity in (SEVERITY_BLOCKING, SEVERITY_ERROR):
            codes.append(str(entry.get("code") or "UNKNOWN"))
    return codes


# ---------------------------------------------------------------------------
# 检查项 e: 财务基准确认(确认人 + 确认内容完整性校验)
# ---------------------------------------------------------------------------


def _latest_baseline_evidence(db: Session, project_id: int) -> AuditRecord | None:
    """最近一次财务基准确认证据(追加式审计, 按 id 倒序取最新)。"""
    rows = audit_domain.list_entries(
        db,
        entity_type="project",
        entity_id=project_id,
        action=BASELINE_ACTION,
        limit=1,
    )
    return rows[0] if rows else None


def _current_assumptions(project: ProjectRecord, config: dict) -> dict:
    """从当前配置导出财务基准关键假设(与确认时记录的键集合一致)。"""
    econ = (config.get("parameters") or {}).get("economic") or {}
    return {
        "discount_rate": econ.get("discount_rate"),
        "tax_rate": econ.get("tax_rate"),
        "project_years": econ.get("project_years"),
        "depreciation_years": econ.get("depreciation_years"),
        "currency": project.currency or econ.get("currency"),
        "irr_floor": config.get("irr_floor"),
    }


def _check_financial_baseline(
    db: Session, project: ProjectRecord, config_data: dict, diags: list[Diagnostic]
) -> None:
    """财务基准确认证据检查(架构宪法 §16 安全与审计): 确认人齐全且确认内容
    与当前配置一致即通过; 不一致 → VALID-FIN-002 警告(直接比对原文, 无摘要)。"""
    loc = {
        "object_type": "project",
        "object_id": str(project.id),
        "field": "baseline_confirmation",
    }
    evidence = _latest_baseline_evidence(db, project.id)
    if evidence is None:
        diags.append(
            make_diag(
                VALID_FIN_NO_CONFIRM,
                severity=SEVERITY_ERROR,
                params={"project_id": project.id, "reason_code": "no_evidence"},
                location=loc,
            )
        )
        return
    after = evidence.after or {}
    if not after.get("confirmed_by"):
        diags.append(
            make_diag(
                VALID_FIN_NO_CONFIRM,
                severity=SEVERITY_ERROR,
                params={"project_id": project.id, "reason_code": "incomplete_evidence"},
                location=loc,
            )
        )
        return
    confirmed = after.get("assumptions") or {}
    config = config_data.get("config") or {}
    current = _current_assumptions(project, config)
    if confirmed != current:
        diags.append(
            make_diag(
                VALID_FIN_STALE,
                severity=SEVERITY_WARNING,
                params={"project_id": project.id},
                location=loc,
            )
        )
        return


# ---------------------------------------------------------------------------
# 检查项 f: 计算就绪(快照可组装)
# ---------------------------------------------------------------------------


def _check_readiness(project: ProjectRecord, diags: list[Diagnostic]) -> None:
    """计算就绪: 项目活动且快照可组装(项目版本存在或草稿可固化)。"""
    loc = {"object_type": "project", "object_id": str(project.id), "field": "snapshot_assembly"}
    if project.status != "active":
        diags.append(
            make_diag(
                VALID_READY_NOT_ASSEMBLABLE,
                severity=SEVERITY_ERROR,
                params={
                    "project_id": project.id,
                    "status": project.status,
                    "reason_code": "project_not_active",
                },
                location=loc,
            )
        )
        return
    if project.current_version_id is None and project.current_draft_id is None:
        diags.append(
            make_diag(
                VALID_READY_NOT_ASSEMBLABLE,
                severity=SEVERITY_ERROR,
                params={"project_id": project.id, "reason_code": "no_draft_or_version"},
                location=loc,
            )
        )


# ---------------------------------------------------------------------------
# 财务基准确认写入(证据由 U11 消费; 本单元只记录, 不覆盖)
# ---------------------------------------------------------------------------


def _bad_assumptions(**params: Any) -> AppError:
    """假设内容校验失败的应用错误(HTTP 400, 属客户端输入问题而非服务故障)。"""
    err = AppError(
        "",
        code="VALID-FIN-003",
        message_key="ies.diag.valid.bad_assumptions",
        params=params,
    )
    err.http_status = 400
    return err


def _mark_baseline_confirmed(
    db: Session,
    project_id: int,
    user: UserRecord,
    assumptions: dict | None = None,
) -> AuditRecord:
    """记录财务基准确认(只 flush，不提交)。

    证据以审计事件追加式记录(不可覆盖, 架构宪法 §16 + domain-model §对象生命周期), 供 U11 指标单元与校验门禁读取。

    参数:
        db: 数据库会话。
        project_id: 项目 id(不存在或已删除抛 NotFoundError)。
        user: 确认人(记录 id 与确认时间)。
        assumptions: 可选的假设原文, 一并记录便于复核与前端回显。
    返回:
        新增的审计记录。
    """
    project = _require_project(db, project_id)
    now = datetime.now(UTC)
    return audit_domain.append_entry(
        db,
        actor_id=user.id if user is not None else None,
        action=BASELINE_ACTION,
        entity_type="project",
        entity_id=project_id,
        actor_type="user",
        extra={
            "assumptions": dict(assumptions or {}),
            "confirmed_by": user.id if user is not None else None,
            "confirmed_at": now.isoformat(),
            "project_version_id": project.current_version_id,
        },
    )


def mark_baseline_confirmed(
    db: Session,
    project_id: int,
    user: UserRecord,
    assumptions: dict | None = None,
) -> AuditRecord:
    """事务型财务基准确认；application 层统一提交或回滚。"""
    try:
        record = _mark_baseline_confirmed(db, project_id, user, assumptions)
        db.commit()
        return record
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 校验报告持久化(对象存储对象 + ref_type='report' 引用；经 storage 公开门面)
# ---------------------------------------------------------------------------


def _store_validation_report(db: Session, project_id: int, report: ValidationReport) -> dict:
    """校验报告落为对象存储对象并登记引用(ref_type='report'), 返回摘要（只 flush，不提交）。

    报告内 project_id 与入参不一致时拒绝(防止跨项目错挂报告)。
    """
    if report.project_id and report.project_id != str(project_id):
        raise AppError(
            "校验报告与项目不匹配",
            code="VALID-FIN-003",
            message_key="ies.diag.valid.report_mismatch",
            params={"report_project_id": report.project_id, "project_id": project_id},
        )
    raw = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    obj = put_object(
        db, raw, _REPORT_MEDIA_TYPE, source_category="dataset"
    )
    add_ref(
        db, obj.id, "report", project_id, ref_entity_type="project", purpose="项目校验报告"
    )
    db.flush()
    return {"object_id": obj.id}


def store_validation_report(db: Session, project_id: int, report: ValidationReport) -> dict:
    """事务型校验报告持久化；application 层统一提交或回滚。"""
    try:
        result = _store_validation_report(db, project_id, report)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def get_latest_validation_report(db: Session, project_id: int) -> dict | None:
    """读取项目最近一次持久化的校验报告(STO-05: 经公开门面查引用与读对象)。"""
    refs = find_refs_by_owner(db, "report", project_id, ref_entity_type="project")
    if not refs:
        return None
    try:
        raw = get_object(db, int(refs[0]["object_id"]))
    except Exception:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "BASELINE_ACTION",
    "LEGACY_SERVICE_CALLS",
    "ValidationReport",
    "get_latest_validation_report",
    "mark_baseline_confirmed",
    "store_validation_report",
    "validate_project",
]
