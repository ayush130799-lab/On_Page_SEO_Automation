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
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif db_url.startswith("postgresql://") and "+psycopg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

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


def sync_database_schema(target_engine: Engine | None = None) -> None:
    """Synchronise database schema with Base.metadata.

    1. Creates any missing tables (e.g. page_intent_profiles, recommendation_scores, etc.).
    2. Inspects existing tables and adds any missing columns using ALTER TABLE ... ADD COLUMN.
    3. Backfills default values for newly added columns where needed.
    4. Ensures alembic_version contains the head revision.
    """
    from . import models  # noqa: F401  (registers mappers)
    import sqlalchemy as sa

    eng = target_engine or engine
    is_postgres = eng.dialect.name == "postgresql"

    # Step 1: Create any missing tables
    Base.metadata.create_all(bind=eng)

    # Step 2: Inspect existing tables and add any missing columns
    inspector = sa.inspect(eng)
    db_tables = set(inspector.get_table_names())

    with eng.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            if table_name not in db_tables:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
            for col in table.columns:
                if col.name not in existing_cols:
                    col_type_str = str(col.type.compile(eng.dialect))
                    default_clause = ""
                    if col.server_default is not None and hasattr(col.server_default.arg, "text"):
                        default_clause = f" DEFAULT {col.server_default.arg.text}"
                    elif col.default is not None and getattr(col.default, "is_scalar", False):
                        val = col.default.arg
                        if isinstance(val, bool):
                            default_clause = (
                                f" DEFAULT {'true' if val else 'false'}"
                                if is_postgres
                                else f" DEFAULT {1 if val else 0}"
                            )
                        elif isinstance(val, (int, float)):
                            default_clause = f" DEFAULT {val}"
                        elif isinstance(val, str):
                            default_clause = f" DEFAULT '{val}'"
                    elif not col.nullable:
                        if isinstance(col.type, sa.Integer):
                            default_clause = " DEFAULT 0"
                        elif isinstance(col.type, sa.Boolean):
                            default_clause = " DEFAULT false" if is_postgres else " DEFAULT 0"
                        elif isinstance(col.type, (sa.String, sa.Text)):
                            default_clause = " DEFAULT ''"

                    if is_postgres:
                        stmt = f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col.name} {col_type_str}{default_clause}"
                    else:
                        stmt = f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type_str}{default_clause}"
                    try:
                        conn.execute(sa.text(stmt))
                        logger.info("Added missing column %s.%s (%s)", table_name, col.name, col_type_str)
                    except Exception as col_exc:
                        logger.warning("Could not add column %s.%s: %s", table_name, col.name, col_exc)

        # Step 3: Backfill any newly added columns that require values
        try:
            conn.execute(
                sa.text(
                    "UPDATE pages SET content_captured_at = last_crawled_at "
                    "WHERE content_captured_at IS NULL AND last_crawled_at IS NOT NULL"
                )
            )
            conn.execute(
                sa.text("UPDATE pages SET crawl_quality = 'ok' WHERE crawl_quality IS NULL OR crawl_quality = ''")
            )
        except Exception:
            pass

        # Step 4: Ensure alembic_version is stamped to head
        try:
            conn.execute(
                sa.text(
                    "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
            conn.execute(sa.text("DELETE FROM alembic_version"))
            conn.execute(sa.text("INSERT INTO alembic_version (version_num) VALUES ('0012_seo_experiments')"))
        except Exception as stamp_exc:
            logger.warning("Could not update alembic_version: %s", stamp_exc)


def init_db(target_engine: Engine | None = None) -> None:
    """Create any missing tables and add any missing columns."""
    sync_database_schema(target_engine)


def get_db():
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
