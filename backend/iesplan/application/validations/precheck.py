"""项目校验用例(Wave 2 W2-A: application/validations)。

U07 项目校验流程（旧 ``iesplan.services.validation`` 已删除；计算前完整预检
与财务基准确认）：
- ``validate_project``（聚合模型完整性/参数变量/数据绑定与质量/配置兼容与
  IRR 硬约束/财务基准确认/计算就绪，一次返回全部诊断）；
- ``mark_baseline_confirmed``（财务基准确认审计追加）；
- ``store_validation_report`` / ``get_latest_validation_report``
  （校验报告对象存储持久化与读取）。

调用方向：
- 对象读写经 storage 公开门面（``put_object`` / ``add_ref`` / ``get_object``）；
- 项目/草稿读取与内容解析经 project 域公开门面（``require_project`` /
  ``require_current_draft`` / ``parse_content_object``）；
- 模型能力经 application.models、计算配置能力经 application.configuration
  用例改接；其余 project / dataset / audit 域调用经域公开门面。

事务：写用例（``mark_baseline_confirmed`` / ``store_validation_report``）
顶层函数拥有提交/回滚；``validate_project`` 与读取函数为只读，不提交事务。

调用方向：``api → application.validations.precheck → {project, dataset,
audit, storage} 域公开门面 + devices 公开门面``；不导入 ORM、不导入其他域
内部模块。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import dataset as dataset_domain
from iesplan import project as project_domain
from iesplan.audit.contracts import AuditRecord
from iesplan.core.diagnostics import (
    CONN_NODE_ORPHAN,
    PARAM_CONFLICT,
    PARAM_RNG_OUT,
    PARAM_UNIT_INCONSISTENT,
    PARAM_UNIT_MISMATCH,
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
from iesplan.devices.contracts2 import PropertySpec
from iesplan.identity.contracts import UserRecord
from iesplan.project.contracts import ProjectRecord
from iesplan import configuration as configuration_domain
from iesplan.storage import add_ref, find_refs_by_owner, get_object, put_object

# ---------------------------------------------------------------------------
# 诊断码(本单元新增, 导入时登记; 04 目录未登记, 见 NEW_DIAG_CODES 扩展模式)
# ---------------------------------------------------------------------------

VALID_MODEL_NO_GRID = "VALID-MODEL-001"  # 模型缺少电网连接
VALID_MODEL_NO_LOAD = "VALID-MODEL-002"  # 模型缺少负荷
VALID_MODEL_NO_SUPPLY = "VALID-MODEL-003"  # 有负荷的载体缺少供给设备
VALID_CONFIG_NOT_SAVED = "VALID-CONFIG-001"  # 计算配置未保存(无默认配置可用)
VALID_DATA_NO_BINDING = "VALID-DATA-001"  # 未绑定任何数据集版本
VALID_DATA_VERSION_INVALID = "VALID-DATA-002"  # 数据集版本质量阻断
VALID_DATA_BINDING_BROKEN = "VALID-DATA-004"  # 绑定的版本缺失/已删除/不属于本项目
VALID_FIN_NO_CONFIRM = "VALID-FIN-001"  # 缺少财务基准确认证据
VALID_READY_NOT_ASSEMBLABLE = "VALID-READY-001"  # 计算快照不可组装


def _register_diag_codes() -> None:
    """登记本单元新增诊断码到共享诊断目录(幂等, 与 U05 数据集单元同模式)。"""
    from iesplan.core import diagnostics as diag_mod

    codes = {
        VALID_MODEL_NO_GRID: "模型缺少电网连接(至少 1 个)",
        VALID_MODEL_NO_LOAD: "模型缺少负荷(至少 1 个)",
        VALID_MODEL_NO_SUPPLY: "有负荷的载体缺少供给设备",
        VALID_CONFIG_NOT_SAVED: "计算配置未保存(无默认配置可用)",
        VALID_DATA_NO_BINDING: "项目未绑定任何数据集版本",
        VALID_DATA_VERSION_INVALID: "所选数据集版本存在阻断性质量问题",
        VALID_DATA_BINDING_BROKEN: "绑定的数据集版本缺失/已删除或不属于本项目",
        VALID_FIN_NO_CONFIRM: "缺少财务基准确认证据(架构宪法 §16 安全与审计)",
        VALID_READY_NOT_ASSEMBLABLE: "计算快照不可组装",
    }
    for code, desc in codes.items():
        diag_mod.NEW_DIAG_CODES.setdefault(code, desc)
    for code, key in {
        VALID_MODEL_NO_GRID: "ies.diag.valid.model_no_grid",
        VALID_MODEL_NO_LOAD: "ies.diag.valid.model_no_load",
        VALID_MODEL_NO_SUPPLY: "ies.diag.valid.model_no_supply",
        VALID_CONFIG_NOT_SAVED: "ies.diag.valid.config_not_saved",
        VALID_DATA_NO_BINDING: "ies.diag.valid.data_no_binding",
        VALID_DATA_VERSION_INVALID: "ies.diag.valid.data_version_invalid",
        VALID_DATA_BINDING_BROKEN: "ies.diag.valid.data_binding_broken",
        VALID_FIN_NO_CONFIRM: "ies.diag.valid.fin_no_confirm",
        VALID_READY_NOT_ASSEMBLABLE: "ies.diag.valid.ready_not_assemblable",
    }.items():
        diag_mod.DIAG_MESSAGE_KEYS.setdefault(code, key)
    for code, hint in {
        VALID_MODEL_NO_GRID: "ies.fix.valid.model_add_grid",
        VALID_MODEL_NO_LOAD: "ies.fix.valid.model_add_load",
        VALID_MODEL_NO_SUPPLY: "ies.fix.valid.model_add_supply",
        VALID_CONFIG_NOT_SAVED: "ies.fix.valid.config_save",
        VALID_DATA_NO_BINDING: "ies.fix.valid.data_bind",
        VALID_DATA_VERSION_INVALID: "ies.fix.valid.data_fix_version",
        VALID_DATA_BINDING_BROKEN: "ies.fix.valid.data_fix_binding",
        VALID_FIN_NO_CONFIRM: "ies.fix.valid.fin_confirm",
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

#: 预检覆盖的负荷载体集合(电/热/冷；燃气等外生供给载体汇-only 合法，不检查)
_CHECKED_LOAD_CARRIERS: frozenset[str] = frozenset({"electricity", "heat", "cool"})

#: 拓扑源汇平衡覆盖的载体集合(solar 等环境侧载体无可连接端口，不参与)
_BALANCE_CARRIERS: frozenset[str] = frozenset({"electricity", "heat", "cool", "gas"})

#: 外生供给载体(平衡豁免：目录内无源设备，汇-only 合法)
_EXOGENOUS_CARRIERS: frozenset[str] = frozenset({"gas"})

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
    source: str = "iesplan.application.validations.precheck"

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
    project = project_domain.require_project(db, project_id)
    diags: list[Diagnostic] = []
    _check_model(db, project_id, diags)
    _check_config(db, project_id, diags)
    if include_data:
        _check_data(db, project, diags)
    _check_financial_baseline(db, project, diags)
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


def _model_section(db: Session, project_id: int) -> tuple[list[dict], list[dict]]:
    """当前草稿内容 model 节设备/连接(2.0 唯一项目建模事实)。"""
    content = _current_draft_content(db, project_id)
    model = content.get("model")
    if not isinstance(model, dict):
        return [], []
    devices = model.get("devices")
    conns = model.get("connections")
    return (
        [d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else [],
        [c for c in conns if isinstance(c, dict)] if isinstance(conns, list) else [],
    )


def _validate_device_params(
    device_type: str | None, params: dict | None, device_name: str = ""
) -> list[Diagnostic]:
    """按注册表 schema 校验设备参数(类型/单位/范围/枚举)。

    规则(与旧 U04 参数校验一致):
    - 类型未注册抛 NotFoundError(CONN-TYPE-002);
    - 未声明的参数键拒绝(PARAM-UNIT-002, actual=unknown);缺省键不校验, 按注册表默认值解释;
    - 数值参数校验 min/max(越界 → PARAM-RNG-003), 类型不匹配 → PARAM-UNIT-002。
    全部传入键均须已在注册表声明，仅一处对齐例外(0.12-A2): 已注册
    descriptor 中 type=predefined 的接口名允许以有效数值标量作 constant
    显式来源声明(与 assembly builder 映射一致); 未知键仍拒绝，非
    predefined 接口名不放宽。
    """
    spec = get_device_type(device_type)
    params = dict(params or {})  # 拷贝: 归一缺省键不得改写调用方原字典
    # 全部传入键必须已在注册表声明(下循环 unknown 即拒)，仅 predefined 常量例外
    provided = dict(params)
    diags: list[Diagnostic] = []
    loc = {"object_type": "device", "object_id": str(device_name), "field": ""}

    for name, value in provided.items():
        pspec = spec.properties.get(name)
        if pspec is None:
            ispec = spec.interfaces.get(name)
            if ispec is None or ispec.type != "predefined":
                diags.append(
                    make_diag(
                        PARAM_UNIT_MISMATCH,
                        severity=SEVERITY_ERROR,
                        params={"param": name, "expected": "registered", "actual": "unknown"},
                        location={**loc, "field": name},
                    )
                )
                continue
            diags.extend(_check_predefined_constant(name, value, ispec, loc))
            continue
        diags.extend(_check_param_value(name, value, pspec, loc))
    return diags


def _check_predefined_constant(name: str, value: Any, ispec: Any, loc: dict) -> list[Diagnostic]:
    """预定义接口 constant 显式来源的入口校验(0.12-A2 与 builder 对齐)。

    仅接受有效数值标量: bool/非数值/NaN/Infinity → PARAM-UNIT-002;
    接口 valid_range 数值界越界 → PARAM-RNG-003。裸标量不带单位声明,
    按接口单位解释(与 builder constant 映射一致); 单位换算不在入口做。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [
            make_diag(
                PARAM_UNIT_MISMATCH,
                severity=SEVERITY_ERROR,
                params={"param": name, "expected": ispec.unit, "actual": type(value).__name__},
                location={**loc, "field": name},
            )
        ]
    if not math.isfinite(float(value)):
        return [
            make_diag(
                PARAM_UNIT_MISMATCH,
                severity=SEVERITY_ERROR,
                params={"param": name, "expected": ispec.unit, "actual": type(value).__name__},
                location={**loc, "field": name},
            )
        ]
    minimum = ispec.minimum if isinstance(ispec.minimum, (int, float)) else None
    maximum = ispec.maximum if isinstance(ispec.maximum, (int, float)) else None
    if minimum is not None and float(value) < minimum:
        return [
            make_diag(
                PARAM_RNG_OUT,
                severity=SEVERITY_ERROR,
                params={"param": name, "value": value, "min": minimum, "max": maximum},
                location={**loc, "field": name},
            )
        ]
    if maximum is not None and float(value) > maximum:
        return [
            make_diag(
                PARAM_RNG_OUT,
                severity=SEVERITY_ERROR,
                params={"param": name, "value": value, "min": minimum, "max": maximum},
                location={**loc, "field": name},
            )
        ]
    return []


