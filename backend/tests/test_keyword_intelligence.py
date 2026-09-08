"""Search & AI Keyword Intelligence — roadmap §5.

Covers what Step 2 added on top of Step 1's scoring fixes:

* the site-wide keyword catalog endpoint (§5.4's "ranked opportunity table", at the website
  rather than single-page grain)
* that AI-generated keyword tiers only reach pages the cost-tier gate actually routed to a model
  (§5's dependency note: "route AI/keyword generation through the cost-tier gate")
* the intent API surface added in Step 1 (page_type, mismatch_evidence, full factor breakdown)
  is actually reachable through the endpoint, not just present on the ORM model
"""

from __future__ import annotations

from app.models import MemberRole, Page, Website, WebsiteMember
from app.models.intent import KeywordOpportunity, PageIntentProfile
from app.services.intent.analyser import _process_page, _AnalysisContext
from app.utils.url_utils import url_hash

from .conftest import auth_headers


def make_site(db, member_user, name="Acme", domain="acme.test"):
    website = Website(
        name=name, url=f"https://{domain}/", domain=domain, created_by_id=member_user.id
    )
    db.add(website)
    db.flush()
    db.add(WebsiteMember(website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER))
    db.commit()
    db.refresh(website)
    return website


def add_page(db, website, path, **kwargs):
    url = f"https://{website.domain}{path}"
    page = Page(
        website_id=website.id, url=url, url_hash=url_hash(url), path=path,
        is_active=True, title=kwargs.pop("title", f"Page {path}"),
        h1=kwargs.pop("h1", "Heading"), **kwargs,
    )
    db.add(page)
    db.flush()
    return page


def add_profile_with_keywords(db, website, page, keywords: list[tuple[str, str, float]]):
    """keywords: list of (keyword, tier, score)."""
    profile = PageIntentProfile(
        page_id=page.id, website_id=website.id, detected_intent="transactional",
        business_intent="transactional",
    )
    db.add(profile)
    db.flush()
    for keyword, tier, score in keywords:
        db.add(KeywordOpportunity(
            intent_profile_id=profile.id, page_id=page.id, website_id=website.id,
            keyword=keyword, keyword_tier=tier, keyword_opportunity_score=score,
            demand_score=0.8, ranking_opportunity_score=0.7, intent_match_score=0.9,
            business_relevance_score=0.6, content_relevance_score=0.5,
            competition_opportunity_score=0.7, source="gsc",
        ))
    db.flush()
    return profile


