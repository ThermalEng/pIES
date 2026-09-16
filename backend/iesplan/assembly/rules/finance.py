"""财务完整性检查(ASM-FIN-001 / ASM-BIND-002)。

消费不可变 ``EffectiveFinanceConfig`` 密封对象(调用方显式注入或包内
文件经 finance 公开边界解析), 校验 finance 精确引用与
finance_binding(实例缺席/类别/driver 齐全性/量纲)。

依赖方向: 本模块只消费 finance 公开门面(``EffectiveFinanceConfig``、
``effective_from_yaml``、``FinanceTripletError``)与 core 公共契约;
快照本身的字头与领域约束由 finance 域在签发/解析边界负责, 本层不
复制字段校验(宪法 §2.6), 只做装配引用身份与绑定语义判定。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from iesplan.assembly.diags import (
    ASM_COST_BIND_INVALID,
    ASM_FIN_INVALID,
)
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.core.diagnostics import Diagnostic
from iesplan.core.units import dims_of
from iesplan.core.yamlmini import YamlParseError
from iesplan.finance import (
    CostComponent,
    EffectiveFinanceConfig,
    FinanceTripletError,
    FinanceTypeEntry,
    effective_from_yaml,
)

#: 量纲多重集键(与 core.units dims_of 口径一致；energy 与 power×time 为不同键)
_DIM_TIME = "time"
_DIM_POWER = "power"
_DIM_ENERGY = "energy"

#: 增量语义下的分量集合: new 计入全量分量；existing 只计入周期可变分量
_COMPONENTS_NEW: tuple[str, ...] = ("upfront_capex", "annual_fixed_om", "period_variable_om")
_COMPONENTS_EXISTING: tuple[str, ...] = ("period_variable_om",)


def _add_dims(left: Mapping, right: Mapping) -> dict:
    """量纲多重集加法(指数可正可负；零指数删除)。

    注意：collections.Counter 的 + 运算符丢弃零与负计数，不可用于量纲运算。
    """
    out = dict(left)
    for key, value in right.items():
        out[key] = out.get(key, 0) + value
        if out[key] == 0:
            del out[key]
    return out


def _fail(
    diags: list[Diagnostic],
    code: str,
    field: str,
    reason: str,
    extra: dict | None = None,
    object_type: str = "assembly",
    object_id: str = "",
) -> None:
    params: dict = {"reason": reason}
    if extra:
        params.update(extra)
    loc: dict = {"object_type": object_type, "field": field}
    if object_id:
        loc["object_id"] = object_id
    diags.append(
        make_diag(
            code,
            severity="error",
            blocking=True,
            params=params,
            location=loc,
        )
    )


def _fin_fail(diags, field, reason, extra=None):
    _fail(diags, ASM_FIN_INVALID, field, reason, extra)


def _cost_fail(diags, field, reason, extra=None, object_id=""):
    _fail(
        diags, ASM_COST_BIND_INVALID, field, reason, extra,
        object_type="device", object_id=object_id,
    )


def resolve_finance_snapshot(
    doc: Mapping[str, Any],
    package_dir: str | Path | None,
    finance_snapshots: Mapping[str, EffectiveFinanceConfig] | None,
    diags: list[Diagnostic],
) -> EffectiveFinanceConfig | None:
    """finance 精确引用 → 密封有效快照(引用缺失/解析失败/身份不符阻断)。

    kind=object: 调用方显式注入的密封对象(可信流程由项目 revision 恢复,
    手写/包交换由调用方经 finance 公开边界解析后注入); 只判定引用身份,
    不重验快照字段。
    kind=relative_file: 用户输入边界, 文件文本经 finance 唯一公开解析
    器校验一次(异常映射为诊断, 不复制字段规则)。
    """
    finance = doc.get("finance") or {}
    ref = finance.get("ref") or {}
    kind = ref.get("kind")
    declared_profile = finance.get("profile_id")
    snapshot = None
    if kind == "object":
        object_id = ref.get("object_id")
        snapshot = (finance_snapshots or {}).get(object_id)
        if snapshot is None:
            _fin_fail(
                diags,
                "finance.ref.object_id",
                "snapshot_unavailable",
                {"object_id": object_id},
            )
            return None
    elif kind == "relative_file":
        if package_dir is None:
            _fin_fail(
                diags,
                "finance.ref.path",
                "package_dir_required",
                {"path": ref.get("path")},
            )
            return None
        full_path = Path(package_dir) / str(ref.get("path"))
        try:
            text = full_path.read_bytes().decode("utf-8")
        except OSError as exc:
            _fin_fail(
                diags,
                "finance.ref.path",
                "file_unreadable",
                {"path": str(full_path), "detail": str(exc)},
            )
            return None
        except ValueError as exc:
            _fin_fail(
                diags,
                "finance.ref.path",
                "file_unreadable",
                {"path": str(full_path), "detail": str(exc)},
            )
            return None
        try:
            snapshot = effective_from_yaml(text)
        except (YamlParseError, FinanceTripletError) as exc:
            _fin_fail(
                diags,
                "finance.ref.path",
                "snapshot_invalid",
                {"path": str(full_path), "detail": str(exc)},
            )
            return None
    else:
        return None
    if snapshot.profile_id != declared_profile:
        _fin_fail(
            diags,
            "finance.profile_id",
            "snapshot_profile_mismatch",
            {"declared": declared_profile, "actual": snapshot.profile_id},
        )
        return None
    return snapshot


def _normalized_driver_dims(target_unit: str, aggregation: str | None) -> dict:
    """归一化 driver 量纲。

    scalar 取单位本身；integrate_positive 将功率序列累积为能量
    (power → energy，与量纲词汇的 energy 键对齐；非功率接口回退为
    通用量纲加时间维)。
    """
    dims = dict(dims_of(target_unit))
    if aggregation == "integrate_positive":
        if dims == {_DIM_POWER: 1}:
            return {_DIM_ENERGY: 1}
        dims = _add_dims(dims, {_DIM_TIME: 1})
    return dims


def _expected_dims(component: str, currency: str) -> dict:
    """分量期望量纲: upfront/period → currency；annual → currency/年。"""
    expected = dict(dims_of(currency))
    if component == "annual_fixed_om":
        expected = _add_dims(expected, {_DIM_TIME: -1})
    return expected


def linear_drivers(ft_entry: FinanceTypeEntry) -> dict[str, list[tuple[str, str]]]:
    """{driver: [(component, unit_cost_unit), ...]}(仅 linear 分量声明的 driver)。

    读密封 ``FinanceTypeEntry`` 属性；planning 标识符映射复用同一函数。
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for component in ("upfront_capex", "annual_fixed_om", "period_variable_om"):
        comp: CostComponent | None = getattr(ft_entry, component)
        if comp is None or comp.linear is None:
            continue
        for driver, cost in comp.linear.items():
            out.setdefault(driver, []).append((component, cost.unit_cost.unit))
    return out


