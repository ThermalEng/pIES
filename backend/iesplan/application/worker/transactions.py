"""Retry a task's short database operation after a PostgreSQL disconnect."""

import logging
from collections.abc import Callable
from time import monotonic, sleep
from typing import Concatenate, ParamSpec, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

T = TypeVar("T")
P = ParamSpec("P")


def run_worker_transaction(
    session_factory: Callable[[], Session],
    operation: Callable[Concatenate[Session, P], T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Keep the task active and start each operation with a reachable database."""
    delay = 1
    next_warning_at = monotonic()
    while True:
        with session_factory() as db:
            try:
                # Checkpoints write files before locking the attempt row.
                db.execute(text("SELECT 1"))
                return operation(db, *args, **kwargs)
            except OperationalError as exc:
                state = getattr(exc.orig, "sqlstate", None)
                if not exc.connection_invalidated and (
                    db.get_bind().dialect.name != "postgresql"
                    or (state and not state.startswith("08") and state not in {"57P01", "57P02", "57P03"})
                ):
                    raise
        now = monotonic()
        if now >= next_warning_at:
            logger.warning("Worker 数据库连接中断，%s 秒后重试", delay)
            next_warning_at = now + 60
        sleep(delay)
        delay = min(delay * 2, 5)
