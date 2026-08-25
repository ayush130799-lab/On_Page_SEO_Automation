"""Database engine, session factory and schema bootstrap.

Production schema management is Alembic (``alembic upgrade head``). ``init_db`` remains for the
SQLite developer/test path, where spinning up a migration chain for an in-memory database is pure
overhead.

The SQLite fallback from the original MVP is deliberately retained: it lets a developer clone the
repository and run the API without provisioning PostgreSQL first.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

SQLITE_FALLBACK_URL = "sqlite:///./seo_automation.db"


class Base(DeclarativeBase):
    pass


def _is_placeholder_url(url: str) -> bool:
    """True when DATABASE_URL is still the unconfigured example value."""
    lowered = url.lower()
    return "your_password" in lowered or "postgres:@" in lowered


def _sqlite_engine(url: str = SQLITE_FALLBACK_URL) -> Engine:
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver callback
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def create_resilient_engine() -> Engine:
    """Build the engine, falling back to SQLite when PostgreSQL is unavailable."""
    db_url = settings.database_url

    if db_url.startswith("sqlite"):
        return _sqlite_engine(db_url)

    if _is_placeholder_url(db_url):
        logger.info("DATABASE_URL is unconfigured; using the local SQLite database.")
        return _sqlite_engine()

    try:
        engine = create_engine(
            db_url,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle,
        )
        with engine.connect():
            pass
        return engine
    except Exception as exc:
        logger.warning(
            "Could not connect to PostgreSQL (%s). Falling back to the local SQLite database.",
            exc,
        )
        return _sqlite_engine()


engine = create_resilient_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db(target_engine: Engine | None = None) -> None:
    """Create any missing tables. Used by tests and the SQLite dev path."""
    from . import models  # noqa: F401  (registers mappers)

    Base.metadata.create_all(bind=target_engine or engine)


def get_db():
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
