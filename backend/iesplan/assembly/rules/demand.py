"""Explicit demand power meter binding checks (4.0.0, D1 only).

Power is read from the declared external-to-system 'out' electricity interface.
No tariff/price relation is inferred. Month/peak calculation belongs to D2.
"""
from __future__ import annotations
from collections.abc import Mapping
from math import isfinite

from iesplan.assembly.diags import ASM_TAR_INVALID
from iesplan.assembly.diags import make_asm_diag
from iesplan.core.units import dims_of


def check_demand_bindings(doc, snapshot, registry, diags) -> None:
    devices = doc.get("devices") or {}
    tariffs = doc.get("tariff_bindings") or {}
    demands = doc.get("demand_bindings") or {}
    reserved = set(devices) | set(tariffs)
    seen: set[tuple[str, str, str]] = set()

    def fail(billing_id: str, reason: str) -> None:
        diags.append(make_asm_diag(
            ASM_TAR_INVALID,
            severity="error",
            blocking=True,
            params={"reason": reason, "billing_id": billing_id},
            location={"object_type": "demand_binding", "field": f"demand_bindings.{billing_id}"},
        ))

    for billing_id, binding in sorted(demands.items()):
        if billing_id in reserved or "__" in billing_id:
            fail(billing_id, "billing_id_clash")
            continue
        charge_id = binding["charge"]
        if charge_id not in snapshot.demand_charges:
            fail(billing_id, "charge_unknown")
            continue
        inst, iface = binding["instance"], binding["interface"]
        key = (charge_id, inst, iface)
        if key in seen:
            fail(billing_id, "duplicate_charge_meter")
            continue
        seen.add(key)
        device = devices.get(inst)
        if not isinstance(device, Mapping):
            fail(billing_id, "instance_unknown")
            continue
        descriptor = registry.get(device['definition']['id'])
        if descriptor is None:
            continue  # earlier device validation already blocks
        spec = descriptor.interfaces.get(iface)
        if spec is None:
            fail(billing_id, "interface_unknown")
        elif (
            spec.type != "out" or spec.carrier != "electricity"
            or dict(dims_of(spec.unit)) != {"power": 1}
            or not nonnegative_instance_lower_bound(spec, device, descriptor)
        ):
            fail(billing_id, "requires_nonnegative_electric_power_out")


def nonnegative_instance_lower_bound(spec, instance: Mapping, descriptor) -> bool:
    """Resolve actual instance-bound property references, not just their existence.

    An otherwise valid instance property may be negative even though an interface
    declares its minimum through a property_ref. This is a meter-eligibility
    check; regular property type/unit validation stays in the existing phase.
    """
    if spec.minimum_property_ref is None:
        return spec.minimum is not None and spec.minimum >= 0

    name = spec.minimum_property_ref
    property_spec = descriptor.properties.get(name)
    if property_spec is None:
        return False
    override = (instance.get("properties") or {}).get(name)
    value = override.get("value") if isinstance(override, Mapping) else property_spec.value
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
        and value >= 0
    )
