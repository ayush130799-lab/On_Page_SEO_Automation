"""Page listing and page-detail schemas.

``PageListItem`` is the row the priority table renders: it deliberately carries the SEO score, the
priority score and the business metrics side by side, because comparing them is the whole point of
the dashboard.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from .common import ORMModel


class MetricSummary(BaseModel):
    """Aggregated provider metrics over the active lookback window."""

    window_days: int = 0
    users: int = 0
    sessions: int = 0
    engagement_rate: float | None = None
    conversions: float = 0.0
    revenue: float = 0.0
    clicks: int = 0
    impressions: int = 0
    ctr: float | None = None
    position: float | None = None
    organic_keywords: int = 0
    organic_traffic: int = 0
    striking_distance_keywords: int = 0
    backlinks: int = 0


class IssueSummary(BaseModel):
    id: int
    rule_id: str
    check_type: str
    category: str | None
    severity: str
    title: str
    description: str
    recommendation: str | None = None
    evidence: dict[str, Any] | None = None


class PageListItem(ORMModel):
    id: int
    url: str
    path: str
    title: str | None
    status_code: int | None
    seo_score: float | None
    seo_category: str | None
    highest_severity: str | None
    issue_count: int
    priority_score: float | None
    priority_band: str | None
    priority_rank: int | None
    ai_status: str
    last_crawled_at: datetime | None

    # Populated by the list endpoint from the metric tables.
    users: int = 0
    sessions: int = 0
    conversions: float = 0.0
    revenue: float = 0.0
    clicks: int = 0
    impressions: int = 0
    ctr: float | None = None
    position: float | None = None
    top_issues: list[str] = []


class PageDetail(ORMModel):
    id: int
    website_id: int
    url: str
    path: str
    final_url: str | None
    is_active: bool
    status_code: int | None
    redirect_chain: list[str] | None

    title: str | None
    meta_description: str | None
    h1: str | None
    h1_count: int
    h2_count: int
    h3_count: int
    canonical_url: str | None
    robots_directive: str | None
    lang: str | None
    hreflang: list[dict[str, Any]] | None
    has_viewport: bool
    has_structured_data: bool
    structured_data_types: list[str] | None
    has_open_graph: bool

    word_count: int
    content_hash: str | None
    image_count: int
    missing_alt_count: int
    internal_link_count: int
    external_link_count: int
    broken_link_count: int
    inbound_internal_links: int

    was_rendered: bool
    response_time_ms: int | None
    crawl_status: str
    crawl_error: str | None

    seo_score: float | None
    seo_category: str | None
    highest_severity: str | None
    issue_count: int
    priority_score: float | None
    priority_band: str | None
    priority_rank: int | None
    ai_status: str
    ai_analysed_at: datetime | None

    first_seen_at: datetime | None
    last_crawled_at: datetime | None


class HistoryPoint(BaseModel):
    date: date
    seo_score: float | None = None
    priority_score: float | None = None
    issue_count: int = 0
    clicks: int = 0
    impressions: int = 0
    users: int = 0
    sessions: int = 0
    conversions: float = 0.0
    revenue: float = 0.0


class PriorityBreakdown(BaseModel):
    score: float
    band: str | None = None
    rank: int | None = None
    components: dict[str, float] = {}
    weights: dict[str, float] = {}
    breakdown: dict[str, Any] = {}
    data_sources: list[str] = []
    computed_at: datetime | None = None


class PageDetailResponse(BaseModel):
    page: PageDetail
    issues: list[IssueSummary] = []
    checks: list[dict[str, Any]] = []
    metrics: MetricSummary = MetricSummary()
    priority: PriorityBreakdown | None = None
    history: list[HistoryPoint] = []
    recommendation: dict[str, Any] | None = None
    github_changes: list[dict[str, Any]] = []
