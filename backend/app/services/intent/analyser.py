"""Intent analysis orchestrator (Phase 2).

Runs the full Phase 2 pipeline for a website:
  1. For each page that has a completed AIRecommendation, extract the intent and
     keyword data already present in the LLM payload (zero extra AI cost).
  2. For pages without an AIRecommendation, run the tiered classifier
     (rules → statistical) to produce an intent classification.
  3. Run mismatch detection using GSC query data.
  4. Build the keyword tier matrix.
  5. Persist / upsert a ``PageIntentProfile`` and its ``KeywordOpportunity`` rows.
  6. Inject a mismatch finding into the existing AIRecommendation payload when a
     P0/P1 mismatch is detected (no duplicate recommendation rows created).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import AIRecommendation, GSCMetric, Page, SemrushMetric, Website
from ...models.intent import KeywordOpportunity, PageIntentProfile
from .classifier import (
    IntentClassificationResult,
    classify_by_rules,
    classify_page_intent,
    classify_page_type,
)
from .keyword_engine import KeywordTierResult, build_keyword_tiers
from .mismatch import MismatchResult, detect_intent_mismatch

logger = logging.getLogger(__name__)


@dataclass
class _AnalysisContext:
    """Everything the per-page pass needs, loaded once for the whole website."""

    gsc: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    semrush: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    recommendations: dict[int, Any] = field(default_factory=dict)
    profiles: dict[int, Any] = field(default_factory=dict)
    business_relevance: dict[int, float] = field(default_factory=dict)


@dataclass
class IntentAnalysisOutcome:
    website_id: int
    considered: int = 0
    classified: int = 0
    mismatches_found: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# GSC / Semrush data fetchers (reuse existing metric rows)
# ---------------------------------------------------------------------------

#: Rows are fetched for every page at once. The previous implementation issued three queries
#: per page inside the loop, which is 30,000 round trips on the 10,000-page site §12.3 requires
#: the architecture to handle.
_CHUNK = 500


def _chunks(items: list[int]):
    for start in range(0, len(items), _CHUNK):
        yield items[start:start + _CHUNK]


def _gsc_queries_bulk(db: Session, page_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Newest GSC query array per page."""
    result: dict[int, list[dict[str, Any]]] = {}
    for chunk in _chunks(page_ids):
        rows = db.scalars(
            select(GSCMetric)
            .where(GSCMetric.page_id.in_(chunk), GSCMetric.queries.isnot(None))
            .order_by(GSCMetric.page_id, GSCMetric.date.desc())
        ).all()
        for row in rows:
            result.setdefault(row.page_id, row.queries or [])
    return result


