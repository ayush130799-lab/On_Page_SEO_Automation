"""The SEO rule registry: individual rules, scoring, bands and site-wide duplication."""

from __future__ import annotations

import pytest

from app.models.enums import Severity
from app.services.crawler.extractor import ExtractedPage
from app.services.seo import (
    annotate_site,
    audit_page,
    audit_site,
    calculate_score,
    determine_category,
    determine_highest_severity,
    determine_priority_band,
    registry,
    resolve_weights,
    rule_catalogue,
)


def make_page(**overrides) -> ExtractedPage:
    """A healthy page; override individual fields to exercise one rule at a time."""
    defaults = dict(
        url="https://example.com/guides/running-shoes",
        final_url="https://example.com/guides/running-shoes",
        status_code=200,
        title="How to Choose Running Shoes for Trail and Road",
        meta_description=(
            "A practical guide to choosing running shoes: cushioning, drop, fit and "
            "when to replace them for road and trail running."
        ),
        meta_robots="index, follow",
        canonical_url="https://example.com/guides/running-shoes",
        lang="en",
        has_viewport=True,
        h1="How to Choose Running Shoes",
        h1_count=1,
        h2_count=4,
        h3_count=6,
        content="Choosing running shoes well " * 120,
        word_count=480,
        content_hash="hash-unique-1",
        image_count=4,
        missing_alt_count=0,
        images_without_dimensions=0,
        internal_links=[f"https://example.com/p{i}" for i in range(8)],
        external_links=["https://runnersworld.com/x"],
        has_structured_data=True,
        structured_data_types=["Article"],
        has_open_graph=True,
        inbound_internal_links=5,
    )
    defaults.update(overrides)
    return ExtractedPage(**defaults)


def issue_ids(result) -> set[str]:
    return {r.rule_id for r in result.issues}


def result_for(result, rule_id: str):
    return next(r for r in result.results if r.rule_id == rule_id)


# ── Registry ────────────────────────────────────────────────────────────────


def test_registry_is_populated_and_unique():
    catalogue = rule_catalogue()
    assert len(catalogue) >= 20
    ids = [r["id"] for r in catalogue]
    assert len(ids) == len(set(ids))
    assert all(r["weight"] > 0 for r in catalogue)


def test_every_rule_declares_a_fix_hint():
    assert all(r["fix_hint"] for r in rule_catalogue())


def test_a_healthy_page_scores_highly_with_no_issues():
    result = audit_page(make_page())
    assert result.issue_count == 0, issue_ids(result)
    assert result.seo_score > 95
    assert result.category == "LOW ISSUES"
    assert result.highest_severity == Severity.NONE
    assert result.priority_band == "P3"


def test_a_rule_that_raises_does_not_break_the_audit():
    """Failure isolation: a buggy rule degrades to a pass rather than losing the whole page."""
    broken = registry.get("title")
    original = broken.func
    broken.func = lambda page: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        result = audit_page(make_page())
        assert result_for(result, "title").status == "pass"
    finally:
        broken.func = original


# ── Individual rules ────────────────────────────────────────────────────────


def test_missing_metadata_and_headings_are_detected():
    result = audit_page(
        make_page(
            title=None,
            meta_description=None,
            h1=None,
            h1_count=0,
            h2_count=0,
            h3_count=0,
            canonical_url=None,
            content="tiny",
            word_count=1,
            image_count=2,
            missing_alt_count=2,
            internal_links=[],
            has_structured_data=False,
            has_open_graph=False,
            has_viewport=False,
            lang=None,
        )
    )
    assert {
        "title", "meta_description", "h1", "heading_structure", "canonical_present",
        "content_length", "image_alt", "internal_links", "structured_data",
        "open_graph", "viewport", "hreflang",
    } <= issue_ids(result)
    # Still scores above zero because status, robots and redirects are all fine.
    assert result.seo_score < 60
    assert result.category == "HIGH ISSUES"


@pytest.mark.parametrize(
    "status,expected_severity",
    [(200, None), (301, Severity.MEDIUM), (404, Severity.CRITICAL), (500, Severity.CRITICAL),
     (0, Severity.CRITICAL)],
)
def test_http_status_severity(status, expected_severity):
    result = result_for(audit_page(make_page(status_code=status)), "http_status")
    assert result.severity == expected_severity


def test_noindex_is_critical_even_on_an_otherwise_perfect_page():
    result = audit_page(make_page(meta_robots="noindex, follow"))
    assert result_for(result, "robots_directive").severity == Severity.CRITICAL
    assert result.highest_severity == Severity.CRITICAL
    assert result.priority_band == "P0"


