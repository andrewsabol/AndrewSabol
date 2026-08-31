"""Database engine and session management."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def init_engine(url: str, *, echo: bool = False) -> Engine:
    """Create the engine and ensure the schema exists."""
    global _engine, _SessionFactory

    connect_args = {}
    if url.startswith("sqlite"):
        # The bot handles updates concurrently; allow cross-thread use and wait
        # rather than failing immediately on a locked write.
        connect_args = {"check_same_thread": False, "timeout": 30}

    _engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)

    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver glue
            cursor = dbapi_connection.cursor()
            # Foreign keys are off by default in SQLite; a financial ledger needs
            # them on so a settlement can never reference a missing payable.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    Base.metadata.create_all(_engine)
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("engine not initialised; call init_engine() first")
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    if _SessionFactory is None:
        raise RuntimeError("engine not initialised; call init_engine() first")
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any exception.

    Financial mutations must be all-or-nothing: a settlement that is created but
    whose audit row is lost would break the immutable history guarantee.
    """
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
