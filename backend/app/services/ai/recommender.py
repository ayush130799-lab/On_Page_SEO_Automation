"""The AI recommendation engine.

The pipeline the specification mandates is enforced here, in order:

    all pages → SEO audit → SEO score → metric enrichment → priority score → rank
              → AI analysis for the pages that earn it

Pages are selected by **priority rank**, not by SEO score, because the point of the platform is
that a healthy page carrying real business value can deserve attention before a broken page nobody
visits. Healthy, high-scoring pages are skipped outright, and an unchanged page reuses its previous
recommendation rather than paying for the same answer twice.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import (
    AIRecommendation,
    AIStatus,
    GSCMetric,
    Page,
    SEOAudit,
    SEOIssue,
    SemrushMetric,
    Severity,
    Website,
)
from ..metrics import aggregate_page_metrics
from .prompts import REPAIR_PROMPT, SYSTEM_PROMPT, build_user_prompt
from .providers import LLMError, LLMProvider, LLMRateLimitError, get_provider
from .schema import PageRecommendation

logger = logging.getLogger(__name__)


@dataclass
class SelectionDecision:
    """Why one page was or was not sent to the model."""

    page_id: int
    url: str
    selected: bool
    reason: str
    rank: int | None = None


@dataclass
class AnalysisOutcome:
    website_id: int
    considered: int = 0
    analysed: int = 0
    skipped: int = 0
    cached: int = 0
    failed: int = 0
    provider: str | None = None
    model: str | None = None
    decisions: list[SelectionDecision] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Selection ───────────────────────────────────────────────────────────────


def select_pages(
    db: Session,
    website: Website,
    *,
    max_pages: int | None = None,
    score_threshold: float | None = None,
    force: bool = False,
) -> tuple[list[Page], list[SelectionDecision]]:
    """Choose which pages are worth an LLM call.

    A page qualifies when it is inside the top ``max_pages`` by priority **and** it is either
    scoring below the threshold or carrying a CRITICAL issue. The CRITICAL override exists because
    a single `noindex` can sit on a page that otherwise scores 95 — precisely the case a score
    cut-off alone would miss.
    """
    limit = max_pages or settings.ai_max_pages
    threshold = (
        score_threshold if score_threshold is not None else settings.ai_seo_score_threshold
    )

    pages = db.scalars(
        select(Page)
        .where(Page.website_id == website.id, Page.is_active.is_(True))
        .order_by(
            Page.priority_score.desc().nullslast(),
            Page.seo_score.asc().nullsfirst(),
            Page.id.asc(),
        )
    ).all()

    selected: list[Page] = []
    decisions: list[SelectionDecision] = []

    for rank, page in enumerate(pages, start=1):
        decision = SelectionDecision(
            page_id=page.id, url=page.url, selected=False, reason="", rank=rank
        )

        if len(selected) >= limit:
            decision.reason = f"outside the top {limit} pages by priority"
        elif force:
            decision.selected = True
            decision.reason = "explicitly requested"
        elif page.highest_severity == Severity.CRITICAL:
            decision.selected = True
            decision.reason = "carries a CRITICAL issue"
        elif page.seo_score is None:
            decision.reason = "not audited yet"
        elif page.seo_score > threshold:
            decision.reason = f"healthy (SEO {page.seo_score} > {threshold})"
        elif page.issue_count == 0:
            decision.reason = "no outstanding issues"
        else:
            decision.selected = True
            decision.reason = f"SEO {page.seo_score} is below the {threshold} threshold"

        if decision.selected:
            selected.append(page)
        decisions.append(decision)

    logger.info(
        "AI selection for website %s: %d of %d pages selected.",
        website.id, len(selected), len(pages),
    )
    return selected, decisions


def cached_recommendation(db: Session, page: Page) -> AIRecommendation | None:
    """A previous recommendation still valid for this page's current content."""
    if not settings.ai_reuse_when_unchanged or not page.content_hash:
        return None

    return db.scalar(
        select(AIRecommendation)
        .where(
            AIRecommendation.page_id == page.id,
            AIRecommendation.content_hash == page.content_hash,
            AIRecommendation.status == "completed",
        )
        .order_by(AIRecommendation.id.desc())
        .limit(1)
    )


