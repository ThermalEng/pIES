# Models and Algorithms Extension Guide

> Status: Effective Specification; Product Version: `1.2.0`; Governing Spec: [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md); Related Formats: [Device Model YAML](formats/device-model-yaml.md), [Algorithm Plugin Package](formats/algorithm-plugin-package.md), [Calculation Config](formats/calculation-config.md)

This guide defines the long-term delivery standards, public market index and stateless submission contracts, identity and single-active-version model, orthogonal operating mode and approval controls, and security isolation boundaries for user-defined device models, algorithm plugin packages, and the independent public **pIES Market** (delivered in `1.2.0`).

## Core Architectural Positioning

The models and algorithms ecosystem comprises **local management (`ThermalEng/pIES`)** and an **independent public marketplace (`ThermalEng/pIES-Market`)**:

- **Local model & plugin management**: Manages declarative device models (`ies.device-model` YAML unified across bidirectional forms for common metadata/`properties`/`interfaces` fields, online YAML editing, and `.device.yaml` file import/export, keeping complex `equations` in YAML/expression editing) and algorithm plugins (`ies.algorithm-plugin` ZIPs built under controlled isolation, offline self-tested, and executed along the single-chain container pipeline), backed by the minimal zero-backend-dependency package `pies-sdk` providing runtime contracts and universal `validate`/`pack` tools.
- **pIES Market (public marketplace)**: Decoupled from any pIES deployment instance, composed of a static JSON index (`index.json`), artifact distribution with a co-hosted pure static browse/download page (allowing unauthenticated filtering, README/license/`sha256` inspection, and offline package downloads), and an independent self-hostable thin stateless submission gateway. The public market MUST NOT become a runtime dependency for local pIES computation—when the market is disabled, unreachable, or offline, locally installed models, plugins, and offline import/export continue operating normally.
- **Unified single-page "Models and Algorithms" view & admin console split**: pIES upgrades `/custom` into a single unified "Models and Algorithms" page without maintaining a parallel "Shared Hub" workflow, organized into two peer tabs:
  - **"Market & Installed"**: Presents the unified artifact stream supporting filtering by artifact/plugin kind (device models and the five algorithm plugin kinds), viewing the current active version/revision, and marking each entry with one of three primary frontend badges:
    - **`Remote`**: Public market artifacts visible only in Online mode;
    - **`Local (Not Installed)`**: Ready (`ready`) local catalog entries that the current member has not yet installed and enabled;
    - **`Local (Installed)`**: Entries installed and active for modeling or computation by the current member.
  - **"My Creations / Drafts"**: Hosts the personal custom device model editor (form/YAML/file import-export, draft saving, and publishing) alongside the member's local submission records and backend lifecycle states (`pending_approval` / `building` / `ready` / `build_failed` / `disabled`) with diagnostics.
  - **Admin Console (`/admin`)**: Centralizes the two orthogonal instance policies (`market_mode` and `local_upload_requires_approval`), market index and submission gateway URLs, optional `pip_index_url` mirror/intranet source, pending approval queue, and enabling/disabling/uninstalling local catalog entries.

## Identity, Namespace Ownership, and Single Active Version Model

### Stable Namespace and Ownership Rules

| Source Category | Stable Identifier Format | Ownership, Market Conversion, and Version Rules |
|---|---|---|
| Official built-in & market releases | `ies.<type>.<slug>` | Reserved for official maintenance and official publications; each `(kind, stable_id)` keeps only 1 current active version or publication `revision` in the local catalog |
| Single-instance local personal drafts | `user.<public_namespace>.<type>.<slug>` | `public_namespace` is a system-allocated 12-char Base32 CSPRNG token (60-bit entropy), immutable for life, preventing cross-instance offline package collisions and redacting internal usernames |
| Community / vendor public market | `<publisher>.<type>.<slug>` | Public market submissions converge to a readable `<publisher>` prefix; upon first approved PR, a static repo registry binds `<publisher>` to a salted/HMAC digest of the verified contributor email (zero plaintext emails), restricting subsequent version/revision releases under that prefix to the same verified email |

