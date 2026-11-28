# Thermal Energy Storage Tank (`ies.device.thermal_storage`, r1)

Reference declarative `ies.device-model@1.0.0` model for a stratified thermal energy storage tank.

## Interfaces
- `heat` (`bidirectional`, carrier `heat`, unit `kW`): Net thermal discharge (`> 0`) or charge (`< 0`).

## State & Constraints
- `stored_energy` (`kWh`) with `initial: "initial_soc * capacity_kwh"` and per-step `standby_retention`.
- Mutually exclusive `charge` / `discharge` (`charge[t] * discharge[t] = 0`) and `min_soc` / `max_soc` headroom bounds (`above_minimum`, `below_maximum`).
