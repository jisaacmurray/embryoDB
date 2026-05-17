"""Database session management.

Centralizes engine creation so the rest of the package never instantiates an
engine directly. `session_scope()` is a context manager that handles commit /
rollback / close around a unit of work.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(settings.db_url, future=True)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autocommit=False, autoflush=True, expire_on_commit=False
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    """Create all tables. Used by tests and the first-time `init-db` CLI command."""
    Base.metadata.create_all(bind=get_engine())


def drop_all() -> None:
    Base.metadata.drop_all(bind=get_engine())


def reset_for_tests(db_url: str) -> None:
    """Swap the engine to a test URL. Tests call this before create_all()."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(db_url, future=True)
    _SessionLocal = sessionmaker(
        bind=_engine, autocommit=False, autoflush=True, expire_on_commit=False
    )
