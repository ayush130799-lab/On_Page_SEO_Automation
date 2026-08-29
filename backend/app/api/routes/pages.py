"""Page listing (the priority table) and page detail."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import Float, and_, case, func, or_, select
from sqlalchemy.orm import Session

from ...config import settings
from ...core.deps import CurrentUser, DbSession, ReadableWebsite, get_website_for_read
from ...core.errors import NotFoundError
from ...models import (
    AIRecommendation,
    GA4Metric,
    GitHubEvent,
    GSCMetric,
    Page,
    PriorityScore,
    SEOAudit,
    SEOIssue,
    SemrushMetric,
    Severity,
    severity_rank,
)
from ...schemas.common import Page as PageEnvelope
from ...schemas.page import (
    HistoryPoint,
    IssueSummary,
    MetricSummary,
    PageDetail,
    PageDetailResponse,
    PageListItem,
    PriorityBreakdown,
)
from ...services.metrics import aggregate_page_metrics, page_history, window_start

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["pages"])

#: Columns the priority table may be sorted by, mapped to their ORM expression.
SORTABLE = {
    "priority_score": Page.priority_score,
    "seo_score": Page.seo_score,
    "issue_count": Page.issue_count,
    "url": Page.url,
    "last_crawled_at": Page.last_crawled_at,
    "status_code": Page.status_code,
}
#: Sorting by a metric requires joining the aggregate; handled separately.
METRIC_SORTS = {"users", "sessions", "conversions", "revenue", "clicks", "impressions"}

SEVERITY_ORDER = case(
    (Page.highest_severity == Severity.CRITICAL, 1),
    (Page.highest_severity == Severity.HIGH, 2),
    (Page.highest_severity == Severity.MEDIUM, 3),
    (Page.highest_severity == Severity.LOW, 4),
    else_=5,
)


def _metric_subquery(db: Session, website_id: int, window_days: int):
    """Per-page metric totals over the window, as a joinable subquery.

    Used only when the caller sorts or filters by a metric — the common path takes the cheaper
    "aggregate just the current page of results" route instead.
    """
    since = window_start(window_days)

    ga4 = (
        select(
            GA4Metric.page_id.label("page_id"),
            func.coalesce(func.sum(GA4Metric.users), 0).label("users"),
            func.coalesce(func.sum(GA4Metric.sessions), 0).label("sessions"),
            func.coalesce(func.sum(GA4Metric.conversions), 0.0).label("conversions"),
            func.coalesce(func.sum(GA4Metric.revenue), 0.0).label("revenue"),
        )
        .where(GA4Metric.website_id == website_id, GA4Metric.date >= since)
        .group_by(GA4Metric.page_id)
        .subquery()
    )
    gsc = (
        select(
            GSCMetric.page_id.label("page_id"),
            func.coalesce(func.sum(GSCMetric.clicks), 0).label("clicks"),
            func.coalesce(func.sum(GSCMetric.impressions), 0).label("impressions"),
        )
        .where(GSCMetric.website_id == website_id, GSCMetric.date >= since)
        .group_by(GSCMetric.page_id)
        .subquery()
    )
    return ga4, gsc


@router.get("/websites/{website_id}/pages", response_model=PageEnvelope[PageListItem])
def list_pages(
    website: ReadableWebsite,
    db: DbSession,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort: str = Query("priority_score", description="Column to sort by."),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    search: str | None = None,
    seo_category: str | None = Query(None, description="LOW ISSUES | MEDIUM ISSUES | HIGH ISSUES"),
    severity: str | None = Query(None, description="CRITICAL | HIGH | MEDIUM | LOW | NONE"),
    priority_band: str | None = Query(None, description="P0 | P1 | P2 | P3"),
    ai_status: str | None = None,
    status_code: int | None = None,
    min_seo_score: float | None = Query(None, ge=0, le=100),
    max_seo_score: float | None = Query(None, ge=0, le=100),
    min_priority_score: float | None = Query(None, ge=0, le=100),
    has_issues: bool | None = None,
    include_inactive: bool = False,
    window_days: int | None = Query(None, ge=1, le=365),
):
    """The priority table.

    Default ordering is by **priority score**, not SEO score: the platform's core claim is that a
    healthier page with real traffic and conversions outranks a broken page nobody visits.
    """
    window = window_days or settings.priority_metric_window_days

    from ...services.pipeline import cleanup_website_parameter_pages
    cleanup_website_parameter_pages(db, website)

    stmt = select(Page).where(Page.website_id == website.id)
    if not include_inactive:
        stmt = stmt.where(Page.is_active.is_(True))
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(or_(Page.url.ilike(pattern), Page.title.ilike(pattern)))
    if seo_category:
        stmt = stmt.where(Page.seo_category == seo_category.upper())
    if severity:
        stmt = stmt.where(Page.highest_severity == severity.upper())
    if priority_band:
        stmt = stmt.where(Page.priority_band == priority_band.upper())
    if ai_status:
        stmt = stmt.where(Page.ai_status == ai_status.lower())
    if status_code is not None:
        stmt = stmt.where(Page.status_code == status_code)
    if min_seo_score is not None:
        stmt = stmt.where(Page.seo_score >= min_seo_score)
    if max_seo_score is not None:
        stmt = stmt.where(Page.seo_score <= max_seo_score)
    if min_priority_score is not None:
        stmt = stmt.where(Page.priority_score >= min_priority_score)
    if has_issues is not None:
        stmt = stmt.where(Page.issue_count > 0 if has_issues else Page.issue_count == 0)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    descending = order == "desc"
    if sort in METRIC_SORTS:
        ga4, gsc = _metric_subquery(db, website.id, window)
        stmt = stmt.outerjoin(ga4, ga4.c.page_id == Page.id).outerjoin(
            gsc, gsc.c.page_id == Page.id
        )
        column = {
            "users": func.coalesce(ga4.c.users, 0),
            "sessions": func.coalesce(ga4.c.sessions, 0),
            "conversions": func.coalesce(ga4.c.conversions, 0.0),
            "revenue": func.coalesce(ga4.c.revenue, 0.0),
            "clicks": func.coalesce(gsc.c.clicks, 0),
            "impressions": func.coalesce(gsc.c.impressions, 0),
        }[sort]
        stmt = stmt.order_by(column.desc() if descending else column.asc(), Page.id.asc())
    elif sort == "severity":
        stmt = stmt.order_by(
            SEVERITY_ORDER.asc() if descending else SEVERITY_ORDER.desc(), Page.id.asc()
        )
    else:
        ga4, gsc = _metric_subquery(db, website.id, window)
        stmt = stmt.outerjoin(ga4, ga4.c.page_id == Page.id).outerjoin(
            gsc, gsc.c.page_id == Page.id
        )
        column = SORTABLE.get(sort, Page.priority_score)
        ordering = column.desc().nullslast() if descending else column.asc().nullsfirst()
        clicks_col = func.coalesce(gsc.c.clicks, 0)
        impr_col = func.coalesce(gsc.c.impressions, 0)
        users_col = func.coalesce(ga4.c.users, 0)
        conv_col = func.coalesce(ga4.c.conversions, 0.0)
        # Priority is headline number; break ties with real engagement (clicks, impressions, users, conversions), then technical urgency.
        stmt = stmt.order_by(
            ordering,
            clicks_col.desc() if descending else clicks_col.asc(),
            impr_col.desc() if descending else impr_col.asc(),
            users_col.desc() if descending else users_col.asc(),
            conv_col.desc() if descending else conv_col.asc(),
            SEVERITY_ORDER.asc(),
            Page.seo_score.asc().nullsfirst(),
        )

    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    page_ids = [row.id for row in rows]
    metrics = aggregate_page_metrics(db, page_ids, window_days=window)
    top_issues = _top_issues_for(db, page_ids)

    items = []
    for row in rows:
        item = PageListItem.model_validate(row)
        values = metrics.get(row.id, {})
        item.users = values.get("users", 0)
        item.sessions = values.get("sessions", 0)
        item.conversions = values.get("conversions", 0.0)
        item.revenue = values.get("revenue", 0.0)
        item.clicks = values.get("clicks", 0)
        item.impressions = values.get("impressions", 0)
        item.ctr = values.get("ctr")
        item.position = values.get("position")
        item.top_issues = top_issues.get(row.id, [])
        items.append(item)

    return PageEnvelope[PageListItem](total=total, limit=limit, offset=offset, items=items)


def _top_issues_for(db: Session, page_ids: list[int], per_page: int = 3) -> dict[int, list[str]]:
    """The most severe unresolved issue titles per page, for the table's "major issues" column."""
    if not page_ids:
        return {}

    rows = db.execute(
        select(SEOIssue.page_id, SEOIssue.severity, SEOIssue.title)
        .where(SEOIssue.page_id.in_(page_ids), SEOIssue.is_resolved.is_(False))
    ).all()

    grouped: dict[int, list[tuple[int, str]]] = {}
    for page_id, severity, title in rows:
        grouped.setdefault(page_id, []).append((severity_rank(severity), title))

    return {
        page_id: [title for _, title in sorted(entries, key=lambda e: -e[0])[:per_page]]
        for page_id, entries in grouped.items()
    }