def required_components(asset_origin: str) -> tuple[str, ...]:
    """增量语义: new 全量分量；existing 仅周期可变分量。"""
    if asset_origin == "new":
        return _COMPONENTS_NEW
    return _COMPONENTS_EXISTING


def required_driver_names(asset_origin: str, ft_entry: FinanceTypeEntry) -> set[str]:
    """按资产身份计入的分量所需 driver 全集(齐全性判定用)。"""
    needed: set[str] = set()
    for component in required_components(asset_origin):
        comp: CostComponent | None = getattr(ft_entry, component)
        if comp is not None and comp.linear is not None:
            needed.update(comp.linear)
    return needed


def check_finance_binding(
    doc: Mapping[str, Any],
    snapshot: EffectiveFinanceConfig,
    registry,
    diags: list[Diagnostic],
) -> None:
    """finance_binding: 实例全覆盖 + costed/none 语义 + driver 齐全性 + 量纲。"""
    devices = doc.get("devices") or {}
    instances = ((doc.get("finance_binding") or {}).get("instances")) or {}
    for dev_id in devices:
        if dev_id not in instances:
            _cost_fail(
                diags,
                f"finance_binding.instances.{dev_id}",
                "instance_missing",
                {"instance": dev_id},
                object_id=str(dev_id),
            )
    for inst_id, entry in instances.items():
        if inst_id not in devices:
            _cost_fail(
                diags,
                f"finance_binding.instances.{inst_id}",
                "unknown_instance",
                {"instance": inst_id},
            )
            continue
        if not isinstance(entry, Mapping):
            continue
        bind_type = entry.get("type")
        if bind_type == "none":
            continue
        if bind_type != "costed":
            continue
        finance_type = entry.get("finance_type")
        ft_entry = snapshot.finance_types.get(finance_type)
        if ft_entry is None:
            _cost_fail(
                diags,
                f"finance_binding.instances.{inst_id}.finance_type",
                "finance_type_unknown",
                {"instance": inst_id, "finance_type": finance_type},
                object_id=str(inst_id),
            )
            continue
        _check_drivers(doc, snapshot, registry, inst_id, finance_type, ft_entry, entry, diags)


