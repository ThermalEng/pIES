"""能源计费绑定校验(ASM-TAR-001)。

``tariff_bindings`` 把有效快照 ``energy_prices`` 的 ``price_id`` 绑定到
计费点实例/接口与聚合, 独立于设备成本绑定。会计购售方向由价格
条目显式声明，技术 in/out 仅表示能量进出所绑定设备，两者不互推。
仅允许非负、量纲匹配的单向计量接口，不接受双向净值。

时序价格的序列元数据只核对自身完整与分辨率一致；全期覆盖在任务提交边界核对，不一致阻断, 不重采样。
"""

from __future__ import annotations

from typing import Any, Mapping

from iesplan.assembly.diags import ASM_TAR_INVALID
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.assembly.rules.demand import _nonnegative_instance_lower_bound
from iesplan.core.diagnostics import Diagnostic
from iesplan.core.units import dims_of
from iesplan.finance import EffectiveFinanceConfig, EnergyPrice

#: 计费聚合(时序接口必需；scalar 不用于能源计价)
TARIFF_AGGREGATIONS: tuple[str, ...] = ("integrate_positive", "sum_positive")


def _meter_dims(unit: str, aggregation: str) -> dict[str, int]:
    """计费聚合后的能量量纲（功率积分按公共单位语义折为能量）。"""
    dims = dict(dims_of(unit))
    if aggregation == "integrate_positive":
        if dims == {"power": 1}:
            return {"energy": 1}
        dims["time"] = dims.get("time", 0) + 1
    return {key: count for key, count in dims.items() if count}


def _price_unit(price: EnergyPrice) -> str:
    if price.kind == "constant":
        return price.value.unit
    if price.kind == "time_series":
        return price.series_meta.unit
    return next(iter(price.months.values())).series_meta.unit


def _tar_fail(diags, field, reason, extra=None):
    params: dict = {"reason": reason}
    if extra:
        params.update(extra)
    diags.append(
        make_diag(
            ASM_TAR_INVALID,
            severity="error",
            blocking=True,
            params=params,
            location={"object_type": "tariff_binding", "field": field},
        )
    )


def _definition_id(dev: Mapping) -> str:
    definition = dev.get("definition") or {}
    raw = definition.get("id") if isinstance(definition, Mapping) else ""
    return str(raw or "")


def binding_prices(
    doc: Mapping[str, Any],
    snapshot: EffectiveFinanceConfig,
) -> dict[str, EnergyPrice]:
    """{binding_id: EnergyPrice}(planning 计费分量映射复用；调用方保证已过校验)。"""
    out: dict[str, EnergyPrice] = {}
    tariff = doc.get("tariff_bindings") or {}
    prices = snapshot.energy_prices or {}
    for binding_id, entry in tariff.items():
        if not isinstance(entry, Mapping):
            continue
        price = prices.get(entry.get("price"))
        if price is not None:
            out[str(binding_id)] = price
    return out


def check_tariff_bindings(
    doc: Mapping[str, Any],
    snapshot: EffectiveFinanceConfig,
    registry,
    *,
    baseline_resolution: str,
    diags: list[Diagnostic],
) -> None:
    """tariff_bindings: 引用、载体、计量量纲与非负语义、序列元数据。"""
    devices = doc.get("devices") or {}
    instance_ids = set(devices)
    tariff = doc.get("tariff_bindings") or {}
    prices = snapshot.energy_prices or {}
    for binding_id in sorted(tariff):
        entry = tariff[binding_id]
        field = f"tariff_bindings.{binding_id}"
        if not isinstance(entry, Mapping):
            continue
        if "__" in str(binding_id):
            _tar_fail(diags, field, "binding_id_reserved", {"binding_id": binding_id})
            continue
        if binding_id in instance_ids:
            _tar_fail(
                diags, field, "binding_id_clashes_instance", {"binding_id": binding_id}
            )
            continue
        price_id = entry.get("price")
        price = prices.get(price_id)
        if price is None:
            _tar_fail(
                diags, field, "price_unknown", {"binding_id": binding_id, "price": price_id}
            )
            continue
        inst_id = entry.get("instance")
        dev = devices.get(inst_id)
        if dev is None or not isinstance(dev, Mapping):
            _tar_fail(
                diags, field, "instance_unknown",
                {"binding_id": binding_id, "instance": inst_id},
            )
            continue
        descriptor = registry.get(_definition_id(dev))
        if descriptor is None:
            continue
        iface_id = entry.get("interface")
        ispec = descriptor.interfaces.get(iface_id)
        if ispec is None:
            _tar_fail(
                diags, field, "interface_unknown",
                {"binding_id": binding_id, "instance": inst_id, "interface": iface_id},
            )
            continue
        if ispec.type not in ("in", "out"):
            _tar_fail(
                diags, field, "interface_not_meterable",
                {
                    "binding_id": binding_id,
                    "instance": inst_id,
                    "interface": iface_id,
                    "type": ispec.type,
                },
            )
            continue
        if price.carrier != ispec.carrier:
            _tar_fail(
                diags, field, "carrier_mismatch",
                {
                    "binding_id": binding_id,
                    "price": price_id,
                    "carrier": price.carrier,
                    "interface": iface_id,
                    "interface_carrier": ispec.carrier,
                },
            )
            continue
        if not _nonnegative_instance_lower_bound(ispec, dev, descriptor):
            _tar_fail(
                diags, field, "interface_not_nonnegative",
                {
                    "binding_id": binding_id,
                    "interface": iface_id,
                },
            )
            continue
        meter_dims = _meter_dims(ispec.unit, entry["aggregation"])
        price_dims = dims_of(_price_unit(price))
        combined = dict(meter_dims)
        for dim, count in price_dims.items():
            combined[dim] = combined.get(dim, 0) + count
        if {dim: count for dim, count in combined.items() if count} != {"currency": 1}:
            _tar_fail(
                diags, field, "meter_price_unit_mismatch",
                {"binding_id": binding_id, "interface_unit": ispec.unit,
                 "price_unit": _price_unit(price)},
            )
            continue
        if price.kind == "time_series":
            meta = price.series_meta
            if meta is None:
                continue
            if meta.resolution != baseline_resolution:
                _tar_fail(
                    diags, field, "series_resolution_mismatch",
                    {
                        "binding_id": binding_id,
                        "price": price_id,
                        "series_resolution": meta.resolution,
                        "baseline_resolution": baseline_resolution,
                    },
                )
                continue
            # 全期覆盖(点数)在任务提交边界核对(已确认评价年限后),
            # 装配只核对元数据自身完整与分辨率一致。


__all__ = ["TARIFF_AGGREGATIONS", "binding_prices", "check_tariff_bindings"]