@router.get("/pages/{page_id}", response_model=PageDetailResponse)
def get_page(page_id: int, user: CurrentUser, db: DbSession, history_days: int = Query(90, ge=7, le=365)):
    """Everything known about one page: issues, metrics, priority reasoning, history, AI output."""
    page = db.get(Page, page_id)
    if page is None:
        raise NotFoundError(f"Page {page_id} was not found.")
    get_website_for_read(page.website_id, user, db)

    deduped_issues: dict[str, SEOIssue] = {}
    for issue in db.scalars(
        select(SEOIssue)
        .where(SEOIssue.page_id == page.id, SEOIssue.is_resolved.is_(False))
        .order_by(SEOIssue.id.desc())
    ).all():
        if issue.rule_id not in deduped_issues:
            deduped_issues[issue.rule_id] = issue
    issues = sorted(deduped_issues.values(), key=lambda i: -severity_rank(i.severity))

    latest_audit = db.scalar(
        select(SEOAudit).where(SEOAudit.page_id == page.id).order_by(SEOAudit.id.desc()).limit(1)
    )

    window = settings.priority_metric_window_days
    aggregated = aggregate_page_metrics(db, [page.id], window_days=window).get(page.id, {})
    metrics = MetricSummary(window_days=window, **{
        key: value for key, value in aggregated.items() if key in MetricSummary.model_fields
    })

    priority_row = db.scalar(
        select(PriorityScore)
        .where(PriorityScore.page_id == page.id)
        .order_by(PriorityScore.id.desc())
        .limit(1)
    )
    priority = None
    if priority_row is not None:
        priority = PriorityBreakdown(
            score=priority_row.score,
            band=priority_row.band,
            rank=priority_row.rank,
            components={
                "seo_severity": priority_row.seo_severity_component,
                "ga4_activity": priority_row.ga4_activity_component,
                "gsc_search": priority_row.gsc_search_component,
                "semrush_opportunity": priority_row.semrush_opportunity_component,
            },
            weights=priority_row.weights or {},
            breakdown=priority_row.breakdown or {},
            data_sources=priority_row.data_sources or [],
            computed_at=priority_row.computed_at,
        )

    recommendation_row = db.scalar(
        select(AIRecommendation)
        .where(AIRecommendation.page_id == page.id)
        .order_by(AIRecommendation.id.desc())
        .limit(1)
    )
    recommendation: dict[str, Any] | None = None
    if recommendation_row is not None:
        recommendation = {
            "id": recommendation_row.id,
            "provider": recommendation_row.provider,
            "model": recommendation_row.model,
            "status": recommendation_row.status,
            "summary": recommendation_row.summary,
            "search_intent": recommendation_row.search_intent,
            "priority": recommendation_row.priority,
            "confidence": recommendation_row.confidence,
            "expected_impact": recommendation_row.expected_impact,
            "suggested_title": recommendation_row.suggested_title,
            "suggested_meta_description": recommendation_row.suggested_meta_description,
            "payload": recommendation_row.payload,
            "analysed_at": recommendation_row.analysed_at,
        }

    github_changes = [
        {
            "id": event.id,
            "repository": event.repository,
            "branch": event.branch,
            "after_sha": event.after_sha,
            "pusher": event.pusher,
            "commit_messages": (event.commit_messages or [])[:5],
            "changed_files": (event.changed_files or [])[:20],
            "action_taken": event.action_taken,
            "created_at": event.created_at,
        }
        for event in db.scalars(
            select(GitHubEvent)
            .where(
                GitHubEvent.website_id == page.website_id,
                or_(
                    GitHubEvent.affected_urls.is_(None),
                    _affected_url_filter(page),
                ),
            )
            .order_by(GitHubEvent.id.desc())
            .limit(5)
        ).all()
    ]

    return PageDetailResponse(
        page=PageDetail.model_validate(page),
        issues=[
            IssueSummary(
                id=i.id,
                rule_id=i.rule_id,
                check_type=i.check_type,
                category=i.category,
                severity=i.severity,
                title=i.title,
                description=i.description,
                recommendation=i.recommendation,
                evidence=i.evidence,
            )
            for i in issues
        ],
        checks=(latest_audit.checks if latest_audit else []) or [],
        metrics=metrics,
        priority=priority,
        history=[HistoryPoint(**point) for point in page_history(db, page.id, days=history_days)],
        recommendation=recommendation,
        github_changes=github_changes,
    )


