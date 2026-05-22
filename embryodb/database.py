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


def _ensure_sqlite_parent(db_url: str) -> None:
    """If `db_url` is a SQLite file URL, create the parent directory."""
    # SQLite URLs look like sqlite:////absolute/path or sqlite:///relative/path
    if not db_url.startswith("sqlite:///"):
        return
    path_part = db_url[len("sqlite:///"):]  # strips the scheme; may start with /
    if not path_part or path_part == ":memory:":
        return
    from pathlib import Path
    parent = Path(path_part).parent
    if parent != Path("."):
        parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _ensure_sqlite_parent(settings.db_url)
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
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _apply_additive_migrations(engine)


def _apply_additive_migrations(engine: Engine) -> None:
    """Add nullable columns that were introduced after a DB was first created.

    SQLAlchemy's `create_all` is idempotent for whole tables but does NOT add
    columns to existing tables. For dev environments where we'd rather not
    require a full DB reset on every additive schema change, we issue
    `ALTER TABLE ... ADD COLUMN` for any expected columns that are missing.

    Only safe for purely additive, nullable changes — not for renames or
    NOT NULL columns. Anything more involved should go through a real
    migration once the schema stabilises post-v1.
    """
    from sqlalchemy import inspect, text

    additive: dict[str, list[tuple[str, str]]] = {
        # table_name -> [(column_name, DDL fragment)]
        "series": [
            ("deleted_at", "TIMESTAMP"),
            ("deleted_by", "VARCHAR(64)"),
        ],
        "pipeline_step_runs": [
            ("not_before", "TIMESTAMP"),
        ],
    }
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, cols in additive.items():
            if table not in existing_tables:
                continue  # create_all just made it fresh; nothing to migrate
            present = {c["name"] for c in inspector.get_columns(table)}
            for col_name, ddl in cols:
                if col_name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {ddl}"))


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
