"""Single-database engine + per-request connection/transaction dependencies."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine

from src.config import settings

_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(settings.LM_DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)
    return _engine


@contextmanager
def _connection() -> Generator[Connection, None, None]:
    conn = engine().connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _transaction() -> Generator[Connection, None, None]:
    conn = engine().connect()
    trans = conn.begin()
    try:
        yield conn
        trans.commit()
    except Exception:
        trans.rollback()
        raise
    finally:
        conn.close()


class UnitOfWork:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection


def get_read_connection() -> Generator[Connection, None, None]:
    with _connection() as conn:
        yield conn


def get_write_uow() -> Generator[UnitOfWork, None, None]:
    with _transaction() as conn:
        yield UnitOfWork(conn)
