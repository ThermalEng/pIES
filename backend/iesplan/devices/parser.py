"""`ies.device-model` 1.0.0 解析与校验（新契约入口）。

职责（宪法 §7.8 + device-model-yaml.md "校验顺序"）：
1. 校验 YAML 安全子集、顶层 schema 与未知字段（使用 JSON Schema）；
2. 校验 ID、版本、语言、枚举与字段类型；
3. 校验参数、端口、数据列与状态的量纲、单位和范围；
4. 校验 ``stateful``、载能汇总、capability 与 command 的交叉一致性；
5. 解析命令精确版本（值形态 ``<command-id>@<exact-version>``）；
6. 生成不可变 ``DeviceModelDocument`` 和规范摘要。

失败路径（宪法 §2.2 / §2.5）：结构非法、未知核心字段、枚举越界、量纲不一致、
capability 无对应命令、命令版本缺失均产生定位到文件/字段/稳定诊断码的诊断；
解析失败不产出部分文档。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from iesplan.core.diagnostics import (
    SEVERITY_ERROR,
    SYS_CFG_INVALID,
    Diagnostic,
    make_diag,
)
from iesplan.devices.contracts import (
    DEVICE_MODEL_SCHEMA_PATH,
    PORT_DIRECTIONS,
    SCHEMA_ID,
    SCHEMA_VERSION,
    VALUE_TYPES,
    _freeze,
    DeviceDataInput,
    DeviceInfo,
    DeviceModelDocument,
    DeviceParameter,
    DevicePort,
    DeviceState,
)

_ID_PATTERN = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")
_SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_COMMAND_REF_PATTERN = re.compile(
    r"^(?P<id>[a-z0-9]+([._-][a-z0-9]+)*)@(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$"
)

#: 载能合法词汇（设备文件声明的载能；装配校验的载体集合以本表为准）
CARRIER_VOCABULARY: tuple[str, ...] = (
    "electric",
    "heat",
    "cool",
    "gas",
    "solar",
    "water",
    "data",
)

#: 载能 → 默认物理量（端口/数据列量纲名称，供交叉一致性检查）
CARRIER_DEFAULT_QUANTITY: dict[str, str] = {
    "electric": "power",
    "heat": "power",
    "cool": "power",
    "gas": "power",
    "solar": "irradiance",
    "water": "flow",
    "data": "signal",
}

#: 物理量 → 规范业务单位（端口量纲一致性校验；比较前单位归一化，W/m² ≡ W/m2）
QUANTITY_CANONICAL_UNIT: dict[str, str] = {
    "power": "kw",
    "irradiance": "w/m2",
    "flow": "m3/h",
    "signal": "-",
}

#: Unicode 上标 → ASCII 数字（端口单位归一化；W/m² ≡ W/m2）
_SUPERSCRIPT_TABLE = str.maketrans({"²": "2", "³": "3"})


def normalize_unit(unit: str) -> str:
    """单位串归一化（小写 + 去空白 + 上标转 ASCII + · → /）。

    用于端口量纲一致性比对：``W/m²`` 与 ``W/m2`` 归一化后相等。
    """
    return (unit or "").strip().lower().replace("·", "/").translate(_SUPERSCRIPT_TABLE)


def _load_schema() -> dict:
    """读取 JSON Schema（包内相对路径）。"""
    path = Path(__file__).resolve().parent / DEVICE_MODEL_SCHEMA_PATH
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


@dataclass(slots=True)
class DeviceModelParseResult:
    """解析结果：要么有完整文档，要么有诊断列表（不允许两者同时缺失）。"""

    document: DeviceModelDocument | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.document is not None and not self.diagnostics

    @property
    def blocking_diags(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity in ("error", "blocking")]


def _diag(code: str, severity: str, detail: str, *, file: str, location: dict) -> Diagnostic:
    """构造设备模型诊断（定位文件 + 字段路径）。"""
    return make_diag(
        code,
        severity=severity,
        params={"detail": detail, "file": file},
        location={"object_type": "device", "file": file, **location},
    )


def _err_file(code: str, severity: str, detail: str, *, file: str, field: str | None = None) -> Diagnostic:
    return _diag(
        code,
        severity,
        detail,
        file=file,
        location={"field": field} if field else {},
    )


def parse_device_model_yaml(raw: dict, *, file: str = "") -> DeviceModelParseResult:
    """校验原始 YAML 映射 → 不可变文档；失败返回诊断列表（无部分文档）。

    ``raw`` 必须已由 ``yamlmini`` 解析（安全子集 + 重复键拒绝 + 未知标量拒绝）。
    """
    diags: list[Diagnostic] = []
    file = file or "<device-model>"

    # ---- 阶段 1: JSON Schema（顶层结构 + 未知核心字段） ----
    try:
        import jsonschema
    except ImportError:  # pragma: no cover
        diags.append(_err_file(SYS_CFG_INVALID, SEVERITY_ERROR, "jsonschema 依赖缺失", file=file))
        return DeviceModelParseResult(document=None, diagnostics=diags)
    schema = _load_schema()
    try:
        jsonschema.validate(instance=raw, schema=schema)
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path)
        msg = exc.message
        diags.append(
            _err_file(
                SYS_CFG_INVALID,
                SEVERITY_ERROR,
                f"{msg} (字段: {path or '顶层'})",
                file=file,
                field=path or None,
            )
        )
        return DeviceModelParseResult(document=None, diagnostics=diags)

    # ---- 阶段 2: 语义校验（JSON Schema 无法表达的交叉约束） ----
    device_raw = raw.get("device") or {}
    d_id = device_raw.get("id", "")
    d_version = device_raw.get("version", "")
    names = device_raw.get("names", {}) if isinstance(device_raw.get("names"), dict) else {}

    # 2a) schema / schema_version 恒定
    if raw.get("schema") != SCHEMA_ID:
        diags.append(
            _err_file(
                SYS_CFG_INVALID, SEVERITY_ERROR, f"schema 必须为 {SCHEMA_ID!r}", file=file, field="schema"
            )
        )
    if str(raw.get("schema_version")) != SCHEMA_VERSION:
        diags.append(
            _err_file(
                SYS_CFG_INVALID,
                SEVERITY_ERROR,
                f"schema_version 必须为 {SCHEMA_VERSION!r}",
                file=file,
                field="schema_version",
            )
        )

    # 2b) 命令精确版本解析（model_commands 值必须是 <id>@<semver>）
    commands_ok = True
    parsed_commands: dict[str, str] = {}
    for capability, ref in (raw.get("model_commands") or {}).items():
        if not isinstance(ref, str):
            commands_ok = False
            diags.append(
                _err_file(
                    SYS_CFG_INVALID,
                    SEVERITY_ERROR,
                    f"命令引用必须为字符串: {capability!r}",
                    file=file,
                    field=f"model_commands.{capability}",
                )
            )
            continue
        m = _COMMAND_REF_PATTERN.match(ref)
        if m is None:
            commands_ok = False
            diags.append(
                _err_file(
                    SYS_CFG_INVALID,
                    SEVERITY_ERROR,
                    f"命令引用必须为 <command-id>@<exact-version>: {ref!r}",
                    file=file,
                    field=f"model_commands.{capability}",
                )
            )
            continue
        parsed_commands[capability] = ref

    # 2c) stateful ↔ states 一致性
    stateful = bool(device_raw.get("stateful", False))
    states_raw = raw.get("states") or {}
    if stateful and not states_raw:
        diags.append(
            _err_file(
                SYS_CFG_INVALID, SEVERITY_ERROR, "stateful 设备必须声明 states", file=file, field="states"
            )
        )
    if not stateful and states_raw:
        diags.append(
            _err_file(
                SYS_CFG_INVALID,
                SEVERITY_ERROR,
                "states 仅允许出现在 stateful 设备",
                file=file,
                field="states",
            )
        )

    # 2d) capability 必须能在 model_commands 中找到命令
    capabilities = device_raw.get("capabilities") or []
    if commands_ok:
        for cap in capabilities:
            if cap not in parsed_commands:
                diags.append(
                    _err_file(
                        SYS_CFG_INVALID,
                        SEVERITY_ERROR,
                        f"capability {cap!r} 缺少对应 model_command",
                        file=file,
                        field=f"capabilities.{cap}",
                    )
                )

    # 2e) 端口方向不能缺省（不允许装配器补齐）
    ports_raw = raw.get("ports") or {}
    for port_name, port_raw in ports_raw.items():
        direction = port_raw.get("direction") if isinstance(port_raw, dict) else None
        if direction not in PORT_DIRECTIONS:
            diags.append(
                _err_file(
                    SYS_CFG_INVALID,
                    SEVERITY_ERROR,
                    f"端口 {port_name!r} 方向非法: {direction!r}",
                    file=file,
                    field=f"ports.{port_name}.direction",
                )
            )

    # 2f) 载能词汇表 + 端口量纲一致性
    for carrier in device_raw.get("energy_carriers") or []:
        if carrier not in CARRIER_VOCABULARY:
            diags.append(
                _err_file(
                    SYS_CFG_INVALID,
                    SEVERITY_ERROR,
                    f"未知载能: {carrier!r}",
                    file=file,
                    field=f"device.energy_carriers.{carrier}",
                )
            )

    # 2f') 端口 quantity/unit 必须与载能物理量一致（unit 归一化后比对，W/m2≡W/m²）
    for port_name, port_raw in ports_raw.items():
        if not isinstance(port_raw, dict):
            continue
        carrier = port_raw.get("carrier")
        expected_quantity = CARRIER_DEFAULT_QUANTITY.get(carrier)
        if expected_quantity is None:
            continue  # 未知载能由词汇表检查拒绝
        declared_quantity = port_raw.get("quantity")
        if declared_quantity != expected_quantity:
            diags.append(
                _err_file(
                    SYS_CFG_INVALID,
                    SEVERITY_ERROR,
                    f"端口 {port_name!r} 物理量 {declared_quantity!r} 与载能 "
                    f"{carrier!r} 不一致（应为 {expected_quantity!r}）",
                    file=file,
                    field=f"ports.{port_name}.quantity",
                )
            )
            continue
        canonical_unit = QUANTITY_CANONICAL_UNIT.get(expected_quantity)
        if canonical_unit is not None:
            declared_unit = port_raw.get("unit")
            if normalize_unit(str(declared_unit)) != canonical_unit:
                diags.append(
                    _err_file(
                        SYS_CFG_INVALID,
                        SEVERITY_ERROR,
                        f"端口 {port_name!r} 单位 {declared_unit!r} 与载能 "
                        f"{carrier!r} 规范单位 {canonical_unit} 不一致（归一化后比对）",
                        file=file,
                        field=f"ports.{port_name}.unit",
                    )
                )

    # 2g) 参数 minimum/maximum 交叉（minimum > maximum 拒绝）
    params_raw = raw.get("parameters") or {}
    for name, p_raw in params_raw.items():
        if not isinstance(p_raw, dict):
            continue
        mn = _number_or_none(p_raw.get("minimum"))
        mx = _number_or_none(p_raw.get("maximum"))
        if mn is not None and mx is not None and mn > mx:
            diags.append(
                _err_file(
                    SYS_CFG_INVALID,
                    SEVERITY_ERROR,
                    f"参数 {name!r} minimum({mn}) 大于 maximum({mx})",
                    file=file,
                    field=f"parameters.{name}",
                )
            )

    # 2h) value_type 与 default/enum 取值类型交叉校验（类型不一致 → error 阻断）
    for name, p_raw in params_raw.items():
        if not isinstance(p_raw, dict):
            continue
        value_type = p_raw.get("value_type")
        if value_type not in VALUE_TYPES:
            continue  # 非法枚举已由 JSON Schema 拒绝
        if not _value_type_matches(value_type, p_raw.get("default")):
            diags.append(
                _err_file(
                    SYS_CFG_INVALID,
                    SEVERITY_ERROR,
                    f"参数 {name!r} default 类型与 value_type={value_type!r} 不一致",
                    file=file,
                    field=f"parameters.{name}.default",
                )
            )
        enum_raw = p_raw.get("enum")
        if isinstance(enum_raw, list):
            for idx, item in enumerate(enum_raw):
                if not _value_type_matches(value_type, item):
                    diags.append(
                        _err_file(
                            SYS_CFG_INVALID,
                            SEVERITY_ERROR,
                            f"参数 {name!r} enum[{idx}] 类型与 value_type={value_type!r} 不一致",
                            file=file,
                            field=f"parameters.{name}.enum[{idx}]",
                        )
                    )

    if diags:
        return DeviceModelParseResult(document=None, diagnostics=diags)

    # ---- 阶段 3: 构建不可变文档 ----
    info = DeviceInfo(
        id=d_id,
        version=d_version,
        names=MappingProxyType(dict(names)),
        model_method=device_raw.get("model_method", ""),
        stateful=stateful,
        fidelity=device_raw.get("fidelity", "medium"),
        energy_carriers=tuple(device_raw.get("energy_carriers") or ()),
        capabilities=tuple(capabilities),
    )
    parameters = MappingProxyType(
        {
            name: DeviceParameter(
                name=name,
                value_type=p_raw.get("value_type", "number"),
                quantity=p_raw.get("quantity", ""),
                unit=p_raw.get("unit", ""),
                required=bool(p_raw.get("required", False)),
                default=_freeze(p_raw.get("default")),
                minimum=_number_or_none(p_raw.get("minimum")),
                maximum=_number_or_none(p_raw.get("maximum")),
                enum=(
                    tuple(_freeze(v) for v in p_raw["enum"])
                    if isinstance(p_raw.get("enum"), list)
                    else None
                ),
                optimizable=bool(p_raw.get("optimizable", False)),
                stock_or_addition=p_raw.get("stock_or_addition", "stock"),
            )
            for name, p_raw in params_raw.items()
        }
    )
    ports = MappingProxyType(
        {
            name: DevicePort(
                name=name,
                carrier=p_raw.get("carrier", ""),
                direction=p_raw.get("direction", ""),
                quantity=p_raw.get("quantity", ""),
                unit=p_raw.get("unit", ""),
                capacity_parameter=p_raw.get("capacity_parameter"),
            )
            for name, p_raw in ports_raw.items()
        }
    )
    data_inputs = MappingProxyType(
        {
            column: DeviceDataInput(
                column_id=column,
                value_type=d_raw.get("value_type", "number"),
                quantity=d_raw.get("quantity", ""),
                unit=d_raw.get("unit", ""),
                required=bool(d_raw.get("required", False)),
                minimum=_number_or_none(d_raw.get("minimum")),
                maximum=_number_or_none(d_raw.get("maximum")),
            )
            for column, d_raw in (raw.get("data_inputs") or {}).items()
        }
    )
    states = MappingProxyType(
        {
            name: DeviceState(
                name=name,
                unit=s_raw.get("unit", ""),
                initial_ref=s_raw.get("initial_ref"),
                minimum_ref=s_raw.get("minimum_ref"),
                maximum_ref=s_raw.get("maximum_ref"),
            )
            for name, s_raw in (states_raw).items()
        }
    )
    # extensions 深度冻结（递归转换嵌套 dict/list，保证不可变文档全深度不可变）
    extensions = _freeze(raw.get("extensions") or {})
    document = DeviceModelDocument(
        schema_id=SCHEMA_ID,
        schema_version=SCHEMA_VERSION,
        device=info,
        parameters=parameters,
        ports=ports,
        data_inputs=data_inputs,
        states=states,
        model_commands=MappingProxyType(parsed_commands),
        extensions=extensions,
    )
    return DeviceModelParseResult(document=document, diagnostics=[])


def _number_or_none(value: object) -> float | None:
    """数值字段：int/float 原样；bool 排除；其余 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _value_type_matches(value_type: str, value: object) -> bool:
    """value_type 与取值类型交叉校验（default/enum 元素）。

    规则：
    - 未声明/显式 null、结构化 dict/list 不在此判定（结构校验另行负责）；
    - ``$price:`` 是价格引用占位符（解析期替换为数值），不按字面字符串判定；
    - bool 不是 number（Python 中 bool 是 int 子类，必须显式排除）。
    """
    if value is None or isinstance(value, (dict, list)):
        return True
    if isinstance(value, str):
        if value.startswith("$price:"):
            return True
        return value_type == "string"
    if isinstance(value, bool):
        return value_type == "boolean"
    if isinstance(value, (int, float)):
        return value_type == "number"
    return False


__all__ = [
    "DeviceModelParseResult",
    "parse_device_model_yaml",
    "CARRIER_VOCABULARY",
    "CARRIER_DEFAULT_QUANTITY",
    "QUANTITY_CANONICAL_UNIT",
    "normalize_unit",
    "_load_schema",
]
