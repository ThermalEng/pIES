# Wind Turbine (`ies.device.wind_turbine`, r1)

Reference declarative `ies.device-model@1.0.0` model for a wind turbine generator.

## Interfaces
- `wind_capacity_factor` (`predefined`, carrier `wind`, unit `1`, range `[0, 1]`): Dimensionless normalized wind generation profile.
- `electric_out` (`out`, carrier `electricity`, unit `kW`): Electrical power output (`rated_capacity_kw * availability_factor * wind_capacity_factor[t]`).