def _affected_url_filter(page: Page):
    """Match GitHub events whose affected URL list mentions this page.

    ``affected_urls`` is a JSON array; a portable ``LIKE`` over its text form is used rather than a
    dialect-specific JSON containment operator, since the same query must run on SQLite in tests.
    """
    return func.cast(GitHubEvent.affected_urls, __import__("sqlalchemy").Text).like(
        f'%{page.url}%'
    )


@router.get("/websites/{website_id}/issues")
def list_website_issues(
    website: ReadableWebsite,
    db: DbSession,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    severity: str | None = None,
    rule_id: str | None = None,
):
    """Every unresolved issue on a website, newest audit first — the "what's broken" view."""
    stmt = (
        select(SEOIssue, Page.url, Page.priority_score, Page.seo_score)
        .join(Page, SEOIssue.page_id == Page.id)
        .where(
            Page.website_id == website.id,
            Page.is_active.is_(True),
            SEOIssue.is_resolved.is_(False),
        )
    )
    if severity:
        stmt = stmt.where(SEOIssue.severity == severity.upper())
    if rule_id:
        stmt = stmt.where(SEOIssue.rule_id == rule_id)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(Page.priority_score.desc().nullslast(), SEOIssue.id.asc())
        .limit(limit)
        .offset(offset)
    ).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": issue.id,
                "page_id": issue.page_id,
                "url": url,
                "priority_score": priority_score,
                "seo_score": seo_score,
                "rule_id": issue.rule_id,
                "check_type": issue.check_type,
                "category": issue.category,
                "severity": issue.severity,
                "title": issue.title,
                "description": issue.description,
                "recommendation": issue.recommendation,
            }
            for issue, url, priority_score, seo_score in rows
        ],
    }


