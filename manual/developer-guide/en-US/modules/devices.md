# Device Catalog

> Code Boundary: `backend/iesplan/devices/`; File Contract: [Device Model YAML](../formats/device-model-yaml.md)

`devices` owns parsing, validation, catalog publication, and public descriptors (`DeviceDescriptor`) for pure technical device specifications. It is the single device source of truth consumed by the frontend device palette, equation modeling, and project assembly.

## Responsibilities and Boundaries

- **Owns**: Stable device type IDs, localized display name keys, time-invariant `properties` (units and valid ranges), five `interfaces` classes (`in` / `out` / `bidirectional` / `predefined` / `blind`), declarative `equations`, device Provider discovery, and atomic catalog snapshot publication.
- **Does not own**: Project device instance IDs, canvas coordinates, `existing`/`new` asset origin, port connections, project dataset bindings, financial prices/costs, calculation precision, or solver options.
- **Strict isolation**: Device files and catalog services MUST NOT carry or fall back to price catalogs; callers obtain `DeviceDescriptor` exclusively through public facades without reading physical file paths.

## Validation Gate and Publication Mechanism

1. **Unified entry point**: Built-in device catalogs, in-project new/saved models, and (`1.1.0` target) user model library forms, online YAML edits, and YAML uploads all pass through the same safe-YAML parsing, schema checking, unit/interface/equation validation, and canonicalization entry in `devices`.
2. **Complete diagnostic reporting**: Validation aggregates all mutually independent errors within the same stage and returns a stably ordered `Diagnostic` list (including error codes, field paths, expected vs. actual values, and YAML line/column numbers).
3. **Atomic snapshot publication**: At startup, all candidate devices are discovered and validated completely before publishing a catalog snapshot atomically; any invalid device or ID collision discards the candidate catalog without falling back to a static built-in table. Catalog snapshots remain immutable at runtime with no hot-reloading.

## Plannable Energy Storage Modeling Rules

Built-in batteries and other storage devices support both fixed-parameter simulation and capacity/power optimization through the unified equation contract:

- Stored energy uses explicit energy units, and capacity, rated power, initial state, SOC bounds, and charge/discharge efficiencies use affine relations—placing a plannable capacity variable in a denominator to form bilinear constraints is prohibited;
- Zero capacity means zero usable storage (initial stored energy scales to zero alongside capacity) without fabricating artificial SOC ratios or injecting undeclared terminal equality constraints.

## Workflow for Adding a New Device

1. Author a pure technical definition per [Device Model YAML](../formats/device-model-yaml.md) with a stable namespaced ID (no per-device semantic version).
2. Define `properties` and the five `interfaces` classes (only `predefined` declares input slots, without embedding project data sources in device files; omitted `type` canonicalizes to `blind`).
3. Author restricted declarative `equations` accompanied by valid/invalid YAML and CSV samples, and verify end-to-end flow through the catalog endpoint, GUI form, assembly validator, and computation pipeline without modifying consumer device-type lookup tables.
