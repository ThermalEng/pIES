"""项目校验服务(U07 项目校验单元): 计算前完整预检与财务基准确认。

对应 RPD 第 9.3 节(任意方案评价)、17.5.7 REQ-CALC-007(校验门禁)、
10.2(财务基准确认)与 01-db-schema.md 第 10 节(审计/对象)。
U07 只聚合只读证据, 不修改模型/数据/配置(21.2):

- 模型完整性: 至少 1 个电网连接与 1 个负荷; 电/热/冷每个有负荷的载体都有供给设备;
  拓扑与设备参数校验复用 U04(services.model.validate_project_model);
- 参数/变量: 复用 U06(config.validate_config)校验参数当前值(范围/单位)与
  变量初始值/边界(REQ-CALC-002);
- 数据: 当前草稿至少绑定一个数据集版本(dataset_bindings 为权威绑定来源),
  绑定的版本存在且属于本项目, 版本质量报告无阻断错误(报告缺失/损坏一律按阻断
  fail-closed 处理), UTC 偏移与项目一致;
- 配置: 目标/约束/算法兼容(config.validate_config), 最低 IRR 硬约束存在(REQ-CALC-006);
- 财务基准确认(RPD 10.2): 必须有用户确认证据(确认人/确认内容完整性校验值),
  确认经 audit_log 追加式记录(mark_baseline_confirmed), 确认内容与当前配置
  不一致时给出警告(参数已变更需重新确认);
- 计算就绪: 项目活动且快照可组装(项目版本存在或草稿可固化)。

预检一次返回全部诊断; error/blocking 级为阻断错误(禁止提交, REQ-CALC-007),
warning 不降级阻断。诊断码 VALID-* 为本单元新增, 在导入时登记到共享诊断目录
(与 U05 数据集单元同模式)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan.core.diagnostics import (
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import AppError, NotFoundError
from iesplan.core.idgen import sha256_hex
from iesplan.devices import DeviceModelDescriptor as DeviceTypeSpec
from iesplan.devices import get_device_descriptor as get_device_type
from iesplan.models.audit import AuditLog
from iesplan.models.dataset import Dataset, DatasetVersion
from iesplan.models.identity import User
from iesplan.models.project import Project
from iesplan.services import config as config_service
from iesplan.services import dataset as dataset_service
from iesplan.services import model as model_service
from iesplan.services import project as project_service
from iesplan.storage import find_refs_by_owner, object_info

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
VALID_DATA_UTC_MISMATCH = "VALID-DATA-003"  # 数据集 UTC 偏移与项目不一致
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
        VALID_DATA_UTC_MISMATCH: "数据集 UTC 偏移与项目不一致",
        VALID_DATA_BINDING_BROKEN: "绑定的数据集版本缺失/已删除或不属于本项目",
        VALID_FIN_NO_CONFIRM: "缺少财务基准确认证据(RPD 10.2)",
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
        VALID_DATA_UTC_MISMATCH: "ies.diag.valid.data_utc_mismatch",
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
        VALID_DATA_UTC_MISMATCH: "ies.fix.valid.data_fix_offset",
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

#: 财务基准确认审计动作(追加式, 不可覆盖; RPD 10.2 确认证据)
BASELINE_ACTION: str = "project.baseline_confirmed"

#: 电网连接注册表类型 id(模型完整性检查)
GRID_TYPE_ID: str = "ies.device.grid_connection"

#: 载体 → 端口类型(与 services.model.CARRIER_PORT_TYPE 一致)
_CARRIER_PORT_TYPE: dict[str, str] = {
    "electric": "electric",
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
    # 统一标记: 阻断错误保持阻断(与严重度一致), 警告不降级也不升级
    for d in diags:
        d.project_id = str(project_id)
        if d.severity in (SEVERITY_BLOCKING, SEVERITY_ERROR):
            d.blocking = True
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


def _require_project(db: Session, project_id: int) -> Project:
    """按 id 取项目; 不存在或已删除(软删)一律 404(与 U03 语义一致)。"""
    project = db.get(Project, project_id)
    if project is None or project.status == "deleted":
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
    # 至少 1 个电网连接(供能接口, RPD 7.1)
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
        if spec is not None and spec.is_load:
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
        if spec is not None and spec.is_load:
            load_carriers.update(c for c in spec.energy_carriers if c in _CARRIER_PORT_TYPE)
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


def _check_data(db: Session, project: Project, diags: list[Diagnostic]) -> None:
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
    rows = db.scalars(select(DatasetVersion).where(DatasetVersion.id.in_(version_ids))).all()
    by_id: dict[int, DatasetVersion] = {v.id: v for v in rows}
    project_dataset_ids = set(
        db.scalars(select(Dataset.id).where(Dataset.project_id == project.id)).all()
    )
    for binding in bindings:
        if not isinstance(binding, dict) or not isinstance(
            binding.get("dataset_version_id"), int
        ):
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
        if version.fixed_utc_offset_minutes != project.fixed_utc_offset_minutes:
            diags.append(
                make_diag(
                    VALID_DATA_UTC_MISMATCH,
                    severity=SEVERITY_ERROR,
                    params={
                        "dataset_id": version.dataset_id,
                        "version_no": version.version_no,
                        "offset_minutes": version.fixed_utc_offset_minutes,
                        "expected_minutes": project.fixed_utc_offset_minutes,
                    },
                    location=loc,
                )
            )


def _load_dataset_bindings(db: Session, project_id: int) -> list[dict]:
    """当前草稿内容文档的 dataset_bindings(U03 dataset.bind 写入的权威绑定来源)。"""
    content = project_service.get_current_draft_content(db, project_id)
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


def _latest_baseline_evidence(db: Session, project_id: int) -> AuditLog | None:
    """最近一次财务基准确认证据(追加式审计, 按 id 倒序取最新)。"""
    return db.scalar(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "project",
            AuditLog.entity_id == project_id,
            AuditLog.action == BASELINE_ACTION,
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    )


def _current_assumptions(project: Project, config: dict) -> dict:
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
    db: Session, project: Project, config_data: dict, diags: list[Diagnostic]
) -> None:
    """财务基准确认证据检查(RPD 10.2): 确认人/确认内容校验值齐全; 内容过期给警告。"""
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
    digest = after.get("assumptions_hash")
    if not isinstance(digest, str) or not digest or not after.get("confirmed_by"):
        diags.append(
            make_diag(
                VALID_FIN_NO_CONFIRM,
                severity=SEVERITY_ERROR,
                params={"project_id": project.id, "reason_code": "incomplete_evidence"},
                location=loc,
            )
        )
        return
    # 内容校验: 确认时的假设哈希须与当前配置导出假设一致; 不一致为警告(参数已变更)
    config = config_data.get("config") or {}
    if digest != hash_assumptions(_current_assumptions(project, config)):
        diags.append(
            make_diag(
                VALID_FIN_STALE,
                severity=SEVERITY_WARNING,
                params={"project_id": project.id, "stored_hash": digest},
                location=loc,
            )
        )


# ---------------------------------------------------------------------------
# 检查项 f: 计算就绪(快照可组装)
# ---------------------------------------------------------------------------


def _check_readiness(project: Project, diags: list[Diagnostic]) -> None:
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


def hash_assumptions(assumptions: dict) -> str:
    """关键假设内容 → 完整性校验值(规范化 → sha256, 与 01 §3.2 同款规范)。

    用于记录"确认内容的完整性校验信息"(RPD 10.2)。规范化要点:
    - 数值(整数/浮点/Decimal)统一转为规范化 Decimal 文本, 20 与 20.0 视为同一内容,
      不同精度不产生误判(REQ-FIN-001 确认内容可复现);
    - 非有限数值(NaN/Infinity)与不支持的类型一律拒绝(失败即抛, 不静默兜底);
    - datetime 统一按 UTC 输出固定格式, 同一瞬时时间表示一致。
    """
    if not isinstance(assumptions, dict):
        raise _bad_assumptions(reason_code="not_an_object")
    normalized = _canonical_value(assumptions)
    raw = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return sha256_hex(raw.encode("utf-8"))


def _canonical_value(value: Any) -> Any:
    """递归规范化假设内容: 数值 → 规范化 Decimal 文本, 时间 → UTC 文本, 其余原样。"""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return _canonical_number(value)
    if isinstance(value, int):
        return _canonical_number(Decimal(value))
    if isinstance(value, float):
        return _canonical_number(Decimal(repr(value)))
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [_canonical_value(v) for v in value]
    if isinstance(value, tuple):
        return [_canonical_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _canonical_value(v) for k, v in value.items()}
    # 其余类型(NaN/Infinity/对象等)一律拒绝: 哈希必须可复现, 不做 str() 兜底
    raise _bad_assumptions(
        reason_code="unsupported_type", type=type(value).__name__
    )


def _canonical_number(value: Decimal) -> str:
    """数值 → 规范化文本(去尾零与指数差异; 非有限值拒绝)。"""
    if not value.is_finite():
        raise _bad_assumptions(reason_code="non_finite_number")
    return format(value.normalize(), "f") if value != value.to_integral() else str(value.normalize())


def mark_baseline_confirmed(
    db: Session,
    project_id: int,
    user: User,
    assumptions_hash: str,
    *,
    assumptions: dict | None = None,
) -> AuditLog:
    """记录财务基准确认(RPD 10.2: 确认人/确认时间/确认内容完整性校验)。

    证据以审计事件追加式记录(不可覆盖, 01 §10.3), 供 U11 指标单元与校验门禁读取。

    参数:
        db: 数据库会话。
        project_id: 项目 id(不存在或已删除抛 NotFoundError)。
        user: 确认人(记录 id 与确认时间)。
        assumptions_hash: 确认内容(关键假设)的完整性校验值(sha256 十六进制)。
        assumptions: 可选的假设原文, 一并记录便于复核与前端回显。
    返回:
        新增的审计记录(服务不主动 commit, 事务边界由 API 层控制)。

    校验值必须为 64 位十六进制 sha256(格式非法抛 AppError 400, 拒绝写坏证据)。
    """
    if not isinstance(assumptions_hash, str) or len(assumptions_hash) != 64 or any(
        c not in "0123456789abcdefABCDEF" for c in assumptions_hash
    ):
        raise _bad_assumptions(reason_code="bad_hash_format")
    project = _require_project(db, project_id)
    now = datetime.now(UTC)
    record = AuditLog(
        entity_type="project",
        entity_id=project_id,
        action=BASELINE_ACTION,
        actor_id=user.id if user is not None else None,
        actor_type="user",
        after={
            "assumptions_hash": assumptions_hash,
            "assumptions": dict(assumptions or {}),
            "confirmed_by": user.id if user is not None else None,
            "confirmed_at": now.isoformat(),
            "project_version_id": project.current_version_id,
        },
    )
    db.add(record)
    db.flush()
    return record


# ---------------------------------------------------------------------------
# 校验报告持久化(01 §10.2: 内容寻址对象 + ref_type='report' 引用)
# ---------------------------------------------------------------------------


def store_validation_report(db: Session, project_id: int, report: ValidationReport) -> dict:
    """校验报告落为内容寻址对象并登记引用(ref_type='report'), 返回摘要。

    GET /validation 可读取该项目的最近一次报告(按引用倒序)。
    报告内 project_id 与入参不一致时拒绝(防止跨项目错挂报告)。
    """
    if report.project_id and report.project_id != str(project_id):
        raise AppError(
            "校验报告与项目不匹配",
            code="VALID-FIN-003",
            message_key="ies.diag.valid.report_mismatch",
            params={"report_project_id": report.project_id, "project_id": project_id},
        )
    raw = json.dumps(
        report.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    obj = dataset_service.put_object(db, raw, _REPORT_MEDIA_TYPE)
    dataset_service.add_object_ref(
        db, {"id": obj.id}, "report", "project", project_id, purpose="项目校验报告"
    )
    obj_info = object_info(db, obj.id)
    db.flush()
    return {"object_id": obj.id, "sha256": obj_info["sha256"]}


def get_latest_validation_report(db: Session, project_id: int) -> dict | None:
    """读取项目最近一次持久化的校验报告(STO-05: 经公开门面查引用与读对象)。"""
    refs = find_refs_by_owner(db, "report", project_id, ref_entity_type="project")
    if not refs:
        return None
    try:
        raw = dataset_service.get_object_bytes(db, int(refs[0]["object_id"]))
    except Exception:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "BASELINE_ACTION",
    "ValidationReport",
    "validate_project",
    "hash_assumptions",
    "mark_baseline_confirmed",
    "store_validation_report",
    "get_latest_validation_report",
]