def _check_param_value(name: str, value: Any, pspec: PropertySpec, loc: dict) -> list[Diagnostic]:
    """单参数取值校验(枚举/类型/范围), 返回诊断列表。"""
    # 布尔类参数
    if isinstance(pspec.value, bool):
        if not isinstance(value, bool):
            return [_type_mismatch_diag(name, value, pspec, loc)]
        return []
    # 字典类参数(如分时电价 import_tariff)
    # 数值类参数(带范围或数值默认值)
    if pspec.minimum is not None or pspec.maximum is not None or isinstance(pspec.value, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [_type_mismatch_diag(name, value, pspec, loc)]
        # M-08: 拒绝 NaN/Infinity(非有限值可绕过 min/max 比较, 污染后续求解与哈希)
        if not math.isfinite(float(value)):
            return [_type_mismatch_diag(name, value, pspec, loc)]
        if pspec.minimum is not None and value < pspec.minimum:
            return [
                make_diag(
                    PARAM_RNG_OUT,
                    severity=SEVERITY_ERROR,
                    params={"param": name, "value": value, "min": pspec.minimum, "max": pspec.maximum},
                    location={**loc, "field": name},
                )
            ]
        if pspec.maximum is not None and value > pspec.maximum:
            return [
                make_diag(
                    PARAM_RNG_OUT,
                    severity=SEVERITY_ERROR,
                    params={"param": name, "value": value, "min": pspec.minimum, "max": pspec.maximum},
                    location={**loc, "field": name},
                )
            ]
    return []


def _type_mismatch_diag(name: str, value: Any, pspec: PropertySpec, loc: dict) -> Diagnostic:
    """参数类型/单位不匹配诊断(期望注册单位, 实际为 Python 类型名)。"""
    return make_diag(
        PARAM_UNIT_MISMATCH,
        severity=SEVERITY_ERROR,
        params={"param": name, "expected": pspec.unit, "actual": type(value).__name__},
        location={**loc, "field": name},
    )


def _connectable_sides(spec: DeviceTypeSpec, params: dict | None) -> dict[str, set[str]]:
    """设备声明接口的载体方向集(名键拓扑校验用)。

    predefined 接口视为汇入侧；热泵按 mode 参数裁剪未启用的冷/热载体，
    避免能源不平衡误报；solar 等环境侧载体无可连接端口，不参与。
    """
    sides: dict[str, set[str]] = {}
    params = params if isinstance(params, dict) else {}
    for iface in spec.interfaces.values():
        carrier = iface.carrier
        if carrier not in _BALANCE_CARRIERS:
            continue
        if (
            spec.device is not None
            and spec.device.id == "ies.device.heat_pump"
            and carrier in ("heat", "cool")
        ):
            mode = params.get("mode", "both")
            if mode == "heating" and carrier == "cool":
                continue
            if mode == "cooling" and carrier == "heat":
                continue
        direction = "in" if iface.type == "predefined" else iface.type
        if direction not in ("in", "out", "bidirectional"):
            continue
        sides.setdefault(carrier, set()).add(direction)
    return sides


def _check_topology(
    devices: list[dict],
    conns: list[dict],
    specs: dict[str, DeviceTypeSpec | None],
    diags: list[Diagnostic],
) -> None:
    """名键拓扑校验(孤立设备警告/负荷汇入警告/能源不平衡/重复连接)。"""
    touched = {c.get("from_device") for c in conns} | {c.get("to_device") for c in conns}

    # 1) 孤立设备警告
    for d in devices:
        name = d.get("name")
        if name not in touched:
            diags.append(
                make_diag(
                    CONN_NODE_ORPHAN,
                    severity=SEVERITY_WARNING,
                    params={"device_name": name},
                    location={"object_type": "device", "object_id": str(name)},
                )
            )

    # 2) 未连接负荷警告(负荷必须有汇入连接)
    for d in devices:
        name = d.get("name")
        spec = specs.get(name)
        if spec is None or not any(i.type == "predefined" for i in spec.interfaces.values()):
            continue
        if name not in touched:
            continue  # 完全无连接 → 已按孤立设备告警
        if not any(c.get("to_device") == name for c in conns):
            diags.append(
                make_diag(
                    CONN_NODE_ORPHAN,
                    severity=SEVERITY_WARNING,
                    params={"device_name": name},
                    location={"object_type": "device", "object_id": str(name), "field": "incoming"},
                )
            )

    # 3) 能源不平衡: 某载体只有源或只有汇(双向视为源汇兼具;
    #    外生供给载体如燃气无源设备, 汇-only 合法, 跳过)
    type_dirs: dict[str, set[str]] = {}
    for d in devices:
        spec = specs.get(d.get("name"))
        if spec is None:
            continue
        for carrier, dirs in _connectable_sides(spec, d.get("params")).items():
            type_dirs.setdefault(carrier, set()).update(dirs)
    for carrier, dirs in sorted(type_dirs.items()):
        if carrier in _EXOGENOUS_CARRIERS:
            continue
        has_source = any(d in ("out", "bidirectional") for d in dirs)
        has_sink = any(d in ("in", "bidirectional") for d in dirs)
        if has_source and has_sink:
            continue
        diags.append(
            make_diag(
                PARAM_UNIT_INCONSISTENT,
                severity=SEVERITY_ERROR,
                params={
                    "param": carrier,
                    "expected": "source_and_sink",
                    "actual": "source_only" if has_source else "sink_only",
                },
                location={"object_type": "model", "field": f"carrier:{carrier}"},
            )
        )

    # 4) 重复连接(同两端同端口)
    seen: dict[tuple, str] = {}
    for c in sorted(conns, key=lambda c: str(c.get("name") or "")):
        key = (c.get("from_device"), c.get("from_port"), c.get("to_device"), c.get("to_port"))
        cname = c.get("name")
        if key in seen:
            diags.append(
                make_diag(
                    PARAM_CONFLICT,
                    severity=SEVERITY_ERROR,
                    params={
                        "p1": f"connection:{seen[key]}",
                        "p2": f"connection:{cname}",
                    },
                    location={
                        "object_type": "connection",
                        "field": "ends",
                    },
                )
            )
        else:
            seen[key] = cname


def _check_model(db: Session, project_id: int, diags: list[Diagnostic]) -> None:
    """模型完整性: 拓扑/设备参数 + 电网连接/负荷/载体供给(草稿内容 2.0 名键事实)。"""
    devices, conns = _model_section(db, project_id)
    by_name = {d.get("name"): d for d in devices if isinstance(d.get("name"), str)}
    specs = {name: _device_spec(dev) for name, dev in by_name.items()}
    # 每台设备参数校验(类型未注册抛 NotFoundError，与旧行为一致先行阻断)
    for name, dev in by_name.items():
        diags.extend(_validate_device_params(dev.get("device_type"), dev.get("params"), name))
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
    load_names = [
        name
        for name, spec in specs.items()
        if spec is not None and any(i.type == "predefined" for i in spec.interfaces.values())
    ]
    if not load_names:
        diags.append(
            make_diag(
                VALID_MODEL_NO_LOAD,
                severity=SEVERITY_ERROR,
                params={"project_id": project_id},
                location=loc,
            )
        )
    # 电/热/冷每个有负荷的载体必须有供给设备(出方向声明接口, 双向不计)
    load_carriers: set[str] = set()
    for name in load_names:
        spec = specs[name]
        assert spec is not None
        load_carriers.update(
            i.carrier for i in spec.interfaces.values() if i.carrier in _CHECKED_LOAD_CARRIERS
        )
    supply_carriers: set[str] = set()
    for name, spec in specs.items():
        if spec is None:
            continue
        for carrier, dirs in _connectable_sides(spec, by_name[name].get("params")).items():
            if "out" in dirs:
                supply_carriers.add(carrier)
    for carrier in sorted(load_carriers - supply_carriers):
        diags.append(
            make_diag(
                VALID_MODEL_NO_SUPPLY,
                severity=SEVERITY_ERROR,
                params={"carrier": carrier, "project_id": project_id},
                location={"object_type": "model", "field": f"carrier:{carrier}"},
            )
        )
    _check_topology(devices, conns, specs, diags)


# ---------------------------------------------------------------------------
# 检查项 b/d: 参数/变量/目标/约束/算法兼容(复用 U06)
# ---------------------------------------------------------------------------


def _check_config(db: Session, project_id: int, diags: list[Diagnostic]) -> None:
    """计算配置: 须存在已保存 revision。

    已保存内容在用户输入入口唯一校验, 此处只判存在性, 不重复校验内容。
    """
    loc = {"object_type": "config", "object_id": ""}
    latest = configuration_domain.get_latest_calculation_config(db, project_id)
    if latest is None:
        # 无默认配置可用: 未保存即阻断提交(错误级, 经 validate_project 统一标
        # 记为 blocking), 不得静默按旧快照提交。
        diags.append(
            make_diag(
                VALID_CONFIG_NOT_SAVED,
                severity=SEVERITY_ERROR,
                params={"project_id": project_id, "reason_code": "not_saved"},
                location=loc,
            )
        )


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
    """当前草稿内容文档(经 project 域公开门面取草稿 + storage 门面读对象，
    内容解析经 project 域 ``parse_content_object``；命令簿记不外泄)。"""
    project = project_domain.require_project(db, project_id)
    draft = project_domain.require_current_draft(db, project)
    raw = get_object(db, draft.content_object_id)
    content = project_domain.parse_content_object(raw)
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


def _check_financial_baseline(
    db: Session, project: ProjectRecord, diags: list[Diagnostic]
) -> None:
    """财务基准确认证据检查(架构宪法 §16 安全与审计): 确认证据存在且确认人
    齐全即通过。

    注: 确认内容与当前假设的一致性比对已随旧形态经济参数移除而暂停;
    评价输入(评价年限/折现率)以后续独立用户确认流程重建该比对, 此处不以
    空假设伪造过期警告。
    """
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
    project = project_domain.require_project(db, project_id)
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
    "ValidationReport",
    "get_latest_validation_report",
    "mark_baseline_confirmed",
    "store_validation_report",
    "validate_project",
]
