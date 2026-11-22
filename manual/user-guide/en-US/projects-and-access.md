# Projects and Access

## Project List and Workbench

The **Projects** page lists projects under your account ordered by most recent update, with filter tabs at the top for **All**, **Active**, and **Archived**. Each project is accessible only to its owner; to collaborate across a team, export a project package and share it with others to import.

Inside the project workbench, the top header consistently displays the project name, current version, currency, and autosave status, with the left sidebar providing feature navigation.

## Archiving and Deleting

- **Archive / Unarchive**: Sets a finished project to read-only while preserving all versions, tasks, and results. Click **Archive** in the project list or project settings and confirm; to resume editing, open the **Archived** list and click **Unarchive**.
- **Delete project**: Deletion is permanent and cannot be undone. Back up by exporting a project package or report beforehand. Click **Delete project**, verify the project name in the confirmation dialog, and confirm.

## Project Package Sharing and Import

A project package (`.zip`) transfers or shares a complete project (including models, working draft, historical versions, datasets, and historical result evidence, excluding accounts and permissions):

1. **Export**: As the project owner, open **Exports** in the workbench and click **Export full project package** to download the archive.
2. **Upload Proposal**: On the **Projects** page, the recipient clicks **Import project package** and selects the `.zip` file. The system parses it and displays a proposal preview (original project name, currency, and number of staged objects).
3. **Confirm import**: Confirming creates a standalone new project owned by the importing user without overwriting the original or any existing project; cancelling aborts the import. If confirmation succeeds on the server while refreshing the project list times out, click the refresh button instead of confirming again.

## Project Versions and Historical Restore

The workbench continuously autosaves your working draft, **but autosave does not create permanent versions**. Use **Versions** in the left navigation to explicitly seal and restore project states:

- **Create version**: Click **Create version**, enter a version name and optional description, and seal the current draft into an immutable version (recorded as a manual save). Submitting a calculation task also automatically creates a snapshot version.
- **View details**: Click any version row to inspect its creation time, reason, baseline, and currency metadata.
- **Restore this version**: Click **Restore this version** in the version details and confirm. This creates a new version (all prior versions, tasks, and results remain unchanged) and restores the current editable draft along with finance and planning configuration pointers to that version's state. Restoring also works to roll back unsealed temporary draft edits made after the current version. Archived projects do not support restoration.

To adopt planning capacities solved by a calculation run, use **Apply scheme** on the **Results** page to derive a new version directly; see [Results and Exports](results-and-exports.md).

Next: [System Modeling](system-modeling.md).
