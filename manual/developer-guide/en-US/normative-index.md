# Normative Index and Document Boundaries

> Status: Effective; Scope: Official product documentation, internal implementation notes, Reviews, and historical Archive.

This page defines the role and sole authoritative owner of each document category so rules are never duplicated across multiple files and allowed to drift.

## Division of Documentation Roles

| Directory / Document | Authoritative Content | Excluded Content |
|---|---|---|
| [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md) | Global principles, module boundaries, conflict precedence order | Release schedule, work slices, progress status |
| Remaining Developer Guide chapters | Long-term architecture, public contracts, file formats, extension mechanisms | Execution process, Roadmap, current completion status |
| [User Guide](../../user-guide/en-US/README.md) | Operations executable directly in the current GUI and current limits | Backend internals, private jargon, unreleased features |
| [`manual/changelog/en-US/`](../../changelog/en-US/README.md) | Accepted change history and release development plan (Roadmap) | Long-term architecture and format specs |
| `docs/development/` | Temporary implementation handoffs and process control | Long-term public contracts and architectural rules |
| `docs/reviews/` | Baselined code and architecture review snapshots | Current implementation specifications |
| `docs/archive/` | Superseded or retired historical materials | Any current development authority |

## Single-Source and Cross-Reference Principles

1. Every stable specification has one authoritative full definition; all other documents state only local context and link to the authoritative section rather than pasting duplicate field tables, parameter lists, or formulas.
2. Concrete HTTP field lists derive from the running version's OpenAPI/Schema; documentation does not manually maintain a second API table.
3. Ownership of cross-file technical specifications:
   - Domain semantics overview: [Architecture Blueprint](architecture.md#domain-semantics-and-mathematical-expression);
   - Network boundaries and `system_equations`: [Network Boundaries and System Equations](formats/network-relations.md);
   - Named cost items, amounts, cash movements, assets, and derived taxes: [Named Amounts, Payments, and Assets](formats/finance-relations.md);
   - Evaluation comparison and frozen baseline inputs: [Public Contracts](contracts.md#evaluation-scope-and-frozen-baseline);
   - Solver declarations, series, and financial fact mappings: [Solver Bundle](formats/solver-bundle.md).
4. When a rule changes, update its authoritative text first, then check links and summaries at all referencing sites.

## Archival Mechanism

Process documents falling under any of the following cases SHOULD move promptly to `docs/archive/`, carrying top-matter noting status, archive date, reason, and current replacement source:

- Completed or superseded implementation plans, handoffs, investigation notes, or legacy specs;
- Historical documents conflicting with the current Constitution, Developer Guide, or Roadmap;
- Review snapshots reflecting only obsolete code baselines.

Archived files retain their original historical text without continuous rewriting for current rules; moving or deleting documents MUST synchronously repair links in active docs, code comments, and tests.
