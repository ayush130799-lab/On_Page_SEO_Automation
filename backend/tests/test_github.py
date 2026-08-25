"""GitHub webhooks: signature verification, file→page mapping and re-audit triggering.

The signature check is the entire security boundary on the only unauthenticated write path in the
API, so :class:`TestSignatureVerification` and :class:`TestWebhookSecurity` are deliberately
exhaustive about the ways it must fail closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.models import (
    CrawlMode,
    CrawlRun,
    CrawlTrigger,
    GitHubEvent,
    IntegrationProvider,
    MemberRole,
    Website,
    WebsiteMember,
)
from app.services.github import (
    branch_from_ref,
    compute_signature,
    extract_changed_files,
    find_website,
    has_global_impact,
    is_ignorable,
    map_changed_files,
    process_push,
    resolve_file,
    verify_signature,
)
from app.services.integrations.base import upsert_integration

from .conftest import auth_headers

SECRET = "a-long-shared-webhook-secret-value"


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
        credentials={"webhook_secret": SECRET},
        config={"repo": "acme/website", "branch": "main"},
    )
    return website


def push_payload(files=None, *, repo="acme/website", ref="refs/heads/main", **overrides):
    """A push payload shaped like a real GitHub delivery."""
    files = files if files is not None else ["pages/about.tsx"]
    payload = {
        "ref": ref,
        "before": "a" * 40,
        "after": "b" * 40,
        "created": False,
        "deleted": False,
        "forced": False,
        "compare": f"https://github.com/{repo}/compare/aaa...bbb",
        "repository": {"full_name": repo, "name": repo.split("/")[-1]},
        "pusher": {"name": "developer"},
        "sender": {"login": "developer"},
        "commits": [
            {
                "id": "b" * 40,
                "message": "Update the about page\n\nMore detail here.",
                "added": [],
                "modified": files,
                "removed": [],
            }
        ],
        "head_commit": {"id": "b" * 40, "added": [], "modified": files, "removed": []},
    }
    payload.update(overrides)
    return payload


def signed_request(client, payload, *, secret=SECRET, delivery="delivery-1", event="push",
                   signature=None):
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature or compute_signature(secret, body),
        "Content-Type": "application/json",
    }
    return client.post("/api/webhooks/github", content=body, headers=headers)


# ── Signature verification ──────────────────────────────────────────────────


class TestSignatureVerification:
    def test_a_correct_signature_verifies(self):
        body = b'{"ref":"refs/heads/main"}'
        assert verify_signature(SECRET, body, compute_signature(SECRET, body)) is True

    def test_the_signature_matches_githubs_documented_algorithm(self):
        body = b"payload"
        expected = "sha256=" + hmac.new(
            SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        assert compute_signature(SECRET, body) == expected

    def test_a_wrong_secret_fails(self):
        body = b"payload"
        assert verify_signature("different-secret", body, compute_signature(SECRET, body)) is False

    def test_a_modified_body_fails(self):
        signature = compute_signature(SECRET, b'{"a":1}')
        assert verify_signature(SECRET, b'{"a":2}', signature) is False

    def test_a_missing_signature_fails(self):
        assert verify_signature(SECRET, b"payload", None) is False
        assert verify_signature(SECRET, b"payload", "") is False

    def test_an_unconfigured_secret_fails_closed(self):
        """No secret must never be read as "anything is valid"."""
        body = b"payload"
        assert verify_signature("", body, compute_signature("", body)) is False

    def test_deprecated_sha1_signatures_are_refused(self):
        body = b"payload"
        sha1 = "sha1=" + hmac.new(SECRET.encode(), body, hashlib.sha1).hexdigest()
        assert verify_signature(SECRET, body, sha1) is False

    def test_a_signature_without_the_algorithm_prefix_is_refused(self):
        body = b"payload"
        bare = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert verify_signature(SECRET, body, bare) is False


# ── File → page mapping ─────────────────────────────────────────────────────


class TestRouteResolution:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("pages/about.tsx", "/about"),
            ("pages/index.tsx", "/"),
            ("pages/blog/first-post.tsx", "/blog/first-post"),
            ("pages/blog/index.tsx", "/blog"),
            ("src/pages/contact.jsx", "/contact"),
            ("app/pricing/page.tsx", "/pricing"),
            ("app/(marketing)/features/page.tsx", "/features"),
        ],
    )
    def test_next_routes(self, path, expected):
        assert resolve_file(path, "next") == expected

    def test_next_dynamic_routes_resolve_to_their_collection(self):
        """`[slug].tsx` cannot name one URL, so the parent collection is re-audited instead."""
        assert resolve_file("pages/blog/[slug].tsx", "next") == "/blog"
        assert resolve_file("app/products/[id]/page.tsx", "next") == "/products"

    @pytest.mark.parametrize(
        "framework,path,expected",
        [
            ("nuxt", "pages/about.vue", "/about"),
            ("astro", "src/pages/blog/post.astro", "/blog/post"),
            ("sveltekit", "src/routes/about/+page.svelte", "/about"),
            ("remix", "app/routes/blog.first-post.tsx", "/blog/first-post"),
            ("hugo", "content/posts/hello.md", "/posts/hello"),
            ("jekyll", "_posts/2026-01-15-launch.md", "/launch"),
            ("gatsby", "src/pages/team.js", "/team"),
            ("static", "public/about.html", "/about"),
            ("static", "dist/index.html", "/"),
        ],
    )
    def test_other_frameworks(self, framework, path, expected):
        assert resolve_file(path, framework) == expected

    def test_framework_can_be_auto_detected(self):
        assert resolve_file("pages/about.tsx") == "/about"
        assert resolve_file("content/blog/post.md") == "/blog/post"

    def test_an_unrecognised_path_returns_none(self):
        assert resolve_file("src/lib/helpers.ts") is None
        assert resolve_file("scripts/deploy.sh") is None


class TestGlobalImpactDetection:
    @pytest.mark.parametrize(
        "path",
        [
            "pages/_app.tsx",
            "app/layout.tsx",
            "src/components/Header.tsx",
            "layouts/default.vue",
            "next.config.js",
            "tailwind.config.js",
            "styles/globals.css",
            "_config.yml",
            "middleware.ts",
            "public/robots.txt",
        ],
    )
    def test_shared_files_force_a_full_recrawl(self, path):
        assert has_global_impact(path) is True

    @pytest.mark.parametrize("path", ["pages/about.tsx", "content/blog/post.md"])
    def test_ordinary_route_files_do_not(self, path):
        assert has_global_impact(path) is False

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            "tests/test_home.spec.ts",
            "README.md",
            "CHANGELOG.md",
            "docs/architecture.md",
            "package-lock.json",
            "Dockerfile",
            "alembic/versions/0001_x.py",
        ],
    )
    def test_irrelevant_files_are_ignored(self, path):
        assert is_ignorable(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "content/posts/hello.md",
            "content/docs/getting-started.md",
            "_posts/2026-01-15-launch.md",
            "src/pages/about.mdx",
            "src/content/blog/post.md",
        ],
    )
    def test_markdown_content_is_not_ignored(self, path):
        """On Hugo, Jekyll and content collections a `.md` file *is* the page."""
        assert is_ignorable(path) is False


class TestMapping:
    def test_route_files_map_to_an_incremental_target_list(self):
        result = map_changed_files(
            ["pages/about.tsx", "pages/contact.tsx"], framework="next"
        )
        assert result.requires_full_recrawl is False
        assert sorted(result.affected_paths) == ["/about", "/contact"]

    def test_a_layout_change_forces_a_full_recrawl(self):
        result = map_changed_files(
            ["pages/about.tsx", "app/layout.tsx"], framework="next"
        )
        assert result.requires_full_recrawl is True
        assert "layout" in result.reason.lower()

    def test_an_explicit_path_map_wins(self):
        result = map_changed_files(
            ["src/data/pricing.json"],
            framework="next",
            path_map={"src/data/pricing.json": "/pricing"},
        )
        assert result.affected_paths == ["/pricing"]

    def test_an_explicit_map_overrides_global_impact_detection(self):
        """The operator knows their codebase better than the heuristics do."""
        result = map_changed_files(
            ["src/components/PricingTable.tsx"],
            path_map={"src/components/PricingTable.tsx": "/pricing"},
        )
        assert result.requires_full_recrawl is False
        assert result.affected_paths == ["/pricing"]

    def test_only_ignorable_files_means_no_work(self):
        result = map_changed_files(["README.md", ".github/workflows/ci.yml"])
        assert result.requires_full_recrawl is False
        assert result.has_targets is False

    def test_unmappable_files_fall_back_to_a_full_recrawl(self):
        """Doing nothing would be wrong; a full re-audit is the safe default."""
        result = map_changed_files(["src/lib/api-client.ts", "src/utils/format.ts"])
        assert result.requires_full_recrawl is True
        assert "could be mapped" in result.reason

    def test_a_very_large_change_set_prefers_a_full_crawl(self):
        files = [f"pages/p{i}.tsx" for i in range(300)]
        result = map_changed_files(files, framework="next", max_targets=200)
        assert result.requires_full_recrawl is True
        assert "exceeds" in result.reason

    def test_duplicate_targets_are_collapsed(self):
        result = map_changed_files(
            ["pages/blog/[slug].tsx", "pages/blog/index.tsx"], framework="next"
        )
        assert result.affected_paths == ["/blog"]

    @pytest.mark.parametrize(
        "framework,path,expected",
        [
            ("hugo", "content/posts/hello.md", "/posts/hello"),
            ("jekyll", "_posts/2026-01-15-launch.md", "/launch"),
            ("astro", "src/pages/guide.mdx", "/guide"),
        ],
    )
    def test_a_content_change_triggers_an_incremental_recrawl(self, framework, path, expected):
        """The blog-post case is the one this platform exists for; it must not be ignored."""
        result = map_changed_files([path], framework=framework)
        assert result.requires_full_recrawl is False
        assert result.affected_paths == [expected]
        assert result.ignored_files == []

    def test_partial_mapping_reports_what_it_could_not_do(self):
        result = map_changed_files(
            ["pages/about.tsx", "src/lib/helper.ts"], framework="next"
        )
        assert result.affected_paths == ["/about"]
        assert result.unmapped_files == ["src/lib/helper.ts"]


class TestPayloadParsing:
    def test_files_are_collected_across_commits_and_operations(self):
        payload = {
            "commits": [
                {"added": ["a.tsx"], "modified": ["b.tsx"], "removed": []},
                {"added": [], "modified": ["b.tsx"], "removed": ["c.tsx"]},
            ]
        }
        assert extract_changed_files(payload) == ["a.tsx", "b.tsx", "c.tsx"]

    def test_head_commit_is_included(self):
        payload = {"commits": [], "head_commit": {"modified": ["x.tsx"]}}
        assert extract_changed_files(payload) == ["x.tsx"]

    def test_an_empty_payload_yields_nothing(self):
        assert extract_changed_files({}) == []

    def test_branch_extraction(self):
        assert branch_from_ref("refs/heads/main") == "main"
        assert branch_from_ref("refs/heads/feature/thing") == "feature/thing"
        assert branch_from_ref("refs/tags/v1.0") is None
        assert branch_from_ref(None) is None


# ── Website resolution ──────────────────────────────────────────────────────


class TestWebsiteResolution:
    def test_repository_and_branch_are_matched(self, db, site):
        assert find_website(db, "acme/website", "main").id == site.id

    def test_repository_matching_is_case_insensitive(self, db, site):
        assert find_website(db, "Acme/Website", "main").id == site.id

    def test_a_different_branch_does_not_match(self, db, site):
        assert find_website(db, "acme/website", "develop") is None

    def test_an_unknown_repository_does_not_match(self, db, site):
        assert find_website(db, "someone/else", "main") is None


# ── Push processing ─────────────────────────────────────────────────────────


class TestProcessPush:
    def test_a_route_change_queues_an_incremental_crawl(self, db, site):
        outcome = process_push(
            db, delivery_id="d1", event_type="push",
            payload=push_payload(["pages/about.tsx"]), website=site,
        )

        assert outcome.action == "incremental_crawl"
        run = db.get(CrawlRun, outcome.crawl_run_id)
        assert run.mode == CrawlMode.INCREMENTAL
        assert run.trigger == CrawlTrigger.GITHUB_PUSH
        assert run.target_urls == ["https://acme.test/about"]

    def test_a_layout_change_queues_a_full_crawl(self, db, site):
        outcome = process_push(
            db, delivery_id="d2", event_type="push",
            payload=push_payload(["app/layout.tsx"]), website=site,
        )
        assert outcome.action == "full_crawl"
        assert db.get(CrawlRun, outcome.crawl_run_id).mode == CrawlMode.FULL

    def test_a_documentation_only_push_does_nothing(self, db, site):
        outcome = process_push(
            db, delivery_id="d3", event_type="push",
            payload=push_payload(["README.md"]), website=site,
        )
        assert outcome.action == "ignored"
        assert outcome.crawl_run_id is None
        assert db.query(CrawlRun).count() == 0

    def test_a_push_with_no_file_list_triggers_a_full_crawl(self, db, site):
        """GitHub omits file lists on very large pushes; unknown is not the same as empty."""
        payload = push_payload([])
        payload["commits"] = [{"message": "huge merge"}]
        payload["head_commit"] = {"id": "b" * 40}

        outcome = process_push(
            db, delivery_id="d4", event_type="push", payload=payload, website=site
        )
        assert outcome.action == "full_crawl"
        assert "no file list" in outcome.reason

    def test_a_branch_deletion_is_ignored(self, db, site):
        outcome = process_push(
            db, delivery_id="d5", event_type="push",
            payload=push_payload(["pages/about.tsx"], deleted=True), website=site,
        )
        assert outcome.action == "ignored"

    def test_a_ping_is_recorded_but_does_nothing(self, db, site):
        outcome = process_push(
            db, delivery_id="d6", event_type="ping", payload={"zen": "Keep it logically awesome."},
            website=site,
        )
        assert outcome.action == "ignored"
        assert db.query(GitHubEvent).count() == 1

    def test_non_push_events_are_ignored(self, db, site):
        outcome = process_push(
            db, delivery_id="d7", event_type="pull_request",
            payload=push_payload(["pages/about.tsx"]), website=site,
        )
        assert outcome.action == "ignored"
        assert db.query(CrawlRun).count() == 0

    def test_an_unmapped_repository_is_recorded_for_diagnosis(self, db):
        """"The webhook fires but nothing happens" must be diagnosable."""
        outcome = process_push(
            db, delivery_id="d8", event_type="push",
            payload=push_payload(["pages/about.tsx"], repo="stranger/repo"), website=None,
        )
        assert outcome.action == "unmatched_repository"
        event = db.query(GitHubEvent).one()
        assert event.website_id is None
        assert event.repository == "stranger/repo"
        assert "Connect the repository" in event.action_reason

    def test_an_inactive_website_is_skipped(self, db, site):
        site.is_active = False
        db.commit()
        outcome = process_push(
            db, delivery_id="d9", event_type="push",
            payload=push_payload(["pages/about.tsx"]), website=site,
        )
        assert outcome.action == "ignored"

    def test_the_delivery_is_recorded_with_its_context(self, db, site):
        process_push(
            db, delivery_id="d10", event_type="push",
            payload=push_payload(["pages/about.tsx"]), website=site,
        )
        event = db.query(GitHubEvent).one()
        assert event.repository == "acme/website"
        assert event.branch == "main"
        assert event.pusher == "developer"
        assert event.commit_messages == ["Update the about page"]
        assert event.changed_files == ["pages/about.tsx"]
        assert event.affected_urls == ["https://acme.test/about"]
        assert event.processed_at is not None


class TestIdempotency:
    def test_a_redelivery_does_not_trigger_a_second_crawl(self, db, site):
        """GitHub retries aggressively; a duplicate must never re-run the work."""
        first = process_push(
            db, delivery_id="same-id", event_type="push",
            payload=push_payload(["pages/about.tsx"]), website=site,
        )
        second = process_push(
            db, delivery_id="same-id", event_type="push",
            payload=push_payload(["pages/about.tsx"]), website=site,
        )

        assert second.duplicate is True
        assert second.event_id == first.event_id
        assert db.query(CrawlRun).count() == 1
        assert db.query(GitHubEvent).count() == 1

    def test_distinct_deliveries_are_processed_separately(self, db, site):
        process_push(db, delivery_id="one", event_type="push",
                     payload=push_payload(["pages/a.tsx"]), website=site)
        process_push(db, delivery_id="two", event_type="push",
                     payload=push_payload(["pages/b.tsx"]), website=site)
        assert db.query(GitHubEvent).count() == 2
        assert db.query(CrawlRun).count() == 2


# ── HTTP endpoint ───────────────────────────────────────────────────────────


class TestWebhookEndpoint:
    @pytest.fixture(autouse=True)
    def no_real_crawls(self, monkeypatch):
        monkeypatch.setattr("app.api.routes.crawls.dispatch_crawl", lambda *a, **k: "test")

    def test_a_signed_push_is_accepted_and_queues_a_crawl(self, client, db, site):
        response = signed_request(client, push_payload(["pages/about.tsx"]))

        assert response.status_code == 202
        body = response.json()
        assert body["action"] == "incremental_crawl"
        assert body["website_id"] == site.id
        assert body["affected_urls"] == ["https://acme.test/about"]
        assert db.query(CrawlRun).count() == 1

    def test_a_ping_without_a_repository_still_verifies(self, client, db, site):
        """Organisation-level hooks omit the repository, so the secret cannot be looked up by it."""
        response = signed_request(
            client, {"zen": "Design for failure.", "hook_id": 2},
            delivery="ping-org", event="ping",
        )
        assert response.status_code == 202

    def test_the_global_secret_also_works(self, client, db, site, monkeypatch):
        monkeypatch.setattr("app.config.settings.github_webhook_secret", "global-secret-value")
        response = signed_request(
            client, push_payload(["pages/about.tsx"]), secret="global-secret-value"
        )
        assert response.status_code == 202

    def test_a_ping_delivery_is_accepted(self, client, db, site):
        response = signed_request(
            client,
            {
                "zen": "Keep it logically awesome.",
                "hook_id": 1,
                "repository": {"full_name": "acme/website"},
            },
            delivery="ping-1", event="ping",
        )
        assert response.status_code == 202
        assert response.json()["action"] == "ignored"

    def test_the_response_reports_what_happened(self, client, db, site):
        body = signed_request(client, push_payload(["app/layout.tsx"])).json()
        assert body["action"] == "full_crawl"
        assert body["reason"]
        assert body["crawl_run_id"] is not None

    def test_a_redelivery_is_reported_as_a_duplicate(self, client, db, site):
        payload = push_payload(["pages/about.tsx"])
        signed_request(client, payload, delivery="dup")
        second = signed_request(client, payload, delivery="dup")
        assert second.json()["duplicate"] is True
        assert db.query(CrawlRun).count() == 1


class TestWebhookSecurity:
    @pytest.fixture(autouse=True)
    def no_real_crawls(self, monkeypatch):
        monkeypatch.setattr("app.api.routes.crawls.dispatch_crawl", lambda *a, **k: "test")

    def test_an_unsigned_request_is_rejected(self, client, db, site):
        body = json.dumps(push_payload()).encode()
        response = client.post(
            "/api/webhooks/github",
            content=body,
            headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "d"},
        )
        assert response.status_code == 401
        assert db.query(GitHubEvent).count() == 0

    def test_a_wrong_signature_is_rejected(self, client, db, site):
        response = signed_request(client, push_payload(), secret="not-the-secret")
        assert response.status_code == 401
        assert db.query(CrawlRun).count() == 0

    def test_a_tampered_body_is_rejected(self, client, db, site):
        """The signature must cover the exact bytes, not the parsed object."""
        original = push_payload(["pages/about.tsx"])
        signature = compute_signature(SECRET, json.dumps(original).encode())

        tampered = push_payload(["pages/evil.tsx"])
        response = client.post(
            "/api/webhooks/github",
            content=json.dumps(tampered).encode(),
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "d",
                "X-Hub-Signature-256": signature,
            },
        )
        assert response.status_code == 401

    def test_a_forged_repository_name_cannot_bypass_verification(self, client, db, site):
        """The repository comes from the unverified body, so it must not grant trust."""
        payload = push_payload(["pages/about.tsx"], repo="acme/website")
        response = signed_request(client, payload, secret="attacker-guess")
        assert response.status_code == 401

    def test_the_rejection_does_not_reveal_why(self, client, db, site):
        unknown_repo = signed_request(
            client, push_payload(repo="nobody/nothing"), secret="wrong"
        )
        known_repo = signed_request(client, push_payload(), secret="wrong")
        assert unknown_repo.status_code == known_repo.status_code == 401
        assert unknown_repo.json() == known_repo.json()

    def test_a_malformed_body_is_rejected_cleanly(self, client, db, site):
        body = b"this is not json"
        response = client.post(
            "/api/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "d",
                "X-Hub-Signature-256": compute_signature(SECRET, body),
            },
        )
        assert response.status_code == 422

    def test_the_webhook_secret_is_never_returned_by_the_api(self, client, db, site, member_user):
        body = client.get(
            f"/api/websites/{site.id}/integrations", headers=auth_headers(member_user)
        ).text
        assert SECRET not in body


class TestGitHubViews:
    @pytest.fixture(autouse=True)
    def no_real_crawls(self, monkeypatch):
        monkeypatch.setattr("app.api.routes.crawls.dispatch_crawl", lambda *a, **k: "test")

    def test_events_are_listed_for_a_website(self, client, db, site, member_user):
        signed_request(client, push_payload(["pages/about.tsx"]), delivery="e1")

        items = client.get(
            f"/api/websites/{site.id}/github/events", headers=auth_headers(member_user)
        ).json()["items"]
        assert len(items) == 1
        assert items[0]["action_taken"] == "incremental_crawl"
        assert items[0]["commit_messages"] == ["Update the about page"]

    def test_mapping_can_be_simulated_before_a_deploy(self, client, site, member_user):
        response = client.post(
            f"/api/websites/{site.id}/github/simulate",
            json=["pages/about.tsx", "app/layout.tsx", "README.md"],
            headers=auth_headers(member_user),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["requires_full_recrawl"] is True
        assert "README.md" in body["ignored_files"]

    def test_simulation_shows_resolved_routes(self, client, site, member_user):
        body = client.post(
            f"/api/websites/{site.id}/github/simulate",
            json=["pages/about.tsx", "pages/blog/index.tsx"],
            headers=auth_headers(member_user),
        ).json()
        assert sorted(body["affected_paths"]) == ["/about", "/blog"]
        assert body["mapped_files"]["pages/about.tsx"] == "/about"

    def test_github_views_require_access(self, client, db, site):
        from .conftest import make_user

        stranger = make_user(db, email="outsider@example.com")
        assert client.get(
            f"/api/websites/{site.id}/github/events", headers=auth_headers(stranger)
        ).status_code == 404