def test_nofollow_is_high_not_critical():
    result = result_for(audit_page(make_page(meta_robots="index, nofollow")), "robots_directive")
    assert result.severity == Severity.HIGH


def test_title_length_bands():
    assert result_for(audit_page(make_page(title="Short")), "title").severity == Severity.MEDIUM
    assert result_for(audit_page(make_page(title="x" * 90)), "title").severity == Severity.MEDIUM
    assert result_for(audit_page(make_page(title="x" * 45)), "title").status == "pass"


def test_multiple_h1_is_medium():
    result = result_for(audit_page(make_page(h1="First | Second", h1_count=2)), "h1")
    assert result.severity == Severity.MEDIUM
    assert result.evidence["h1_count"] == 2


def test_canonical_pointing_elsewhere_is_flagged():
    result = result_for(
        audit_page(make_page(canonical_url="https://example.com/other")), "canonical_target"
    )
    assert result.severity == Severity.HIGH
    assert result.evidence["canonical"] == "https://example.com/other"


def test_redirect_chain_severity_scales_with_hops():
    one = result_for(
        audit_page(make_page(redirect_chain=["https://example.com/somewhere-else"])),
        "redirect_chain",
    )
    assert one.severity == Severity.LOW

    three = result_for(
        audit_page(
            make_page(
                redirect_chain=[
                    "https://example.com/a", "https://example.com/b", "https://example.com/c",
                ]
            )
        ),
        "redirect_chain",
    )
    assert three.severity == Severity.HIGH
    assert three.evidence["hops"] == 3


def test_redirect_loop_is_critical():
    result = result_for(
        audit_page(
            make_page(
                url="https://example.com/loop",
                final_url="https://example.com/loop",
                redirect_chain=["https://example.com/loop", "https://example.com/x"],
            )
        ),
        "redirect_chain",
    )
    assert result.severity == Severity.CRITICAL


def test_trailing_slash_redirect_is_not_reported():
    """`/page` -> `/page/` normalises to the same URL and must not look like a loop."""
    result = result_for(
        audit_page(
            make_page(
                url="https://example.com/async",
                final_url="https://example.com/async",
                redirect_chain=["https://example.com/async/"],
            )
        ),
        "redirect_chain",
    )
    assert result.status == "pass"
    assert result.severity is None


def test_a_repeated_hop_is_a_loop():
    result = result_for(
        audit_page(
            make_page(
                url="https://example.com/a",
                final_url="https://example.com/c",
                redirect_chain=[
                    "https://example.com/a",
                    "https://example.com/b",
                    "https://example.com/a",
                ],
            )
        ),
        "redirect_chain",
    )
    assert result.severity == Severity.CRITICAL


def test_alt_text_severity_scales_with_coverage():
    mostly_ok = result_for(audit_page(make_page(image_count=10, missing_alt_count=2)), "image_alt")
    assert mostly_ok.severity == Severity.MEDIUM
    assert mostly_ok.evidence["coverage_percent"] == 80

    mostly_bad = result_for(audit_page(make_page(image_count=10, missing_alt_count=8)), "image_alt")
    assert mostly_bad.severity == Severity.HIGH


def test_broken_links_are_reported():
    result = result_for(audit_page(make_page(broken_link_count=5)), "broken_links")
    assert result.severity == Severity.HIGH
    assert result.evidence["broken_links"] == 5


def test_orphan_page_detection_skips_the_homepage():
    orphan = result_for(
        audit_page(
            make_page(url="https://example.com/hidden", final_url="https://example.com/hidden",
                      inbound_internal_links=0)
        ),
        "orphan_page",
    )
    assert orphan.severity == Severity.MEDIUM

    homepage = result_for(
        audit_page(
            make_page(url="https://example.com/", final_url="https://example.com/",
                      inbound_internal_links=0)
        ),
        "orphan_page",
    )
    assert homepage.status == "pass"


def test_invalid_json_ld_is_reported_separately_from_missing():
    invalid = result_for(
        audit_page(make_page(has_structured_data=True, structured_data_invalid=True)),
        "structured_data",
    )
    assert invalid.severity == Severity.MEDIUM

    missing = result_for(
        audit_page(make_page(has_structured_data=False, structured_data_types=[])),
        "structured_data",
    )
    assert missing.severity == Severity.LOW


