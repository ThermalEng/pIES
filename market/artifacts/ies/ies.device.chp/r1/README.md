# Gas CHP Unit (`ies.device.chp`, r1)

Reference declarative `ies.device-model@1.0.0` model for a natural-gas combined heat and power (CHP) unit.

## Interfaces
- `gas_in` (`in`, carrier `gas`, unit `m3/h`): Natural gas fuel intake.
- `electric_out` (`out`, carrier `electricity`, unit `kW`, capped by `rated_electric_kw`).
- `heat_out` (`out`, carrier `heat`, unit `kW`): Co-generated thermal output.

## Key Properties
- `rated_electric_kw` (`kW`): Rated electrical capacity.
- `electric_efficiency` (`1`), `thermal_efficiency` (`1`): Electrical and thermal conversion efficiencies.
- `lhv_kwh_per_m3` (`kWh/m3`): Lower heating value of natural gas.
