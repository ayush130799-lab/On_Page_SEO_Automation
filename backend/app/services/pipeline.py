"""The crawl → audit → score pipeline.

Owns the transition from in-memory crawl output to durable rows, and is the single place that
decides how a page's denormalised snapshot columns are refreshed.

Writes are batched: a 10 000-page site produces ~10 000 pages + ~10 000 audits + ~40 000 issues, so
flushing per page would dominate the run time.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    CrawlMode,
    CrawlRun,
    CrawlTrigger,
    Page,
    RunStatus,
    SEOAudit,
    SEOIssue,
    Severity,
    Website,
)
from ..utils.url_utils import normalize_url, url_hash, url_path
from .crawler import CrawlConfig, CrawlProgress, Crawler
from .crawler.extractor import ExtractedPage
from .seo import PageAuditResult, aggregate_scores, audit_site, resolve_weights

logger = logging.getLogger(__name__)

#: How often (in crawled pages) progress is written back to the crawl_runs row.
PROGRESS_FLUSH_EVERY = 25


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PipelineOutcome:
    crawl_run_id: int
    pages_crawled: int
    pages_audited: int
    issues_created: int
    average_seo_score: float | None
    critical_issues: int
    duration_seconds: float
    truncated: bool = False
    truncation_reason: str | None = None


# ── Crawl run lifecycle ─────────────────────────────────────────────────────


def create_crawl_run(
    db: Session,
    website: Website,
    *,
    trigger: str = CrawlTrigger.MANUAL,
    mode: str = CrawlMode.FULL,
    target_urls: list[str] | None = None,
    triggered_by_id: int | None = None,
    github_event_id: int | None = None,
    max_pages: int | None = None,
) -> CrawlRun:
    """Record a queued crawl run before any work starts, so the UI can show it immediately.

    ``max_pages`` overrides the website's configured limit **for this run only** — a one-off
    smaller crawl must not permanently reconfigure the website.
    """
    run = CrawlRun(
        website_id=website.id,
        status=RunStatus.QUEUED,
        trigger=trigger,
        mode=mode,
        target_urls=target_urls,
        triggered_by_id=triggered_by_id,
        github_event_id=github_event_id,
        stage="queued",
        config_snapshot={
            "max_pages": max_pages or website.max_pages or settings.max_pages,
            "render_mode": website.render_mode,
            "respect_robots_txt": website.respect_robots_txt,
            "include_patterns": website.include_patterns,
            "exclude_patterns": website.exclude_patterns,
        },
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


# ── Page persistence ────────────────────────────────────────────────────────


def _snapshot_page(page: Page, extracted: ExtractedPage, seen_at: datetime) -> None:
    """Copy the latest crawl observation onto the stable page row."""
    page.status_code = extracted.status_code
    page.final_url = extracted.final_url or extracted.url
    page.redirect_chain = extracted.redirect_chain or None
    page.title = extracted.title
    page.meta_description = extracted.meta_description
    page.h1 = extracted.h1
    page.h1_count = extracted.h1_count
    page.h2_count = extracted.h2_count
    page.h3_count = extracted.h3_count
    page.canonical_url = extracted.canonical_url
    page.robots_directive = extracted.meta_robots
    page.x_robots_tag = getattr(extracted, "x_robots_tag", None)
    page.content_type = getattr(extracted, "content_type", None)
    page.lang = extracted.lang
    page.hreflang = extracted.hreflang or None
    page.has_viewport = extracted.has_viewport
    page.has_structured_data = extracted.has_structured_data
    page.structured_data_types = extracted.structured_data_types or None
    page.has_open_graph = extracted.has_open_graph
    page.word_count = extracted.word_count
    page.content = extracted.content or None
    page.content_hash = extracted.content_hash
    page.image_count = extracted.image_count
    page.missing_alt_count = extracted.missing_alt_count
    page.internal_link_count = extracted.internal_link_count
    page.external_link_count = extracted.external_link_count
    page.broken_link_count = extracted.broken_link_count
    page.inbound_internal_links = extracted.inbound_internal_links
    page.was_rendered = extracted.was_rendered
    page.response_time_ms = extracted.response_time_ms
    page.content_bytes = extracted.content_bytes
    page.crawl_error = extracted.crawl_error
    page.crawl_status = "failed" if extracted.crawl_error else "crawled"
    page.last_seen_at = seen_at
    page.last_crawled_at = seen_at
    page.is_active = True


def upsert_pages(
    db: Session, website: Website, extracted_pages: Sequence[ExtractedPage]
) -> dict[str, Page]:
    """Create or refresh the stable Page row for every crawled URL.

    Returns a mapping of ``url_hash`` → ``Page``. Existing pages are matched by hash so that a page
    keeps its identity (and therefore its metric history) across re-crawls.
    """
    seen_at = _now()
    by_hash: dict[str, ExtractedPage] = {}
    for extracted in extracted_pages:
        canonical_url = normalize_url(extracted.final_url or extracted.url)
        digest = url_hash(canonical_url)
        # A redirect can collapse two crawled URLs onto one page; last write wins.
        by_hash[digest] = extracted

    existing = {
        page.url_hash: page
        for page in db.scalars(
            select(Page).where(
                Page.website_id == website.id, Page.url_hash.in_(list(by_hash.keys()))
            )
        )
    }

    result: dict[str, Page] = {}
    for digest, extracted in by_hash.items():
        canonical_url = normalize_url(extracted.final_url or extracted.url)
        page = existing.get(digest)
        if page is None:
            page = Page(
                website_id=website.id,
                url=canonical_url,
                url_hash=digest,
                path=url_path(canonical_url),
                first_seen_at=seen_at,
            )
            db.add(page)
        else:
            page.url = canonical_url
            page.path = url_path(canonical_url)

        _snapshot_page(page, extracted, seen_at)
        result[digest] = page

    db.flush()
    return result


def crawl_reached_the_site(pages: Sequence[ExtractedPage]) -> bool:
    """True when at least one page was actually fetched.

    A run where every request failed means the site was unreachable — DNS, an outage, or the
    crawler being blocked — not a site that lost all its content.
    """
    return any(
        page.status_code and 200 <= page.status_code < 400 and not page.crawl_error
        for page in pages
    )


def _mark_missing_pages_inactive(
    db: Session,
    website: Website,
    seen_hashes: set[str],
    full_crawl: bool,
    *,
    crawl_succeeded: bool = True,
) -> int:
    """Deactivate pages a full crawl no longer found; incremental runs never deactivate."""
    if not full_crawl:
        return 0

    if not seen_hashes or not crawl_succeeded:
        # Deactivating here would empty the dashboard and zero the website summary because of a
        # transient outage, and the next successful crawl would have to rediscover everything.
        logger.warning(
            "Crawl of website %s reached no pages successfully; leaving existing pages active.",
            website.id,
        )
        return 0

    stale = db.scalars(
        select(Page).where(
            Page.website_id == website.id,
            Page.is_active.is_(True),
            Page.url_hash.notin_(seen_hashes),
        )
    ).all()
    for page in stale:
        page.is_active = False
    return len(stale)


# ── Audit persistence ───────────────────────────────────────────────────────


def persist_audits(
    db: Session,
    crawl_run: CrawlRun,
    pages_by_hash: dict[str, Page],
    extracted_pages: Sequence[ExtractedPage],
    audits: Sequence[PageAuditResult],
    weights: dict[str, float],
) -> int:
    """Write SEOAudit + SEOIssue rows and refresh each page's score snapshot."""
    audited_at = _now()
    issues_created = 0
    seen_page_ids: set[int] = set()

    for extracted, audit in zip(extracted_pages, audits):
        digest = url_hash(normalize_url(extracted.final_url or extracted.url))
        page = pages_by_hash.get(digest)
        if page is None or page.id in seen_page_ids:
            continue
        seen_page_ids.add(page.id)

        db.execute(
            update(SEOIssue)
            .where(SEOIssue.page_id == page.id, SEOIssue.is_resolved.is_(False))
            .values(is_resolved=True)
        )

        counts = audit.counts
        seo_audit = SEOAudit(
            crawl_run_id=crawl_run.id,
            page_id=page.id,
            seo_score=audit.seo_score,
            category=audit.category,
            highest_severity=audit.highest_severity,
            issue_count=audit.issue_count,
            critical_count=counts[Severity.CRITICAL],
            high_count=counts[Severity.HIGH],
            medium_count=counts[Severity.MEDIUM],
            low_count=counts[Severity.LOW],
            checks=audit.checks_payload(),
            weights_snapshot=weights,
            status_code=extracted.status_code,
            content_hash=extracted.content_hash,
            audited_at=audited_at,
        )
        db.add(seo_audit)
        db.flush()

        for result in audit.issues:
            db.add(
                SEOIssue(
                    seo_audit_id=seo_audit.id,
                    page_id=page.id,
                    rule_id=result.rule_id,
                    check_type=result.check_type,
                    category=result.category,
                    severity=result.severity or Severity.MEDIUM,
                    title=result.title,
                    description=result.details,
                    recommendation=result.recommendation,
                    evidence=result.evidence,
                )
            )
            issues_created += 1

        page.seo_score = audit.seo_score
        page.seo_category = audit.category
        page.highest_severity = audit.highest_severity
        page.issue_count = audit.issue_count

    db.flush()
    return issues_created


