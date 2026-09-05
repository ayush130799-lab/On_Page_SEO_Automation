"""The Website aggregate — one site built by the company."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .crawl import CrawlRun
    from .github import GitHubEvent
    from .integration import Integration
    from .job import Job
    from .page import Page
    from .roadmap import SeoRoadmap
    from .user import WebsiteMember


class Website(TimestampMixin, Base):
    __tablename__ = "websites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    # ── GitHub mapping ──────────────────────────────────────────────────────
    #: ``owner/repo`` — matched case-insensitively against webhook payloads.
    github_repo: Mapped[str | None] = mapped_column(String(255), index=True)
    github_branch: Mapped[str | None] = mapped_column(String(255), default="main")
    #: Optional explicit ``{"src/pages/about.tsx": "/about"}`` overrides for file→page mapping.
    github_path_map: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    #: Framework hint ("next", "nuxt", "astro", "hugo", …) used by the mapping heuristics.
    github_framework: Mapped[str | None] = mapped_column(String(50))

    # ── Crawl configuration ─────────────────────────────────────────────────
    max_pages: Mapped[int | None] = mapped_column(Integer)
    #: "auto" (render only when static HTML is thin), "always", or "never".
    render_mode: Mapped[str] = mapped_column(String(20), default="auto", nullable=False)
    crawl_delay: Mapped[float | None] = mapped_column(Float)
    respect_robots_txt: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    include_patterns: Mapped[list[str] | None] = mapped_column(JSONColumn)
    exclude_patterns: Mapped[list[str] | None] = mapped_column(JSONColumn)

    # ── Denormalised dashboard summary (refreshed after each crawl/scoring) ──
    total_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_seo_score: Mapped[float | None] = mapped_column(Float)
    critical_issue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    high_priority_page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_crawled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_scored_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # ── Relationships ───────────────────────────────────────────────────────
    members: Mapped[list["WebsiteMember"]] = relationship(
        back_populates="website", cascade="all, delete-orphan"
    )
    integrations: Mapped[list["Integration"]] = relationship(
        back_populates="website", cascade="all, delete-orphan"
    )
    pages: Mapped[list["Page"]] = relationship(
        back_populates="website", cascade="all, delete-orphan"
    )
    crawl_runs: Mapped[list["CrawlRun"]] = relationship(
        back_populates="website", cascade="all, delete-orphan"
    )
    github_events: Mapped[list["GitHubEvent"]] = relationship(
        back_populates="website", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="website", cascade="all, delete-orphan"
    )
    roadmaps: Mapped[list["SeoRoadmap"]] = relationship(
        back_populates="website", cascade="all, delete-orphan"
    )
