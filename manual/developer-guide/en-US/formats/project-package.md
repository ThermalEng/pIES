# Project Package

> Contract ID: `ies.project-package`; Target `format_version`: `"1.0.0"`; File Format: `*.zip` (containing root `manifest.json`)

Project Package is the offline portability and archive format for a project, fully encapsulating the active working state, packaged historical versions, and all finance configs, planning configs, dataset versions, project models, and historical result evidence required to restore them. It excludes accounts, passwords, sessions, permissions, and host paths; source database IDs serve strictly as package-internal lookup keys and are reassigned fresh identities upon confirmed import.

## ZIP Directory Layout

```text
manifest.json
project.json
draft.json
finance_profile.yaml            # Active working state FinanceProfile
finance_overrides.yaml          # Active working state FinanceOverrides (empty overrides doc if none)
effective_finance.yaml          # Active working state Effective snapshot (verified by re-merging sources)
planning_config.yaml            # Active working state planning config (absent if unset)
versions/0001.json …            # Packaged historical version snapshots
datasets/{dataset_id}/v{version_no}/dataset.json    # Dataset version metadata
datasets/{dataset_id}/v{version_no}/{file_kind}.csv # Corresponding dataset CSV
evidence/{package_id}.json …    # Historical result evidence (preserves provenance; does not fabricate local tasks)
version_configs/{NNNN}/*.yaml   # Per-historical-version independent finance & planning dependency files
models/{source_device_id}.json  # Canonical JSON text for project models referenced by draft and historical versions
```

## Key Structure of `manifest.json`

Top-level `manifest.json` contains `format_version`, `package_type: "project"`, `objects: [{path, size_bytes, media_type}]`, and `files: {project, draft, configs, versions[], datasets[], evidence[], version_configs, models[]}`. Every path MUST correspond 1-to-1 with `objects` and actual archive entries with exact byte sizes, rejecting duplicate or out-of-bounds paths.

```json
{
  "format_version": "1.0.0",
  "package_type": "project",
  "objects": [
    {"path": "project.json", "size_bytes": 412, "media_type": "application/json"},
    {"path": "datasets/7/v2/dataset.json", "size_bytes": 388, "media_type": "application/json"},
    {"path": "datasets/7/v2/data.csv", "size_bytes": 1024, "media_type": "text/csv"},
    {"path": "models/acme.device.load_1.json", "size_bytes": 320, "media_type": "application/json"},
    {"path": "version_configs/0001/finance_profile.yaml", "size_bytes": 190, "media_type": "application/yaml"}
  ],
  "files": {
    "project": "project.json",
    "draft": "draft.json",
    "configs": {"finance_profile": "finance_profile.yaml"},
    "versions": ["versions/0001.json"],
    "datasets": ["datasets/7/v2/dataset.json"],
    "evidence": [],
    "version_configs": {
      "0001": {
        "finance_profile": "version_configs/0001/finance_profile.yaml",
        "finance_overrides": "version_configs/0001/finance_overrides.yaml"
      }
    },
    "models": [
      {
        "device_id": "acme.device.load_1",
        "base_device_id": "acme.device.load",
        "model_text": "models/acme.device.load_1.json"
      }
    ]
  }
}
```

### `files.version_configs`

Every historical version `versions/NNNN.json` carrying `effective_finance` or `planning_config` references MUST carry its exact dependency YAML files under `files.version_configs["NNNN"]` (`finance_profile`, `finance_overrides`, `effective_finance`, `planning_config`, omitting keys only when absent). On import, the finance snapshot MUST be re-merged and verified from the packaged raw `FinanceProfile` and `FinanceOverrides` (rejecting imports if a Profile with the same ID already exists in the target DB with conflicting content)—never substituting current config or the target DB's latest Profile for historical version dependencies.

### `files.models`

- MUST contain the exact canonical model JSON texts for all project models referenced by the current draft and all packaged historical versions (sorted ascending by source suffix); the packaged set MUST match the referenced set exactly (missing referenced models or extra unreferenced models are both rejected); device type strings appearing solely in historical result evidence are excluded.
- Each entry specifies `{device_id, base_device_id, model_text}`: upon confirmed import, each model re-validates through the official model save gate under `base_device_id` to receive a fresh `_N` suffix and validation receipt inside the target project, remapping draft and version model references and instance `device_type` IDs to the new IDs.

## Two-Phase Import Semantics

1. **Upload and proposal**: Validates archive integrity, Manifest structure, skeleton shapes, full model reference set, and finance merge consistency, returning full diagnostics via `params.reasons` on failure; passing creates a staged proposal without creating a project.
2. **Confirm and publish**: Within a single database transaction, creates the target project and reconstructs config revisions, dataset versions, project models, and historical version reference mappings; failure at any step rolls back the entire transaction cleanly without creating partial projects, leaving an accepted proposal retryable.