def refresh_website_summary(db: Session, website: Website) -> None:
    """Recompute the denormalised counters the portfolio dashboard reads."""
    active = select(Page).where(Page.website_id == website.id, Page.is_active.is_(True))

    website.total_pages = db.scalar(select(func.count()).select_from(active.subquery())) or 0
    website.average_seo_score = db.scalar(
        select(func.avg(Page.seo_score)).where(
            Page.website_id == website.id, Page.is_active.is_(True), Page.seo_score.isnot(None)
        )
    )
    if website.average_seo_score is not None:
        website.average_seo_score = round(float(website.average_seo_score), 1)

    website.critical_issue_count = (
        db.scalar(
            select(func.count(SEOIssue.id))
            .join(Page, SEOIssue.page_id == Page.id)
            .where(
                Page.website_id == website.id,
                Page.is_active.is_(True),
                SEOIssue.severity == Severity.CRITICAL,
                SEOIssue.is_resolved.is_(False),
            )
        )
        or 0
    )
    website.high_priority_page_count = (
        db.scalar(
            select(func.count(Page.id)).where(
                Page.website_id == website.id,
                Page.is_active.is_(True),
                Page.priority_band.in_(["P0", "P1"]),
            )
        )
        or 0
    )
    website.last_crawled_at = _now()


