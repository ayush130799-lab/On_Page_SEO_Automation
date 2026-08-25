"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .core.errors import register_exception_handlers
from .core.logging import configure_logging
from .db import init_db

configure_logging()
logger = logging.getLogger(__name__)


def _bootstrap_admin() -> None:
    """Create the initial administrator when BOOTSTRAP_ADMIN_* is configured.

    This is what makes a fresh container usable without an interactive step; it is a no-op when the
    account already exists.
    """
    if not (settings.bootstrap_admin_email and settings.bootstrap_admin_password):
        return

    from .core.security import hash_password
    from .db import SessionLocal
    from .models import User, UserRole

    db = SessionLocal()
    try:
        email = settings.bootstrap_admin_email.lower().strip()
        if db.query(User).filter(User.email == email).first():
            return
        db.add(
            User(
                email=email,
                full_name="Administrator",
                password_hash=hash_password(settings.bootstrap_admin_password),
                role=UserRole.ADMIN,
            )
        )
        db.commit()
        logger.info("Bootstrap administrator account created for %s", email)
    except Exception as exc:  # pragma: no cover - startup convenience only
        logger.warning("Could not create the bootstrap administrator: %s", exc)
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.is_production and settings.secret_key.startswith("dev-only"):
        raise RuntimeError(
            "SECRET_KEY must be set to a strong random value when ENVIRONMENT=production."
        )
    try:
        # Alembic owns the schema in production; this keeps the SQLite dev path frictionless.
        init_db()
    except Exception as exc:
        logger.warning("Database initialisation skipped (%s). Run 'alembic upgrade head'.", exc)
    _bootstrap_admin()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="2.0.0",
        description=(
            "Crawls every website the company builds, audits on-page SEO, enriches each page with "
            "Search Console, GA4 and Semrush data, and ranks the work by business priority."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    from .api.routes import (
        auth,
        crawls,
        dashboard,
        integrations,
        jobs,
        pages,
        priority,
        recommendations,
        webhooks,
        websites,
    )

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(websites.router)
    app.include_router(crawls.router)
    app.include_router(pages.router)
    app.include_router(integrations.router)
    app.include_router(priority.router)
    app.include_router(recommendations.router)
    app.include_router(webhooks.router)
    app.include_router(jobs.router)

    @app.get("/", tags=["system"])
    def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "version": app.version,
            "status": "ok",
            "health": "/health",
            "docs": "/docs",
        }

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": app.version, "environment": settings.environment}

    return app


app = create_app()
