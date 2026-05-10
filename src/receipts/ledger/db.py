"""Engine + session factory for the receipts ledger.

DATABASE_URL drives connection. Default is a local-file SQLite so tests and
first-run dev work without external infra. Postgres in prod.
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DEFAULT_DATABASE_URL = "sqlite:///./receipts.db"


def get_database_url() -> str:
    """Resolve the database URL from env, defaulting to local sqlite."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


class Base(DeclarativeBase):
    """Declarative base for all receipts ledger models."""


def make_engine(url: str | None = None, **kwargs: Any) -> Engine:
    """Build an Engine.  For SQLite, enable FK pragmas so cascade works."""
    resolved = url or get_database_url()
    engine = create_engine(resolved, **kwargs)
    if resolved.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_fk(
            dbapi_connection, connection_record
        ) -> None:  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine | None = None) -> sessionmaker:
    """Build a sessionmaker bound to the given (or default) engine."""
    return sessionmaker(bind=engine or make_engine(), expire_on_commit=False)


# Lazy module-level singletons for convenience.
_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory(engine())
    return _session_factory
