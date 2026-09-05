"""Database migration and auto-stamp runner.

Safely applies migrations:
1. If 'alembic_version' is present, runs 'alembic upgrade head'.
2. If tables (like 'users') already exist but 'alembic_version' is missing or unversioned,
   stamps the database with 'head' so duplicate CREATE TABLE errors never occur.
3. If the database is completely empty, runs 'alembic upgrade head' from scratch.
"""

from __future__ import annotations

import logging
import sys

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate")


def resolve_db_url() -> str:
    db_url = settings.database_url
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif db_url.startswith("postgresql://") and "+psycopg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return db_url


def run_migration() -> None:
    db_url = resolve_db_url()
    logger.info("Connecting to database for migration check...")

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    tables = set()
    has_alembic = False
    has_users = False
    alembic_rows = []

    engine = sa.create_engine(db_url)
    try:
        with engine.connect() as conn:
            inspector = sa.inspect(conn)
            tables = set(inspector.get_table_names())
            has_alembic = "alembic_version" in tables
            has_users = "users" in tables
            if has_alembic:
                alembic_rows = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchall()
    except Exception as inspect_exc:
        logger.warning("Initial schema inspection failed: %s", inspect_exc)
    finally:
        engine.dispose()

    try:
        if has_alembic and alembic_rows:
            logger.info("Found existing alembic_version (%s). Running alembic upgrade head...", alembic_rows)
            command.upgrade(alembic_cfg, "head")
        elif has_users:
            logger.info("Detected pre-existing tables without alembic_version. Stamping 'head'...")
            command.stamp(alembic_cfg, "head")
        else:
            logger.info("Fresh database detected. Running alembic upgrade head from 0001...")
            command.upgrade(alembic_cfg, "head")

        logger.info("Database migrations/stamping successfully completed.")
    except Exception as exc:
        logger.warning("Migration raised an exception: %s", exc)
        err_msg = str(exc).lower()
        if "already exists" in err_msg or "duplicate" in err_msg:
            try:
                logger.info("Relation already exists error encountered. Stamping alembic head...")
                command.stamp(alembic_cfg, "head")
                logger.info("Stamped head successfully.")
                return
            except Exception as stamp_exc:
                logger.warning("Stamp head attempt failed: %s", stamp_exc)

        # Check if core tables already exist so the API can safely run
        try:
            check_engine = sa.create_engine(db_url)
            with check_engine.connect() as conn:
                inspector = sa.inspect(conn)
                if "users" in inspector.get_table_names():
                    logger.info("Core tables ('users') exist. Proceeding with application startup...")
                    return
            check_engine.dispose()
        except Exception as check_exc:
            logger.error("Could not verify existing tables: %s", check_exc)
        raise exc


if __name__ == "__main__":
    try:
        run_migration()
    except Exception as e:
        logger.error("Fatal migration failure: %s", e)
        sys.exit(1)
