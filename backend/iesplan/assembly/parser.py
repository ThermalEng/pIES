"""ies.assembly 3.0.0 安全解析与结构校验。

单管线职责切分:
- ``parse_assembly_doc`` 只做 YAML 1.2 安全子集解析(复用 core.yamlmini
  安全解析器,拒绝重复键/锚点/别名/合并键/任意对象构造),产出原始文档树,
  不做任何结构语义判定;
- ``run_structure_checks`` 做唯一一次结构阶段校验(schema 标识与版本、
  顶层章节与各节字段/类型/ID/枚举/引用形状、精确版本引用、资源来源与
  路径安全、禁止字段扫描、extensions 命名空间规则),由校验器在
  ``validate_assembly_doc`` 内调用一次。装配不包含 ``calculation``:
  含该章节的文档以未知章节结构诊断拒绝。

本模块只消费结构信息,不读取设备注册表、数据集与计算能力(由
validator.py 领域阶段完成)。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from iesplan.assembly.contracts import SCHEMA_ID, SCHEMA_VERSION
from iesplan.assembly.diags import (
    ASM_SYN_FIELD,
    ASM_SYN_FORBIDDEN,
    ASM_SYN_PARSE,
    ASM_SYN_PATH,
    ASM_SYN_SCHEMA,
    ASM_SYN_SECTION,
    ASM_SYN_TYPE,
    ASM_SYN_VERSION,
    ASM_SYN_VERSION_PIN,
)
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.core.diagnostics import Diagnostic
from iesplan.core.yamlmini import YamlParseError as _YamlParseError
from iesplan.core.yamlmini import load as _load_yaml

#: 顶层章节(各节均必需,无内容写空 {} 或 [];resources 数据集注册表已删除,
#: 预定义来源直接在设备 predefined_interfaces 中声明路径/值/目标类型)
TOP_SECTIONS: tuple[str, ...] = (
    "schema",
    "schema_version",
    "assembly",
    "project_baseline",
    "devices",
    "connections",
    "finance",
    "finance_binding",
    "tariff_bindings",
    "planning",
    "outputs",
    "extensions",
)

_ASSEMBLY_KEYS: tuple[str, ...] = ("id", "name")
_BASELINE_KEYS: tuple[str, ...] = ("resolution", "start_year", "scenario_mode")
_SOURCE_KINDS: tuple[str, ...] = ("relative_file", "object")
#: 设备实例键(ies.device-model 2.0 纯技术契约):定义引用/存量新增身份/
#: 非时变技术常量覆盖/预定义接口来源声明；旧 model/parameters/data 一律未知键拒绝
_DEVICE_KEYS: tuple[str, ...] = ("definition", "asset_origin", "properties", "predefined_interfaces")
#: 存量/新增身份(不得推断，缺失阻断)
_ASSET_ORIGINS: tuple[str, ...] = ("existing", "new")
#: 预定义来源模式判别联合
_PREDEFINED_MODES: tuple[str, ...] = ("constant", "data_repeat", "data_full", "data_predict")
#: 各模式允许的精确键集合(缺少/多余一律拒绝，无默认)
_PREDEFINED_KEYS: dict[str, frozenset] = {
    "constant": frozenset({"mode", "value"}),
    "data_repeat": frozenset({"mode", "path"}),
    "data_full": frozenset({"mode", "path"}),
    "data_predict": frozenset({"mode", "path", "target_type"}),
}

#: finance.ref kind 判别联合(object 精确对象引用 / 包内 relative_file)
_FINANCE_REF_KINDS: tuple[str, ...] = ("object", "relative_file")
#: finance_binding 实例类别判别联合
_COST_BIND_TYPES: tuple[str, ...] = ("costed", "none")
#: driver 聚合(时序 interface 必需；property 禁止写)
_DRIVER_AGGREGATIONS: tuple[str, ...] = ("integrate_positive", "sum_positive")
_CONNECTION_KEYS: tuple[str, ...] = ("from", "to")
_OUTPUTS_KEYS: tuple[str, ...] = ("series", "metrics")
#: 计费聚合白名单(时序接口必需；scalar 不用于能源计价)
_TARIFF_AGGREGATIONS: tuple[str, ...] = ("integrate_positive", "sum_positive")
#: 计费绑定条目键
_TARIFF_BINDING_KEYS: tuple[str, ...] = ("price", "instance", "interface", "aggregation")
#: planning 章节键(objective 必需；variables/constraints 缺席视为空)
_PLANNING_KEYS: tuple[str, ...] = ("objective", "variables", "constraints")

#: planning 变量条目允许键(公共 PlanningVariable 契约的结构侧；无年度索引)
_VARIABLE_KEYS: tuple[str, ...] = (
    "device_ref", "parameter",
    "lower_bound", "upper_bound", "unit",
)

#: 全项目统一计算分辨率(均能确定性切分一天:96/48/24 步)
_RESOLUTIONS: tuple[str, ...] = ("15min", "30min", "1h")
#: 场景模式(当前固定为 single)
_SCENARIO_MODES: tuple[str, ...] = ("single",)

#: 局部 ID(lower_snake_case 或短横线;同一文件内保持一致)
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
#: 端点/输出引用 <device-instance>.<port-id> / <device>.<output>
_REF_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]*\.[a-z0-9_][a-z0-9_.-]*$")

def _is_finite_number(val: object) -> bool:
    """有限数值判定(bool 永假；NaN/Inf 假；其余 int/float 真)。"""
    import math as _math

    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    return _math.isfinite(float(val))


#: 禁止出现在装配 YAML 中的键(宪法 §7.8:shell/command/executable/函数模块路径/
#: 环境变量/凭证);source 映射内的 path 等资源字段除外
_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "command",
        "shell",
        "executable",
        "module",
        "function",
        "env",
        "environment",
        "credential",
        "credentials",
        "secret",
        "token",
    }
)


@dataclass(slots=True)
class ParseDocResult:
    """安全解析结果:原始文档树(doc)与解析诊断列表(无结构判定)。"""

    doc: dict[str, Any] | None
    diagnostics: list[Diagnostic]

    @property
    def ok(self) -> bool:
        """无阻断诊断(解析成功;结构判定由校验器完成)。"""
        return self.doc is not None and not any(d.blocking for d in self.diagnostics)


def parse_assembly_doc(text: str, *, source_name: str = "assembly.yaml") -> ParseDocResult:
    """ies.assembly 3.0.0 文本 → 原始文档树(仅安全解析,无结构判定)。

    产出 YAML 安全解析诊断(ASM-SYN-PARSE);解析失败时 doc 为 None。
    结构语义判定只在 ``run_structure_checks`` 做一次,由校验器经
    ``validate_assembly_doc`` 调用。资源(relative_file)不在本阶段读取,
    由校验器解析为对象形态引用。
    """
    diags: list[Diagnostic] = []
    try:
        tree = _load_yaml(text)
    except _YamlParseError as exc:
        diags.append(
            make_diag(
                ASM_SYN_PARSE,
                severity="error",
                blocking=True,
                params={"source": source_name, "line": exc.line, "detail": str(exc)},
                location={"object_type": "assembly", "field": f"{source_name}:{exc.line}"},
            )
        )
        return ParseDocResult(doc=None, diagnostics=diags)
    if not isinstance(tree, dict):
        diags.append(
            make_diag(
                ASM_SYN_PARSE,
                severity="error",
                blocking=True,
                params={"source": source_name, "reason": "top_level_not_mapping"},
                location={"object_type": "assembly", "field": source_name},
            )
        )
        return ParseDocResult(doc=None, diagnostics=diags)
    return ParseDocResult(doc=dict(tree), diagnostics=diags)


def run_structure_checks(
    tree: dict[str, Any],
    *,
    source_name: str = "<doc>",
    diags: list[Diagnostic] | None = None,
) -> dict[str, Any] | None:
    """唯一一次结构阶段校验入口(仅由校验器 ``validate_assembly_doc`` 调用)。

    返回 normalized 文档树(已浅拷贝);存在阻断诊断时返回 None,诊断写入
    ``diags``(调用方传入的容器)或内部容器。
    """
    own_diags: list[Diagnostic] = [] if diags is None else diags
    builder = _DocBuilder(own_diags)
    doc = builder.build(tree, source_name=source_name)
    return doc


# ---------------------------------------------------------------------------
# 结构校验器
# ---------------------------------------------------------------------------


class _DocBuilder:
    """结构校验 + 原始文档树构建(定位路径如 "devices.hp1.parameters.cop")。"""

    def __init__(self, diags: list[Diagnostic]) -> None:
        self.diags = diags

    # -- 诊断辅助 ------------------------------------------------------------

    def _diag(
        self,
        code: str,
        field: str,
        params: dict | None = None,
        *,
        severity: str = "error",
        blocking: bool = True,
    ) -> None:
        self.diags.append(
            make_diag(
                code,
                severity=severity,
                blocking=blocking,
                params={"field": field, **(params or {})},
                location={"object_type": "assembly", "field": field},
            )
        )

    def _require_map(self, value: Any, field: str) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            self._diag(ASM_SYN_TYPE, field, {"reason": "expected_map", "actual": type(value).__name__})
            return None
        for key in value:
            if not isinstance(key, str):
                self._diag(ASM_SYN_PARSE, f"{field}.<key>", {"reason": "non_string_key"})
                return None
        return value

    def _str(self, obj: dict, key: str, field: str, *, required: bool = False) -> str | None:
        val = obj.get(key)
        if val is None:
            if required:
                self._diag(ASM_SYN_FIELD, field, {"reason": "missing_field", "key": key})
            return None
        if isinstance(val, str):
            return val
        self._diag(ASM_SYN_TYPE, field, {"reason": "expected_string", "actual": type(val).__name__})
        return None

    def _bool(self, val: Any, field: str) -> bool | None:
        if isinstance(val, bool):
            return val
        self._diag(ASM_SYN_TYPE, field, {"reason": "expected_boolean", "actual": type(val).__name__})
        return None

    def _int(self, val: Any, field: str) -> int | None:
        if isinstance(val, int) and not isinstance(val, bool):
            return val
        self._diag(ASM_SYN_TYPE, field, {"reason": "expected_integer", "actual": type(val).__name__})
        return None

    def _enum(self, val: Any, allowed: tuple[str, ...], field: str) -> str | None:
        if not isinstance(val, str) or val not in allowed:
            self._diag(
                ASM_SYN_TYPE,
                field,
                {"reason": "invalid_enum", "value": repr(val), "allowed": list(allowed)},
            )
            return None
        return val

    def _local_id(self, val: Any, field: str) -> str | None:
        if not isinstance(val, str) or not _ID_RE.match(val):
            self._diag(
                ASM_SYN_TYPE,
                field,
                {"reason": "invalid_local_id", "value": repr(val), "pattern": _ID_RE.pattern},
            )
            return None
        return val

    def _ref_shape(self, val: Any, field: str) -> str | None:
        if isinstance(val, str) and _REF_RE.match(val):
            return val
        self._diag(ASM_SYN_TYPE, field, {"reason": "invalid_ref", "value": repr(val)})
        return None

    # -- 顶层 ----------------------------------------------------------------

    def build(self, tree: dict[str, Any], *, source_name: str) -> dict[str, Any] | None:
        for key in tree:
            if key not in TOP_SECTIONS:
                self._diag(ASM_SYN_SECTION, key, {"reason": "unknown_section", "section": key})
        missing = [k for k in TOP_SECTIONS if k not in tree]
        for section in missing:
            self._diag(ASM_SYN_FIELD, section, {"reason": "missing_section", "section": section})

        schema = self._str(tree, "schema", "schema")
        if schema is not None and schema != SCHEMA_ID:
            self._diag(ASM_SYN_SCHEMA, "schema", {"actual": schema, "expected": SCHEMA_ID})
        version = self._str(tree, "schema_version", "schema_version")
        if version is not None and version != SCHEMA_VERSION:
            self._diag(
                ASM_SYN_VERSION,
                "schema_version",
                {"expected": SCHEMA_VERSION, "actual": version},
            )

        self._build_assembly(tree.get("assembly"), "assembly")
        self._build_project_baseline(tree.get("project_baseline"), "project_baseline")
        self._build_devices(tree.get("devices"), "devices")
        self._build_finance(tree.get("finance"), "finance")
        self._build_finance_binding(tree.get("finance_binding"), "finance_binding")
        self._build_connections(tree.get("connections"), "connections")
        self._build_tariff_bindings(tree.get("tariff_bindings"), "tariff_bindings")
        self._build_planning(tree.get("planning"), "planning")
        self._build_outputs(tree.get("outputs"), "outputs")
        self._build_extensions(tree.get("extensions"), "extensions")
        self._scan_forbidden(tree, "assembly")

        has_blocking = any(d.blocking for d in self.diags)
        return None if has_blocking else dict(tree)

    # -- 各节 ----------------------------------------------------------------

    def _build_assembly(self, value: Any, field: str) -> None:
        m = self._require_map(value, field)
        if m is None:
            return
        self._local_id(m.get("id"), f"{field}.id")
        self._str(m, "name", f"{field}.name", required=True)
        for key in m:
            if key not in _ASSEMBLY_KEYS:
                self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})

    def _build_project_baseline(self, value: Any, field: str) -> None:
        """项目计算基线:resolution/start_year/scenario_mode 均须显式声明。

        基线不保存时区;含 start/end/endpoint/leap_year 等旧键以未知键拒绝。
        """
        m = self._require_map(value, field)
        if m is None:
            return
        if "resolution" not in m:
            self._diag(ASM_SYN_FIELD, f"{field}.resolution", {"reason": "missing_field", "key": "resolution"})
        else:
            self._enum(m.get("resolution"), _RESOLUTIONS, f"{field}.resolution")
        if "start_year" not in m:
            self._diag(ASM_SYN_FIELD, f"{field}.start_year", {"reason": "missing_field", "key": "start_year"})
        else:
            self._int(m.get("start_year"), f"{field}.start_year")
        if "scenario_mode" not in m:
            self._diag(
                ASM_SYN_FIELD, f"{field}.scenario_mode", {"reason": "missing_field", "key": "scenario_mode"}
            )
        else:
            self._enum(m.get("scenario_mode"), _SCENARIO_MODES, f"{field}.scenario_mode")
        for key in m:
            if key not in _BASELINE_KEYS:
                self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})

    def _check_package_path(self, path: str, field: str) -> None:
        """包内相对路径:禁止绝对路径、..、~、盘符、反斜杠与空段。"""
        reasons: list[str] = []
        if not path:
            reasons.append("empty")
        if path.startswith("/"):
            reasons.append("absolute")
        if path.startswith("\\"):
            reasons.append("backslash")
        if path.startswith("~"):
            reasons.append("home_escape")
        if ":" in path:
            reasons.append("drive_or_scheme")
        for segment in path.split("/"):
            if segment in ("", ".", ".."):
                reasons.append(f"segment:{segment!r}")
        if reasons:
            self._diag(
                ASM_SYN_PATH,
                field,
                {"reason": "invalid_package_relative_path", "path": path, "detail": reasons},
            )

    def _build_devices(self, value: Any, field: str) -> None:
        m = self._require_map(value, field)
        if m is None:
            return
        for dev_id in m:
            self._local_id(dev_id, f"{field}.{dev_id}")
            self._build_device(m[dev_id], f"{field}.{dev_id}")

    def _build_device(self, value: Any, field: str) -> None:
        m = self._require_map(value, field)
        if m is None:
            return
        self._build_definition(m.get("definition"), f"{field}.definition")
        if "asset_origin" not in m:
            self._diag(ASM_SYN_FIELD, f"{field}.asset_origin", {"reason": "missing_field", "key": "asset_origin"})
        else:
            self._enum(m.get("asset_origin"), _ASSET_ORIGINS, f"{field}.asset_origin")
        for key in m:
            if key not in _DEVICE_KEYS:
                self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
        props = m.get("properties")
        if props is not None:
            pmap = self._require_map(props, f"{field}.properties")
            if pmap is not None:
                for name, entry in pmap.items():
                    self._build_property(entry, f"{field}.properties.{name}")
        predef = m.get("predefined_interfaces")
        if predef is not None:
            pmap = self._require_map(predef, f"{field}.predefined_interfaces")
            if pmap is not None:
                for iface_id, binding in pmap.items():
                    if not isinstance(iface_id, str) or not _ID_RE.match(iface_id):
                        self._diag(
                            ASM_SYN_TYPE,
                            f"{field}.predefined_interfaces.{iface_id}",
                            {"reason": "invalid_local_id"},
                        )
                        continue
                    self._build_predefined_binding(binding, f"{field}.predefined_interfaces.{iface_id}")

    def _build_definition(self, value: Any, field: str) -> None:
        """definition.id: 稳定设备 ID(裸 ID，禁止 @版本钉选与 latest 别名)。"""
        m = self._require_map(value, field)
        if m is None:
            self._diag(ASM_SYN_FIELD, field, {"reason": "missing_field", "key": "definition"})
            return
        for key in m:
            if key != "id":
                self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
        raw_id = m.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            self._diag(ASM_SYN_TYPE, f"{field}.id", {"reason": "expected_non_empty_string"})
            return
        if "@" in raw_id:
            self._diag(
                ASM_SYN_VERSION_PIN,
                f"{field}.id",
                {"reason": "version_pin_forbidden", "value": raw_id},
            )

    def _build_property(self, value: Any, field: str) -> None:
        """properties.<name>: {value: 有限标量, unit: 非空单位}，精确键集合。"""
        m = self._require_map(value, field)
        if m is None:
            return
        for key in m:
            if key not in ("value", "unit"):
                self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
        if "value" not in m:
            self._diag(ASM_SYN_FIELD, f"{field}.value", {"reason": "missing_field", "key": "value"})
        else:
            val = m["value"]
            if isinstance(val, bool) or not isinstance(val, (int, float, str)):
                self._diag(
                    ASM_SYN_TYPE,
                    f"{field}.value",
                    {"reason": "expected_scalar", "actual": type(val).__name__},
                )
            elif not _is_finite_number(val):
                self._diag(ASM_SYN_TYPE, f"{field}.value", {"reason": "non_finite_value"})
        unit = m.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            self._diag(ASM_SYN_TYPE, f"{field}.unit", {"reason": "expected_non_empty_unit"})

    def _build_predefined_binding(self, value: Any, field: str) -> None:
        """predefined_interfaces.<iface>: 严格判别联合，无默认无多余键。"""
        m = self._require_map(value, field)
        if m is None:
            return
        mode = m.get("mode")
        if mode not in _PREDEFINED_MODES:
            self._diag(
                ASM_SYN_TYPE,
                f"{field}.mode",
                {"reason": "invalid_enum", "value": repr(mode), "allowed": list(_PREDEFINED_MODES)},
            )
            return
        allowed = _PREDEFINED_KEYS[mode]
        for key in m:
            if key not in allowed:
                self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
        for key in allowed:
            if key not in m:
                self._diag(ASM_SYN_FIELD, f"{field}.{key}", {"reason": "missing_field", "key": key})
        if mode == "constant":
            val = m.get("value")
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                self._diag(
                    ASM_SYN_TYPE,
                    f"{field}.value",
                    {"reason": "expected_finite_number", "actual": type(val).__name__},
                )
            elif not _is_finite_number(val):
                self._diag(ASM_SYN_TYPE, f"{field}.value", {"reason": "non_finite_value"})
        else:
            path = m.get("path")
            if isinstance(path, str) and path:
                self._check_package_path(path, f"{field}.path")
                if not path.lower().endswith(".csv"):
                    self._diag(
                        ASM_SYN_TYPE, f"{field}.path", {"reason": "expected_csv_path", "path": path}
                    )
            if mode == "data_predict":
                target = m.get("target_type")
                if not isinstance(target, str) or not target.strip():
                    self._diag(
                        ASM_SYN_TYPE,
                        f"{field}.target_type",
                        {"reason": "expected_non_empty_string"},
                    )

    def _build_finance(self, value: Any, field: str) -> None:
        """finance: {ref: {kind, object_id?/path?}, profile_id, revision?} 精确引用。

        只固定有效快照身份，不内联完整财务配置。revision 为应用装配闸门
        填写的冻结血缘(手写文件可缺席)；出现时须为正整数。
        """
        m = self._require_map(value, field)
        if m is None:
            return
        for key in m:
            if key not in ("ref", "profile_id", "revision"):
                self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
        if m.get("revision") is not None:
            rev = m.get("revision")
            if isinstance(rev, bool) or not isinstance(rev, int) or rev < 1:
                self._diag(
                    ASM_SYN_TYPE, f"{field}.revision",
                    {"reason": "wrong_type", "actual": type(rev).__name__},
                )
        ref = self._require_map(m.get("ref"), f"{field}.ref")
        if ref is not None:
            kind = self._enum(ref.get("kind"), _FINANCE_REF_KINDS, f"{field}.ref.kind")
            for key in ref:
                if key not in ("kind", "object_id", "media_type", "path"):
                    self._diag(
                        ASM_SYN_PARSE, f"{field}.ref.{key}", {"reason": "unknown_key", "key": key}
                    )
            if kind == "object":
                object_id = self._str(ref, "object_id", f"{field}.ref.object_id", required=True)
                if object_id is not None and not object_id:
                    self._diag(
                        ASM_SYN_FIELD,
                        f"{field}.ref.object_id",
                        {"reason": "missing_field", "key": "object_id"},
                    )
                if "path" in ref:
                    self._diag(
                        ASM_SYN_FIELD,
                        f"{field}.ref.path",
                        {"reason": "unexpected_field", "key": "path"},
                    )
            elif kind == "relative_file":
                path = self._str(ref, "path", f"{field}.ref.path", required=True)
                if path is not None:
                    self._check_package_path(path, f"{field}.ref.path")
                if "object_id" in ref:
                    self._diag(
                        ASM_SYN_FIELD,
                        f"{field}.ref.object_id",
                        {"reason": "unexpected_field", "key": "object_id"},
                    )
        self._str(m, "profile_id", f"{field}.profile_id", required=True)

    def _build_finance_binding(self, value: Any, field: str) -> None:
        """finance_binding.instances: 每个设备实例显式 costed/none 判别联合。"""
        m = self._require_map(value, field)
        if m is None:
            return
        for key in m:
            if key != "instances":
                self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
        instances = self._require_map(m.get("instances"), f"{field}.instances")
        if instances is None:
            return
        for inst_id, entry in instances.items():
            if not isinstance(inst_id, str) or not _ID_RE.match(inst_id):
                self._diag(
                    ASM_SYN_TYPE,
                    f"{field}.instances.{inst_id}",
                    {"reason": "invalid_local_id"},
                )
                continue
            self._build_cost_binding(entry, f"{field}.instances.{inst_id}")

    def _build_cost_binding(self, value: Any, field: str) -> None:
        entry = self._require_map(value, field)
        if entry is None:
            return
        bind_type = self._enum(entry.get("type"), _COST_BIND_TYPES, f"{field}.type")
        for key in entry:
            if key not in ("type", "finance_type", "drivers"):
                self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
        if bind_type == "none":
            for forbidden in ("finance_type", "drivers"):
                if forbidden in entry:
                    self._diag(
                        ASM_SYN_TYPE,
                        f"{field}.{forbidden}",
                        {"reason": "forbidden_for_type_none"},
                    )
            return
        if bind_type != "costed":
            return
        self._str(entry, "finance_type", f"{field}.finance_type", required=True)
        drivers = entry.get("drivers")
        if drivers is None:
            return
        dmap = self._require_map(drivers, f"{field}.drivers")
        if dmap is None:
            return
        for driver, target in dmap.items():
            if not isinstance(driver, str) or not _ID_RE.match(driver):
                self._diag(
                    ASM_SYN_TYPE,
                    f"{field}.drivers.{driver}",
                    {"reason": "invalid_local_id"},
                )
                continue
            self._build_driver_target(target, f"{field}.drivers.{driver}")

    def _build_driver_target(self, value: Any, field: str) -> None:
        """driver 映射: {property: <prop>} XOR {interface: <iface>, aggregation}。"""
        m = self._require_map(value, field)
        if m is None:
            return
        has_property = "property" in m
        has_interface = "interface" in m
        if has_property == has_interface:
            self._diag(
                ASM_SYN_TYPE,
                field,
                {"reason": "property_interface_xor", "keys": sorted(m)},
            )
            return
        for key in m:
            if key not in ("property", "interface", "aggregation"):
                self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
        if has_property:
            self._str(m, "property", f"{field}.property", required=True)
            if "aggregation" in m:
                self._diag(
                    ASM_SYN_TYPE,
                    f"{field}.aggregation",
                    {"reason": "forbidden_for_property_driver"},
                )
        else:
            self._str(m, "interface", f"{field}.interface", required=True)
            self._enum(
                m.get("aggregation"), _DRIVER_AGGREGATIONS, f"{field}.aggregation"
            )

    def _build_connections(self, value: Any, field: str) -> None:
        m = self._require_map(value, field)
        if m is None:
            return
        for conn_id in m:
            self._local_id(conn_id, f"{field}.{conn_id}")
            conn = self._require_map(m[conn_id], f"{field}.{conn_id}")
            if conn is None:
                continue
            self._ref_shape(conn.get("from"), f"{field}.{conn_id}.from")
            self._ref_shape(conn.get("to"), f"{field}.{conn_id}.to")
            for key in conn:
                if key not in _CONNECTION_KEYS:
                    self._diag(
                        ASM_SYN_PARSE, f"{field}.{conn_id}.{key}", {"reason": "unknown_key", "key": key}
                    )

    def _build_tariff_bindings(self, value: Any, field: str) -> None:
        """tariff_bindings: {binding_id: {price, instance, interface, aggregation}}。"""
        m = self._require_map(value, field)
        if m is None:
            return
        for binding_id in m:
            self._local_id(binding_id, f"{field}.{binding_id}")
            entry = self._require_map(m[binding_id], f"{field}.{binding_id}")
            if entry is None:
                continue
            self._str(entry, "price", f"{field}.{binding_id}.price", required=True)
            self._str(entry, "instance", f"{field}.{binding_id}.instance", required=True)
            self._str(entry, "interface", f"{field}.{binding_id}.interface", required=True)
            self._enum(
                entry.get("aggregation"), _TARIFF_AGGREGATIONS,
                f"{field}.{binding_id}.aggregation",
            )
            for key in entry:
                if key not in _TARIFF_BINDING_KEYS:
                    self._diag(
                        ASM_SYN_PARSE, f"{field}.{binding_id}.{key}",
                        {"reason": "unknown_key", "key": key},
                    )

    def _build_planning(self, value: Any, field: str) -> None:
        """planning: {objective: {sense, expression}, variables?, constraints?}。

        结构只消费形态(键存在性/字符串非空)；sense 枚举归属、表达式语义
        与量纲由阶段 4 经核心契约与公共引擎判定。
        """
        m = self._require_map(value, field)
        if m is None:
            return
        for key in m:
            if key not in _PLANNING_KEYS:
                self._diag(
                    ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key}
                )
        objective = self._require_map(m.get("objective"), f"{field}.objective")
        if objective is not None:
            self._str(objective, "sense", f"{field}.objective.sense", required=True)
            expr = self._str(
                objective, "expression", f"{field}.objective.expression", required=True
            )
            if expr is not None and not expr.strip():
                self._diag(
                    ASM_SYN_FIELD, f"{field}.objective.expression", {"reason": "empty_expr"}
                )
        variables = m.get("variables")
        if variables is None:
            # 公共 PlanningConfig 要求至少一个规划变量：缺席与空映射一致阻断
            self._diag(
                ASM_SYN_FIELD, f"{field}.variables",
                {"reason": "missing_field", "key": "variables"},
            )
        else:
            vm = self._require_map(variables, f"{field}.variables")
            if vm is not None:
                if not vm:
                    # 公共 PlanningConfig 要求至少一个规划变量(空映射结构阻断)
                    self._diag(
                        ASM_SYN_FIELD, f"{field}.variables",
                        {"reason": "empty_variables"},
                    )
                for var_name in vm:
                    self._local_id(var_name, f"{field}.variables.{var_name}")
                    entry = self._require_map(
                        vm[var_name], f"{field}.variables.{var_name}"
                    )
                    if entry is not None:
                        self._variable_entry(entry, f"{field}.variables.{var_name}")
        constraints = m.get("constraints")
        if constraints is not None:
            cm = self._require_map(constraints, f"{field}.constraints")
            if cm is not None:
                for const_name in cm:
                    self._local_id(const_name, f"{field}.constraints.{const_name}")
                    entry = self._require_map(cm[const_name], f"{field}.constraints.{const_name}")
                    if entry is None:
                        continue
                    kind = self._str(
                        entry, "type", f"{field}.constraints.{const_name}.type",
                        required=True,
                    )
                    if kind is not None and not kind.strip():
                        self._diag(
                            ASM_SYN_FIELD, f"{field}.constraints.{const_name}.type",
                            {"reason": "empty_string", "key": "type"},
                        )
                    expr = self._str(
                        entry, "expression", f"{field}.constraints.{const_name}.expression",
                        required=True,
                    )
                    if expr is not None and not expr.strip():
                        self._diag(
                            ASM_SYN_FIELD, f"{field}.constraints.{const_name}.expression",
                            {"reason": "empty_expr"},
                        )
                    if entry.get("enabled") is not None:
                        self._bool(entry["enabled"], f"{field}.constraints.{const_name}.enabled")
                    for key in entry:
                        if key not in ("type", "expression", "enabled"):
                            self._diag(
                                ASM_SYN_PARSE, f"{field}.constraints.{const_name}.{key}",
                                {"reason": "unknown_key", "key": key},
                            )

    def _variable_entry(self, entry: Mapping, field: str) -> None:
        """规划变量条目形态(公共 PlanningVariable 契约的结构侧)。

        只判定形态(required/允许键/标量类型);界区间/设备存在性等语义由
        阶段 4 经核心契约判定。
        """
        for key in entry:
            if key not in _VARIABLE_KEYS:
                self._diag(
                    ASM_SYN_PARSE, f"{field}.{key}",
                    {"reason": "unknown_key", "key": key},
                )
        for req in ("device_ref", "parameter"):
            if req not in entry:
                self._diag(
                    ASM_SYN_FIELD, field,
                    {"reason": "missing_field", "key": req},
                )
            else:
                val = self._str(entry, req, f"{field}.{req}", required=True)
                if val is not None and not val.strip():
                    self._diag(
                        ASM_SYN_FIELD, f"{field}.{req}", {"reason": "empty_string"},
                    )
        for bound in ("lower_bound", "upper_bound"):
            # 公共契约界为十进制: 确定性投影输出规范字符串，int 同样接受；
            # float/bool 与其他类型结构阻断(与 schema 接受集合一致)。
            val = entry.get(bound)
            if val is not None and (
                isinstance(val, bool) or not isinstance(val, (int, str))
            ):
                self._diag(
                    ASM_SYN_TYPE, f"{field}.{bound}",
                    {"reason": "wrong_type", "actual": type(val).__name__},
                )
        unit = entry.get("unit")
        if unit is not None:
            val = self._str(entry, "unit", f"{field}.unit", required=True)
            if val is not None and not val.strip():
                self._diag(
                    ASM_SYN_FIELD, f"{field}.unit", {"reason": "empty_string"},
                )

    def _build_outputs(self, value: Any, field: str) -> None:
        m = self._require_map(value, field)
        if m is None:
            return
        for key in m:
            if key not in _OUTPUTS_KEYS:
                self._diag(ASM_SYN_PARSE, f"{field}.{key}", {"reason": "unknown_key", "key": key})
        for list_key in ("series", "metrics"):
            arr = m.get(list_key)
            if arr is None:
                self._diag(ASM_SYN_FIELD, f"{field}.{list_key}", {"reason": "missing_field", "key": list_key})
                continue
            if not isinstance(arr, list):
                self._diag(ASM_SYN_TYPE, f"{field}.{list_key}", {"reason": "expected_list"})
                continue
            for i, ref in enumerate(arr):
                self._ref_shape(ref, f"{field}.{list_key}[{i}]")

    def _build_extensions(self, value: Any, field: str) -> None:
        m = self._require_map(value, field)
        if m is None:
            return
        for key in m:
            if not isinstance(key, str) or "." not in key:
                self._diag(
                    ASM_SYN_PARSE,
                    f"{field}.{key}",
                    {"reason": "extensions_key_not_namespaced", "key": key},
                )

    # -- 禁止字段扫描 ---------------------------------------------------------

    def _scan_forbidden(self, node: Any, path: str) -> None:
        """递归扫描禁止键(shell/command/executable/函数模块路径/环境变量/凭证)。"""
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    continue
                # 资源来源映射内的字段(如 relative_file.path)不是可执行字段
                if key == "source" and isinstance(child, dict) and child.get("kind") in _SOURCE_KINDS:
                    continue
                if key in _FORBIDDEN_KEYS:
                    self._diag(
                        ASM_SYN_FORBIDDEN,
                        f"{path}.{key}",
                        {"reason": "forbidden_field", "key": key},
                    )
                self._scan_forbidden(child, f"{path}.{key}")
        elif isinstance(node, list):
            for i, child in enumerate(node):
                self._scan_forbidden(child, f"{path}[{i}]")


__all__ = [
    "ParseDocResult",
    "parse_assembly_doc",
    "run_structure_checks",
    "TOP_SECTIONS",
]