# ── Analysis ────────────────────────────────────────────────────────────────


def _page_context(db: Session, page: Page, window_days: int) -> dict[str, Any]:
    """Everything the prompt needs about one page."""
    issues = db.scalars(
        select(SEOIssue)
        .where(SEOIssue.page_id == page.id, SEOIssue.is_resolved.is_(False))
        .order_by(SEOIssue.id.asc())
    ).all()

    latest_gsc = db.scalar(
        select(GSCMetric)
        .where(GSCMetric.page_id == page.id, GSCMetric.queries.isnot(None))
        .order_by(GSCMetric.date.desc())
        .limit(1)
    )
    latest_semrush = db.scalar(
        select(SemrushMetric)
        .where(SemrushMetric.page_id == page.id)
        .order_by(SemrushMetric.date.desc())
        .limit(1)
    )

    return {
        "issues": [
            {
                "rule_id": issue.rule_id,
                "severity": issue.severity,
                "description": issue.description,
                "evidence": issue.evidence,
            }
            for issue in issues
        ],
        "queries": latest_gsc.queries if latest_gsc else None,
        "keywords": latest_semrush.keywords if latest_semrush else None,
        "metrics": aggregate_page_metrics(db, [page.id], window_days=window_days).get(page.id, {}),
    }


async def analyse_page(
    provider: LLMProvider,
    page: Page,
    context: dict[str, Any],
    *,
    window_days: int,
    semaphore: asyncio.Semaphore | None = None,
) -> tuple[PageRecommendation, Any]:
    """Run one page through the model and validate the response.

    A schema-invalid response gets exactly one repair attempt with the validation errors fed back;
    beyond that the page is recorded as failed rather than retried indefinitely.
    """
    user_prompt = build_user_prompt(
        page,
        context["issues"],
        context["metrics"],
        priority_score=page.priority_score,
        priority_band=page.priority_band,
        queries=context["queries"],
        keywords=context["keywords"],
        window_days=window_days,
    )

    async def _call(prompt: str, system: str = SYSTEM_PROMPT):
        for attempt in range(1, settings.ai_max_retries + 1):
            try:
                return await provider.complete(system, prompt)
            except LLMRateLimitError:
                if attempt == settings.ai_max_retries:
                    raise
                delay = 2.5 * attempt
                logger.info(
                    "Rate limited on %s; retrying in %.1fs (attempt %d/%d).",
                    page.url, delay, attempt, settings.ai_max_retries,
                )
                await asyncio.sleep(delay)
        raise LLMError("Exhausted retries against the model provider.")

    async def _run():
        response = await _call(user_prompt)
        try:
            return PageRecommendation.model_validate(response.json()), response
        except (PydanticValidationError, LLMError) as exc:
            logger.info("Invalid model response for %s; attempting one repair.", page.url)
            repair = await _call(
                f"{user_prompt}\n\n{REPAIR_PROMPT.format(errors=str(exc)[:1500])}"
            )
            return PageRecommendation.model_validate(repair.json()), repair

    if semaphore is not None:
        async with semaphore:
            return await _run()
    return await _run()


def persist_recommendation(
    db: Session,
    website: Website,
    page: Page,
    recommendation: PageRecommendation,
    response: Any,
    *,
    crawl_run_id: int | None = None,
) -> AIRecommendation:
    """Store a validated recommendation and update the page's AI status."""
    latest_audit = db.scalar(
        select(SEOAudit.id).where(SEOAudit.page_id == page.id).order_by(SEOAudit.id.desc()).limit(1)
    )

    row = AIRecommendation(
        website_id=website.id,
        page_id=page.id,
        seo_audit_id=latest_audit,
        crawl_run_id=crawl_run_id,
        provider=response.provider,
        model=response.model,
        status="completed",
        summary=recommendation.summary,
        search_intent=recommendation.search_intent,
        priority=recommendation.priority,
        confidence=recommendation.confidence,
        expected_impact=recommendation.expected_impact,
        content_quality_score=recommendation.content_quality_score,
        topic_coverage_score=recommendation.topic_coverage_score,
        suggested_title=recommendation.suggested_title,
        suggested_meta_description=recommendation.suggested_meta_description,
        payload=recommendation.model_dump(),
        content_hash=page.content_hash,
        seo_score_at_analysis=page.seo_score,
        priority_score_at_analysis=page.priority_score,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        latency_ms=response.latency_ms,
        analysed_at=_now(),
    )
    db.add(row)

    page.ai_status = AIStatus.COMPLETED
    page.ai_analysed_at = row.analysed_at
    db.commit()
    db.refresh(row)
    return row


