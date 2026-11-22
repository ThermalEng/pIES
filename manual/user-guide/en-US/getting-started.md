# Getting Started

## Prerequisites

Confirm the following details with your system administrator before signing in:

- The pIES access URL;
- Your username and initial password;
- The settlement currency for new projects (projects do not store a time zone; the UTC offset for generic timestamp data files is declared separately when uploading datasets).

If you need to start the service yourself, see [Deployment and Operations](../../developer-guide/en-US/deployment.md).

## Sign In and Sessions

1. Open the pIES URL in a browser.
2. Enter your username and password, choose your interface language, and click **Sign in**.
3. If prompted to change your password on first sign-in, enter your current password and confirm the new password twice, then sign in again.

**Tips**:

- **Language and Dark/Light Mode**: Both the sign-in page and top navigation bar provide language (`简体中文` / `English`) and light/dark mode toggle buttons (keyboard focus followed by `Enter` or `Space` is supported). Preferences are stored locally in the current browser, persist across reloads and sign-outs, and are not synced to other devices via your account.
- **Independent Multi-Window Sessions**: The same account can sign in across multiple browsers or windows simultaneously. Signing out in one window ends only that session; changing your password or deactivating an account invalidates all existing sessions.
- **Idle Expiration and Sign-Out**: Sessions expire automatically after 8 hours of inactivity by default (real pointer clicks, canvas dragging, keyboard edits, and scrolling renew the session; idle background polling does not). Background submitted calculation tasks are not cancelled when a session expires or you sign out. A single request timeout (60 s for standard requests, 120 s for downloads) does not trigger sign-out.

## Create Your First Project

1. On the **Projects** page, click **New project**.
2. Enter the project name and select the settlement currency (all monetary amounts within a project use this currency; automatic currency conversion is not supported).
3. Enter the **Start year (immutable after creation)** (a Gregorian integer year, e.g., `2026`; the system automatically derives leap years from the Gregorian calendar) and select the **Resolution (immutable after creation)** (e.g., `1h`). **Start year and resolution cannot be changed after creation.**
4. Click **Confirm**, then open the project from the list to enter the **Workbench**.

The left workbench navigation lists **System Modeling**, **Data**, **Calc Config**, **Validation**, **Tasks**, **Results**, and **Exports** in order (a newly opened project defaults to **Tasks**).

## Quick End-to-End Walkthrough

When getting familiar with the interface for the first time, follow this sequence to complete an end-to-end run:

1. In **System Modeling**, drag and configure devices and connect their ports.
2. In **Data**, click **Built-in sample data** to generate a demonstration load and weather dataset, and click **Bind to project** once validation passes.
3. In **Calc Config** (including Finance, Cost & tariff bindings, Planning config, and Calculation Config), fill in economic parameters, planning objectives, and solver settings, and save each section.
4. In **Validation**, run the project preflight check and confirm there are no blocking errors.
5. In **Tasks**, enter the evaluation confirmation parameters and click **Submit task**.
6. After the task completes, inspect the solution, step curves, and financial metrics in **Results**, and download the Excel report in **Exports**.

*(Note: Built-in sample data is for feature demonstration only. For engineering projects, upload real project data and verify all parameters.)*

## Common Sign-In Issues

- **Page cannot be opened**: Check the URL, local network, and whether the service containers are healthy.
- **Sign-in failed**: Verify your username and password. If the prompt states the account is disabled, contact an administrator.
- **Workbench reports save failed**: Inspect the error message, verify server connectivity, save again, and verify the autosave indicator at the top of the workbench.

Next: [Projects and Access](projects-and-access.md).