def _semrush_keywords_bulk(db: Session, page_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Newest Semrush keyword array per page."""
    result: dict[int, list[dict[str, Any]]] = {}
    for chunk in _chunks(page_ids):
        rows = db.scalars(
            select(SemrushMetric)
            .where(SemrushMetric.page_id.in_(chunk))
            .order_by(SemrushMetric.page_id, SemrushMetric.date.desc())
        ).all()
        for row in rows:
            result.setdefault(row.page_id, row.keywords or [])
    return result


def _recommendations_bulk(db: Session, page_ids: list[int]) -> dict[int, AIRecommendation]:
    """Newest completed AI recommendation per page."""
    result: dict[int, AIRecommendation] = {}
    for chunk in _chunks(page_ids):
        rows = db.scalars(
            select(AIRecommendation)
            .where(
                AIRecommendation.page_id.in_(chunk),
                AIRecommendation.status == "completed",
            )
            .order_by(AIRecommendation.page_id, AIRecommendation.id.desc())
        ).all()
        for row in rows:
            result.setdefault(row.page_id, row)
    return result


def _profiles_bulk(db: Session, page_ids: list[int]) -> dict[int, PageIntentProfile]:
    result: dict[int, PageIntentProfile] = {}
    for chunk in _chunks(page_ids):
        for profile in db.scalars(
            select(PageIntentProfile).where(PageIntentProfile.page_id.in_(chunk))
        ):
            result[profile.page_id] = profile
    return result


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

def _upsert_intent_profile(
    db: Session,
    page: Page,
    website: Website,
    classification: IntentClassificationResult,
    mismatch: MismatchResult,
    kw_result: KeywordTierResult,
    crawl_run_id: int | None,
    page_type: str,
) -> PageIntentProfile:
    """Insert or update the intent profile for this page."""
    existing = db.scalar(
        select(PageIntentProfile).where(PageIntentProfile.page_id == page.id)
    )

    profile = existing or PageIntentProfile(page_id=page.id, website_id=website.id)

    profile.crawl_run_id = crawl_run_id
    profile.detected_intent = classification.intent
    profile.intent_confidence = round(classification.confidence, 3)
    profile.detection_method = classification.method
    profile.business_intent = mismatch.business_intent
    profile.page_type = page_type
    profile.mismatch_evidence = mismatch.evidence_source
    # Recorded so the next run can tell whether the page has changed since this classification.
    profile.content_hash = page.content_hash
    profile.intent_mismatch = mismatch.has_mismatch
    profile.mismatch_severity = mismatch.severity
    profile.mismatch_explanation = mismatch.explanation if mismatch.has_mismatch else None
    profile.primary_keywords = kw_result.primary or None
    profile.secondary_keywords = kw_result.secondary or None
    profile.long_tail_keywords = kw_result.long_tail or None
    profile.semantic_entities = kw_result.semantic or None
    profile.question_keywords = kw_result.question or None
    profile.keyword_opportunity_score = kw_result.page_keyword_opportunity_score
    profile.analysed_at = _now()

    if not existing:
        db.add(profile)

    db.flush()  # get profile.id for keyword rows

    # Replace keyword opportunity rows
    for kw_row in list(profile.keyword_opportunities):
        db.delete(kw_row)
    db.flush()

    for entry in kw_result.keywords:
        db.add(
            KeywordOpportunity(
                intent_profile_id=profile.id,
                page_id=page.id,
                website_id=website.id,
                keyword=entry.keyword,
                keyword_tier=entry.tier,
                demand_score=entry.demand_score,
                ranking_opportunity_score=entry.ranking_opportunity_score,
                intent_match_score=entry.intent_match_score,
                business_relevance_score=entry.business_relevance_score,
                content_relevance_score=entry.content_relevance_score,
                competition_opportunity_score=entry.competition_opportunity_score,
                keyword_opportunity_score=entry.keyword_opportunity_score,
                current_position=entry.current_position,
                current_impressions=entry.current_impressions,
                source=entry.source,
                extra_data=entry.metadata or None,
            )
        )

    return profile


def _inject_mismatch_finding(
    db: Session,
    rec: AIRecommendation,
    mismatch: MismatchResult,
) -> None:
    """Add a mismatch finding to the existing AIRecommendation payload.

    We do not create a new row — we augment the existing Phase 1 recommendation
    so the dashboard surfaces the mismatch without double-counting the page.
    """
    if not mismatch.has_mismatch:
        return

    payload = dict(rec.payload or {})
    findings = list(payload.get("findings", []))

    # Avoid duplicate injection on re-runs
    if any(f.get("issue") == "search_intent_mismatch" for f in findings):
        # Update existing mismatch finding instead
        for f in findings:
            if f.get("issue") == "search_intent_mismatch":
                f["explanation"] = mismatch.explanation
                f["priority"] = mismatch.severity.lower().replace("p0", "critical").replace("p1", "high").replace("p2", "medium").replace("p3", "low") if mismatch.severity else "high"
        payload["findings"] = findings
    else:
        priority_map = {"P0": "critical", "P1": "high", "P2": "medium", "P3": "low"}
        findings.insert(0, {
            "issue": "search_intent_mismatch",
            "explanation": mismatch.explanation,
            "why_it_matters": (
                f"This page's business intent ({mismatch.business_intent}) conflicts with "
                f"the dominant query intent reaching it ({mismatch.query_intent}). "
                f"This misalignment suppresses conversions and can cause Google to "
                f"re-classify the page, harming rankings."
            ),
            "recommended_fix": (
                "Re-align <title>, <h1>, and opening content to match the page's "
                "business intent. Update internal anchor text pointing to this page "
                "to use high-intent anchor text. Add a prominent CTA if transactional."
            ),
            "priority": priority_map.get(mismatch.severity or "P1", "high"),
            "effort": "medium",
            "confidence": round(mismatch.query_intent_confidence, 2),
            "expected_impact": (
                "Aligning page intent with content and internal linking typically lifts "
                "conversion rates and improves topical relevance signals to search engines."
            ),
        })
        payload["findings"] = findings

    # Also store the mismatch flag at the top level of the payload
    payload["intent_mismatch"] = True
    payload["mismatch_severity"] = mismatch.severity
    payload["mismatch_explanation"] = mismatch.explanation

    rec.payload = payload
    logger.info(
        "Injected intent mismatch finding (%s) into recommendation %s for page %s",
        mismatch.severity, rec.id, rec.page_id,
    )


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def analyse_intent_for_website(
    db: Session,
    website: Website,
    *,
    crawl_run_id: int | None = None,
    page_ids: Sequence[int] | None = None,
    force: bool = False,
) -> IntentAnalysisOutcome:
    """Run the Phase 2 pipeline for all active pages on a website.

    This is a **synchronous** function — it is called from the post-AI stage of
    the crawl pipeline and is not expected to run concurrently with itself.
    """
    outcome = IntentAnalysisOutcome(website_id=website.id)

    # Select pages to process
    if page_ids:
        pages = list(
            db.scalars(
                select(Page).where(
                    Page.id.in_(list(page_ids)), Page.website_id == website.id
                )
            )
        )
    else:
        pages = list(
            db.scalars(
                select(Page).where(
                    Page.website_id == website.id, Page.is_active.is_(True)
                )
            )
        )

    outcome.considered = len(pages)
    if not pages:
        return outcome

    ids = [p.id for p in pages]
    context = _AnalysisContext(
        gsc=_gsc_queries_bulk(db, ids),
        semrush=_semrush_keywords_bulk(db, ids),
        recommendations=_recommendations_bulk(db, ids),
        profiles=_profiles_bulk(db, ids),
        business_relevance=_business_relevance_by_page(db, website, ids),
    )

    for page in pages:
        try:
            _process_page(
                db, website, page,
                crawl_run_id=crawl_run_id, force=force, outcome=outcome, context=context,
            )
        except Exception as exc:
            outcome.failed += 1
            msg = f"{page.url}: {type(exc).__name__}: {str(exc)[:200]}"
            outcome.errors.append(msg)
            logger.exception("Intent analysis failed for %s", page.url)

    db.commit()
    logger.info(
        "Intent analysis for website %s: %d classified, %d mismatches, %d failed.",
        website.id, outcome.classified, outcome.mismatches_found, outcome.failed,
    )
    return outcome


def _process_page(
    db: Session,
    website: Website,
    page: Page,
    *,
    crawl_run_id: int | None,
    force: bool,
    outcome: IntentAnalysisOutcome,
    context: "_AnalysisContext",
) -> None:
    """Run the full Phase 2 pipeline for a single page."""
    existing = context.profiles.get(page.id)

    # Re-analyse when the page has changed. The previous check skipped any page that already had
    # a profile *at all*, which meant a page rewritten from a blog post into a booking form kept
    # its original intent for ever.
    if existing is not None and not force:
        unchanged = (
            page.content_hash is not None
            and existing.content_hash == page.content_hash
        )
        if unchanged:
            return

    # ── Fetch raw data (all pre-loaded in bulk) ──────────────────────────────
    gsc_queries = context.gsc.get(page.id, [])
    semrush_kws = context.semrush.get(page.id, [])
    rec = context.recommendations.get(page.id)
    ai_payload = rec.payload if rec else {}

    ai_detected_intent = ai_payload.get("search_intent") if ai_payload else None
    ai_intent_confidence = ai_payload.get("intent_confidence") if ai_payload else None
    ai_keyword_tiers = ai_payload.get("keyword_tiers", []) if ai_payload else []

    # The crawler stores only the H1 text and H2/H3 *counts* on the persisted Page row — the
    # full per-heading text array lives on the transient ExtractedPage during a crawl and is
    # never written to the database. H1 is passed separately below (as `h1=`), so there is no
    # additional heading text to supply here without double-weighting it.
    heading_texts: list[str] = []

    # ── Level 1+2+3 classification ───────────────────────────────────────────
    classification = classify_page_intent(
        url=page.url,
        structured_data_types=page.structured_data_types,
        robots_directive=page.robots_directive,
        gsc_queries=gsc_queries,
        ai_detected_intent=ai_detected_intent,
        ai_intent_confidence=ai_intent_confidence,
    )

    # Business intent = Level-1 rule result (what the URL is designed for)
    rule_result = classify_by_rules(page.url, page.structured_data_types, page.robots_directive)
    business_intent = rule_result.intent if rule_result else classification.intent

    # ── §6.1 second axis: commercial / informational / hybrid ────────────────
    page_type, _page_type_signals = classify_page_type(
        classification.intent,
        title=page.title,
        h1=page.h1,
        content=page.content,
        structured_data_types=page.structured_data_types,
    )

    # ── Mismatch detection ───────────────────────────────────────────────────
    mismatch = detect_intent_mismatch(
        url=page.url,
        business_intent=business_intent,
        gsc_queries=gsc_queries,
        title=page.title,
        h1=page.h1,
        headings=heading_texts,
    )

    # ── Keyword tier building ────────────────────────────────────────────────
    kw_result = build_keyword_tiers(
        page_url=page.url,
        detected_intent=classification.intent,
        gsc_queries=gsc_queries,
        semrush_keywords=semrush_kws,
        ai_keyword_tiers=ai_keyword_tiers,
        page_text={
            "title": page.title,
            "h1": page.h1,
            "headings": " ".join(heading_texts),
            "content": page.content,
        },
        business_relevance=context.business_relevance.get(page.id, 0.50),
    )

    # ── Persist ──────────────────────────────────────────────────────────────
    _upsert_intent_profile(
        db, page, website, classification, mismatch, kw_result, crawl_run_id, page_type
    )

    if mismatch.has_mismatch and rec is not None:
        _inject_mismatch_finding(db, rec, mismatch)

    outcome.classified += 1
    if mismatch.has_mismatch:
        outcome.mismatches_found += 1


def _business_relevance_by_page(
    db: Session, website: Website, page_ids: list[int]
) -> dict[int, float]:
    """Per-page business relevance, 0-1, from the impact engine's definition.

    Computed once here and handed to the keyword engine so keyword scoring and impact scoring
    agree on how commercially important a page is, rather than each inventing its own answer.
    """
    from ...config import settings
    from ..impact.scoring import business_relevance
    from ..metrics import aggregate_page_metrics

    metrics = aggregate_page_metrics(
        db, page_ids, window_days=settings.priority_metric_window_days
    )
    site_revenue = sum(float(m.get("revenue") or 0) for m in metrics.values())
    site_conversions = sum(float(m.get("conversions") or 0) for m in metrics.values())
    patterns = tuple(settings.business_value_paths or ())

    paths = dict(
        db.execute(select(Page.id, Page.path).where(Page.id.in_(page_ids))).all()
    )

    result: dict[int, float] = {}
    for page_id in page_ids:
        score, _ = business_relevance(
            metrics.get(page_id, {}),
            site_revenue=site_revenue,
            site_conversions=site_conversions,
            path=paths.get(page_id),
            high_value_patterns=patterns,
        )
        result[page_id] = score
    return result
