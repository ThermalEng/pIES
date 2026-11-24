# Models and Algorithms Extension Guide

> Status: Effective Target Guide; Target Product Version: `1.2.0`; Governing Spec: [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md); Related Formats: [Device Model YAML](formats/device-model-yaml.md), [Algorithm Plugin Package](formats/algorithm-plugin-package.md)

This guide defines the long-term delivery standards, identity and versioning model, operating modes, and isolation boundaries for user-defined device models, algorithm plugin packages, and the independent public **pIES Market** (planned for delivery in `1.2.0`).

## Core Architectural Positioning

The models and algorithms ecosystem comprises **local management (`ThermalEng/pIES`)** and an **independent public marketplace (`ThermalEng/pIES-Market`)**:

- **Local model & plugin management**: Manages declarative device models (`ies.device-model` YAML unified across web forms, online YAML editing, and file imports) and algorithm plugins (`ies.algorithm-plugin` ZIPs validated and executed inside isolated environments) within a pIES deployment, backed by a standalone SDK (`pies-sdk`, zero dependency on the pIES backend) for authoring and local verification.
- **pIES Market (public marketplace)**: Operates independently of any pIES deployment instance, providing unauthenticated catalog browsing/downloading, email-verified submissions, manual review, and versioned artifact distribution. The public market MUST NOT become a runtime dependency for local pIES computation—when the market is disabled, unreachable, or offline, locally installed models, plugins, and offline import/export continue operating normally.
- **Unified single-page catalog view**: Rather than maintaining a separate parallel "Shared Hub" approval workflow, pIES presents models and plugins in a single unified market view supporting filtering by artifact/plugin kind and marking each entry with one of three explicit states:
  - **`Remote`**: Public market artifacts visible only in Online mode;
  - **`Local (Not Installed)`**: Artifacts already present in the local administrator catalog that the current member has not yet installed;
  - **`Local (Installed)`**: Artifacts installed and ready for modeling or computation by the current member.

## Identity, Immutable Publication, and Installation Model

### Stable Namespace Rules

| Source Category | Stable Identifier Format | Publication and Distribution Rules |
|---|---|---|
| Official built-in & market releases | `ies.<type>.<slug>` | Reserved for official maintenance and official publications; pinned by immutable version or revision |
| Single-instance local personal drafts | `user.<public_namespace>.<type>.<slug>` | `public_namespace` is assigned via CSPRNG and never changes; avoids cross-instance naming collisions |
| Community / vendor public market | `<publisher>.<type>.<slug>` | Uses a stable prefix for the contributor, team, or vendor (assigned at first market review if unspecified); guaranteed globally unique via manual review and immutable once released |

- When downloading from the public market or importing an offline package across instances, **the original stable ID is preserved verbatim** without rewriting namespaces;
- Device models pin by publication `revision` and validation receipt (no per-device semantic version); algorithm plugins pin by exact three-part semantic version. Published artifacts MUST NOT be overwritten silently, and re-publishing identical canonical content remains idempotent.

### No-Hot-Reload Registration, Installation, and Snapshot Freezing

- **No runtime hot-reloading**: Web, API, and ordinary Worker processes never load or `import` user plugin source code; the composition root statically registers trusted execution bridges at process startup only.
- **Immediate availability for subsequent computations**: Installed plugins are stored as immutable records in the database and managed object storage; installing or toggling availability **requires no service restart**. Projects and calculation configurations MAY reference only installed, enabled entries by exact revision or version—referencing drafts or `latest` is prohibited.
- **Submission gate and change isolation**: Before launching a calculation, the submission gate verifies compatibility and parameters, then freezes exact `(artifact_id, version, package_object_id, env_id, parameters)` into an immutable `CalcSnapshot` with object reference tracking. Installing, disabling, or uninstalling affects **only newly submitted calculations afterward**, leaving in-flight runs and historical project versions, snapshots, and evidence untouched.

### External Integrity Verification Boundary