@router.get("/websites/{website_id}/issues/summary")
def issue_summary(website: ReadableWebsite, db: DbSession):
    """Issue counts grouped by rule and by severity — drives the website overview cards."""
    by_rule = db.execute(
        select(
            SEOIssue.rule_id,
            SEOIssue.title,
            SEOIssue.severity,
            func.count(SEOIssue.id),
            func.count(func.distinct(SEOIssue.page_id)),
        )
        .join(Page, SEOIssue.page_id == Page.id)
        .where(
            Page.website_id == website.id,
            Page.is_active.is_(True),
            SEOIssue.is_resolved.is_(False),
        )
        .group_by(SEOIssue.rule_id, SEOIssue.title, SEOIssue.severity)
    ).all()

    by_severity: dict[str, int] = {}
    # One row per rule. The same rule can fire at different severities across pages, and reporting
    # those separately would show the same problem twice with partial counts.
    grouped: dict[str, dict[str, Any]] = {}
    for rule_id, title, severity, count, pages in by_rule:
        by_severity[severity] = by_severity.get(severity, 0) + count
        entry = grouped.setdefault(
            rule_id,
            {
                "rule_id": rule_id,
                "title": title,
                "severity": severity,
                "issue_count": 0,
                "page_count": 0,
            },
        )
        entry["issue_count"] += count
        entry["page_count"] += pages
        if severity_rank(severity) > severity_rank(entry["severity"]):
            entry["severity"] = severity

    rules = list(grouped.values())
    rules.sort(key=lambda r: (-severity_rank(r["severity"]), -r["page_count"]))
    return {"by_severity": by_severity, "by_rule": rules}