- **Preserving original identity and revision across instances**: Downloading from the public market or importing an offline package **preserves the original stable ID and publisher-assigned exact version or integer `revision` verbatim** (pure technical `.device.yaml` bodies carry no version field; `revision` travels in market index or export metadata, defaulting to `revision = 1` or an explicit import parameter for bare single-file imports), never renumbering locally.
- **Conflict rejection and explicit overwrite upgrade**: The local catalog keeps **only 1 current active version** per `(kind, stable_id)`: duplicate submissions for the same `(kind, stable_id, version/revision)` are rejected directly with `409` conflict (**automatic renaming, auto-incrementing revisions, or silent overwrites are strictly prohibited**); submitting a new version for the same `(kind, stable_id)` performs an explicit **overwrite upgrade**—replacing the active entry in place and deleting the old plugin image on a best-effort basis (logging a warning on cleanup failure without affecting the new version). **The platform provides no multi-version coexistence, compatibility adapter layer, or automated cross-version migration**—when a model/plugin upgrades, existing projects or configs have only two valid outcomes: either the newer model/plugin handles older inputs itself, or validation/execution fails explicitly according to current contracts.

### Instance-Level Single-Version Cache and Snapshot Freezing

- **No runtime hot-reloading**: Web, API, and scheduler control-plane processes never load or `import` user plugin source code; the composition root statically registers trusted execution bridges at startup only.
- **Instance-level single active version cache + per-member installation**: Underlying artifact blobs and built plugin images maintain one unique current active version per `(kind, stable_id)` in the instance catalog, while installation/activation state is tracked independently **per member**. Project drafts and calculation configs consume artifacts directly by stable ID (and the plugin's sole current version ref), without per-project version pin tables, draft multi-version switchers, or cross-version compatibility shims.
- **Immediate post-install availability & submission freezing**: Only the current active artifact in `ready` state that is installed and enabled for the current member MAY be referenced by projects and calculation configs (drafts and unready entries are prohibited). At task submission, exact references, per-stage option slots, and immutable input files freeze into `CalcSnapshot` with object references. Artifact upgrades, disables, and uninstalls use a direct-operation model: administrators SHOULD choose an appropriate maintenance window, and the platform does not guarantee that in-flight tasks not yet entering affected computation stages can still complete after artifact changes (missing plugins or images fail explicitly), while frozen inputs and persisted results/evidence remain intact.

### External Integrity Verification Boundary

In strict compliance with the [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md#26-validate-only-at-true-boundaries), only **public market remote downloads** verify the `sha256` declared in `index.json` alongside archive safety and contract validation once prior to ingestion; local uploads and offline imports do not require or check `expected_sha256`.

Once ingested into managed object storage under an immutable `ObjectId`, pIES database tables, snapshots, and runtime handoffs **never store, recompute, or compare SHA-256 digests**, nor use content digests as business identifiers.

## Public Market Index Contract and Stateless Submission Protocol

### Static Market Index Contract (`ies.market-index@1.0.0`)

`index.json` generated by `ThermalEng/pIES-Market` is the **sole authoritative public artifact catalog** consumed identically by both the co-hosted static browse/download web page and pIES clients (no duplicate catalog is maintained):

```json
{
  "schema": "ies.market-index",
  "schema_version": "1.0.0",
  "generated_at": "2026-11-25T12:00:00Z",
  "artifacts": [
    {
      "id": "acme.device.chp",
      "kind": "device_model",
      "revision": 1,
      "name": { "zh_cn": "燃气热电联产机组", "en_us": "Gas CHP Unit" },
      "description": "Pure technical declarative gas CHP device model.",
      "publisher": "acme",
      "license": "MPL-2.0",
      "compatibility": { "pies": ">=1.2.0,<2.0.0" },
      "url": "artifacts/acme.device.chp-r1.device.yaml",
      "readme_url": "artifacts/acme.device.chp-r1.README.md",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "size_bytes": 4096
    },
    {
      "id": "acme.solver.cbc_mps",
      "kind": "solver",
      "version": "1.0.0",
      "name": { "zh_cn": "CBC MPS 求解器", "en_us": "CBC MPS Solver" },
      "description": "Consumes standard MPS bundles and emits solver-neutral primal solutions.",
      "publisher": "acme",
      "license": "MPL-2.0",
      "compatibility": { "pies": ">=1.2.0,<2.0.0" },
      "consumes": ["ies.solver-bundle.mps@1.0.0"],
      "produces": ["ies.primal-solution@1.0.0"],
      "url": "artifacts/acme.solver.cbc_mps-1.0.0.algorithm.zip",
      "readme_url": "artifacts/acme.solver.cbc_mps-1.0.0.README.md",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "size_bytes": 102400
    }
  ]
}
```

- **Version markers**: `kind: "device_model"` uses positive integer `revision`; the five algorithm plugin kinds (`predictor` / `generator` / `solver` / `evaluator` / `algorithm`) use three-part semantic `version`; multiple historical versions/revisions of the same `id` coexist in `artifacts`.
- **Delisting isolation**: Delisting in the public market removes or marks entries for remote distribution only and **never remotely revokes or breaks artifacts already installed on local pIES instances**.

### Thin Stateless Submission Gateway Protocol

The submission gateway requires no database or account system, treating GitHub PRs themselves as persistent review records:

1. **Request OTP**: Contributor submits an email address to receive a short-lived one-time code over SMTP alongside a short-lived server-HMAC-signed stateless challenge token binding the email, salt, and expiry (MAY keep a simple OTP send cooldown or rely on deployment-layer rate limiting without building complex sliding-window, distributed-counter, or anti-abuse systems);
2. **Stateless verification**: Matching the code against the challenge token issues a purpose-bound, short-lived stateless submission token;
3. **Upload & pre-check**: Uploading `.device.yaml` or `.algorithm.zip` with the token checks basic ZIP layout/total-size boundaries, manifest/structural schema rules, and `publishers.yaml` ownership computed with the target market repository's `MARKET_HMAC_SECRET` (without scanning every text file inside the archive for emails);
4. **PR creation**: Using a least-privilege GitHub App / Token, the gateway checks for existing open PRs on the same `(id, version/revision)` and opens a branch and PR (failing explicitly on conflict without building complex GitHub idempotent PR recovery state machines); **contributor emails serve solely for verification and non-plaintext digest binding and are omitted from public PR bodies and `index.json`**;
5. **Read-only isolated CI & human merge**: PRs trigger a read-only `pull_request` workflow (`pull_request_target` prohibited; zero GitHub write tokens, zero SMTP/release secrets, and `--network=none` during self-tests); after human PR review and merge, a separate trusted release workflow computes `sha256` and regenerates `index.json` and the static catalog page. **Responsibilities are explicitly bounded: the submission gateway handles only submission workflow and basic entry checks; `pIES-Market` is responsible solely for artifact structure, identity, and public contract format; publishers are responsible for public artifact content and domain correctness; and third-party code runs solely inside isolated containers.**

## Device Model Library Rules

1. **Pure technical semantics and dual-mode authoring**: Follow [Device Model YAML](formats/device-model-yaml.md), containing only stable identity, time-invariant `properties`, five `interfaces` classes, optional template `inputs`, and declarative `equations`, excluding prices, costs, precision, or code paths. "My Creations / Drafts" provides bidirectional common-field forms, online YAML editing, and `.device.yaml` import/export backed by the authoritative backend validator.
2. **Validation gate, palette visibility, and version isolation**: Invalid candidates remain in drafts only; installed and enabled models appear in project model creation selectors, while restoring historical project versions displays only model references declared in that version.

## Algorithm Plugin Unidirectional Pipeline, Build Trust Boundary, and Execution Rules

1. **Framework zero-copy handoffs, minimal-subprocess restoration & retryable parallel evaluators**: Following [Algorithm Plugin Package](formats/algorithm-plugin-package.md) (`plugin.yaml`) and [Calculation Config](formats/calculation-config.md), the five plugin kinds execute along `Frozen Input → [predictions[]: grouped per predictor plugin] → (generator → solver → backend minimal-subprocess restoration | algorithm) → [evaluators[]: 0..N independent evaluators in parallel] → Result`:
   - **Single-in/single-out containers & framework hardlinks**: Ephemeral containers execute only single-input/single-output pluggable computation stages and never relay files manually; cross-stage read-only context is staged into `/work/in` via zero-copy hardlinks by the orchestrator;
   - **Per-plugin grouping & joint multi-target prediction**: Within the logical prediction stage, targets group by selected `predictor` plugin so correlated targets run jointly in one pass before the framework merges all predicted series; iterative search algorithms loop internally inside one container via `iteration`;
   - **`backend` minimal-subprocess restoration & retryable parallel evaluators**: Once staged `solver` emits a 0-based original-column-order primal solution, `backend` streams `variable_map` inside a minimal `spawn` child subprocess to faithfully restore the canonical business-unit engineering solution (reclaiming memory on exit without re-solving third-party solvers; non-feasible outcomes skip solution-dependent evaluators); under feasible core solves, top-level task outcome reflects only the core solve while `0..N` independent `evaluators` run in parallel—individual evaluator failures preserve the sealed engineering solution and successful evaluation blocks, offering isolated single-evaluator retry on the results page; the official financial evaluator reuses shared `finance` rules for full forward evaluation, while third-party evaluators output self-describing `metrics` / `charts` / `tables` blocks rendered generically by the frontend; compatibility matches on `consumes` / `produces`.
2. **Build-stage trust boundary & single-index rule**:
   - Dependency-free plugins (`requirements` omitted) MUST build with `--network=none` directly atop local `pies-plugin-base`;
   - Plugins declaring `requirements.txt` build inside `job_runner`'s controlled in-memory context with **zero host business directory mounts, zero database/Redis/secret credentials, and simple timeout termination (executing builds in an isolated execution unit without probing Docker SDK private objects, underlying sockets, or response-stream internals)**; `pip_index_url` uses default public PyPI when unset and **passes solely `--index-url <pip_index_url>` when set** (never appending `--extra-index-url`, preventing multi-index non-determinism and enterprise dependency confusion while leaving upstream caching/fallback to the mirror service itself); dependency failures mark `build_failed` and surface an explicit warning naming the failed plugin/dependency and instructing the user to contact the administrator.
3. **Install-time network-disabled self-test gate & backend states**: Once built, if `plugin.yaml` declares `tests` (supporting both single-JSON shorthand and `in/` → `expected/` directory fixtures), `backend` MUST run all cases via `job_runner` in a `--network=none` container before advancing status from `building` to `ready` (marking `build_failed` on failure). **Passing self-tests proves compliance with author samples and public output structures only—the platform never endorses mathematical optimality or engineering/financial correctness**.
4. **Minimal `pies-sdk`**: Remains zero-backend-dependency, providing only runtime contracts/entrypoints plus universal `validate` (manifest, path layout, and offline self-tests) and `pack` (canonical `*.algorithm.zip` creation) across all five plugin kinds.

## Orthogonal Operating Modes, Approval Toggle, and Direct Uninstall

Administrators configure two **mutually orthogonal** instance policies under `/admin`:

| Admin Setting | Scope and Enforcement Rule |
|---|---|
| `market_mode` (`local` / `online`) | Controls remote `pIES-Market` connectivity: Local Mode makes zero outbound market requests, hides remote items, and disables external submissions; Online Mode enables remote browsing, downloading, and email-verified submissions |
| `local_upload_requires_approval` (`true` / `false`) | **Enforced uniformly in both Local Mode and Online Mode**: Direct downloads from the audited public market in Online Mode require no local approval, whereas **every non-admin user-supplied local upload or offline import** obeys this toggle (when enabled, static structure/contract validation places items in `pending_approval` without building images until admin approval advances them to `building → ready`; when disabled, valid uploads build and publish directly; admin imports always bypass approval). |

**Single-step disable & image cleanup**:

- When disabling or uninstalling a local artifact, administrators mark it `disabled` to immediately block new task selections and call `job_runner`'s allowlisted `delete_plugin_image` on a best-effort basis to remove the `pies.managed="true"` `pies-plugin/*` image (never touching `pies-builtin-stages` or `pies-plugin-base`; cleanup failures log warnings and clear the state reference without maintaining background retry state machines);
- **Object reference protection**: Object-storage packages referenced by historical project versions, task snapshots, or evidence remain protected by `ObjectOwner` and MUST NOT be physically deleted.

## External Delivery Self-Check List

- **Before submitting a device model**: Confirm `1.0.0` schema compliance, absence of financial/precision/code fields, readable `<publisher>` prefix (for market submissions), license, and valid sample inputs.
- **Before submitting an algorithm plugin**: Run `pies-sdk validate` and `pies-sdk pack` to verify ZIP layout safety, manifest and `consumes`/`produces` compliance, and deterministic offline self-test execution; include a clear `README.md`, explicit license, and at least one network-free `tests` case for public market submissions.
