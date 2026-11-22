# Frontend and Help Center

> Status: Effective Blueprint; Code Boundary: `frontend/src/`; Governing Spec: [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md)

The frontend maps public backend contracts into clear interactive screens and manages unsubmitted forms and UI display preferences; it holds no authoritative business state and does not replicate domain calculations or authorization logic client-side.

## Directory Layering and Dependency Direction

```text
app / pages → features → shared
```

| Layer | Responsibility | Constraint |
|---|---|---|
| `app` | Route wiring, global Providers, session lifecycle, error boundaries | Contains no concrete business form logic |
| `pages` | Organizes route-level user workflows and multi-feature composition | Does not assemble backend DTOs directly or invoke low-level HTTP clients |
| `features` | Owns a single capability's `contracts`, `model`, `form`, pure `mappers`, query/mutation hooks, and components | Features MUST NOT import each other's internal forms or caches |
| `shared` | Business-agnostic HTTP client, error parsing, formatting, i18n, and shared UI primitives | Depends on no business feature |

## Frontend-Backend Data Mapping Boundary

- **Type isolation**: Strictly separate `ResponseContract` (server JSON output), `RequestContract` (submission DTO), `FrontendModel` (frontend domain view), and `FormState` (unsubmitted input strings).
- **Pure mapper boundary**: Percentage display conversions (e.g., `8` ↔ `0.08`), form string parsing, and simple display unit conversions belong inside pure mappers covered by unit tests; canonical business values returned by the backend are consumed directly without re-validating internal fields client-side.
- **Appearance and local preferences**: Light/dark themes and Chinese/English language switching use unified semantic CSS variables (`color-scheme` and `document.lang` stay synchronized); language and theme preferences persist in browser local storage independently of accounts and projects and apply before first paint.

## State Management and Failure Handling

| State Type | Authoritative Owner | Examples |
|---|---|---|
| Server facts | Feature query/cache layer | Projects, drafts, tasks, results, device catalog |
| Unsubmitted edits | Feature form state | Parameter input text, staged upload files, in-progress edge edits |
| Transient UI state | Component local state | Dialog open/close, selected row, collapsed TOC |
| Navigation state | Router / URL | Current project ID, task ID, help chapter ID |

Core interaction rules:

1. **Distinguish states rather than guessing empty states**: Clearly distinguish "loading", "empty successful list", "network failure", "unauthorized", "version conflict (409)", and "server error". On failure, display the error and a retry action—never masquerade failures as "No data"; canvas saves and sign-in follow actual HTTP request results.
2. **Optimistic locking and edit protection**: On concurrent conflicts (`409`) or validation errors (`400`/`422`), retain unsubmitted user edits, display field-level diagnostics near offending fields alongside a summary overview, and let the user choose whether to reload the latest revision or overwrite.
3. **Sessions and async tasks**: Only real user interactions on visible pages trigger idle session renewal—background polling and auto-refresh do not keep sessions alive; task list and result polling stop upon leaving the page or reaching terminal task state; paginated step-curve fetching cancels stale requests so out-of-order responses never overwrite newer selections.
4. **Immutable result presentation**: Result metrics, financial breakdowns, construction decisions, calendar-month demand bills, and charts all render sealed facts of the chosen task without recomputing financials client-side or backfilling fields from the live project draft.

## Schema-Driven GUI and General Modeling

Following [Model Document and Backend Centric](architecture.md#model-document-and-backend-centric):

- **Coexistence of forms and full documents**: Structured forms support everyday parameter editing while complete YAML/JSON editors cover the full contract (such as `step_relation`, `system_equations`, `amount_relation`, named finance relations, and multi-period rules). Both manipulate the same backend document, preserving valid fields and exact decimal strings across view switches without silently stripping fields outside a form allowlist.
- **Generic devices and canvas**: The device palette, inspector forms, and port types derive entirely from backend `DeviceDescriptor` metadata; the canvas enforces only basic graph geometry and typing rules (blocking self-loops, duplicate edges, `blind` port wiring, and restricting `predefined` ports to data source binding) and submits commands using real backend instance and port IDs.
- **Unified save entry**: Model, finance, and configuration documents authored via forms, template instantiation, or online editors serialize into complete documents and enter through the same backend parsing endpoint for validation and persistence.

### Browser Exploration and Automated Regression

Browser acceptance runs exclusively against real Chromium inside Docker, starting every scenario from page entry points using accessible locators and never bypassing tested steps via state injection:

- **Agent exploration**: When new features land or interactions undergo substantial refactoring, explore user paths, error prompts, and recovery flows in a real browser and codify confirmed critical paths into Playwright specs.
- **Automated regression**: Local development and CI share the exact same Playwright suite, data factories, and business assertions, differing only in orchestration environment variables; routine commits run directly impacted and adjacent specs, while releases and major architectural changes run the full suite. Every test report MUST state its commit baseline, executed scope, and uncovered areas.

## Help Center Mechanism

The top-bar "Help Center" reads `manual/` directly during the frontend build (`frontend/scripts/generate-help.mjs`) to generate a read-only `manifest.json`:

- `manual/SUMMARY.md` registers available locales, while `manual/SUMMARY.<locale>.md` defines the three top-level trees ("User Guide", "Developer Guide", "Changelog"), deriving stable cross-locale chapter IDs from file basenames.
- Supports deep links, heading anchors, previous/next navigation, collapsible mobile navigation, and keyboard focus management; the renderer strictly strips raw HTML, scripts, and unsafe URL schemes.
- Served completely statically at runtime, remaining readable even when the backend or database is unavailable; when a target locale is unavailable, explicitly lists available languages without silently substituting another language.

## Definition of Done

- Within each feature, `contract`, `model`, `form`, and `mapper` responsibilities are cleanly separated, with unit tests covering critical mapper boundaries;
- Loading, empty, error, conflict, and authorization states remain distinct and preserve unsubmitted edits;
- Canvas interactions, file exports, deep-link navigation, theme/language persistence, and keyboard/mobile layouts pass Docker Playwright acceptance;
- User-visible changes update the User Guide and Changelog in the same change.
