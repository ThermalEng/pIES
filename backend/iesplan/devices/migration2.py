"""`ies.device-model` 1.0.0 → 2.0.0 显式离线迁移器。

规则（格式标准「完成标准」）：
- 旧 1.0.0 文件只通过显式离线迁移进入新格式，不保留运行期兼容分支；
- 迁移输出必须重新通过完整 2.0.0 校验，成功才产出文档；
- 失败返回聚合诊断，不产生部分迁移结果。

映射：
- ``device.version`` / ``model_method`` / ``stateful`` / ``fidelity`` / ``energy_carriers`` /
  ``capabilities`` → 删除（不属于 2.0 纯技术语义）；
- ``parameters`` → ``properties``：value 取 default，unit 原样，minimum/maximum → valid_range；
  boolean/string 的 unit 为空时用 ``-``；
- ``ports`` → ``interfaces``：direction → type（in/out；bidirectional 保留），
  carrier 归一化（electric→electricity 等），capacity_parameter 移除；
- ``data_inputs`` → ``interfaces``：type: predefined，source 由 device.model_method 决定
  （data_repeat → mode: data_repeat + data_ref: <列名>；data_predict → mode: data_predict）；
- ``states`` → ``equations.variables``（仅 unit/valid_range/initial 可表达的字段）；
- ``model_commands`` → 删除（无独立命令版本；技术关系由 equations 表达）。

迁移是显式动作：只有 schema_version == "1.0.0" 的输入才会处理；
2.0.0 输入直接报错，防止迁移器被当作兼容层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.units import UnitError, is_known_unit
from iesplan.devices.contracts2 import SCHEMA_ID, SCHEMA_VERSION
from iesplan.devices.parser2 import DeviceModelParseResult, parse_device_model_v2

#: 1.0 载体 → 2.0 carrier 归一化表
CARRIER_MAP: dict[str, str] = {
    "electric": "electricity",
    "heat": "heat",
    "cool": "cooling",
    "gas": "gas",
    "solar": "solar",
    "water": "water",
    "data": "data",
}

#: 1.0 参数 quantity → 2.0 单位默认（无法识别时保留原单位）
_QUANTITY_DEFAULT_UNIT: dict[str, str] = {
    "power": "kW",
    "energy": "kWh",
    "ratio": "-",
    "temperature": "°C",
}


@dataclass(slots=True)
class MigrationResult:
    """迁移结果：要么有文档（含回执），要么有诊断列表。"""

    document: Any = None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.document is not None and not self.diagnostics


def _diag(file: str, detail: str, field: str | None = None) -> Diagnostic:
    return make_diag(
        "SYS-CFG-001",
        severity="error",
        params={"file": file, "detail": detail},
        location={"object_type": "device-model-migration", "file": file, "field": field} if field else {"object_type": "device-model-migration", "file": file},
    )


def migrate_v1_to_v2(raw: Mapping[str, Any], *, file: str = "") -> MigrationResult:
    """显式迁移 1.0.0 设备模型到 2.0.0；成功后输出通过完整校验的文档。"""
    file = file or "<device-model-v1>"
    diags: list[Diagnostic] = []

    if raw.get("schema") != SCHEMA_ID:
        return MigrationResult(diagnostics=[_diag(file, f"schema 必须为 {SCHEMA_ID!r}", "schema")])
    if str(raw.get("schema_version")) != "1.0.0":
        return MigrationResult(
            diagnostics=[_diag(file, "迁移器只接受 schema_version 1.0.0 的输入（2.0.0 请直接校验）", "schema_version")]
        )

    out: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
    }

    # ---- device ----
    dev = raw.get("device") or {}
    if not isinstance(dev, Mapping):
        return MigrationResult(diagnostics=[_diag(file, "device 必须是 mapping", "device")])
    out["device"] = {"id": dev.get("id"), "names": dev.get("names")}

    # ---- parameters → properties ----
    properties: dict[str, Any] = {}
    params = raw.get("parameters") or {}
    if not isinstance(params, Mapping):
        return MigrationResult(diagnostics=[_diag(file, "parameters 必须是 mapping", "parameters")])
    for pid, praw in params.items():
        if not isinstance(praw, Mapping):
            diags.append(_diag(file, f"parameters.{pid} 必须是 mapping", f"parameters.{pid}"))
            continue
        unit = praw.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            unit = _QUANTITY_DEFAULT_UNIT.get(praw.get("quantity"), "-")
        try:
            is_known_unit(unit.strip())
        except UnitError:
            diags.append(_diag(file, f"parameters.{pid}.unit 无法识别: {unit!r}", f"parameters.{pid}.unit"))
            continue
        default = praw.get("default")
        if default is None:
            vt = praw.get("value_type")
            default = False if vt == "boolean" else 0 if vt == "number" else ""
        prop: dict[str, Any] = {"value": default, "unit": unit.strip()}
        lo, hi = praw.get("minimum"), praw.get("maximum")
        if lo is not None or hi is not None:
            prop["valid_range"] = {"minimum": lo, "maximum": hi}
        properties[pid] = prop
    out["properties"] = properties

    # ---- ports → interfaces ----
    interfaces: dict[str, Any] = {}
    ports = raw.get("ports") or {}
    if not isinstance(ports, Mapping):
        return MigrationResult(diagnostics=[_diag(file, "ports 必须是 mapping", "ports")])
    for pid, praw in ports.items():
        if not isinstance(praw, Mapping):
            diags.append(_diag(file, f"ports.{pid} 必须是 mapping", f"ports.{pid}"))
            continue
        direction = praw.get("direction")
        if direction == "in":
            type_ = "in"
        elif direction == "out":
            type_ = "out"
        elif direction in ("bidirectional", "inout", "out_in"):
            type_ = "bidirectional"
        else:
            diags.append(_diag(file, f"ports.{pid}.direction 无法映射: {direction!r}", f"ports.{pid}.direction"))
            continue
        carrier = CARRIER_MAP.get(praw.get("carrier"), praw.get("carrier"))
        unit = praw.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            unit = _QUANTITY_DEFAULT_UNIT.get(praw.get("quantity"), "-")
        iface: dict[str, Any] = {
            "type": type_,
            "carrier": carrier,
            "unit": unit,
            "valid_range": {"minimum": None, "maximum": None},
        }
        cap = praw.get("capacity_parameter")
        if cap in properties:
            rng = properties[cap].get("valid_range")
            if rng:
                iface["valid_range"] = rng
        interfaces[pid] = iface
    out["interfaces"] = interfaces

    # ---- data_inputs → predefined interfaces ----
    model_method = (dev.get("model_method") or "mechanism") if isinstance(dev, Mapping) else "mechanism"
    data_inputs = raw.get("data_inputs") or {}
    if not isinstance(data_inputs, Mapping):
        return MigrationResult(diagnostics=[_diag(file, "data_inputs 必须是 mapping", "data_inputs")])
    for did, draw in data_inputs.items():
        if not isinstance(draw, Mapping):
            diags.append(_diag(file, f"data_inputs.{did} 必须是 mapping", f"data_inputs.{did}"))
            continue
        if model_method == "data_predict":
            mode = "data_predict"
        elif model_method == "data_repeat":
            mode = "data_repeat"
        else:
            diags.append(
                _diag(file, f"data_inputs.{did} 的 model_method {model_method!r} 不支持迁移",
                      f"data_inputs.{did}")
            )
            continue
        unit = draw.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            unit = _QUANTITY_DEFAULT_UNIT.get(draw.get("quantity"), "-")
        interfaces[did] = {
            "type": "predefined",
            "carrier": "data" if model_method == "data_predict" else draw.get("carrier", "data"),
            "unit": unit,
            "valid_range": {"minimum": None, "maximum": None},
            "source": {"mode": mode, "data_ref": did},
        }
    out["interfaces"] = interfaces

    # ---- states → equations.variables ----
    variables: dict[str, Any] = {}
    states = raw.get("states") or {}
    if not isinstance(states, Mapping):
        return MigrationResult(diagnostics=[_diag(file, "states 必须是 mapping", "states")])
    for sid, sraw in states.items():
        if not isinstance(sraw, Mapping):
            diags.append(_diag(file, f"states.{sid} 必须是 mapping", f"states.{sid}"))
            continue
        unit = sraw.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            unit = _QUANTITY_DEFAULT_UNIT.get(sraw.get("quantity"), "-")
        var: dict[str, Any] = {"unit": unit}
        if "minimum" in sraw or "maximum" in sraw:
            var["valid_range"] = {"minimum": sraw.get("minimum"), "maximum": sraw.get("maximum")}
        if sraw.get("initial"):
            var["initial"] = {"property_ref": sraw["initial"]}
        variables[sid] = var
    out["equations"] = {"variables": variables, "relations": []}

    # ---- 重新完整校验 ----
    if diags:
        return MigrationResult(diagnostics=diags)
    result = parse_device_model_v2(out, file=file)
    if not result.ok:
        return MigrationResult(diagnostics=result.diagnostics)
    return MigrationResult(document=result.document)