async def analyse_website(
    db: Session,
    website: Website,
    *,
    max_pages: int | None = None,
    score_threshold: float | None = None,
    page_ids: Sequence[int] | None = None,
    force: bool = False,
    crawl_run_id: int | None = None,
) -> AnalysisOutcome:
    """Run the AI stage for a website."""
    outcome = AnalysisOutcome(website_id=website.id)

    if not settings.ai_enabled:
        outcome.errors.append("AI analysis is disabled (AI_ENABLED=false).")
        return outcome

    provider = get_provider()
    if provider is None:
        outcome.errors.append(
            f"No API key is configured for the '{settings.llm_provider}' provider."
        )
        return outcome

    outcome.provider = provider.name
    outcome.model = provider.model
    window = settings.priority_metric_window_days

    if page_ids:
        selected = list(
            db.scalars(
                select(Page).where(Page.id.in_(list(page_ids)), Page.website_id == website.id)
            )
        )
        decisions = [
            SelectionDecision(p.id, p.url, True, "explicitly requested") for p in selected
        ]
    else:
        selected, decisions = select_pages(
            db, website, max_pages=max_pages, score_threshold=score_threshold, force=force
        )

    outcome.decisions = decisions
    outcome.considered = len(decisions)

    # Pages the gate rejected are marked skipped so the dashboard can explain the blank cell.
    skipped_ids = [d.page_id for d in decisions if not d.selected]
    if skipped_ids:
        for page in db.scalars(select(Page).where(Page.id.in_(skipped_ids))):
            if page.ai_status in (AIStatus.PENDING, AIStatus.QUEUED):
                page.ai_status = AIStatus.SKIPPED
        db.commit()
        outcome.skipped = len(skipped_ids)

    if not selected:
        return outcome

    # Reuse unchanged pages before spending anything.
    to_analyse: list[Page] = []
    for page in selected:
        cached = cached_recommendation(db, page)
        if cached is not None and not force:
            page.ai_status = AIStatus.CACHED
            page.ai_analysed_at = cached.analysed_at
            outcome.cached += 1
        else:
            page.ai_status = AIStatus.QUEUED
            to_analyse.append(page)
    db.commit()

    if not to_analyse:
        return outcome

    semaphore = asyncio.Semaphore(settings.ai_concurrency)
    contexts = {page.id: _page_context(db, page, window) for page in to_analyse}

    async def _one(page: Page):
        try:
            recommendation, response = await analyse_page(
                provider, page, contexts[page.id], window_days=window, semaphore=semaphore
            )
            return page, recommendation, response, None
        except Exception as exc:
            return page, None, None, exc

    results = await asyncio.gather(*[_one(page) for page in to_analyse])

    for page, recommendation, response, error in results:
        if error is not None or recommendation is None:
            page.ai_status = AIStatus.FAILED
            message = f"{type(error).__name__}: {error}" if error else "unknown failure"
            outcome.failed += 1
            outcome.errors.append(f"{page.url}: {message[:200]}")
            db.add(
                AIRecommendation(
                    website_id=website.id,
                    page_id=page.id,
                    crawl_run_id=crawl_run_id,
                    provider=provider.name,
                    model=provider.model,
                    status="failed",
                    error=message[:1000],
                    content_hash=page.content_hash,
                    analysed_at=_now(),
                )
            )
            db.commit()
            logger.warning("AI analysis failed for %s: %s", page.url, message)
            continue

        persist_recommendation(
            db, website, page, recommendation, response, crawl_run_id=crawl_run_id
        )
        outcome.analysed += 1

    logger.info(
        "AI analysis for website %s: %d analysed, %d cached, %d skipped, %d failed (%s/%s).",
        website.id, outcome.analysed, outcome.cached, outcome.skipped, outcome.failed,
        outcome.provider, outcome.model,
    )
    return outcome
