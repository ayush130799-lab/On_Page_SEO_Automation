"""GitHub Change Analysis — roadmap §8.

Covers the diff analyzer (pattern detection against real-shaped unified diffs), the prediction
engine (positive/negative classification, risk banding, the §8.3 comment format), the keyword
cross-reference that resolves an otherwise-ambiguous title/H1 rewrite using Step 2's intent data,
and the pull_request webhook path end to end against a mocked GitHub API.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.models import (
    DeploymentAnalysis,
    GitHubChange,
    GitHubPullRequest,
    IntegrationProvider,
    MemberRole,
    Page,
    PageIntentProfile,
    Website,
    WebsiteMember,
)
from app.services.github.api_client import PullRequestFile
from app.services.github.diff_analyzer import analyse_file_diff, analyse_pr_diff
from app.services.github.prediction import (
    format_pr_comment,
    predict_deployment_impact,
    refine_with_keywords,
)
from app.services.github.signature import compute_signature
from app.services.integrations.base import upsert_integration
from app.utils.url_utils import url_hash

from .conftest import auth_headers

SECRET = "a-long-shared-webhook-secret-value"
TOKEN = "ghp_test_token_1234567890"


@pytest.fixture
def site(db, member_user):
    website = Website(
        name="Acme", url="https://acme.test/", domain="acme.test",
        created_by_id=member_user.id,
        github_repo="acme/website", github_branch="main", github_framework="next",
    )
    db.add(website)
    db.flush()
    db.add(WebsiteMember(website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER))
    db.commit()
    db.refresh(website)
    upsert_integration(
        db, website, IntegrationProvider.GITHUB,
        credentials={"webhook_secret": SECRET, "access_token": TOKEN},
        config={"repo": "acme/website", "branch": "main", "deployment_gate": "off"},
    )
    return website


def add_page(db, website, path, **kwargs):
    url = f"{website.url.rstrip('/')}{path}"
    page = Page(
        website_id=website.id, url=url, url_hash=url_hash(url), path=path,
        is_active=True, title=f"Page {path}", **kwargs,
    )
    db.add(page)
    db.flush()
    return page


def patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        return original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def pr_payload(*, action="opened", number=245, repo="acme/website", base_ref="main",
               head_ref="feature/x", base_sha="a" * 40, head_sha="b" * 40, title="Update page"):
    return {
        "action": action,
        "number": number,
        "repository": {"full_name": repo, "name": repo.split("/")[-1]},
        "pull_request": {
            "number": number,
            "title": title,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "state": "open",
            "merged": False,
            "user": {"login": "developer"},
            "base": {"ref": base_ref, "sha": base_sha},
            "head": {"ref": head_ref, "sha": head_sha},
        },
    }


def signed_request(client, payload, *, secret=SECRET, delivery="pr-delivery-1", event="pull_request"):
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": compute_signature(secret, body),
        "Content-Type": "application/json",
    }
    return client.post("/api/webhooks/github", content=body, headers=headers)


TITLE_H1_PATCH = """@@ -1,7 +1,5 @@
 <head>
-  <title>Book Your Darshan Online | Temple Booking</title>
+  <title>Temple</title>
-  <link rel="canonical" href="https://acme.test/darshan-booking">
   <meta name="description" content="x">
 </head>
