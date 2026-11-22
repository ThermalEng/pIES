# Models and Algorithms Extension Guide

> Status: Effective Target Guide; Target Product Version: `1.1.0`; Governing Spec: [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md); Related Formats: [Device Model YAML](formats/device-model-yaml.md), [Algorithm Plugin Package](formats/algorithm-plugin-package.md)

This guide defines the long-term delivery standards, identity/version model, and isolation security boundaries for user-defined device models, algorithm plugin packages, and the shared hub (planned for delivery in `1.1.0`).

## Core Architectural Positioning

"Models and Algorithms" is exposed to authenticated engineers with three entry areas: **Model Library**, **Algorithm Plugins**, and **Shared Hub**:

- **Model Library**: Delivers user device models in `ies.device-model` YAML. Structured web forms, online YAML editing, and YAML uploads share the exact same backend parsing and validation gate, producing an identical canonical model object.
- **Algorithm Plugins**: Delivers user-defined assembly-processing and solving algorithms inside `ies.algorithm-plugin` ZIP archives. Uploaded packages are validated and executed by an isolated containerized runner; Web, API, and ordinary Worker processes never load or execute user Python code.
- **Shared Hub**: Owners MAY submit validated immutable publications for sharing; once approved by an administrator, other users can browse and install them.

## Identity, Immutable Publication, and Installation Model

| Entity | Stable Identifier | Publication Pinning Rules | Naming and Reference Rules |
|---|---|---|---|
| User Device Model | `user.<public_namespace>.device.<slug>` | Monotonically increasing `revision` + `schema_version` + validation receipt | `public_namespace` is assigned via a CSPRNG and never changes; no per-device semantic version |
| User Algorithm Plugin | Namespaced `plugin_id` | Exact three-part semantic version + full dependency lock | Changing entry points or schema requires bumping the version |
| Shared Hub Installation | `CatalogInstallation` reference record | Points solely to the same immutable publication without copying underlying files | Unsharing or uninstalling never breaks historical tasks or project version traceability |

- Projects and calculation configs MAY reference only installed items with `selectable=true` by exact device `revision` or plugin version; referencing drafts or `latest` is prohibited.
- Published items are immutable; re-publishing identical canonical content remains idempotent.

## Device Model Library Rules

1. **Pure technical semantics**: Follow [Device Model YAML](formats/device-model-yaml.md), including only stable identity, time-invariant `properties`, five `interfaces` classes, and restricted declarative `equations`; carrying prices, costs, calculation precision, or code paths is strictly prohibited.
2. **Validation gate and atomic saving**: Submitted candidate models MUST pass safe-YAML, schema, unit, interface, and equation checks completely; failing candidates remain only in editing state or temporary staging without receiving official IDs or entering modeling selectors.
3. **Version and palette visibility**: Models shown in a project's modeling palette derive strictly from the model references declared in the current draft or selected project version; restoring a historical version never displays model assets created after that version.

## Algorithm Plugin Development and Execution Constraints

1. **Two-stage separation**: Algorithm plugins expose `generate` (canonical assembly → solver inputs & declarations) and `solve` (input files → declared results) under `src/`, continuing to follow standard `Solver Bundle` and `ResultAdapter` contracts without bypassing the common pipeline.
2. **Offline pure-Python delivery**: The initial release accepts only Python ZIPs matching the [Algorithm Plugin Package](formats/algorithm-plugin-package.md) layout with offline dependency locks, rejecting native binaries, shared libraries, Git/URL dependencies, and install scripts.
3. **Strict sandbox assumptions**:
   - The execution environment has no internet or intranet access, no database or object-storage credentials, and no access to host repository directories;
   - Plugins read only Manifest-declared inputs and a dedicated temporary working directory, strictly bounded by deployment limits (CPU, memory, timeout, process count, file size);
   - Identical inputs, versions, dependencies, and seeds MUST yield deterministic results.

## Sharing, Permissions, and Lifecycle

- **Access isolation**: Unpublished drafts, uploaded archives, and validation details are visible only to the owner; administrators hold authority to approve sharing, disable items, or block versions for security risks; the Shared Hub never exposes other users' projects or datasets.
- **Unsharing and uninstalling**: Unsharing blocks new installations only; user uninstallation removes only the personal catalog reference, and objects still referenced by historical versions, task snapshots, or evidence MUST NOT be physically deleted, with reference impact surfaced before uninstalling.

## External Delivery Self-Check List

- **Before submitting a device model**: Confirm `1.0.0` schema compliance, absence of financial/precision/code fields, stable ID, license, and valid sample inputs.
- **Before submitting an algorithm plugin**: Confirm compliant ZIP layout and Manifest, completely locked offline dependencies, and deterministic execution with valid output schemas inside an offline container on a minimal self-test case.
