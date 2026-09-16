"""Keyword cannibalization detection (Phase 2, roadmap §5's remaining item).

Two or more pages targeting the same primary/secondary keyword compete against each other in the
SERP: Google usually shows only one of them for a given query, and which one it picks can change
crawl to crawl, capping both pages' practical ranking below what a single consolidated page could
achieve. This differs from the intent *mismatch* check in :mod:`mismatch` — that compares one page
against its own traffic; this compares pages against each other, which is why it is computed from
the site-wide keyword catalog (the same rows behind ``GET /websites/{id}/keywords``) rather than
during the per-page analysis pass.

Only the ``primary`` and ``secondary`` tiers are checked. ``long_tail``, ``semantic`` and
``question`` keywords are expected to recur naturally across a site's content (many pages
legitimately answer the same sub-question), and flagging every shared long-tail phrase would bury
the handful of real collisions in noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Page
from ...models.intent import KeywordOpportunity

#: Tiers a page is actually *trying* to rank for. Cannibalization only matters here.
_CANNIBALIZATION_TIERS = frozenset({"primary", "secondary"})

_SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


@dataclass
class CannibalizationPage:
    """One page competing for the shared keyword."""

    page_id: int
    url: str
    keyword_opportunity_score: float | None
    current_position: float | None
    current_impressions: int | None
    source: str | None


@dataclass
class CannibalizationGroup:
    """Two or more pages on the same site targeting the same keyword."""

    keyword: str
    tier: str
    severity: str  # P0 | P1 | P2 | P3
    pages: list[CannibalizationPage] = field(default_factory=list)
    recommended_canonical_page_id: int | None = None
    recommended_canonical_url: str | None = None
    explanation: str = ""


def _severity(pages: list[CannibalizationPage]) -> str:
    """Rank how urgent this collision is from real ranking signals, not invented ones.

    P0 — both pages already rank in the top 10: Google is actively splitting authority between
         them today, which is the highest-value leakage.
    P1 — both pages rank somewhere, just not both in the top 10: real competition, lower stakes.
    P2 — only one page has a confirmed position: a duplicate target, not yet a proven competitor.
    P3 — neither page ranks yet: an authoring collision worth fixing before it becomes a real one.
    """
    ranked = [p.current_position for p in pages if p.current_position is not None]
    if len(ranked) >= 2:
        top_two = sorted(ranked)[:2]
        return "P0" if all(pos <= 10 for pos in top_two) else "P1"
    if len(ranked) == 1:
        return "P2"
    return "P3"


def _canonical(pages: list[CannibalizationPage]) -> CannibalizationPage:
    """Pick the page that should own this keyword going forward.

    Highest keyword opportunity score wins; ties are broken by the better (lower) current
    position, since that page already carries whatever ranking signal exists for the term.
    """

    def sort_key(p: CannibalizationPage) -> tuple[float, float]:
        score = p.keyword_opportunity_score if p.keyword_opportunity_score is not None else -1.0
        position_rank = -(p.current_position if p.current_position is not None else 9999.0)
        return (score, position_rank)

    return max(pages, key=sort_key)


def _explain(
    keyword: str, tier: str, severity: str, pages: list[CannibalizationPage], canonical: CannibalizationPage
) -> str:
    others = [p for p in pages if p.page_id != canonical.page_id]
    other_desc = ", ".join(o.url for o in others)

    if severity == "P0":
        detail = (
            "Both pages currently rank in the top 10 for this term, which means Google is "
            "actively splitting authority between them and can swap which one it shows on any "
            "given crawl."
        )
    elif severity == "P1":
        detail = (
            "Both pages currently rank for this term, diluting the ranking signal that would "
            "otherwise concentrate on a single page."
        )
    elif severity == "P2":
        visible = next(p for p in pages if p.current_position is not None)
        detail = (
            f"{visible.url} already ranks for this term; the other page targets the same "
            "keyword without yet showing up in search, so it is a duplicate target rather than "
            "a proven competitor — fix it before it starts competing for real."
        )
    else:
        detail = (
            "Neither page currently ranks for this term, but both are written to target it as a "
            f"{tier} keyword — worth resolving before either one starts ranking and the two "
            "begin competing."
        )

    return (
        f'"{keyword}" is targeted as a {tier} keyword by {len(pages)} pages on this site '
        f"({canonical.url} and {other_desc}). {detail} Recommended fix: consolidate on "
        f"{canonical.url} as the canonical target — update its content to fully cover the term, "
        "and either 301-redirect, canonicalise, or re-target the competing page(s) to a "
        "different keyword."
    )


def _rows_for_website(
    db: Session, website_id: int, page_ids: list[int] | None = None
) -> list[tuple[KeywordOpportunity, str]]:
    stmt = (
        select(KeywordOpportunity, Page.url)
        .join(Page, KeywordOpportunity.page_id == Page.id)
        .where(
            KeywordOpportunity.website_id == website_id,
            KeywordOpportunity.keyword_tier.in_(_CANNIBALIZATION_TIERS),
        )
    )
    rows = list(db.execute(stmt).all())
    if page_ids is None:
        return rows

    wanted = set(page_ids)
    # Keep every row for a keyword touched by any wanted page, so the *other* pages sharing that
    # keyword are still visible in the group — filtering rows down to only the wanted pages first
    # would make every group look like it has just one page, and cannibalization would vanish.
    keywords_touched = {(kw.keyword, kw.keyword_tier) for kw, _ in rows if kw.page_id in wanted}
    return [(kw, url) for kw, url in rows if (kw.keyword, kw.keyword_tier) in keywords_touched]


def detect_cannibalization(
    db: Session,
    website_id: int,
    *,
    page_ids: list[int] | None = None,
    tier: str | None = None,
    severity: str | None = None,
) -> list[CannibalizationGroup]:
    """Find keywords targeted by 2+ distinct pages on a website.

    Args:
        db: Active session.
        website_id: The site to check.
        page_ids: When given, only return groups that involve at least one of these pages (every
            page in a matching group is still included — see :func:`_rows_for_website`). Used to
            scope the site-wide check down to a single page's view.
        tier: Restrict to one tier (``primary`` or ``secondary``). Both are checked by default.
        severity: Restrict to one severity (``P0``-``P3``).

    Returns:
        Groups sorted most severe first, then by the canonical page's opportunity score
        descending.
    """
    rows = _rows_for_website(db, website_id, page_ids)

    grouped: dict[tuple[str, str], list[tuple[KeywordOpportunity, str]]] = {}
    for kw, url in rows:
        if tier and kw.keyword_tier != tier:
            continue
        grouped.setdefault((kw.keyword, kw.keyword_tier), []).append((kw, url))

    groups: list[CannibalizationGroup] = []
    for (keyword, kw_tier), entries in grouped.items():
        distinct_pages = {kw.page_id for kw, _ in entries}
        if len(distinct_pages) < 2:
            continue  # only one page targets this keyword — nothing to cannibalise

        # One row per page: the upsert in analyser.py already prevents a page from having two
        # rows for the same keyword, but pick the best-scoring row per page defensively rather
        # than ever double-counting a page as two "competitors" against itself.
        best_per_page: dict[int, tuple[KeywordOpportunity, str]] = {}
        for kw, url in entries:
            current = best_per_page.get(kw.page_id)
            if current is None or (kw.keyword_opportunity_score or 0) > (
                current[0].keyword_opportunity_score or 0
            ):
                best_per_page[kw.page_id] = (kw, url)

        pages = [
            CannibalizationPage(
                page_id=kw.page_id,
                url=url,
                keyword_opportunity_score=kw.keyword_opportunity_score,
                current_position=kw.current_position,
                current_impressions=kw.current_impressions,
                source=kw.source,
            )
            for kw, url in best_per_page.values()
        ]
        pages.sort(key=lambda p: (p.keyword_opportunity_score or 0), reverse=True)

        group_severity = _severity(pages)
        if severity and group_severity != severity:
            continue

        canonical = _canonical(pages)
        groups.append(
            CannibalizationGroup(
                keyword=keyword,
                tier=kw_tier,
                severity=group_severity,
                pages=pages,
                recommended_canonical_page_id=canonical.page_id,
                recommended_canonical_url=canonical.url,
                explanation=_explain(keyword, kw_tier, group_severity, pages, canonical),
            )
        )

    def _group_sort_key(g: CannibalizationGroup) -> tuple[int, float]:
        best_score = max((p.keyword_opportunity_score or 0.0) for p in g.pages)
        return (_SEVERITY_ORDER.get(g.severity, 3), -best_score)

    groups.sort(key=_group_sort_key)
    return groups
