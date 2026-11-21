"""Retry a task's short database operation after a PostgreSQL disconnect."""

from collections.abc import Callable
from time import sleep
from typing import Concatenate, ParamSpec, TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

T = TypeVar("T")
P = ParamSpec("P")


def run_worker_transaction(
    session_factory: Callable[[], Session],
    operation: Callable[Concatenate[Session, P], T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Leave the attempt active while the database recovers."""
    while True:
        with session_factory() as db:
            try:
                return operation(db, *args, **kwargs)
            except OperationalError as exc:
                state = getattr(exc.orig, "sqlstate", None)
                if not exc.connection_invalidated and (
                    db.get_bind().dialect.name != "postgresql"
                    or (state and not state.startswith("08") and state not in {"57P01", "57P02", "57P03"})
                ):
                    raise
        sleep(1)
