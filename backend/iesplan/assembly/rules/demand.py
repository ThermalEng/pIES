"""Explicit demand power meter binding checks (4.0.0, D1 only).

Power is read from the declared external-to-system 'out' electricity interface.
No tariff/price relation is inferred. Month/peak calculation belongs to D2.
"""
from __future__ import annotations
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from math import isfinite

from iesplan.assembly.diags import ASM_TAR_INVALID
from iesplan.assembly.diags import make_asm_diag
from iesplan.core.units import dims_of


def check_demand_bindings(doc, snapshot, registry, diags) -> None:
    devices = doc.get("devices") or {}
    planning_variables = (doc.get("planning") or {}).get("variables") or {}
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
            or not nonnegative_instance_lower_bound(
                spec, device, descriptor,
                instance_id=inst, planning_variables=planning_variables,
            )
        ):
            fail(billing_id, "requires_nonnegative_electric_power_out")


def nonnegative_instance_lower_bound(
    spec, instance: Mapping, descriptor, *,
    instance_id: str, planning_variables: Mapping,
) -> bool:
    """Check the effective interface floor after planning-property takeover.

    A planned property no longer uses its instance/default value. Its planning
    lower bound controls the flow through the generator's per-step capacity row;
    an omitted planning lower bound means zero for the capacity variable.
    Other planning/property validity is checked by the existing assembly phases.
    """
    if spec.minimum_property_ref is None:
        return spec.minimum is not None and spec.minimum >= 0

    name = spec.minimum_property_ref
    property_spec = descriptor.properties.get(name)
    if property_spec is None:
        return False
    for variable in planning_variables.values():
        if not isinstance(variable, Mapping) or (
            variable.get("device_ref"), variable.get("parameter")
        ) != (instance_id, name):
            continue
        lower = variable.get("lower_bound")
        if lower is None:
            return True  # planning capacity's established implicit zero floor
        if isinstance(lower, bool) or not isinstance(lower, (int, str)):
            return False
        try:
            bound = Decimal(lower)
        except (InvalidOperation, ValueError):
            return False
        return bound.is_finite() and bound >= 0
    override = (instance.get("properties") or {}).get(name)
    value = override.get("value") if isinstance(override, Mapping) else property_spec.value
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
        and value >= 0
    )