def cleanup_website_parameter_pages(db: Session, website: Website) -> int:
    """Deactivate duplicate parameter variant Page rows created during older crawls or metric syncs."""
    active_pages = db.scalars(
        select(Page).where(Page.website_id == website.id, Page.is_active.is_(True))
    ).all()

    deactivated = 0
    seen_canonical_hashes: set[str] = set()

    # Sort pages so clean base URLs (without parameters) come first
    sorted_pages = sorted(active_pages, key=lambda p: (1 if "?" in p.url else 0, len(p.url)))

    for page in sorted_pages:
        norm = normalize_url(page.url)
        norm_hash = url_hash(norm)

        if norm_hash in seen_canonical_hashes:
            page.is_active = False
            deactivated += 1
        else:
            seen_canonical_hashes.add(norm_hash)
            if page.url != norm:
                page.url = norm
                page.url_hash = norm_hash
                page.path = url_path(norm)

    if deactivated > 0:
        db.flush()
        active = select(Page).where(Page.website_id == website.id, Page.is_active.is_(True))
        website.total_pages = db.scalar(select(func.count()).select_from(active.subquery())) or 0
        website.average_seo_score = db.scalar(
            select(func.avg(Page.seo_score)).where(
                Page.website_id == website.id, Page.is_active.is_(True), Page.seo_score.isnot(None)
            )
        )
        if website.average_seo_score is not None:
            website.average_seo_score = round(float(website.average_seo_score), 1)
        db.commit()

    return deactivated


# ── Entry point ─────────────────────────────────────────────────────────────


