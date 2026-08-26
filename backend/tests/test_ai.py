"""The AI recommendation engine: schema validation, provider abstraction and the selection gate.

The gate is the part that matters commercially — sending 10 000 pages to an LLM is both wasteful
and slow — so its behaviour is pinned down in detail here.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.models import (
    AIRecommendation,
    AIStatus,
    MemberRole,
    Page,
    SEOIssue,
    Severity,
    Website,
    WebsiteMember,
)
from app.services.ai import (
    PageRecommendation,
    analyse_website,
    available_providers,
    build_user_prompt,
    cached_recommendation,
    extract_json,
    get_provider,
    select_pages,
)
from app.services.ai.providers import LLMError, LLMResponse
from app.utils.url_utils import url_hash, url_path

from .conftest import auth_headers

VALID_RESPONSE = {
    "summary": "The pricing page converts well but its title and schema are holding back clicks.",
    "search_intent": "transactional",
    "content_quality_score": 74,
    "topic_coverage_score": 68,
    "findings": [
        {
            "issue": "Title is truncated in results",
            "explanation": "The title is 78 characters, so search engines cut it off.",
            "why_it_matters": "The value proposition is lost before the user reads it, "
                              "suppressing click-through on 180,000 monthly impressions.",
            "recommended_fix": "Rewrite the title to under 60 characters, keyword first.",
            "implementation": "Update the `title` field in the pricing page's CMS entry.",
            "expected_impact": "A 10-15% CTR lift on existing impressions.",
            "priority": "high",
            "effort": "trivial",
            "confidence": 0.85,
        }
    ],
    "suggested_changes": [
        {
            "field": "title",
            "current": "Our Pricing Plans And Packages For Every Team Size And Budget Available",
            "suggested": "Pricing Plans — Simple Per-Seat Billing | Acme",
            "rationale": "Fits the 60-character limit and leads with the head term.",
        },
        {
            "field": "meta_description",
            "current": None,
            "suggested": "Compare Acme pricing plans. Per-seat billing, no setup fees, "
                         "cancel any time. Start a 14-day free trial.",
            "rationale": "Gives the listing a reason to be clicked.",
        },
    ],
    "expected_impact": "Recovering CTR on this page is the highest-value single fix on the site.",
    "priority": "high",
    "confidence": 0.82,
    "implementation_notes": "All changes are content-level; no template work is required.",
}


@pytest.fixture
def site(db, member_user):
    website = Website(
        name="Acme", url="https://acme.test/", domain="acme.test",
        created_by_id=member_user.id,
    )
    db.add(website)
    db.flush()
    db.add(WebsiteMember(website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER))
    db.commit()
    db.refresh(website)
    return website


def add_page(db, site, path, *, seo_score=60.0, severity=Severity.HIGH, issues=4,
             priority_score=50.0, content_hash="hash-1"):
    url = f"https://acme.test{path}"
    page = Page(
        website_id=site.id, url=url, url_hash=url_hash(url), path=url_path(url),
        is_active=True, seo_score=seo_score, highest_severity=severity, issue_count=issues,
        priority_score=priority_score, status_code=200, content_hash=content_hash,
        title="A Page", content="Some page content here.", word_count=120,
    )
    db.add(page)
    db.flush()
    return page


def add_issue(db, page, rule_id="title", severity=Severity.HIGH):
    db.add(
        SEOIssue(
            seo_audit_id=0, page_id=page.id, rule_id=rule_id, check_type=rule_id,
            category="metadata", severity=severity, title="Title tag",
            description="Title is too long.", recommendation="Shorten it.",
        )
    )
    db.commit()


class FakeProvider:
    """A provider double that records calls and replays scripted responses."""

    name = "fake"
    model = "fake-model-1"

    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [json.dumps(VALID_RESPONSE)])
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system_prompt, user_prompt, **kwargs):
        self.calls.append((system_prompt, user_prompt))
        if self.error is not None:
            raise self.error
        content = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return LLMResponse(
            content=content, model=self.model, provider=self.name,
            prompt_tokens=1200, completion_tokens=400, latency_ms=850,
        )


@pytest.fixture
def fake_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr("app.services.ai.recommender.get_provider", lambda name=None: provider)
    monkeypatch.setattr("app.config.settings.ai_enabled", True)
    return provider


# ── Schema ──────────────────────────────────────────────────────────────────


class TestSchema:
    def test_a_full_response_validates(self):
        recommendation = PageRecommendation.model_validate(VALID_RESPONSE)
        assert recommendation.priority == "high"
        assert len(recommendation.findings) == 1
        assert recommendation.findings[0].why_it_matters
        assert recommendation.findings[0].implementation

    def test_suggested_title_and_description_are_extracted(self):
        recommendation = PageRecommendation.model_validate(VALID_RESPONSE)
        assert recommendation.suggested_title.startswith("Pricing Plans")
        assert "free trial" in recommendation.suggested_meta_description

    def test_a_minimal_response_validates_with_defaults(self):
        recommendation = PageRecommendation.model_validate({"summary": "Looks healthy."})
        assert recommendation.findings == []
        assert recommendation.priority == "medium"
        assert recommendation.confidence == 0.7

    def test_a_response_missing_the_summary_is_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            PageRecommendation.model_validate({"findings": []})

    @pytest.mark.parametrize(
        "given,expected",
        [("P0", "critical"), ("URGENT", "critical"), ("P2", "medium"),
         ("minor", "low"), ("nonsense", "medium"), (None, "medium")],
    )
    def test_priority_synonyms_are_normalised(self, given, expected):
        """Models answer with P0/urgent/High regardless of the instruction."""
        assert PageRecommendation.model_validate(
            {"summary": "x", "priority": given}
        ).priority == expected

    def test_confidence_on_a_hundred_scale_is_rescaled(self):
        assert PageRecommendation.model_validate(
            {"summary": "x", "confidence": 85}
        ).confidence == 0.85

    def test_scores_given_on_a_zero_to_one_scale_are_rescaled(self):
        recommendation = PageRecommendation.model_validate(
            {"summary": "x", "content_quality_score": 0.7}
        )
        assert recommendation.content_quality_score == 70.0

    def test_out_of_range_scores_are_clamped(self):
        recommendation = PageRecommendation.model_validate(
            {"summary": "x", "content_quality_score": 5000, "topic_coverage_score": -20}
        )
        assert recommendation.content_quality_score == 100.0
        assert recommendation.topic_coverage_score == 0.0

    def test_effort_synonyms_are_normalised(self):
        finding = PageRecommendation.model_validate(
            {
                "summary": "x",
                "findings": [
                    {"issue": "i", "explanation": "e", "why_it_matters": "w",
                     "recommended_fix": "f", "effort": "XL"}
                ],
            }
        ).findings[0]
        assert finding.effort == "large"


# ── JSON extraction ─────────────────────────────────────────────────────────


class TestJsonExtraction:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_a_leading_sentence(self):
        assert extract_json('Here is the analysis:\n{"a": 1}') == {"a": 1}

    def test_empty_and_non_json_responses_raise(self):
        for bad in ("", "   ", "I could not analyse this page."):
            with pytest.raises(LLMError):
                extract_json(bad)


# ── Provider selection ──────────────────────────────────────────────────────


class TestProviders:
    def test_no_key_configured_returns_none_rather_than_raising(self, monkeypatch):
        """AI is an enrichment layer; a deployment without a key must still work."""
        for key in ("gemini_api_key", "groq_api_key", "anthropic_api_key", "openai_api_key"):
            monkeypatch.setattr(f"app.config.settings.{key}", "")
        assert get_provider() is None
        assert available_providers() == []

    def test_each_provider_can_be_selected(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.gemini_api_key", "gem-x")
        monkeypatch.setattr("app.config.settings.groq_api_key", "gsk-x")
        monkeypatch.setattr("app.config.settings.anthropic_api_key", "sk-ant-x")
        monkeypatch.setattr("app.config.settings.openai_api_key", "sk-x")

        assert get_provider("gemini").name == "gemini"
        assert get_provider("groq").name == "groq"
        assert get_provider("anthropic").name == "anthropic"
        assert get_provider("openai").name == "openai"
        assert set(available_providers()) == {"gemini", "groq", "anthropic", "openai"}

    def test_an_unknown_provider_falls_back_to_gemini(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.gemini_api_key", "gem-x")
        assert get_provider("mystery-llm").name == "gemini"

    def test_the_active_provider_follows_configuration(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.anthropic_api_key", "sk-ant-x")
        monkeypatch.setattr("app.config.settings.llm_provider", "anthropic")
        assert get_provider().name == "anthropic"

    async def test_anthropic_prefill_is_restored(self, monkeypatch):
        """Anthropic has no JSON mode; the prefilled brace must be added back."""
        from app.services.ai.providers.anthropic_provider import AnthropicProvider

        def handler(request):
            body = json.loads(request.read())
            assert body["messages"][-1] == {"role": "assistant", "content": "{"}
            return httpx.Response(
                200,
                json={
                    "model": "claude-sonnet-5",
                    "content": [{"type": "text", "text": '"summary": "ok"}'}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            )

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient.__init__
        monkeypatch.setattr(
            httpx.AsyncClient, "__init__",
            lambda self, *a, **kw: original(self, *a, **{**kw, "transport": transport}),
        )

        response = await AnthropicProvider("k", "claude-sonnet-5").complete("sys", "user")
        assert response.json() == {"summary": "ok"}

    async def test_openai_rate_limit_is_typed(self, monkeypatch):
        from app.services.ai.providers import LLMRateLimitError
        from app.services.ai.providers.openai_provider import OpenAIProvider

        transport = httpx.MockTransport(lambda r: httpx.Response(429))
        original = httpx.AsyncClient.__init__
        monkeypatch.setattr(
            httpx.AsyncClient, "__init__",
            lambda self, *a, **kw: original(self, *a, **{**kw, "transport": transport}),
        )

        with pytest.raises(LLMRateLimitError):
            await OpenAIProvider("k", "gpt-4o-mini").complete("sys", "user")


# ── Prompt construction ─────────────────────────────────────────────────────


class TestPrompt:
    def test_the_prompt_carries_real_page_and_metric_data(self, db, site):
        page = add_page(db, site, "/pricing", seo_score=62.0)
        db.commit()

        prompt = build_user_prompt(
            page,
            [{"rule_id": "title", "severity": "HIGH", "description": "Title is too long.",
              "evidence": {"length": 78}}],
            {"users": 42000, "conversions": 980, "revenue": 310000,
             "clicks": 9800, "impressions": 180000, "ctr": 0.054, "position": 6.4},
            priority_score=91.2,
            priority_band="P0",
            queries=[{"query": "acme pricing", "clicks": 4000, "impressions": 60000,
                      "position": 3.1}],
        )

        assert "https://acme.test/pricing" in prompt
        assert "42,000 users" in prompt
        assert "980 conversions" in prompt
        assert "9,800 clicks" in prompt
        assert "acme pricing" in prompt
        assert "Title is too long." in prompt
        assert "91.2/100" in prompt

    def test_a_page_without_metrics_says_so_rather_than_inventing_numbers(self, db, site):
        page = add_page(db, site, "/x")
        db.commit()
        prompt = build_user_prompt(page, [], {})
        assert "No analytics or Search Console data" in prompt

    def test_content_is_truncated_to_the_configured_budget(self, db, site, monkeypatch):
        monkeypatch.setattr("app.config.settings.ai_max_content_length", 100)
        page = add_page(db, site, "/long")
        page.content = "word " * 5000
        db.commit()

        prompt = build_user_prompt(page, [], {})
        assert len(prompt) < 4000


# ── The selection gate ──────────────────────────────────────────────────────


class TestSelectionGate:
    def test_healthy_high_scoring_pages_are_skipped(self, db, site):
        add_page(db, site, "/healthy", seo_score=96.0, severity=Severity.LOW, issues=1,
                 priority_score=95.0)
        db.commit()

        selected, decisions = select_pages(db, site, score_threshold=90.0)
        assert selected == []
        assert "healthy" in decisions[0].reason

    def test_high_scoring_pages_are_selected_with_default_threshold(self, db, site):
        page = add_page(db, site, "/high_score", seo_score=96.0, severity=Severity.LOW, issues=1,
                        priority_score=95.0)
        db.commit()

        selected, decisions = select_pages(db, site)
        assert [p.id for p in selected] == [page.id]
        assert "threshold" in decisions[0].reason

    def test_low_scoring_pages_are_selected(self, db, site):
        page = add_page(db, site, "/broken", seo_score=45.0, issues=8)
        db.commit()

        selected, _ = select_pages(db, site)
        assert [p.id for p in selected] == [page.id]

    def test_a_critical_issue_overrides_a_high_score(self, db, site):
        """A single `noindex` can sit on a page scoring 95 — the threshold alone would miss it."""
        page = add_page(
            db, site, "/noindexed", seo_score=95.0, severity=Severity.CRITICAL, issues=1
        )
        db.commit()

        selected, decisions = select_pages(db, site)
        assert [p.id for p in selected] == [page.id]
        assert "CRITICAL" in decisions[0].reason

    def test_pages_with_no_outstanding_issues_are_skipped(self, db, site):
        add_page(db, site, "/clean", seo_score=80.0, severity=Severity.NONE, issues=0)
        db.commit()

        selected, decisions = select_pages(db, site)
        assert selected == []
        assert "no outstanding issues" in decisions[0].reason

    def test_unaudited_pages_are_skipped(self, db, site):
        add_page(db, site, "/new", seo_score=None, issues=0)
        db.commit()

        selected, decisions = select_pages(db, site)
        assert selected == []
        assert "not audited" in decisions[0].reason

    def test_selection_is_ordered_by_priority_not_seo_score(self, db, site):
        """The core claim: business priority decides who gets the expensive call."""
        low_priority_worse_seo = add_page(
            db, site, "/obscure", seo_score=20.0, priority_score=5.0, issues=12
        )
        high_priority_better_seo = add_page(
            db, site, "/pricing", seo_score=70.0, priority_score=95.0, issues=3
        )
        db.commit()

        selected, _ = select_pages(db, site, max_pages=1)
        assert [p.id for p in selected] == [high_priority_better_seo.id]
        assert low_priority_worse_seo.id not in [p.id for p in selected]

    def test_the_page_budget_is_respected(self, db, site):
        for index in range(20):
            add_page(db, site, f"/p{index}", seo_score=50.0, priority_score=100 - index)
        db.commit()

        selected, decisions = select_pages(db, site, max_pages=5)
        assert len(selected) == 5
        assert any("outside the top 5" in d.reason for d in decisions)

    def test_the_threshold_is_configurable(self, db, site):
        add_page(db, site, "/decent", seo_score=85.0, issues=2)
        db.commit()

        assert select_pages(db, site, score_threshold=90)[0]  # below 90 -> selected
        assert not select_pages(db, site, score_threshold=80)[0]  # above 80 -> skipped

    def test_force_bypasses_every_rule(self, db, site):
        add_page(db, site, "/perfect", seo_score=100.0, severity=Severity.NONE, issues=0)
        db.commit()

        assert len(select_pages(db, site, force=True)[0]) == 1

    def test_inactive_pages_are_never_selected(self, db, site):
        page = add_page(db, site, "/gone", seo_score=10.0, issues=9)
        page.is_active = False
        db.commit()

        assert select_pages(db, site)[0] == []


# ── Caching ─────────────────────────────────────────────────────────────────


class TestRecommendationCache:
    def test_an_unchanged_page_reuses_its_recommendation(self, db, site):
        page = add_page(db, site, "/a", content_hash="stable-hash")
        db.add(
            AIRecommendation(
                website_id=site.id, page_id=page.id, provider="fake", model="m",
                status="completed", content_hash="stable-hash", summary="cached",
            )
        )
        db.commit()

        assert cached_recommendation(db, page).summary == "cached"

    def test_changed_content_invalidates_the_cache(self, db, site):
        page = add_page(db, site, "/a", content_hash="new-hash")
        db.add(
            AIRecommendation(
                website_id=site.id, page_id=page.id, provider="fake", model="m",
                status="completed", content_hash="old-hash", summary="stale",
            )
        )
        db.commit()

        assert cached_recommendation(db, page) is None

    def test_a_failed_recommendation_is_not_cached(self, db, site):
        page = add_page(db, site, "/a", content_hash="h")
        db.add(
            AIRecommendation(
                website_id=site.id, page_id=page.id, provider="fake", model="m",
                status="failed", content_hash="h",
            )
        )
        db.commit()

        assert cached_recommendation(db, page) is None


# ── End-to-end analysis ─────────────────────────────────────────────────────


class TestAnalyseWebsite:
    async def test_a_selected_page_is_analysed_and_stored(self, db, site, fake_provider):
        page = add_page(db, site, "/pricing", seo_score=55.0, issues=4)
        add_issue(db, page)

        outcome = await analyse_website(db, site)

        assert outcome.analysed == 1
        assert outcome.failed == 0
        assert outcome.provider == "fake"

        db.expire_all()
        stored = db.query(AIRecommendation).filter(
            AIRecommendation.page_id == page.id
        ).one()
        assert stored.status == "completed"
        assert stored.priority == "high"
        assert stored.suggested_title.startswith("Pricing Plans")
        assert stored.payload["findings"][0]["why_it_matters"]
        assert stored.prompt_tokens == 1200
        assert db.get(Page, page.id).ai_status == AIStatus.COMPLETED

    async def test_skipped_pages_are_marked_not_left_pending(self, db, site, fake_provider):
        healthy = add_page(db, site, "/healthy", seo_score=98.0, severity=Severity.NONE, issues=0)
        db.commit()

        outcome = await analyse_website(db, site)

        assert outcome.analysed == 0
        assert outcome.skipped == 1
        assert len(fake_provider.calls) == 0
        db.expire_all()
        assert db.get(Page, healthy.id).ai_status == AIStatus.SKIPPED

    async def test_unchanged_pages_hit_the_cache_instead_of_the_model(
        self, db, site, fake_provider
    ):
        page = add_page(db, site, "/a", seo_score=50.0, issues=5, content_hash="h1")
        add_issue(db, page)

        await analyse_website(db, site)
        assert len(fake_provider.calls) == 1

        db.expire_all()
        second = await analyse_website(db, db.get(Website, site.id))
        assert second.cached == 1
        assert len(fake_provider.calls) == 1  # no second call

    async def test_force_bypasses_the_cache(self, db, site, fake_provider):
        page = add_page(db, site, "/a", seo_score=50.0, issues=5, content_hash="h1")
        add_issue(db, page)

        await analyse_website(db, site)
        db.expire_all()
        await analyse_website(db, db.get(Website, site.id), force=True)
        assert len(fake_provider.calls) == 2

    async def test_a_malformed_response_is_repaired_on_the_second_try(
        self, db, site, monkeypatch
    ):
        provider = FakeProvider(
            responses=["not json at all", json.dumps(VALID_RESPONSE)]
        )
        monkeypatch.setattr("app.services.ai.recommender.get_provider", lambda name=None: provider)
        monkeypatch.setattr("app.config.settings.ai_enabled", True)

        page = add_page(db, site, "/a", seo_score=50.0, issues=5)
        add_issue(db, page)

        outcome = await analyse_website(db, site)
        assert outcome.analysed == 1
        assert len(provider.calls) == 2
        assert "could not be parsed" in provider.calls[1][1]

    async def test_a_persistently_broken_response_records_a_failure(
        self, db, site, monkeypatch
    ):
        provider = FakeProvider(responses=["garbage", "still garbage"])
        monkeypatch.setattr("app.services.ai.recommender.get_provider", lambda name=None: provider)
        monkeypatch.setattr("app.config.settings.ai_enabled", True)

        page = add_page(db, site, "/a", seo_score=50.0, issues=5)
        add_issue(db, page)

        outcome = await analyse_website(db, site)
        assert outcome.failed == 1
        db.expire_all()
        assert db.get(Page, page.id).ai_status == AIStatus.FAILED
        assert db.query(AIRecommendation).filter(
            AIRecommendation.status == "failed"
        ).count() == 1

    async def test_one_page_failing_does_not_stop_the_rest(self, db, site, monkeypatch):
        calls = {"n": 0}

        class FlakyProvider(FakeProvider):
            async def complete(self, system_prompt, user_prompt, **kwargs):
                calls["n"] += 1
                if "/broken" in user_prompt:
                    raise LLMError("provider exploded")
                return await FakeProvider.complete(self, system_prompt, user_prompt, **kwargs)

        provider = FlakyProvider()
        monkeypatch.setattr("app.services.ai.recommender.get_provider", lambda name=None: provider)
        monkeypatch.setattr("app.config.settings.ai_enabled", True)

        good = add_page(db, site, "/good", seo_score=50.0, issues=5, content_hash="a")
        bad = add_page(db, site, "/broken", seo_score=40.0, issues=6, content_hash="b")
        add_issue(db, good)
        add_issue(db, bad)

        outcome = await analyse_website(db, site)
        assert outcome.analysed == 1
        assert outcome.failed == 1
        assert outcome.errors

    async def test_analysis_is_a_no_op_when_disabled(self, db, site, monkeypatch):
        monkeypatch.setattr("app.config.settings.ai_enabled", False)
        add_page(db, site, "/a", seo_score=10.0, issues=9)
        db.commit()

        outcome = await analyse_website(db, site)
        assert outcome.analysed == 0
        assert "disabled" in outcome.errors[0]

    async def test_analysis_is_a_no_op_without_a_provider_key(self, db, site, monkeypatch):
        monkeypatch.setattr("app.config.settings.ai_enabled", True)
        monkeypatch.setattr("app.services.ai.recommender.get_provider", lambda name=None: None)
        add_page(db, site, "/a", seo_score=10.0, issues=9)
        db.commit()

        outcome = await analyse_website(db, site)
        assert outcome.analysed == 0
        assert "No API key" in outcome.errors[0]

    async def test_specific_pages_can_be_targeted(self, db, site, fake_provider):
        first = add_page(db, site, "/a", seo_score=50.0, issues=5, content_hash="a")
        add_page(db, site, "/b", seo_score=50.0, issues=5, content_hash="b")
        db.commit()

        outcome = await analyse_website(db, site, page_ids=[first.id])
        assert outcome.analysed == 1
        assert len(fake_provider.calls) == 1

    async def test_multi_provider_load_balancing_and_failover(self, db, site, monkeypatch):
        """When multiple AI providers are configured, pages are distributed round-robin with failover."""
        provider_a = FakeProvider()
        provider_a.name = "gemini"
        provider_b = FakeProvider()
        provider_b.name = "groq"

        monkeypatch.setattr("app.config.settings.ai_enabled", True)
        monkeypatch.setattr("app.services.ai.recommender.get_active_providers", lambda: [provider_a, provider_b])

        add_page(db, site, "/p1", seo_score=50.0, issues=5, content_hash="p1")
        add_page(db, site, "/p2", seo_score=50.0, issues=5, content_hash="p2")
        db.commit()

        outcome = await analyse_website(db, site)
        assert outcome.analysed == 2
        assert "gemini" in outcome.provider and "groq" in outcome.provider


# ── API ─────────────────────────────────────────────────────────────────────


class TestRecommendationApi:
    def test_provider_status_is_exposed(self, client, member_user, monkeypatch):
        monkeypatch.setattr("app.config.settings.llm_provider", "groq")
        monkeypatch.setattr("app.config.settings.groq_api_key", "gsk-x")
        body = client.get("/api/ai/providers", headers=auth_headers(member_user)).json()
        assert body["active"] == "groq"
        assert "groq" in body["configured"]

    def test_selection_preview_explains_each_decision(self, client, db, site, member_user):
        add_page(db, site, "/healthy", seo_score=98.0, severity=Severity.NONE, issues=0)
        add_page(db, site, "/broken", seo_score=30.0, issues=9)
        db.commit()

        body = client.get(
            f"/api/websites/{site.id}/ai/selection", headers=auth_headers(member_user)
        ).json()
        assert body["considered_count"] == 2
        assert body["selected_count"] == 1
        assert all(d["reason"] for d in body["decisions"])

    def test_analyse_can_run_synchronously(self, client, db, site, member_user, fake_provider):
        page = add_page(db, site, "/a", seo_score=50.0, issues=5)
        add_issue(db, page)

        response = client.post(
            f"/api/websites/{site.id}/ai/analyse",
            json={"wait": True},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 200
        assert response.json()["analysed"] == 1

    def test_analyse_can_be_queued(self, client, site, member_user, monkeypatch):
        monkeypatch.setattr(
            "app.api.routes.recommendations.dispatch_analysis", lambda *a, **k: "test"
        )
        response = client.post(
            f"/api/websites/{site.id}/ai/analyse", json={}, headers=auth_headers(member_user)
        )
        assert response.status_code == 200
        assert "queued" in response.json()["status"]

    def test_recommendations_list_is_ordered_by_business_priority(
        self, client, db, site, member_user
    ):
        low = add_page(db, site, "/low", priority_score=10.0)
        high = add_page(db, site, "/high", priority_score=95.0)
        for page in (low, high):
            db.add(
                AIRecommendation(
                    website_id=site.id, page_id=page.id, provider="fake", model="m",
                    status="completed", summary="s", priority="high", payload={"findings": []},
                )
            )
        db.commit()

        items = client.get(
            f"/api/websites/{site.id}/recommendations", headers=auth_headers(member_user)
        ).json()["items"]
        assert items[0]["page_id"] == high.id

    def test_a_recommendation_detail_returns_the_full_payload(
        self, client, db, site, member_user
    ):
        page = add_page(db, site, "/a")
        record = AIRecommendation(
            website_id=site.id, page_id=page.id, provider="fake", model="m",
            status="completed", summary="s", payload=VALID_RESPONSE,
        )
        db.add(record)
        db.commit()

        body = client.get(
            f"/api/recommendations/{record.id}", headers=auth_headers(member_user)
        ).json()
        assert body["recommendation"]["findings"][0]["recommended_fix"]
        assert body["url"] == page.url

    def test_recommendations_are_not_visible_to_other_users(self, client, db, site):
        from .conftest import make_user

        stranger = make_user(db, email="nosy2@example.com")
        assert client.get(
            f"/api/websites/{site.id}/recommendations", headers=auth_headers(stranger)
        ).status_code == 404
