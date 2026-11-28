# Sample Solar PV Device (`ies.device.solar_pv_sample` r1)

Pure technical declarative solar photovoltaic device model conforming to `ies.device-model@1.0.0`.

- **Properties**: `rated_capacity_kwp` (`kWp`), `reference_irradiance` (`W/m2`)
- **Interfaces**: `solar_irradiance` (`predefined`, `solar`), `electric_out` (`out`, `electricity`)
- **Equations**: `electric_out[t] = rated_capacity_kwp * solar_irradiance[t] / reference_irradiance`