- Public market indices publish the `sha256` digest of each released artifact; pIES verifies `sha256` and archive safety **once at the external download or import boundary**.
- In strict compliance with the [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md#26-validation-only-at-real-boundaries): once ingested under an immutable `ObjectId`, pIES database tables, `CalcSnapshot`, and internal runtime handoffs **never store, recompute, or compare SHA-256 digests**, nor use content digests as business identifiers.

## Device Model Library Rules

1. **Pure technical semantics**: Follow [Device Model YAML](formats/device-model-yaml.md), including only stable identity, time-invariant `properties`, five `interfaces` classes, and restricted declarative `equations`; carrying prices, costs, calculation precision, or code paths is strictly prohibited.
2. **Validation gate and atomic saving**: Submitted candidate models MUST pass safe-YAML, schema, unit, interface, and equation checks completely; failing candidates remain only in editing state or temporary staging without receiving official IDs or entering modeling selectors.
3. **Version and palette visibility**: Models shown in a project's modeling palette derive strictly from the model references declared in the current draft or selected project version; restoring a historical version never displays model assets created after that version.

## Algorithm Plugin Development and Execution Constraints

1. **Unified concise contract**: Both built-in computation stages and external algorithm plugins follow [Algorithm Plugin Package](formats/algorithm-plugin-package.md) (`plugin.yaml`), supporting five kinds (`predictor`, `generator`, `solver`, `evaluator`, and unified single-step `algorithm`) plus the dedicated `iteration` interface for internal loops; simple plugins require no `Dockerfile` or multi-file boilerplates.
2. **Automated immutable image builds at install time**: When installing plugins with extra dependencies, the allowlisted proxy `job_runner` builds a dedicated immutable computation image atop the official base image, strictly separating dependency installation from runtime execution; dependency-free plugins reuse the base layer directly.
3. **Ephemeral network-disabled container execution**: `backend` orchestrates each computation step via `job_runner` inside an ephemeral network-disabled container (`/work/in → /work/out`) destroyed immediately upon exit; runtime containers have zero public/internal network access, no database/object-storage credentials, no Docker socket, and no host repository paths, bounded by deployment resource limits (CPU, memory, timeout, process count, output size). See [Deployment and Operations](deployment.md).
4. **Determinism and output boundary**: Identical inputs, versions, dependencies, and seeds MUST yield deterministic results; plugin outputs undergo strict public result-contract validation before entering result models and contain only finite values and bounded logs; **the platform validates public result-contract compliance only and never endorses mathematical optimality or financial correctness**. Reference plugins MUST pass the four runtime modularity gates in [Extension System](extensions.md#boundary-split-and-runtime-modularity-gates).

## Operating Modes, Permission Isolation, and Lifecycle

- **Strict separation of roles and accounts**: Public market administrators manage public review, publishing, and delisting without any administrative access to pIES deployments or user engineering data; pIES local administrators control instance operating mode, market endpoint, and local installation status. Market email-verified contributor identities are independent of local pIES accounts and require no credential synchronization.
- **Local Mode vs. Online Mode** (toggled by the local administrator):
  - **Local Mode**: Disables the remote public market—zero outbound market requests are made and remote items are hidden; ordinary users' "share" actions target only the instance's local administrator level, with no external upload or submission permissions; local offline import/export and regular computations remain unaffected.
  - **Online Mode**: All signed-in users can browse remote market models and plugins, freely download and install them, and submit contributions to the public market via email verification.
- **Isolation and reference protection**: Unpublished drafts and validation details are visible only to the owner; administrators can enable, disable, or uninstall local entries. Uninstalling removes only catalog availability references—objects still referenced by historical project versions, task snapshots, or evidence MUST NOT be physically deleted, with reference impact surfaced beforehand.

## External Delivery Self-Check List

- **Before submitting a device model**: Confirm `1.0.0` schema compliance, absence of financial/precision/code fields, stable ID, license, and valid sample inputs.
- **Before submitting an algorithm plugin**: Confirm compliant ZIP layout, security rules, and manifest, deterministic self-test execution with valid output schemas inside an offline isolated environment, and clear `README.md` plus explicit license for market submissions.
