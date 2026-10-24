"""Isolated PostgreSQL init_db concurrency probe, invoked ONLY by Docker CI.

Not a pytest module: two separate containers must run this script concurrently.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Executed as /app/tests/*.py inside the existing backend Docker image. Put
# /app first so the probe exercises the working tree and its ./deploy config,
# rather than the pip-installed site-packages copy of iesplan.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from iesplan.bootstrap import collect_immutable_tables, collect_trigger_statements
from iesplan import db


def initialize(worker: str) -> None:
    directory = Path("/data")
    ready = directory / f"pg-{worker}.ready"
    go = directory / "pg-init-go"
    ready.touch(exist_ok=False)
    for _ in range(400):
        if go.exists():
            break
        time.sleep(0.05)
    else:
        raise TimeoutError("Both processes did not reach shared start barrier")
    db.init_db(collect_trigger_statements())
    print(f"{worker}: init_db committed", flush=True)


def verify() -> None:
    assert db.engine.dialect.name == "postgresql", "Real PostgreSQL required"
    immutable_tables = collect_immutable_tables()
    assert immutable_tables, "Must test real installed immutable triggers"
    with db.engine.connect() as conn:
        for table in db.Base.metadata.tables:
            exists = conn.execute(text("SELECT to_regclass(:table) IS NOT NULL"), {"table": table}).scalar_one()
            assert exists, f"Missing table: {table}"
        for table in immutable_tables:
            trigger_count = conn.execute(
                text("""
                    SELECT count(*) FROM pg_trigger
                    WHERE tgrelid = to_regclass(:table)
                      AND NOT tgisinternal
                      AND tgname IN (:update_name, :delete_name)
                """),
                {"table": table, "update_name": f"tg_{table}_no_update",
                 "delete_name": f"tg_{table}_no_delete"},
            ).scalar_one()
            assert trigger_count == 2, f"{table}: expected both immutable triggers, got {trigger_count}"
    print(f"Postgres tables and triggers verified ({len(immutable_tables)} immutable tables)", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "run" and sys.argv[2] in ("first", "second"):
        initialize(sys.argv[2])
    elif sys.argv[1:] == ["verify"]:
        verify()
    else:
        raise SystemExit("usage: pg_init_concurrency_probe.py run first|second OR verify")
