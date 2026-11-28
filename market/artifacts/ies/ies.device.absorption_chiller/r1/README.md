# Absorption Chiller (`ies.device.absorption_chiller`, r1)

Reference declarative `ies.device-model@1.0.0` model for a heat-driven absorption chiller.

## Interfaces
- `heat_in` (`in`, carrier `heat`, unit `kW`): Thermal driving heat input.
- `electricity_in` (`in`, carrier `electricity`, unit `kW`): Auxiliary pump electrical draw (`cool_out[t] * aux_electric_ratio`).
- `cool_out` (`out`, carrier `cool`, unit `kW`, capped by `rated_cooling_kw`): Cooling output (`heat_in[t] * thermal_cop`).