async def run_crawl_pipeline(
    db: Session,
    crawl_run_id: int,
    *,
    session_factory=None,
) -> PipelineOutcome:
    """Execute one crawl run end to end and persist everything it produced."""
    crawl_run = db.get(CrawlRun, crawl_run_id)
    if crawl_run is None:
        raise ValueError(f"Crawl run {crawl_run_id} does not exist.")

    website = db.get(Website, crawl_run.website_id)
    if website is None:
        raise ValueError(f"Website {crawl_run.website_id} does not exist.")

    crawl_run.status = RunStatus.RUNNING
    crawl_run.started_at = _now()
    crawl_run.stage = "discovering"
    db.commit()

    try:
        config = CrawlConfig.for_website(
            website,
            target_urls=crawl_run.target_urls,
            follow_links=crawl_run.mode == CrawlMode.FULL,
            # The run's own snapshot wins, so a one-off limit stays scoped to this run.
            max_pages=(crawl_run.config_snapshot or {}).get("max_pages"),
        )

        last_flush = 0

        async def on_progress(progress: CrawlProgress) -> None:
            nonlocal last_flush
            if progress.pages_crawled - last_flush < PROGRESS_FLUSH_EVERY:
                return
            last_flush = progress.pages_crawled
            crawl_run.urls_discovered = progress.urls_discovered
            crawl_run.pages_queued = progress.pages_queued
            crawl_run.pages_crawled = progress.pages_crawled
            crawl_run.pages_rendered = progress.pages_rendered
            crawl_run.pages_failed = progress.pages_failed
            crawl_run.stage = progress.stage
            # Crawling is the first 60% of a run; auditing and scoring make up the rest.
            crawl_run.progress_percent = round(
                min(60.0, 60.0 * progress.pages_crawled / max(1, progress.urls_discovered)), 1
            )
            db.commit()

        crawl = await Crawler(website.url, config).run(on_progress)

        crawl_run.urls_discovered = crawl.urls_discovered
        crawl_run.pages_crawled = crawl.pages_crawled
        crawl_run.pages_rendered = crawl.pages_rendered
        crawl_run.pages_failed = crawl.pages_failed
        crawl_run.stage = "auditing"
        crawl_run.progress_percent = 60.0
        db.commit()

        weights = resolve_weights(_website_weight_overrides(db, website))
        audits = await asyncio.to_thread(audit_site, crawl.pages, weights)

        crawl_run.stage = "persisting"
        crawl_run.progress_percent = 80.0
        db.commit()

        pages_by_hash = upsert_pages(db, website, crawl.pages)
        issues_created = persist_audits(
            db, crawl_run, pages_by_hash, crawl.pages, audits, weights
        )
        _mark_missing_pages_inactive(
            db,
            website,
            set(pages_by_hash.keys()),
            crawl_run.mode == CrawlMode.FULL,
            crawl_succeeded=crawl_reached_the_site(crawl.pages),
        )

        summary = aggregate_scores(audits)
        crawl_run.pages_analysed = summary["page_count"]
        crawl_run.average_seo_score = summary["average_seo_score"]
        crawl_run.total_issue_count = summary["total_issues"]
        crawl_run.critical_issue_count = summary["critical_issues"]
        crawl_run.status = RunStatus.COMPLETED
        crawl_run.stage = "completed"
        crawl_run.progress_percent = 100.0
        crawl_run.completed_at = _now()
        crawl_run.duration_seconds = crawl.duration_seconds
        if crawl.truncated:
            crawl_run.error = crawl.truncation_reason

        # Automatically compute priority scores for pages so dashboard is fully populated immediately
        try:
            from .priority.engine import score_website
            score_website(db, website)
        except Exception as score_exc:
            logger.warning(
                "Priority scoring after crawl failed for website %s: %s", website.id, score_exc
            )

        refresh_website_summary(db, website)
        db.commit()

        logger.info(
            "Crawl run %s finished: %d pages, avg score %s, %d issues (%d critical) in %.1fs",
            crawl_run.id,
            summary["page_count"],
            summary["average_seo_score"],
            summary["total_issues"],
            summary["critical_issues"],
            crawl.duration_seconds,
        )

        return PipelineOutcome(
            crawl_run_id=crawl_run.id,
            pages_crawled=crawl.pages_crawled,
            pages_audited=summary["page_count"],
            issues_created=issues_created,
            average_seo_score=summary["average_seo_score"],
            critical_issues=summary["critical_issues"],
            duration_seconds=crawl.duration_seconds,
            truncated=crawl.truncated,
            truncation_reason=crawl.truncation_reason,
        )

    except Exception as exc:
        logger.exception("Crawl run %s failed: %s", crawl_run_id, exc)
        db.rollback()
        failed = db.get(CrawlRun, crawl_run_id)
        if failed is not None:
            failed.status = RunStatus.FAILED
            failed.stage = "failed"
            failed.error = f"{type(exc).__name__}: {exc}"[:2000]
            failed.completed_at = _now()
            db.commit()
        raise


def _website_weight_overrides(db: Session, website: Website) -> dict[str, float] | None:
    """Per-website SEO weight overrides from the settings table, if any."""
    from ..models import Setting

    row = (
        db.query(Setting)
        .filter(Setting.website_id == website.id, Setting.key == "seo_weights")
        .first()
    )
    if row and isinstance(row.value, dict):
        return {k: float(v) for k, v in row.value.items()}
    return None


def resolve_incremental_urls(website: Website, paths: Sequence[str]) -> list[str]:
    """Turn site-relative paths (from a GitHub push) into absolute crawl targets."""
    base = website.url.rstrip("/")
    urls: list[str] = []
    for path in paths:
        if path.startswith("http"):
            urls.append(normalize_url(path))
        else:
            urls.append(normalize_url(f"{base}/{path.lstrip('/')}"))
    return list(dict.fromkeys(urls))
