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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import (
    AIRecommendation,
    AIStatus,
    GSCMetric,
    Page,
    PageIntentProfile,
    RecommendationScore,
    SEOAudit,
    SEOIssue,
    SemrushMetric,
    Severity,
    Website,
)
from ..metrics import aggregate_page_metrics
from .prompts import REPAIR_PROMPT, SYSTEM_PROMPT, build_user_prompt
from .providers import (
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    get_active_providers,
    get_provider,
)
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
    #: Which cost tier this page was routed to — rules | statistical | ai | deep_ai.
    tier: str | None = None
    #: The impact score the routing decision was made on, so the choice can be audited.
    impact_score: float | None = None


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
    """Choose which pages are worth an LLM call, using the tiered cost model (§12.4).

    Pages are ranked by their best computed impact score where one exists, because the question
    the budget must answer is "where is the most unclaimed opportunity", not "what is most
    broken": a page at 40/100 SEO health with no traffic is a worse use of a model call than one
    at 80 bleeding click-through on 50,000 impressions.

    Impact scores only exist once :mod:`app.services.impact.engine` has run. Before that — a
    site's first crawl, or a caller that skipped scoring — this falls back to the priority score
    and the SEO-score threshold, rather than treating "not yet scored" as "zero impact" and
    silently selecting nothing.

    Whether AI is enabled at all is deliberately *not* considered here. This function answers
    which pages would be worth the call; the caller decides whether to make it, so the selection
    preview endpoint still explains its reasoning on a deployment with AI switched off.
    """
    from .tiering import Tier, route_page

    limit = max_pages or settings.ai_max_pages
    threshold = (
        score_threshold if score_threshold is not None else settings.ai_seo_score_threshold
    )

    pages = db.scalars(
        select(Page).where(Page.website_id == website.id, Page.is_active.is_(True))
    ).all()
    if not pages:
        return [], []

    page_ids = [p.id for p in pages]

    best_impact: dict[int, float] = {
        page_id: float(best or 0.0)
        for page_id, best in db.execute(
            select(
                RecommendationScore.page_id,
                func.max(RecommendationScore.overall_priority),
            )
            .where(RecommendationScore.page_id.in_(page_ids))
            .group_by(RecommendationScore.page_id)
        ).all()
    }

    mismatched: set[int] = {
        row[0]
        for row in db.execute(
            select(PageIntentProfile.page_id).where(
                PageIntentProfile.page_id.in_(page_ids),
                PageIntentProfile.intent_mismatch.is_(True),
            )
        ).all()
    }

    # Rank by impact where the engine has produced it; otherwise by the priority score, which is
    # the best available proxy and what this gate ranked on before impact scoring existed.
    have_impact = bool(best_impact)
    ranked = sorted(
        pages,
        key=lambda p: (
            best_impact.get(p.id, 0.0) if have_impact else (p.priority_score or 0.0),
            -(p.seo_score if p.seo_score is not None else 0.0),
        ),
        reverse=True,
    )

    selected: list[Page] = []
    decisions: list[SelectionDecision] = []

    for rank, page in enumerate(ranked, start=1):
        impact = best_impact.get(page.id)
        decision = SelectionDecision(
            page_id=page.id, url=page.url, selected=False, reason="",
            rank=rank, impact_score=impact,
        )

        if force:
            decision.selected = True
            decision.reason = "explicitly requested"
            decision.tier = Tier.L3_AI.value
        elif page.seo_score is None:
            # Checked before the issue count: a page that has never been audited has no issues
            # *recorded*, which is a different statement from having none.
            decision.reason = "not audited yet"
            decision.tier = Tier.L1_RULES.value
        elif (page.issue_count or 0) == 0 and page.id not in mismatched:
            decision.reason = "no outstanding issues"
            decision.tier = Tier.L1_RULES.value
        elif len(selected) >= limit:
            decision.reason = f"outside the top {limit} pages by priority"
            decision.tier = Tier.L2_STATISTICAL.value
        else:
            # Inside the budget and worth considering — let the tier gate decide how far.
            routed = route_page(
                rank=rank,
                impact_score=impact,
                has_critical_issue=page.highest_severity == Severity.CRITICAL,
                has_intent_mismatch=page.id in mismatched,
                issue_count=page.issue_count or 0,
                max_ai_pages=limit,
                # Enablement is the caller's decision, not this gate's.
                ai_enabled=True,
            )
            decision.tier = routed.tier.value

            if impact is None:
                # No impact score yet: fall back to the SEO-score gate.
                if page.highest_severity == Severity.CRITICAL:
                    decision.selected = True
                    decision.reason = "carries a CRITICAL issue"
                    decision.tier = Tier.L3_AI.value
                elif page.seo_score > threshold:
                    decision.reason = f"healthy (SEO {page.seo_score} > {threshold})"
                    decision.tier = Tier.L2_STATISTICAL.value
                else:
                    decision.selected = True
                    decision.reason = (
                        f"SEO {page.seo_score} is at or below the {threshold} threshold"
                    )
                    decision.tier = Tier.L3_AI.value
            else:
                decision.selected = routed.uses_ai
                decision.reason = routed.reason
                # An explicit SEO-score cut-off still applies on top of impact routing.
                if decision.selected and page.seo_score > threshold:
                    decision.selected = False
                    decision.reason = f"healthy (SEO {page.seo_score} > {threshold})"
                    decision.tier = Tier.L2_STATISTICAL.value

        if decision.selected:
            selected.append(page)
        decisions.append(decision)

    logger.info(
        "AI selection for website %s: %d of %d pages selected (limit %d, ranked by %s).",
        website.id, len(selected), len(pages), limit,
        "impact score" if have_impact else "priority score",
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

    search_impact = recommendation.search_impact_score
    user_impact = recommendation.user_activity_score
    impact_score = recommendation.impact_score
    reason = recommendation.reason

    # Fallback to mathematical estimation if missing
    if search_impact is None or user_impact is None or impact_score is None:
        metrics = aggregate_page_metrics(
            db, [page.id], window_days=settings.priority_metric_window_days
        ).get(page.id, {})
        from ..priority.components import (
            compute_search_impact_score,
            compute_user_activity_score,
        )

        if search_impact is None:
            search_impact = compute_search_impact_score(metrics)
        if user_impact is None:
            user_impact = compute_user_activity_score(metrics)
        if impact_score is None:
            conf = recommendation.confidence if recommendation.confidence is not None else 0.75
            impact_score = round(
                min(100.0, (0.55 * search_impact + 0.45 * user_impact) * (0.5 + 0.5 * conf)), 1
            )
        if not reason:
            reason = (
                f"Search impact potential: {search_impact}/100, "
                f"User activity impact: {user_impact}/100."
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
        search_impact_score=search_impact,
        user_activity_score=user_impact,
        impact_score=impact_score,
        reason=reason,
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

    active_providers = get_active_providers()
    single_p = get_provider()
    if single_p is not None and not any(p.name == single_p.name for p in active_providers):
        active_providers.append(single_p)

    if not active_providers:
        outcome.errors.append(
            f"No API key is configured for any LLM provider (attempted '{settings.llm_provider}')."
        )
        return outcome

    primary = get_provider() or active_providers[0]
    outcome.provider = " + ".join(dict.fromkeys([p.name for p in active_providers]))
    outcome.model = " / ".join(dict.fromkeys([p.model for p in active_providers]))
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

    # Scale concurrency with the number of available provider API keys.
    concurrency = settings.ai_concurrency * max(1, len(active_providers))
    semaphore = asyncio.Semaphore(concurrency)
    contexts = {page.id: _page_context(db, page, window) for page in to_analyse}

    async def _one(idx: int, page: Page):
        # Round-robin primary provider assignment with fallback across all available providers.
        assigned = active_providers[idx % len(active_providers)]
        fallbacks = [p for p in active_providers if p.name != assigned.name]
        try_order = [assigned] + fallbacks

        last_exc = None
        for prov in try_order:
            try:
                rec, resp = await analyse_page(
                    prov, page, contexts[page.id], window_days=window, semaphore=semaphore
                )
                return page, rec, resp, None
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "AI provider %s failed for %s (%s); attempting fallback provider.",
                    prov.name, page.url, exc,
                )
        return page, None, None, last_exc

    results = await asyncio.gather(*[_one(idx, page) for idx, page in enumerate(to_analyse)])

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
                    provider=primary.name,
                    model=primary.model,
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