def test_url_structure_problems_are_enumerated():
    result = result_for(
        audit_page(
            make_page(
                url="https://example.com/Very/Deep/Path_With/Underscores/And/More/Levels",
                final_url="https://example.com/Very/Deep/Path_With/Underscores/And/More/Levels",
            )
        ),
        "url_structure",
    )
    assert result.severity == Severity.LOW
    assert len(result.evidence["problems"]) >= 3


# ── Scoring ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "score,expected",
    [(100, "LOW ISSUES"), (90.1, "LOW ISSUES"), (90.0, "MEDIUM ISSUES"),
     (75.0, "MEDIUM ISSUES"), (74.9, "HIGH ISSUES"), (0, "HIGH ISSUES")],
)
def test_category_boundaries(score, expected):
    """90 is deliberately the top of MEDIUM, matching the original engine."""
    assert determine_category(score) == expected


@pytest.mark.parametrize(
    "severity,band",
    [(Severity.CRITICAL, "P0"), (Severity.HIGH, "P1"), (Severity.MEDIUM, "P2"),
     (Severity.LOW, "P3"), (Severity.NONE, "P3")],
)
def test_priority_band_mapping(severity, band):
    assert determine_priority_band(severity) == band


def test_highest_severity_picks_the_worst():
    result = audit_page(make_page(meta_robots="noindex", title=None))
    assert determine_highest_severity(result.results) == Severity.CRITICAL


def test_score_is_a_weighted_mean_not_a_plain_average():
    """A failure on a 20-weight rule must cost more than one on a 1-weight rule."""
    heavy = audit_page(make_page(content="short", word_count=2)).seo_score
    light = audit_page(make_page(has_open_graph=False)).seo_score
    assert heavy < light


def test_zero_weights_produce_a_zero_score_rather_than_dividing_by_zero():
    results = audit_page(make_page()).results
    assert calculate_score(results, {}) == 0.0


def test_website_weight_overrides_are_applied():
    weights = resolve_weights({"content": 999.0})
    assert weights["content"] == 999.0
    assert weights["title"] == 8.0  # untouched keys keep their default


# ── Site-wide rules ─────────────────────────────────────────────────────────


def test_duplicate_titles_are_detected_across_pages():
    pages = [
        make_page(url="https://example.com/a", final_url="https://example.com/a",
                  title="Same Title Everywhere On This Site", content_hash="a"),
        make_page(url="https://example.com/b", final_url="https://example.com/b",
                  title="Same Title Everywhere On This Site", content_hash="b"),
        make_page(url="https://example.com/c", final_url="https://example.com/c",
                  title="A Completely Different Page Title Here", content_hash="c"),
    ]
    results = audit_site(pages)
    assert "duplicate_title" in issue_ids(results[0])
    assert "duplicate_title" in issue_ids(results[1])
    assert "duplicate_title" not in issue_ids(results[2])
    assert results[0].results and result_for(results[0], "duplicate_title").evidence[
        "duplicate_count"
    ] == 2


def test_duplicate_content_without_canonical_is_high_severity():
    pages = [
        make_page(url="https://example.com/a", final_url="https://example.com/a",
                  canonical_url="https://example.com/a", content_hash="identical", word_count=300),
        make_page(url="https://example.com/b", final_url="https://example.com/b",
                  canonical_url="https://example.com/b", content_hash="identical", word_count=300),
    ]
    results = audit_site(pages)
    assert result_for(results[0], "duplicate_content").severity == Severity.HIGH


def test_duplicate_content_with_a_canonical_is_downgraded():
    pages = [
        make_page(url="https://example.com/a", final_url="https://example.com/a",
                  canonical_url="https://example.com/a", content_hash="identical", word_count=300),
        make_page(url="https://example.com/b?page=2", final_url="https://example.com/b?page=2",
                  canonical_url="https://example.com/a", content_hash="identical", word_count=300),
    ]
    results = audit_site(pages)
    assert result_for(results[1], "duplicate_content").severity == Severity.LOW


def test_very_short_pages_are_excluded_from_duplicate_content():
    """Empty states and stubs collide trivially and would otherwise flood the report."""
    pages = [
        make_page(url=f"https://example.com/{i}", final_url=f"https://example.com/{i}",
                  content_hash="tiny", word_count=10)
        for i in range(3)
    ]
    annotate_site(pages)
    assert all(not p.duplicate_content_urls for p in pages)


def test_audit_site_on_an_empty_crawl_returns_nothing():
    assert audit_site([]) == []
