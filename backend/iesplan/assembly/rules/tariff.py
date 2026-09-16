"""能源计费绑定校验(ASM-TAR-001)。

``tariff_bindings`` 把有效快照 ``energy_prices`` 的 ``price_id`` 绑定到
计费点实例/接口与聚合, 独立于设备成本绑定。会计方向与载体只由价格
条目的显式声明决定, 不靠名称推断；流向语义按设备接口方向机械判定：
``purchase`` 计量的能量经 ``out`` 接口流出计量设备(流入本站),
``sale`` 计量的能量经 ``in`` 接口流入计量设备(流出本站)；
需猜测的绑定阻断。

时序价格的序列元数据与项目基线逐项核对(resolution/leap_year/
point_count), 不一致阻断, 不重采样。
"""

from __future__ import annotations

from typing import Any, Mapping

from iesplan.assembly.diags import ASM_TAR_INVALID
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.core.diagnostics import Diagnostic
from iesplan.finance import EffectiveFinanceConfig, EnergyPrice

#: 计费聚合(时序接口必需；scalar 不用于能源计价)
TARIFF_AGGREGATIONS: tuple[str, ...] = ("integrate_positive", "sum_positive")


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
    baseline_leap_year: bool,
    baseline_steps: int,
    diags: list[Diagnostic],
) -> None:
    """tariff_bindings: price/实例/接口存在性 + 载体/方向相容 + 序列元数据核对。"""
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
        expected_type = "out" if price.direction == "purchase" else "in"
        if ispec.type != expected_type:
            _tar_fail(
                diags, field, "direction_flow_mismatch",
                {
                    "binding_id": binding_id,
                    "price": price_id,
                    "direction": price.direction,
                    "interface": iface_id,
                    "interface_type": ispec.type,
                },
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
            if meta.leap_year != baseline_leap_year:
                _tar_fail(
                    diags, field, "series_leap_year_mismatch",
                    {
                        "binding_id": binding_id,
                        "price": price_id,
                        "series_leap_year": meta.leap_year,
                        "baseline_leap_year": baseline_leap_year,
                    },
                )
                continue
            if meta.point_count != baseline_steps:
                _tar_fail(
                    diags, field, "series_point_count_mismatch",
                    {
                        "binding_id": binding_id,
                        "price": price_id,
                        "series_point_count": meta.point_count,
                        "baseline_steps": baseline_steps,
                    },
                )
                continue


__all__ = ["TARIFF_AGGREGATIONS", "binding_prices", "check_tariff_bindings"]