class TestSiteWideKeywordCatalog:
    def test_returns_a_ranked_table_across_all_pages(self, client, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/booking")
        p2 = add_page(db, site, "/pricing")
        add_profile_with_keywords(db, site, p1, [("darshan booking", "primary", 88.0)])
        add_profile_with_keywords(db, site, p2, [("temple pricing", "primary", 42.0)])
        db.commit()

        resp = client.get(
            f"/api/websites/{site.id}/keywords", headers=auth_headers(member_user)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["items"][0]["keyword"] == "darshan booking"
        assert body["items"][0]["keyword_opportunity_score"] == 88.0
        assert body["items"][1]["keyword"] == "temple pricing"

    def test_the_same_keyword_on_two_pages_is_one_entry_with_both_pages_listed(
        self, client, db, member_user
    ):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        add_profile_with_keywords(db, site, p1, [("shared term", "secondary", 30.0)])
        add_profile_with_keywords(db, site, p2, [("shared term", "secondary", 55.0)])
        db.commit()

        resp = client.get(
            f"/api/websites/{site.id}/keywords", headers=auth_headers(member_user)
        )
        body = resp.json()
        assert body["total"] == 1
        entry = body["items"][0]
        assert entry["page_count"] == 2
        # The best-scoring page's factor breakdown wins the top-level entry.
        assert entry["keyword_opportunity_score"] == 55.0
        assert {p["page_id"] for p in entry["pages"]} == {p1.id, p2.id}

    def test_tier_filter_narrows_the_results(self, client, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        add_profile_with_keywords(db, site, page, [
            ("main term", "primary", 80.0),
            ("what is this", "question", 20.0),
        ])
        db.commit()

        resp = client.get(
            f"/api/websites/{site.id}/keywords?tier=question",
            headers=auth_headers(member_user),
        )
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["keyword"] == "what is this"

    def test_a_site_with_no_keywords_returns_an_empty_table_not_an_error(
        self, client, db, member_user
    ):
        site = make_site(db, member_user)
        db.commit()
        resp = client.get(
            f"/api/websites/{site.id}/keywords", headers=auth_headers(member_user)
        )
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "limit": 100, "offset": 0, "items": []}


class TestIntentApiSurfacesStepOneFields:
    def test_page_type_and_mismatch_evidence_reach_the_api(self, client, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/booking")
        db.add(PageIntentProfile(
            page_id=page.id, website_id=site.id, detected_intent="informational",
            business_intent="transactional", page_type="hybrid",
            intent_mismatch=True, mismatch_severity="P0",
            mismatch_explanation="targets informational terms",
            mismatch_evidence="page_targeting",
        ))
        db.commit()

        resp = client.get(
            f"/api/websites/{site.id}/pages/{page.id}/intent",
            headers=auth_headers(member_user),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["page_type"] == "hybrid"
        assert body["mismatch_evidence"] == "page_targeting"

    def test_keyword_factor_breakdown_reaches_the_api(self, client, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/booking")
        add_profile_with_keywords(db, site, page, [("darshan booking", "primary", 88.0)])
        db.commit()

        resp = client.get(
            f"/api/websites/{site.id}/pages/{page.id}/intent",
            headers=auth_headers(member_user),
        )
        kw = resp.json()["keywords"][0]
        assert kw["content_relevance_score"] == 0.5
        assert kw["competition_opportunity_score"] == 0.7
        assert kw["business_relevance_score"] == 0.6


class TestAiKeywordDiscoveryRespectsTheCostGate:
    """§5's dependency note: keyword generation must go through the tier gate, not run
    unconditionally for every page."""

    def test_a_page_with_no_ai_recommendation_gets_no_ai_keyword_tier(self, db, member_user):
        """Pages the tier gate never routed to a model must not carry AI-sourced keywords —
        there is no AI payload to draw them from, by construction."""
        site = make_site(db, member_user)
        page = add_page(db, site, "/quiet", robots_directive=None, structured_data_types=None)
        db.commit()

        from app.services.intent.analyser import IntentAnalysisOutcome

        context = _AnalysisContext(gsc={}, semrush={}, recommendations={}, profiles={},
                                    business_relevance={page.id: 0.5})
        outcome = IntentAnalysisOutcome(website_id=site.id)
        _process_page(db, site, page, crawl_run_id=None, force=True, outcome=outcome,
                      context=context)
        db.commit()

        profile = db.query(PageIntentProfile).filter_by(page_id=page.id).one()
        keywords = db.query(KeywordOpportunity).filter_by(intent_profile_id=profile.id).all()
        assert not any(k.source == "ai" for k in keywords)


class TestIntentAnalysisSurvivesOneBadPage:
    """A single page-level DB error (e.g. a real site's unusual URL or content shape tripping a
    constraint) must not silently zero out intent data for an entire site. Before the fix, the
    per-page loop never rolled back after a caught exception: on Postgres (and in SQLAlchemy's own
    session state machine, regardless of backend) a failed flush leaves the transaction unusable,
    so every later page in the same run fails the same way, and the single commit at the very end
    then discards everything — including pages classified successfully before the bad one."""

    def test_one_bad_page_does_not_wipe_out_the_rest_of_the_site(self, db, member_user, monkeypatch):
        website = make_site(db, member_user, name="Bulk", domain="bulk.test")
        pages = [add_page(db, website, f"/page-{i}") for i in range(5)]
        db.commit()

        from app.services.intent import analyser as analyser_module

        # A periodic-commit checkpoint every 2 pages, exercised against a 5-page site with the
        # failure on page 3 of 5, proves both halves of the fix together: pages checkpointed
        # *before* the bad page survive its rollback, and pages *after* it are not caught in the
        # same cascade the rollback prevents.
        monkeypatch.setattr(analyser_module, "COMMIT_EVERY", 2)

        def fake_process_page(db, website, page, *, crawl_run_id, force, outcome, context):
            if page.path == "/page-2":
                # A real flush-time IntegrityError (unique constraint on page_id) — not a
                # contrived exception — is what a single malformed page can trigger for real.
                db.add(PageIntentProfile(page_id=page.id, website_id=website.id, detected_intent="a"))
                db.add(PageIntentProfile(page_id=page.id, website_id=website.id, detected_intent="b"))
                db.flush()
            else:
                db.add(
                    PageIntentProfile(
                        page_id=page.id, website_id=website.id, detected_intent="informational"
                    )
                )
                outcome.classified += 1

        monkeypatch.setattr(analyser_module, "_process_page", fake_process_page)

        outcome = analyser_module.analyse_intent_for_website(db, website)

        assert outcome.failed == 1
        assert outcome.classified == 4

        db.expire_all()
        surviving = (
            db.query(PageIntentProfile).filter_by(website_id=website.id).all()
        )
        assert len(surviving) == 4
        assert {p.page_id for p in surviving} == {
            page.id for page in pages if page.path != "/page-2"
        }
