# Accounts, Administration, and Troubleshooting

## Personal Settings

Click **Settings** in the top navigation to switch the interface language, toggle light/dark mode, or change your sign-in password. Changing your password requires entering your current password and confirming the new password twice; after saving, you must sign in again. *(Self-service password recovery is not available in the current GUI; contact an administrator to reset a forgotten password.)*

## Administrator Operations

- **Accounts** (**Accounts** in top navigation): View all accounts and deactivate, reactivate, reset passwords for, or delete non-built-in accounts. Resetting a password generates a one-time temporary password and revokes all active sessions for that user. Deleting an account permanently deletes all projects it owns (the confirmation dialog previews the affected project list; export backups beforehand).
- **Settings** (**Settings** in top navigation): Toggle self-registration on the sign-in page, inspect object storage used space and object count, inspect component health and current product version, and unlock orphaned running tasks left behind after a full host restart.
- **Open Ecosystem** (**Open Ecosystem** in top navigation, `/admin/ecosystem`, admin-only):
  - Manage the **two orthogonal switches**: `market_mode` (`local` offline mode vs `online` public market mode) and `local_upload_requires_approval` (uniformly governing non-admin local `.device.yaml` / `.algorithm.zip` imports across both modes to prevent policy bypasses);
  - Configure single-source external URLs: `pip_index_url` (passed solely via `--index-url` during plugin builds when non-empty), `market_index_url`, and optional `submission_gateway_url`;
  - Review the pending approval queue (approve to trigger build/self-test, or reject with reason) and uninstall plugin images with in-flight task protection (`409 ECO-UNINSTALL-IN-USE` blocks image deletion while preventing new submissions until in-flight tasks finish).
- **Audit log** (**Audit log** in top navigation): Read-only cursor-paginated inspection of key system change audit events filtered by action or entity type/ID (inaccessible to standard engineers). *(Online backup/restore and storage reconciliation buttons are not exposed in the GUI; operators perform those procedures on the server side.)*

### Upgrading from `1.1.5` / `1.1.6` and Ecosystem Troubleshooting

- **Upgrade playbook**: Before upgrading to `1.2.0`, drain all `queued`/`running`/`cancelling` tasks, run database migrations (`v1_2_0_calc_config` and `20261127_120_ecosystem`), and rebuild via `docker compose up -d --build`. Historical frozen snapshots and sealed evidence remain strictly read-only.
- **Plugin build or self-test failure (`build_failed`)**: Check diagnostics in the `/custom` local submissions table; contact your administrator to inspect `pip_index_url` or build container logs in `/admin/ecosystem`.
- **Remote market load error**: Under `online` mode, unreachable `market_index_url` endpoints surface explicit errors with a retry button rather than masking failures as empty catalogs; already-installed local artifacts remain usable.

## Common Troubleshooting

### Page Fails to Load or Reports Network Error

Check local connectivity and click **Retry**. If sign-in or saving fails with a network error, keep the current page open, restore connectivity to the server, resubmit, and verify the autosave status and canvas state afterward.

### Validation Fails or Task Submission Is Blocked

Check connection topology and data source bindings in **System Modeling**, dataset version bindings in **Data**, saved revisions across **Finance**, **Cost & tariff bindings**, **Planning config**, and **Calc Config**, then resolve each blocking diagnostic reported on **Validation**.

### Task Remains Queued Long or Shows Running After Host Restart

Refresh task details and ask an administrator to check **Service health** on **Settings**; do not submit duplicate tasks. Tasks that continue running across brief network drops or sleep resume publishing under their existing attempt once connectivity returns. If the host restarted and the task process exited, an administrator can unlock the task in **Settings** so it requeues from its original frozen snapshot and resumes from committed stage checkpoints (if interrupted mid-solve, unsaved solver search trees restart from the generated bundle; inspect diagnostics or retry if failed).

### Task Failed, Timed Out, or Exhausted Resources

Inspect the business note and diagnostic location in task details:

- **Input or model error**: Follow the diagnostic hint to fix the model, dataset, or configuration, then submit a new task.
- **Timed out**: Check whether model scale is too large or `Time limit (s)` is too short, adjust the limit in **Calc Config**, and submit a new task.
- **Resource exhausted**: Ask an administrator to verify and increase container memory limits before clicking **Retry task**.

### Results Fail to Load or Show Unexpected Empty States

Refresh and verify the selected task and filters first. If expected data consistently fails to load, record the following information for your administrator (never include passwords, tokens, or sensitive business screenshots):

1. Product version and current page name;
2. Project ID and task ID;
3. Exact diagnostic code, displayed message, and request trace ID;
4. Reliable reproduction steps.
