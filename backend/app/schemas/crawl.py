"""Crawl-run schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ..models.enums import CrawlMode
from .common import ORMModel


class CrawlCreate(BaseModel):
    mode: CrawlMode = CrawlMode.FULL
    target_urls: list[str] | None = Field(
        default=None,
        description="Incremental mode only: the exact URLs to re-audit.",
        max_length=5000,
    )
    max_pages: int | None = Field(default=None, ge=1, le=200_000)


class CrawlRunResponse(ORMModel):
    id: int
    website_id: int
    status: str
    trigger: str
    mode: str
    stage: str | None
    progress_percent: float

    urls_discovered: int
    pages_queued: int
    pages_crawled: int
    pages_rendered: int
    pages_analysed: int
    pages_failed: int
    ai_completed: int
    ai_failed: int
    ai_skipped: int

    average_seo_score: float | None
    critical_issue_count: int
    total_issue_count: int

    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: float | None
    error: str | None
    created_at: datetime


class RuleInfo(BaseModel):
    id: str
    check_type: str
    category: str
    title: str
    weight: float
    description: str
    fix_hint: str
    site_wide: bool
