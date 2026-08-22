"""catalog 设备 YAML 一次性迁移回执（roadmap 0.5.0 事项 3）。

职责：把旧格式 catalog/*.yaml（``type_id``/``function``/``parameters{unit,min,max}``）
一次性迁移到 ``ies.device-model`` 1.0.0 结构，语义等价，并生成迁移回执。

回执（宪法 §2.5 / file-formats.md "格式变更流程"）记录：
- 迁移文件清单；
- 每个文件的新旧规范摘要（SHA-256）；
- 迁移后校验结果（错误即回执失败，不部分发布）。

迁移规则（语义等价，禁止静默改义）：
- ``type_id/version/name_zh/name_en/model_method/stateful/fidelity`` → ``device`` 节；
- ``energy_carriers/capabilities/is_load`` → ``device.energy_carriers/capabilities``；
- ``parameters`` → ``parameters``（unit/min/max/default/enum/is_optimizable/
  stock_or_addition 平移，补 value_type=number/quantity 由 unit 量纲推断）；
- ``ports``（name/port_type/direction/energy_carrier/capacity_ref）→ ``ports``
  （carrier 取 energy_carrier；quantity/unit 由载能推断）；
- ``time_series.inputs`` → ``data_inputs``（列 ID/单位/必填）；
- ``states`` → ``states``（key/unit/initial_ref/bounds）；
- ``function.entry/package`` → ``model_commands``：由建模命令 provider 的
  稳定命令 ID 映射（组合根/ provider 内部解析，文件不暴露函数/包/模块路径）。

本模块只提供迁移构建与回执生成；实际写文件由调用方/测试触发（一次性脚本）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from iesplan.core.diagnostics import Diagnostic
from iesplan.devices import yamlmini
from iesplan.devices.contracts import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    DeviceModelDocument,
    canonicalize_device_model,
    canonicalize_raw_mapping,
)
from iesplan.devices.parser import parse_device_model_yaml

#: 载能 → (quantity, unit) 推断（迁移时端口量纲/单位缺省补全）
_CARRIER_QUANTITY_UNIT: dict[str, tuple[str, str]] = {
    "electric": ("power", "kW"),
    "heat": ("power", "kW"),
    "cool": ("power", "kW"),
    "gas": ("power", "kW"),
    "solar": ("irradiance", "W/m2"),
    "water": ("flow", "m3/h"),
    "data": ("signal", "-"),
}

#: 参数单位 → 物理量推断（迁移回执用；不影响新契约强制量纲校验）
_UNIT_TO_QUANTITY: tuple[tuple[str, str], ...] = (
    ("kwp", "power"),
    ("kw", "power"),
    ("mw", "power"),
    ("w/m2", "irradiance"),
    ("w/m", "irradiance"),
    ("kwh", "energy"),
    ("mwh", "energy"),
    ("cny/kwh", "currency_per_energy"),
    ("cny/kw", "currency_per_power"),
    ("cny/m", "currency_per_volume"),
    ("kj/m", "energy_per_volume"),
    ("deg", "angle"),
    ("c", "temperature"),
    ("-", "ratio"),
    ("1", "ratio"),
    ("reference", "ratio"),
    ("step", "ratio"),
    ("a", "duration"),
    ("次", "ratio"),
)

#: 旧 mechanism 函数入口 → 稳定建模命令 ID（组合根/provider 解析的唯一事实源）
#: 命令 ID 是稳定命名空间字符串，不含 Python 模块/包/函数名。
MECHANISM_COMMAND_IDS: dict[str, str] = {
    "pv_output": "ies.model-command.pv.generation",
    "heat_pump_cop": "ies.model-command.heat_pump.operation",
    "boiler_output": "ies.model-command.boiler.generation",
    "chiller_output": "ies.model-command.chiller.generation",
    "gas_volume_m3": "ies.model-command.boiler.gas_volume",
    "simulate_battery": "ies.model-command.battery.storage",
    "heat_transfer_q": "ies.model-command.heat_transfer.simple",
    "power_balance": "ies.model-command.grid.power_balance",
    "transport_pipe": "ies.model-command.pipeline.transport",
    "periodic_load_output": "ies.model-command.load.periodic",
}

#: 命令版本（迁移回执固定；命令 provider 需与此一致）
COMMAND_VERSION = "1.0.0"


@dataclass(slots=True)
class MigrationReceipt:
    """一次性迁移回执。"""

    schema: str = SCHEMA_ID
    schema_version: str = SCHEMA_VERSION
    migrated_files: list[dict] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "migrated_files": self.migrated_files,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


def _quantity_of_unit(unit: str) -> str:
    """由单位串推断物理量（迁移补全用；新契约要求显式声明）。"""
    u = (unit or "").strip().lower().replace("·", "/")
    for prefix, quantity in _UNIT_TO_QUANTITY:
        if u == prefix or u.startswith(prefix + "/"):
            return quantity
    return "ratio"


def migrate_device_mapping(raw: dict) -> dict:
    """旧格式设备 YAML 映射 → 新 ``ies.device-model`` 1.0.0 映射（语义等价）。

    纯函数：不读写文件、不注册任何状态。迁移结果交给调用方写盘 + 回执。
    """
    device = {
        "id": raw.get("type_id", ""),
        "version": str(raw.get("version", "1.0.0")),
        "names": {
            "zh-CN": raw.get("name_zh", ""),
            "en-US": raw.get("name_en", ""),
        },
        "model_method": raw.get("model_method", "mechanism"),
        "stateful": bool(raw.get("stateful", False)),
        "fidelity": raw.get("fidelity", "medium"),
        "energy_carriers": list(raw.get("energy_carriers") or []),
        "capabilities": list(raw.get("capabilities") or []),
    }
    parameters: dict[str, dict] = {}
    for name, p in (raw.get("parameters") or {}).items():
        if not isinstance(p, dict):
            continue
        entry: dict = {
            "value_type": "number",
            "quantity": _quantity_of_unit(str(p.get("unit", ""))),
            "unit": str(p.get("unit", "-")),
            "required": p.get("required", False),
        }
        # 旧 min/max → 新 minimum/maximum
        if p.get("min") is not None:
            entry["minimum"] = p["min"]
        if p.get("max") is not None:
            entry["maximum"] = p["max"]
        for key in ("default", "enum"):
            if p.get(key) is not None:
                entry[key] = p[key]
        if p.get("is_optimizable") is not None:
            entry["optimizable"] = bool(p["is_optimizable"])
        if p.get("stock_or_addition") is not None:
            entry["stock_or_addition"] = p["stock_or_addition"]
        parameters[name] = entry

    ports: dict[str, dict] = {}
    for port in raw.get("ports") or []:
        if not isinstance(port, dict):
            continue
        name = port.get("name", "")
        carrier = port.get("energy_carrier", port.get("port_type", "electric"))
        qty, unit = _CARRIER_QUANTITY_UNIT.get(carrier, ("power", "kW"))
        entry: dict = {
            "carrier": carrier,
            "direction": port.get("direction", "out"),
            "quantity": qty,
            "unit": unit,
        }
        if port.get("capacity_ref"):
            entry["capacity_parameter"] = port["capacity_ref"]
        ports[name] = entry

    data_inputs: dict[str, dict] = {}
    extensions_payload: dict[str, object] = {
        "ies.meta": {
            "help_topic": raw.get("help_topic", ""),
            "is_load": bool(raw.get("is_load", False)),
            "extends": raw.get("extends", "ies.device.base"),
            "param_help": {
                name: str(p.get("help_key", ""))
                for name, p in (raw.get("parameters") or {}).items()
                if isinstance(p, dict) and p.get("help_key")
            },
        }
    }
    periods: dict[str, str] = {}
    for section in ("inputs", "outputs"):
        for s in (raw.get("time_series") or {}).get(section, []):
            if not isinstance(s, dict):
                continue
            key = s.get("key", "")
            if not key:
                continue
            unit = str(s.get("unit", "kW"))
            data_inputs[key] = {
                "value_type": "number",
                "quantity": _quantity_of_unit(unit),
                "unit": unit,
                "required": bool(s.get("required", True)),
            }
            if s.get("period"):
                # 周期语义保留在 extensions（1.0.0 schema 的 data_inputs 无 period 字段，
                # 扩展不得改变核心语义，但允许承载周期粒度元数据）
                periods[key] = str(s["period"])
    if periods:
        extensions_payload["periods"] = periods

    states: dict[str, dict] = {}
    for st in raw.get("states") or []:
        if not isinstance(st, dict):
            continue
        key = st.get("key", "")
        if not key:
            continue
        entry: dict = {"unit": st.get("unit", "-")}
        if st.get("initial_ref"):
            entry["initial_ref"] = st["initial_ref"]
        bounds = st.get("bounds") or {}
        if bounds.get("min_ref"):
            entry["minimum_ref"] = bounds["min_ref"]
        if bounds.get("max_ref"):
            entry["maximum_ref"] = bounds["max_ref"]
        states[key] = entry

    # function → model_commands（稳定命令 ID；provider 内部解析）
    function = raw.get("function") or {}
    entry_name: str | None = None
    if isinstance(function, dict):
        entry_name = function.get("entry")
        if not entry_name:
            model_file = function.get("model_file")
            if isinstance(model_file, dict):
                entry_name = model_file.get("path")
    model_commands: dict[str, str] = {}
    for cap in device["capabilities"]:
        command_id = MECHANISM_COMMAND_IDS.get(entry_name or "", "ies.model-command.load.periodic")
        model_commands[cap] = f"{command_id}@{COMMAND_VERSION}"

    new_raw = {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "device": device,
        "parameters": parameters,
        "ports": ports,
        "data_inputs": data_inputs,
        "states": states,
        "model_commands": model_commands,
        "extensions": extensions_payload,
    }
    return new_raw


def build_migration_receipt(yamls: list[Path]) -> MigrationReceipt:
    """对旧格式 YAML 文件一次性迁移并生成回执。

    任一文件迁移后校验失败 → 回执含 error 诊断且 ``migrated_files`` 标记该文件
    失败（不把部分结果当作成功发布）。
    """
    receipt = MigrationReceipt()
    for path in yamls:
        raw_text = path.read_text(encoding="utf-8")
        try:
            old_raw = yamlmini.load(raw_text)
        except yamlmini.YamlParseError as exc:
            receipt.diagnostics.append(_file_diag(path, f"YAML 解析失败: {exc}"))
            receipt.migrated_files.append(_file_entry(path, migrated=False, error=str(exc)))
            continue
        if not isinstance(old_raw, dict):
            receipt.diagnostics.append(_file_diag(path, "旧格式顶层必须为映射"))
            receipt.migrated_files.append(_file_entry(path, migrated=False, error="顶层非映射"))
            continue
        _, old_digest = canonicalize_raw_mapping(old_raw)
        new_raw = migrate_device_mapping(old_raw)
        result = parse_device_model_yaml(new_raw, file=str(path))
        _, new_digest = canonicalize_device_model(result.document) if result.document is not None else ("", "")
        if not result.ok:
            receipt.diagnostics.extend(result.diagnostics)
            receipt.migrated_files.append(
                _file_entry(
                    path,
                    migrated=False,
                    old_digest=old_digest,
                    new_digest="",
                    errors=[d.to_dict() for d in result.diagnostics],
                )
            )
            continue
        receipt.migrated_files.append(
            _file_entry(
                path,
                migrated=True,
                old_digest=old_digest,
                new_digest=new_digest,
                device_id=result.document.device.id if result.document.device else "",
                version=result.document.device.version if result.document.device else "",
            )
        )
    return receipt


def _file_diag(path: Path, detail: str) -> Diagnostic:
    from iesplan.core.diagnostics import SYS_CFG_INVALID, SEVERITY_ERROR, make_diag

    return make_diag(
        SYS_CFG_INVALID,
        severity=SEVERITY_ERROR,
        params={"detail": detail, "file": str(path)},
        location={"object_type": "device", "file": str(path)},
    )


def _file_entry(
    path: Path,
    *,
    migrated: bool,
    old_digest: str = "",
    new_digest: str = "",
    device_id: str = "",
    version: str = "",
    error: str | None = None,
    errors: list | None = None,
) -> dict:
    return {
        "file": str(path),
        "migrated": migrated,
        "old_sha256": old_digest,
        "new_sha256": new_digest,
        "device_id": device_id,
        "version": version,
        "error": error,
        "errors": errors or [],
    }


__all__ = [
    "MigrationReceipt",
    "migrate_device_mapping",
    "build_migration_receipt",
    "MECHANISM_COMMAND_IDS",
    "COMMAND_VERSION",
]