-<h1>Book Your Temple Darshan</h1>
+<h1>History of the Temple</h1>
-<a href="/pricing">Pricing</a>
-<a href="/faq">FAQ</a>
-<a href="/contact">Contact</a>
"""


# ── Diff analyzer ────────────────────────────────────────────────────────────


class TestDiffAnalyzer:
    def test_title_h1_canonical_and_link_changes_are_all_detected(self):
        f = PullRequestFile(
            filename="pages/darshan-booking.tsx", status="modified",
            patch=TITLE_H1_PATCH, additions=2, deletions=8,
        )
        changes = analyse_file_diff(f, framework="next", path_map=None)
        types = {c.change_type for c in changes}
        assert types == {"title", "h1", "canonical", "internal_links"}
        canonical = next(c for c in changes if c.change_type == "canonical")
        assert canonical.direction == "negative"
        assert "acme.test/darshan-booking" in canonical.before_value

    def test_a_newly_added_noindex_is_flagged_negative(self):
        patch = '@@ -1,3 +1,4 @@\n <head>\n+<meta name="robots" content="noindex">\n </head>\n'
        f = PullRequestFile(filename="pages/x.tsx", status="modified", patch=patch,
                            additions=1, deletions=0)
        changes = analyse_file_diff(f, framework=None, path_map=None)
        robots = next(c for c in changes if c.change_type == "robots")
        assert robots.direction == "negative"
        assert "noindex" in robots.description.lower()

    def test_a_removed_noindex_is_flagged_positive(self):
        patch = '@@ -1,4 +1,3 @@\n <head>\n-<meta name="robots" content="noindex">\n </head>\n'
        f = PullRequestFile(filename="pages/x.tsx", status="modified", patch=patch,
                            additions=0, deletions=1)
        changes = analyse_file_diff(f, framework=None, path_map=None)
        robots = next(c for c in changes if c.change_type == "robots")
        assert robots.direction == "positive"

    def test_structured_data_removal_is_detected(self):
        patch = (
            '@@ -1,5 +1,1 @@\n <head>\n-<script type="application/ld+json">\n'
            '-{"@type": "FAQPage"}\n-</script>\n </head>\n'
        )
        f = PullRequestFile(filename="pages/x.tsx", status="modified", patch=patch,
                            additions=0, deletions=3)
        changes = analyse_file_diff(f, framework=None, path_map=None)
        schema = next(c for c in changes if c.change_type == "schema")
        assert schema.direction == "negative"
        assert "FAQPage" in schema.description

    def test_a_large_content_reduction_is_flagged(self):
        patch = "@@ -1,20 +1,2 @@\n" + "\n".join(f"-<p>paragraph {i}</p>" for i in range(18)) + "\n+<p>short</p>\n"
        f = PullRequestFile(filename="pages/x.tsx", status="modified", patch=patch,
                            additions=1, deletions=18)
        changes = analyse_file_diff(f, framework=None, path_map=None)
        content = next(c for c in changes if c.change_type == "content_length")
        assert content.direction == "negative"

    def test_a_small_edit_does_not_trigger_a_content_length_finding(self):
        patch = "@@ -1,2 +1,2 @@\n-<p>hello world</p>\n+<p>hello there world</p>\n"
        f = PullRequestFile(filename="pages/x.tsx", status="modified", patch=patch,
                            additions=1, deletions=1)
        changes = analyse_file_diff(f, framework=None, path_map=None)
        assert not any(c.change_type == "content_length" for c in changes)

    def test_unrelated_text_changes_produce_no_false_positives(self):
        patch = "@@ -1,2 +1,2 @@\n-<p>hello world</p>\n+<p>hello there world</p>\n"
        f = PullRequestFile(filename="pages/x.tsx", status="modified", patch=patch,
                            additions=1, deletions=1)
        assert analyse_file_diff(f, framework=None, path_map=None) == []

    def test_ignorable_files_are_skipped_entirely(self):
        f = PullRequestFile(filename="README.md", status="modified",
                            patch="@@ -1 +1 @@\n-<title>a</title>\n+<title>b</title>\n",
                            additions=1, deletions=1)
        assert analyse_file_diff(f, framework=None, path_map=None) == []

    def test_a_binary_file_with_no_patch_produces_no_changes(self):
        f = PullRequestFile(filename="pages/x.tsx", status="modified", patch=None,
                            additions=0, deletions=0)
        assert analyse_file_diff(f, framework=None, path_map=None) == []

    def test_pr_level_analysis_aggregates_every_file(self):
        files = [
            PullRequestFile(filename="pages/a.tsx", status="modified",
                            patch=TITLE_H1_PATCH, additions=2, deletions=8),
            PullRequestFile(filename="pages/b.tsx", status="modified",
                            patch='@@ -1,3 +1,4 @@\n <head>\n+<meta name="robots" content="noindex">\n </head>\n',
                            additions=1, deletions=0),
        ]
        changes = analyse_pr_diff(files, framework="next", path_map=None)
        assert len(changes) > len(analyse_file_diff(files[0], framework="next", path_map=None))


# ── Keyword cross-reference ──────────────────────────────────────────────────


class TestKeywordRefinement:
    def test_losing_the_target_keyword_from_h1_becomes_negative(self):
        f = PullRequestFile(filename="pages/darshan-booking.tsx", status="modified",
                            patch=TITLE_H1_PATCH, additions=2, deletions=8)
        changes = analyse_file_diff(
            f, framework="next", path_map={"pages/darshan-booking.tsx": "/darshan-booking"}
        )
        h1_before = next(c for c in changes if c.change_type == "h1")
        assert h1_before.direction == "neutral"  # ambiguous without keyword context

        refined = refine_with_keywords(
            changes, {"/darshan-booking": ["darshan", "temple booking"]}
        )
        h1_after = next(c for c in refined if c.change_type == "h1")
        assert h1_after.direction == "negative"
        assert "no longer present" in h1_after.description

    def test_gaining_the_target_keyword_becomes_positive(self):
        patch = "@@ -1,2 +1,2 @@\n-<h1>Welcome</h1>\n+<h1>Book Your Darshan Today</h1>\n"
        f = PullRequestFile(filename="pages/x.tsx", status="modified", patch=patch,
                            additions=1, deletions=1)
        changes = analyse_file_diff(f, framework=None, path_map={"pages/x.tsx": "/x"})
        refined = refine_with_keywords(changes, {"/x": ["darshan"]})
        h1 = next(c for c in refined if c.change_type == "h1")
        assert h1.direction == "positive"

    def test_a_page_with_no_known_keywords_is_left_neutral(self):
        f = PullRequestFile(filename="pages/darshan-booking.tsx", status="modified",
                            patch=TITLE_H1_PATCH, additions=2, deletions=8)
        changes = analyse_file_diff(
            f, framework="next", path_map={"pages/darshan-booking.tsx": "/darshan-booking"}
        )
        refined = refine_with_keywords(changes, {})
        h1 = next(c for c in refined if c.change_type == "h1")
        assert h1.direction == "neutral"


# ── Prediction ───────────────────────────────────────────────────────────────


class TestPrediction:
    def test_no_changes_is_neutral_and_low_risk(self):
        pred = predict_deployment_impact([])
        assert pred.expected_impact == "neutral"
        assert pred.risk_level == "low"

    def test_the_roadmap_8_3_scenario_is_high_risk_negative_and_blocks_deployment(self):
        f = PullRequestFile(filename="pages/darshan-booking.tsx", status="modified",
                            patch=TITLE_H1_PATCH, additions=2, deletions=8)
        changes = analyse_file_diff(
            f, framework="next", path_map={"pages/darshan-booking.tsx": "/darshan-booking"}
        )
        # "temple" alone survives into the new H1 ("History of the Temple"), so the keyword list
        # must be specific enough that the rewrite genuinely drops it — matching the roadmap's
        # own example, which is about losing the *primary* keyword, not any related word.
        changes = refine_with_keywords(changes, {"/darshan-booking": ["darshan", "temple booking"]})
        pred = predict_deployment_impact(changes)
        assert pred.expected_impact == "negative"
        assert pred.risk_level in ("high", "critical")
        assert "DO NOT DEPLOY" in pred.recommendation

    def test_a_newly_added_noindex_is_always_critical(self):
        patch = '@@ -1,3 +1,4 @@\n <head>\n+<meta name="robots" content="noindex">\n </head>\n'
        f = PullRequestFile(filename="pages/x.tsx", status="modified", patch=patch,
                            additions=1, deletions=0)
        changes = analyse_file_diff(f, framework=None, path_map=None)
        pred = predict_deployment_impact(changes)
        assert pred.risk_level == "critical"

    def test_purely_positive_changes_are_low_risk(self):
        patch = (
            '@@ -1,2 +1,6 @@\n <head>\n+<script type="application/ld+json">\n'
            '+{"@type": "Product"}\n+</script>\n </head>\n'
        )
        f = PullRequestFile(filename="pages/x.tsx", status="modified", patch=patch,
                            additions=4, deletions=0)
        changes = analyse_file_diff(f, framework=None, path_map=None)
        pred = predict_deployment_impact(changes)
        assert pred.expected_impact == "positive"
        assert pred.risk_level == "low"

    def test_confidence_never_reaches_certainty(self):
        f = PullRequestFile(filename="pages/darshan-booking.tsx", status="modified",
                            patch=TITLE_H1_PATCH, additions=2, deletions=8)
        changes = analyse_file_diff(f, framework="next", path_map=None)
        pred = predict_deployment_impact(changes)
        assert pred.negative_confidence <= 0.95
        assert pred.positive_confidence <= 0.95

    def test_comment_format_matches_the_roadmap_8_3_layout(self):
        f = PullRequestFile(filename="pages/darshan-booking.tsx", status="modified",
                            patch=TITLE_H1_PATCH, additions=2, deletions=8)
        changes = analyse_file_diff(
            f, framework="next", path_map={"pages/darshan-booking.tsx": "/darshan-booking"}
        )
        pred = predict_deployment_impact(changes)
        comment = format_pr_comment(245, changes, pred, affected_urls=["/darshan-booking"])
        assert "PR #245" in comment
        assert "**Overall Impact:**" in comment
        assert "**Risk:**" in comment
        assert "**Affected URL" in comment
        assert "**Changes detected:**" in comment
        assert "**Expected SEO impact:**" in comment
        assert "**Recommendation:**" in comment
        assert "guarantee" in comment.lower()

    def test_comment_never_promises_an_outcome(self):
        """§9.2 — expected, never guaranteed, applies to PR comments too."""
        pred = predict_deployment_impact([])
        comment = format_pr_comment(1, [], pred)
        assert "will rank" not in comment.lower()
        assert "guarantee" in comment.lower()


# ── End-to-end webhook path ──────────────────────────────────────────────────


def _github_api_handler(pr_files_patch: str = TITLE_H1_PATCH, *, posted_comments: list,
                        posted_statuses: list):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/files"):
            return httpx.Response(200, json=[
                {
                    "filename": "pages/darshan-booking.tsx",
                    "status": "modified",
                    "patch": pr_files_patch,
                    "additions": 2,
                    "deletions": 8,
                },
            ])
        if "/issues/" in path and path.endswith("/comments"):
            posted_comments.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 999})
        if "/statuses/" in path:
            posted_statuses.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404, json={"message": "not found"})
    return handler


class TestProcessPullRequest:
    """Exercises the analysis pipeline directly against the test session — the same convention
    ``process_push`` uses elsewhere in this suite. ``SessionLocal`` (used by the webhook route's
    background-dispatch path) points at the real app database, not this test's isolated one, so
    dispatch itself is covered separately in :class:`TestPullRequestWebhookDispatch` with the
    dispatcher monkeypatched — exactly how ``dispatch_crawl`` is tested for push events.
    """

    async def test_a_full_analysis_persists_and_posts_a_comment(self, db, site, monkeypatch):
        from app.services.github.pr_handler import process_pull_request

        add_page(db, site, "/darshan-booking")
        db.add(PageIntentProfile(
            page_id=db.query(Page).filter_by(path="/darshan-booking").one().id,
            website_id=site.id, detected_intent="transactional", business_intent="transactional",
            primary_keywords=["darshan", "temple booking"],
        ))
        db.commit()

        posted_comments: list = []
        posted_statuses: list = []
        patch_transport(
            monkeypatch,
            _github_api_handler(posted_comments=posted_comments, posted_statuses=posted_statuses),
        )

        outcome = await process_pull_request(
            db, event_type="pull_request", payload=pr_payload(action="opened"), website=site,
        )
        assert outcome.action == "analysed"
        assert outcome.expected_impact == "negative"
        assert outcome.comment_posted is True

        pr = db.query(GitHubPullRequest).filter_by(website_id=site.id, number=245).one()
        assert pr.title == "Update page"

        analysis = db.query(DeploymentAnalysis).filter_by(pull_request_id=pr.id).one()
        assert analysis.expected_impact == "negative"
        assert len(posted_comments) == 1
        assert "PR #245" in posted_comments[0]["body"]

        changes = db.query(GitHubChange).filter_by(deployment_analysis_id=analysis.id).all()
        assert any(c.change_type == "h1" and c.direction == "negative" for c in changes)
        # Gate mode is "off" for this site — no status should be posted.
        assert posted_statuses == []

    async def test_a_repo_with_no_access_token_skips_analysis_without_erroring(
        self, db, member_user, monkeypatch
    ):
        from app.services.github.pr_handler import process_pull_request

        website = Website(
            name="NoToken", url="https://notoken.test/", domain="notoken.test",
            created_by_id=member_user.id,
            github_repo="acme/notoken", github_branch="main",
        )
        db.add(website)
        db.flush()
        db.add(WebsiteMember(website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER))
        db.commit()
        upsert_integration(
            db, website, IntegrationProvider.GITHUB,
            credentials={"webhook_secret": SECRET},  # no access_token
            config={"repo": "acme/notoken", "branch": "main"},
        )
        patch_transport(monkeypatch, _github_api_handler(posted_comments=[], posted_statuses=[]))

        outcome = await process_pull_request(
            db, event_type="pull_request",
            payload=pr_payload(repo="acme/notoken", action="opened"), website=website,
        )
        assert outcome.action == "skipped_no_token"
        pr = db.query(GitHubPullRequest).filter_by(website_id=website.id, number=245).one()
        assert db.query(DeploymentAnalysis).filter_by(pull_request_id=pr.id).count() == 0

    async def test_block_mode_posts_a_failure_status_on_high_risk(self, db, site, monkeypatch):
        from app.models import Integration
        from app.services.github.pr_handler import process_pull_request

        # The h1 change only reaches "high" risk once it is cross-referenced against a known
        # target keyword that the rewrite genuinely drops (see TestKeywordRefinement) — without
        # a keyword profile, canonical + internal_links alone land at "medium".
        add_page(db, site, "/darshan-booking")
        db.add(PageIntentProfile(
            page_id=db.query(Page).filter_by(path="/darshan-booking").one().id,
            website_id=site.id, detected_intent="transactional", business_intent="transactional",
            primary_keywords=["darshan", "temple booking"],
        ))
        integration = db.query(Integration).filter_by(
            website_id=site.id, provider=IntegrationProvider.GITHUB
        ).one()
        integration.config = {**(integration.config or {}), "deployment_gate": "block"}
        db.commit()

        posted_statuses: list = []
        patch_transport(
            monkeypatch, _github_api_handler(posted_comments=[], posted_statuses=posted_statuses)
        )
        outcome = await process_pull_request(
            db, event_type="pull_request", payload=pr_payload(action="opened"), website=site,
        )

        assert outcome.risk_level in ("high", "critical")
        assert len(posted_statuses) == 1
        assert posted_statuses[0]["state"] == "failure"

    async def test_warn_mode_never_fails_the_status_even_when_risk_is_high(self, db, site, monkeypatch):
        from app.models import Integration
        from app.services.github.pr_handler import process_pull_request

        add_page(db, site, "/darshan-booking")
        db.add(PageIntentProfile(
            page_id=db.query(Page).filter_by(path="/darshan-booking").one().id,
            website_id=site.id, detected_intent="transactional", business_intent="transactional",
            primary_keywords=["darshan", "temple booking"],
        ))
        integration = db.query(Integration).filter_by(
            website_id=site.id, provider=IntegrationProvider.GITHUB
        ).one()
        integration.config = {**(integration.config or {}), "deployment_gate": "warn"}
        db.commit()

        posted_statuses: list = []
        patch_transport(
            monkeypatch, _github_api_handler(posted_comments=[], posted_statuses=posted_statuses)
        )
        outcome = await process_pull_request(
            db, event_type="pull_request", payload=pr_payload(action="opened"), website=site,
        )

        assert outcome.risk_level in ("high", "critical")
        assert len(posted_statuses) == 1
        assert posted_statuses[0]["state"] == "success"

    async def test_a_synchronize_event_reanalyses_at_the_new_head_sha(self, db, site, monkeypatch):
        from app.services.github.pr_handler import process_pull_request

        posted_comments: list = []
        patch_transport(
            monkeypatch, _github_api_handler(posted_comments=posted_comments, posted_statuses=[])
        )

        await process_pull_request(
            db, event_type="pull_request",
            payload=pr_payload(action="opened", head_sha="b" * 40), website=site,
        )
        await process_pull_request(
            db, event_type="pull_request",
            payload=pr_payload(action="synchronize", head_sha="c" * 40), website=site,
        )

        pr = db.query(GitHubPullRequest).filter_by(website_id=site.id, number=245).one()
        assert pr.analysis_count == 2
        assert pr.head_sha == "c" * 40
        assert len(posted_comments) == 2

    async def test_a_closed_pr_is_not_analysed(self, db, site, monkeypatch):
        from app.services.github.pr_handler import process_pull_request

        patch_transport(monkeypatch, _github_api_handler(posted_comments=[], posted_statuses=[]))
        outcome = await process_pull_request(
            db, event_type="pull_request", payload=pr_payload(action="closed"), website=site,
        )
        assert outcome.action == "ignored"

    async def test_an_unmapped_repository_is_reported_not_silently_dropped(self, db):
        """"The webhook fires but nothing happens" must be diagnosable — same convention
        ``process_push`` follows: called directly with website=None, bypassing the HTTP route's
        signature/secret-fallback resolution, which is a separate concern tested elsewhere."""
        from app.services.github.pr_handler import process_pull_request

        outcome = await process_pull_request(
            db, event_type="pull_request",
            payload=pr_payload(repo="stranger/repo", action="opened"), website=None,
        )
        assert outcome.action == "unmatched_repository"


# ── Webhook route: dispatch, idempotency, signature-adjacent behaviour ──────


class TestPullRequestWebhookDispatch:
    """HTTP-route-level behaviour only. The analysis pipeline itself is not run here — the
    dispatcher is monkeypatched, matching how push-triggered crawls are tested elsewhere in this
    suite, because the real dispatch path opens its own session against the app's configured
    database rather than this test's isolated one."""

    @pytest.fixture(autouse=True)
    def _stub_dispatch(self, monkeypatch):
        self.dispatched: list[tuple] = []
        monkeypatch.setattr(
            "app.api.routes.webhooks.dispatch_pr_analysis",
            lambda website_id, event_type, payload, background_tasks: (
                self.dispatched.append((website_id, event_type, payload)) or "test"
            ),
        )

    def test_an_opened_pr_is_recorded_and_dispatched(self, client, db, site):
        resp = signed_request(client, pr_payload(action="opened"))
        assert resp.status_code == 202
        assert resp.json()["action"] == "queued_pr_analysis"
        assert len(self.dispatched) == 1
        assert self.dispatched[0][0] == site.id

    def test_an_unhandled_action_is_ignored_and_not_dispatched(self, client, db, site):
        resp = signed_request(client, pr_payload(action="labeled"), delivery="pr-delivery-labeled")
        assert resp.json()["action"] == "ignored"
        assert self.dispatched == []

    def test_a_closed_action_is_dispatched_to_check_for_a_merge(self, client, db, site):
        """Step 5: 'closed' now reaches the dispatcher too, since a merged PR starts a §8.4
        post-deployment experiment — process_pull_request itself resolves a close-without-merge
        to a cheap 'ignored' outcome, but the route can't know which case it is without asking."""
        resp = signed_request(client, pr_payload(action="closed"), delivery="pr-delivery-closed")
        assert resp.json()["action"] == "queued_pr_analysis"
        assert len(self.dispatched) == 1

    def test_duplicate_deliveries_are_not_redispatched(self, client, db, site):
        payload = pr_payload(action="opened")
        first = signed_request(client, payload, delivery="pr-dup-1")
        second = signed_request(client, payload, delivery="pr-dup-1")
        assert first.json()["duplicate"] is False
        assert second.json()["duplicate"] is True
        assert len(self.dispatched) == 1

    def test_a_bad_signature_never_reaches_dispatch(self, client, db, site):
        body = json.dumps(pr_payload(action="opened")).encode()
        resp = client.post(
            "/api/webhooks/github", content=body,
            headers={
                "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "pr-badsig-1",
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401
        assert self.dispatched == []


# ── API surface ──────────────────────────────────────────────────────────────


class TestPullRequestApi:
    """Seeds analysis data via a direct ``process_pull_request`` call (same session as the test
    client, unlike the webhook's background dispatch), then reads it back through the real
    endpoints — those are pure reads through the request-scoped session and need no special
    handling."""

    async def _seed(self, db, site, monkeypatch):
        from app.services.github.pr_handler import process_pull_request

        patch_transport(monkeypatch, _github_api_handler(posted_comments=[], posted_statuses=[]))
        await process_pull_request(
            db, event_type="pull_request", payload=pr_payload(action="opened"), website=site,
        )

    async def test_list_and_detail_endpoints(self, client, db, site, monkeypatch):
        await self._seed(db, site, monkeypatch)

        from app.models import User
        member = db.query(User).first()

        listed = client.get(
            f"/api/websites/{site.id}/github/pull-requests", headers=auth_headers(member)
        )
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        assert items[0]["number"] == 245
        assert items[0]["latest_analysis"]["expected_impact"] == "negative"

        detail = client.get(
            f"/api/websites/{site.id}/github/pull-requests/245", headers=auth_headers(member)
        )
        assert detail.status_code == 200
        body = detail.json()
        assert body["analysis"]["expected_impact"] == "negative"
        assert len(body["changes"]) > 0

    def test_an_unknown_pr_number_404s(self, client, db, site):
        from app.models import User
        member = db.query(User).first()
        resp = client.get(
            f"/api/websites/{site.id}/github/pull-requests/9999", headers=auth_headers(member)
        )
        assert resp.status_code == 404