def _check_drivers(
    doc: Mapping[str, Any],
    snapshot: EffectiveFinanceConfig,
    registry,
    inst_id: str,
    finance_type: str,
    ft_entry: FinanceTypeEntry,
    entry: Mapping,
    diags: list[Diagnostic],
) -> None:
    devices = doc.get("devices") or {}
    dev = devices.get(inst_id) or {}
    definition = dev.get("definition") or {}
    descriptor = registry.get(definition.get("id"))
    if descriptor is None:
        return
    asset_origin = dev.get("asset_origin")
    declared = linear_drivers(ft_entry)
    mapped = entry.get("drivers") or {}
    # driver 必须为该 finance_type 已声明的 driver
    for driver, target in mapped.items():
        if driver not in declared:
            _cost_fail(
                diags,
                f"finance_binding.instances.{inst_id}.drivers.{driver}",
                "driver_undeclared",
                {"instance": inst_id, "finance_type": finance_type, "driver": driver},
                object_id=str(inst_id),
            )
            continue
        _check_driver_target(
            doc, snapshot, descriptor, inst_id, finance_type, driver, target,
            declared[driver], diags,
        )
    # driver 齐全性(按资产身份的增量语义)
    missing = sorted(d for d in required_driver_names(asset_origin, ft_entry) if d not in mapped)
    if missing:
        _cost_fail(
            diags,
            f"finance_binding.instances.{inst_id}.drivers",
            "drivers_incomplete",
            {
                "instance": inst_id,
                "finance_type": finance_type,
                "asset_origin": asset_origin,
                "missing": missing,
            },
            object_id=str(inst_id),
        )


def _check_driver_target(
    doc: Mapping[str, Any],
    snapshot: EffectiveFinanceConfig,
    descriptor,
    inst_id: str,
    finance_type: str,
    driver: str,
    target: Any,
    declarations: list[tuple[str, str]],
    diags: list[Diagnostic],
) -> None:
    """driver 映射目标存在性 + unit_cost×归一化 driver 量纲。"""
    field = f"finance_binding.instances.{inst_id}.drivers.{driver}"
    if not isinstance(target, Mapping):
        return
    aggregation = None
    if "property" in target:
        prop = target["property"]
        pspec = descriptor.properties.get(prop)
        if pspec is None:
            _cost_fail(
                diags, field, "target_unknown_property",
                {"instance": inst_id, "property": prop},
                object_id=str(inst_id),
            )
            return
        target_unit = pspec.unit
    else:
        iface_id = target.get("interface")
        ispec = descriptor.interfaces.get(iface_id)
        if ispec is None:
            _cost_fail(
                diags, field, "target_unknown_interface",
                {"instance": inst_id, "interface": iface_id},
                object_id=str(inst_id),
            )
            return
        aggregation = target.get("aggregation")
        target_unit = ispec.unit
    try:
        driver_dims = _normalized_driver_dims(target_unit, aggregation)
    except Exception as exc:
        _fail(
            diags, field, "driver_unit_unrecognized",
            {"instance": inst_id, "unit": target_unit, "detail": str(exc)},
            object_id=str(inst_id),
        )
        return
    for component, cost_unit in declarations:
        try:
            product = _add_dims(driver_dims, dims_of(cost_unit))
        except Exception as exc:
            _cost_fail(
                diags, field, "cost_unit_unrecognized",
                {"instance": inst_id, "unit": cost_unit, "detail": str(exc)},
                object_id=str(inst_id),
            )
            return
        if product != _expected_dims(component, snapshot.currency):
            _cost_fail(
                diags, field, "driver_dims_mismatch",
                {
                    "instance": inst_id,
                    "finance_type": finance_type,
                    "driver": driver,
                    "component": component,
                    "cost_unit": cost_unit,
                    "driver_unit": target_unit,
                    "expected": dict(_expected_dims(component, snapshot.currency)),
                    "actual": dict(product),
                },
                object_id=str(inst_id),
            )
            return


__all__ = [
    "check_finance_binding",
    "linear_drivers",
    "required_components",
    "required_driver_names",
    "resolve_finance_snapshot",
]
