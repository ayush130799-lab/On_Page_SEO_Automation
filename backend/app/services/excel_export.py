"""SEO audit data as a downloadable .xlsx workbook.

Every value here is read from data the platform already computed and stored — the crawler's
``Page`` snapshot, ``SEOIssue``/``AIRecommendation`` rows, the intent/keyword engine's output, and
the GA4/GSC/Semrush metric tables. Nothing is recalculated with different logic than the dashboard
uses, and nothing is invented when a value does not exist (that cell is left blank, never guessed).

Five sheets: SEO Summary, Page Overview, SEO Issues, AI Recommendations, Keywords. Column sets are
documented on each ``_build_*_sheet`` function.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AIRecommendation,
    KeywordOpportunity,
    Page,
    PageIntentProfile,
    SemrushMetric,
    SEOIssue,
    Website,
)
from .metrics import aggregate_page_metrics, window_start

# ── Styling ──────────────────────────────────────────────────────────────────

_HEADER_FILL = PatternFill("solid", fgColor="1E293B")  # slate-800
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_WRAP = Alignment(wrap_text=True, vertical="top")
_TOP = Alignment(vertical="top")

_SEVERITY_FILLS = {
    "CRITICAL": PatternFill("solid", fgColor="FECACA"),
    "HIGH": PatternFill("solid", fgColor="FED7AA"),
    "MEDIUM": PatternFill("solid", fgColor="FEF9C3"),
    "LOW": PatternFill("solid", fgColor="E2E8F0"),
}
_BAND_FILLS = {
    "P0": PatternFill("solid", fgColor="FECACA"),
    "P1": PatternFill("solid", fgColor="FED7AA"),
    "P2": PatternFill("solid", fgColor="FEF9C3"),
    "P3": PatternFill("solid", fgColor="E2E8F0"),
}
#: critical/high/medium/low (AIRecommendation / AiFinding priority vocabulary — lowercase).
_PRIORITY_FILLS = {
    "critical": _SEVERITY_FILLS["CRITICAL"],
    "high": _SEVERITY_FILLS["HIGH"],
    "medium": _SEVERITY_FILLS["MEDIUM"],
    "low": _SEVERITY_FILLS["LOW"],
}

#: SQLite/Postgres-safe chunk size for large IN(...) clauses (mirrors the same limit already
#: respected elsewhere in the codebase, e.g. services/intent/analyser.py).
_CHUNK = 500


def _chunks(values: Sequence[int]) -> list[Sequence[int]]:
    return [values[i : i + _CHUNK] for i in range(0, len(values), _CHUNK)]


def _set_header(ws: Worksheet, headers: list[str], widths: list[float]) -> None:
    """Bold white-on-slate header row, frozen, with an autofilter added once data exists."""
    for col, title in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"
    for col, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[1].height = 20


def _finish_table(ws: Worksheet, header_count: int) -> None:
    """Autofilter over the full written range. Called once every row is appended."""
    if ws.max_row < 1:
        return
    last_col = get_column_letter(header_count)
    ws.auto_filter.ref = f"A1:{last_col}{max(ws.max_row, 1)}"


def _fmt_pct(value: float | None, digits: int = 1) -> str | None:
    if value is None:
        return None
    return f"{value * 100:.{digits}f}%"


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _compact_evidence(evidence: dict[str, Any] | None) -> str:
    """Render a rule's free-form evidence dict as one readable line.

    Evidence shape is rule-specific (see services/seo/rules/*.py) — there is no consistent
    "current value" field across rules, so this is a faithful rendering of whatever was actually
    recorded rather than a guess at a schema that doesn't exist.
    """
    if not evidence:
        return ""
    parts = []
    for key, value in evidence.items():
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value[:5])
        parts.append(f"{key}: {value}")
    return "; ".join(parts)[:500]


# ── Bulk fetchers (one query per table, never one query per page) ───────────


def _bulk_unresolved_issues(db: Session, page_ids: list[int]) -> dict[int, list[SEOIssue]]:
    result: dict[int, list[SEOIssue]] = {}
    for chunk in _chunks(page_ids):
        for issue in db.scalars(
            select(SEOIssue)
            .where(SEOIssue.page_id.in_(chunk), SEOIssue.is_resolved.is_(False))
            .order_by(SEOIssue.page_id, SEOIssue.id)
        ):
            result.setdefault(issue.page_id, []).append(issue)
    return result


def _bulk_latest_recommendations(db: Session, page_ids: list[int]) -> dict[int, AIRecommendation]:
    """Newest *completed* AI recommendation per page — the one the dashboard shows."""
    result: dict[int, AIRecommendation] = {}
    for chunk in _chunks(page_ids):
        for rec in db.scalars(
            select(AIRecommendation)
            .where(AIRecommendation.page_id.in_(chunk), AIRecommendation.status == "completed")
            .order_by(AIRecommendation.page_id, AIRecommendation.id.desc())
        ):
            result.setdefault(rec.page_id, rec)
    return result


def _bulk_intent_profiles(db: Session, page_ids: list[int]) -> dict[int, PageIntentProfile]:
    result: dict[int, PageIntentProfile] = {}
    for chunk in _chunks(page_ids):
        for profile in db.scalars(
            select(PageIntentProfile).where(PageIntentProfile.page_id.in_(chunk))
        ):
            result[profile.page_id] = profile
    return result


def _bulk_primary_keyword_opportunities(
    db: Session, page_ids: list[int]
) -> dict[int, KeywordOpportunity]:
    """The primary-tier keyword row per page, for its tracked current ranking."""
    result: dict[int, KeywordOpportunity] = {}
    for chunk in _chunks(page_ids):
        for row in db.scalars(
            select(KeywordOpportunity).where(
                KeywordOpportunity.page_id.in_(chunk),
                KeywordOpportunity.keyword_tier == "primary",
            )
        ):
            result.setdefault(row.page_id, row)
    return result


def _bulk_latest_semrush(db: Session, page_ids: list[int]) -> dict[int, SemrushMetric]:
    """Newest Semrush snapshot per page — carries per-keyword volume/difficulty when connected."""
    result: dict[int, SemrushMetric] = {}
    for chunk in _chunks(page_ids):
        for row in db.scalars(
            select(SemrushMetric)
            .where(SemrushMetric.page_id.in_(chunk))
            .order_by(SemrushMetric.page_id, SemrushMetric.date.desc())
        ):
            result.setdefault(row.page_id, row)
    return result


def _semrush_keyword_lookup(
    semrush: SemrushMetric | None, keyword: str | None
) -> dict[str, Any] | None:
    if not semrush or not semrush.keywords or not keyword:
        return None
    lowered = keyword.strip().lower()
    for entry in semrush.keywords:
        if str(entry.get("keyword", "")).strip().lower() == lowered:
            return entry
    return None


# ── Sheet builders ───────────────────────────────────────────────────────────


def _build_summary_sheet(
    wb: Workbook,
    website: Website,
    pages: list[Page],
    issues_by_severity: dict[str, int],
) -> None:
    """Website / Audit Date / Total Pages / Average SEO Score / High-Medium-Low Issues /
    Total Issues / Pages Requiring Action."""
    ws = wb.active
    ws.title = "SEO Summary"
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 40

    total_issues = sum(issues_by_severity.values())
    pages_requiring_action = sum(1 for p in pages if (p.issue_count or 0) > 0)

    rows = [
        ("Website", website.domain or website.url),
        ("Audit Date", _fmt_dt(website.last_crawled_at)),
        ("Total Pages", len(pages)),
        # Denormalised on Website by the priority/audit pipeline — reused rather than
        # recomputed so this sheet can never disagree with the dashboard.
        ("Average SEO Score", website.average_seo_score),
        ("High Issues", issues_by_severity.get("HIGH", 0)),
        ("Medium Issues", issues_by_severity.get("MEDIUM", 0)),
        ("Low Issues", issues_by_severity.get("LOW", 0)),
        ("Total Issues", total_issues),
        ("Pages Requiring Action", pages_requiring_action),
    ]
    for row_index, (label, value) in enumerate(rows, start=1):
        label_cell = ws.cell(row=row_index, column=1, value=label)
        label_cell.font = Font(bold=True)
        ws.cell(row=row_index, column=2, value=value)


def _build_page_overview_sheet(
    ws: Worksheet,
    pages: list[Page],
    metrics: dict[int, dict[str, Any]],
) -> None:
    """URL / SEO Score / Priority / Priority Band / Users / Engagement Rate /
    Engagement Time/User / Clicks / Conversions / Conversion Rate / Traffic Potential /
    Lead Potential / Content Optimization Score."""
    headers = [
        "URL", "SEO Score", "Priority", "Priority Band", "Users", "Engagement Rate",
        "Engagement Time/User (s)", "Clicks", "Conversions", "Conversion Rate",
        "Traffic Potential", "Lead Potential", "Content Optimization Score",
    ]
    widths = [55, 11, 10, 12, 10, 14, 18, 10, 12, 14, 14, 12, 20]
    _set_header(ws, headers, widths)

    for row_index, page in enumerate(pages, start=2):
        m = metrics.get(page.id, {})
        sessions = m.get("sessions") or 0
        conversions = m.get("conversions") or 0.0
        conversion_rate = (conversions / sessions) if sessions else None

        values = [
            page.url,
            page.seo_score,
            page.priority_score,
            page.priority_band,
            m.get("users", 0),
            _fmt_pct(m.get("engagement_rate")),
            round(m["average_engagement_time"], 1) if m.get("average_engagement_time") else None,
            m.get("clicks", 0),
            conversions,
            _fmt_pct(conversion_rate),
            page.traffic_potential_score,
            page.lead_potential_score,
            None,  # filled below once the caller supplies the latest AIRecommendation
        ]
        for col, value in enumerate(values, start=1):
            ws.cell(row=row_index, column=col, value=value)

        band_fill = _BAND_FILLS.get(page.priority_band or "")
        if band_fill:
            ws.cell(row=row_index, column=4).fill = band_fill

    _finish_table(ws, len(headers))


def _build_seo_issues_sheet(
    ws: Worksheet,
    pages: list[Page],
    issues_by_page: dict[int, list[SEOIssue]],
) -> None:
    """URL / SEO Score / Issue / Category / Severity / Current Value / Recommended Value /
    Recommendation."""
    headers = [
        "URL", "SEO Score", "Issue", "Category", "Severity", "Current Value",
        "Recommended Value", "Recommendation",
    ]
    widths = [50, 11, 35, 16, 11, 45, 20, 60]
    _set_header(ws, headers, widths)

    row_index = 2
    for page in pages:
        for issue in issues_by_page.get(page.id, []):
            values = [
                page.url,
                page.seo_score,
                issue.title,
                issue.category,
                issue.severity,
                _compact_evidence(issue.evidence),
                None,  # not a field this system tracks — see the export's Limitations note
                issue.recommendation or issue.description,
            ]
            for col, value in enumerate(values, start=1):
                cell = ws.cell(row=row_index, column=col, value=value)
                if col in (6, 8):
                    cell.alignment = _WRAP
                else:
                    cell.alignment = _TOP
            fill = _SEVERITY_FILLS.get(issue.severity)
            if fill:
                ws.cell(row=row_index, column=5).fill = fill
            row_index += 1

    _finish_table(ws, len(headers))


def _build_ai_recommendations_sheet(
    ws: Worksheet,
    pages: list[Page],
    recs_by_page: dict[int, AIRecommendation],
) -> None:
    """URL / Issue / Severity / AI Recommendation / Recommended Action / Expected Impact /
    Priority / Search Intent — one row per finding in the page's latest AI recommendation."""
    headers = [
        "URL", "Issue", "Severity", "AI Recommendation", "Recommended Action",
        "Expected Impact", "Priority", "Search Intent",
    ]
    widths = [50, 30, 11, 55, 45, 45, 11, 16]
    _set_header(ws, headers, widths)

    row_index = 2
    for page in pages:
        rec = recs_by_page.get(page.id)
        if rec is None:
            continue
        findings = (rec.payload or {}).get("findings") or []
        for finding in findings:
            values = [
                page.url,
                finding.get("issue"),
                finding.get("priority"),  # per-finding severity/priority
                finding.get("explanation"),
                finding.get("recommended_fix"),
                finding.get("expected_impact") or rec.expected_impact,
                rec.priority,  # the recommendation's overall priority
                rec.search_intent,
            ]
            for col, value in enumerate(values, start=1):
                cell = ws.cell(row=row_index, column=col, value=value)
                cell.alignment = _WRAP if col in (4, 5, 6) else _TOP
            fill = _PRIORITY_FILLS.get((finding.get("priority") or "").lower())
            if fill:
                ws.cell(row=row_index, column=3).fill = fill
            row_index += 1

    _finish_table(ws, len(headers))


def _build_keywords_sheet(
    ws: Worksheet,
    pages: list[Page],
    profiles_by_page: dict[int, PageIntentProfile],
    primary_kw_by_page: dict[int, KeywordOpportunity],
    semrush_by_page: dict[int, SemrushMetric],
) -> None:
    """URL / Primary Keyword / Secondary Keywords / Search Volume / Keyword Difficulty /
    Current Ranking / Traffic Potential / Search Intent."""
    headers = [
        "URL", "Primary Keyword", "Secondary Keywords", "Search Volume", "Keyword Difficulty",
        "Current Ranking", "Traffic Potential", "Search Intent",
    ]
    widths = [50, 28, 45, 14, 15, 15, 14, 16]
    _set_header(ws, headers, widths)

    row_index = 2
    for page in pages:
        profile = profiles_by_page.get(page.id)
        if profile is None or not (profile.primary_keywords or profile.secondary_keywords):
            continue

        primary = (profile.primary_keywords or [None])[0]
        secondary = "; ".join(profile.secondary_keywords or [])
        semrush_entry = _semrush_keyword_lookup(semrush_by_page.get(page.id), primary)
        primary_kw_row = primary_kw_by_page.get(page.id)

        current_ranking = None
        if primary_kw_row is not None and primary_kw_row.current_position is not None:
            current_ranking = primary_kw_row.current_position
        elif semrush_entry is not None:
            current_ranking = semrush_entry.get("position")

        values = [
            page.url,
            primary,
            secondary,
            semrush_entry.get("volume") if semrush_entry else None,
            semrush_entry.get("difficulty") if semrush_entry else None,
            current_ranking,
            page.traffic_potential_score,
            profile.detected_intent,
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row_index, column=col, value=value)
            if col == 3:
                cell.alignment = _WRAP
        row_index += 1

    _finish_table(ws, len(headers))


# ── Entry point ──────────────────────────────────────────────────────────────


@dataclass
class ExcelReport:
    content: bytes
    filename: str


#: Same label vocabulary as the frontend's ``AiBadge``/``IntentBadge`` components, so the export
#: reads exactly like the dashboard cell rather than a raw internal status string.
_AI_STATUS_LABELS = {
    "completed": "Analysed", "cached": "Cached", "skipped": "Skipped",
    "failed": "Failed", "queued": "Queued", "pending": "Pending",
}
_INTENT_LABELS = {
    "informational": "Informational", "navigational": "Navigational", "commercial": "Commercial",
    "transactional": "Transactional", "local": "Local",
}


def _slugify_issue_title(title: str) -> str:
    """"Meta description" -> "Meta-Description" (for the "SEO-Issue-<slug>.xlsx" filename)."""
    words = re.findall(r"[A-Za-z0-9]+", title)
    return "-".join(word.capitalize() for word in words) or "Issue"


def generate_issue_pages_excel(
    *,
    issue_title: str,
    pages: list[Page],
    metrics: dict[int, dict[str, Any]],
    top_issues: dict[int, list[str]],
    intent_map: dict[int, dict[str, Any]],
) -> ExcelReport:
    """One-sheet export of an issue-detail page's "Affected pages" table.

    Every value is exactly what that table already shows (same ``Page`` rows, same
    ``aggregate_page_metrics``/``_top_issues_for``/``_intent_for`` outputs the route computed for
    the page) — nothing recalculated, and nothing from an individual page's own detail view
    (no findings, recommendations, meta/H1/canonical/keyword detail).
    """
    headers = [
        "URL", "Priority", "SEO", "Traffic", "Leads", "Severity", "Users", "Clicks",
        "Impressions", "CTR", "Conversions", "Issues", "Major Issues", "Intent", "AI",
    ]
    widths = [55, 10, 9, 10, 9, 11, 10, 10, 12, 9, 12, 9, 45, 16, 12]

    wb = Workbook()
    ws = wb.active
    ws.title = "Affected Pages"
    _set_header(ws, headers, widths)

    for row_index, page in enumerate(pages, start=2):
        m = metrics.get(page.id, {})
        intent = (intent_map.get(page.id) or {}).get("intent")
        values = [
            page.url,
            page.priority_score,
            page.seo_score,
            page.traffic_potential_score,
            page.lead_potential_score,
            page.highest_severity,
            m.get("users", 0),
            m.get("clicks", 0),
            m.get("impressions", 0),
            _fmt_pct(m.get("ctr"), 2),
            m.get("conversions", 0.0),
            page.issue_count,
            " · ".join(top_issues.get(page.id, [])) or "—",
            _INTENT_LABELS.get(intent, intent) if intent else "—",
            _AI_STATUS_LABELS.get(page.ai_status, page.ai_status),
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row_index, column=col, value=value)
            cell.alignment = _WRAP if col == 13 else _TOP
        fill = _SEVERITY_FILLS.get(page.highest_severity or "")
        if fill:
            ws.cell(row=row_index, column=6).fill = fill

    _finish_table(ws, len(headers))

    buffer = io.BytesIO()
    wb.save(buffer)
    filename = f"SEO-Issue-{_slugify_issue_title(issue_title)}.xlsx"
    return ExcelReport(content=buffer.getvalue(), filename=filename)


def generate_seo_excel_report(
    db: Session, website: Website, *, window_days: int | None = None
) -> ExcelReport:
    """Build the full multi-sheet workbook for one website's current audit state.

    Every figure here is read from ``pages``/``seo_issues``/``ai_recommendations``/the intent
    engine/GA4-GSC-Semrush metric tables — the same rows the dashboard reads — so a score shown
    here is, by construction, the score the dashboard shows.
    """
    window = window_days or settings.priority_metric_window_days

    pages = list(
        db.scalars(
            select(Page)
            .where(Page.website_id == website.id, Page.is_active.is_(True))
            .order_by(Page.priority_score.desc().nullslast(), Page.id.asc())
        )
    )
    page_ids = [p.id for p in pages]

    metrics = aggregate_page_metrics(db, page_ids, window_days=window)
    issues_by_page = _bulk_unresolved_issues(db, page_ids)
    recs_by_page = _bulk_latest_recommendations(db, page_ids)
    profiles_by_page = _bulk_intent_profiles(db, page_ids)
    primary_kw_by_page = _bulk_primary_keyword_opportunities(db, page_ids)
    semrush_by_page = _bulk_latest_semrush(db, page_ids)

    issues_by_severity: dict[str, int] = {}
    for page_issues in issues_by_page.values():
        for issue in page_issues:
            issues_by_severity[issue.severity] = issues_by_severity.get(issue.severity, 0) + 1

    wb = Workbook()
    _build_summary_sheet(wb, website, pages, issues_by_severity)

    overview_ws = wb.create_sheet("Page Overview")
    _build_page_overview_sheet(overview_ws, pages, metrics)
    # Content Optimization Score comes from the AI recommendation, fetched after the sheet's
    # main pass so the summary/overview build stays independent of whether AI has run yet.
    for row_index, page in enumerate(pages, start=2):
        rec = recs_by_page.get(page.id)
        if rec is not None and rec.content_quality_score is not None:
            overview_ws.cell(row=row_index, column=13, value=rec.content_quality_score)

    _build_seo_issues_sheet(wb.create_sheet("SEO Issues"), pages, issues_by_page)
    _build_ai_recommendations_sheet(wb.create_sheet("AI Recommendations"), pages, recs_by_page)
    _build_keywords_sheet(
        wb.create_sheet("Keywords"), pages, profiles_by_page, primary_kw_by_page, semrush_by_page
    )

    buffer = io.BytesIO()
    wb.save(buffer)

    safe_domain = (website.domain or website.url or "website").replace("/", "_").replace(":", "_")
    filename = f"SEO_Audit_Report_{safe_domain}_{date.today().isoformat()}.xlsx"
    return ExcelReport(content=buffer.getvalue(), filename=filename)
