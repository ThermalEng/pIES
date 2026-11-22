# Persistence

> Code Boundary: Domain modules' `persistence.py` / `tables.py` and `backend/iesplan/migrations/`; Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

The persistence layer reliably persists domain state and enforces uniqueness, referential integrity, and immutability via database constraints. Every database table and Repository belongs to a single authoritative domain owner and MUST NOT be queried arbitrarily as a shared cross-module table model.

## Ownership and Transaction Boundaries

- **Table ownership isolation**: Each domain accesses solely its own tables and Repositories (e.g., accounts belong to `identity`, snapshots/tasks to `tasks`, evidence projections to `results`, object references to `storage`); cross-domain read views compose public domain interfaces inside `application`, prohibiting cross-domain ORM joins inside API or peer domain modules.
- **Immutability protection**: Published project versions, dataset versions, config revisions, calculation snapshots, execution evidence, and audit logs forbid in-place updates or overwrites via database constraints and triggers.
- **Repository responsibilities and transaction discipline**: Repositories handle domain-to-ORM mapping, unique constraint handling (`upsert` or local `savepoint`), and DB error classification; returning lazy-loaded ORM graphs or calling global `commit`/`rollback` inside Repositories is prohibited (full transaction lifecycles belong solely to `application` use cases).
- **Large payload separation**: Binary files and large result payloads never enter relational business tables; they persist through `storage` while databases retain `ObjectId` values and reference records only.

## Database Evolution and Migration Rules

1. Schema changes belong clearly to a domain owner and ship as versioned Alembic migrations; using `create_all` instead of formal migrations in production or runtime paths is prohibited.
2. Enforce uniqueness, foreign keys, status enums, and immutability primarily through DB constraints and triggers.
3. All Repository and migration tests MUST verify upgrades, idempotent replay, foreign keys, and concurrency conflicts against real PostgreSQL Docker containers.
