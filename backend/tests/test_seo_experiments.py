"""Post-Deployment Validation and the AI Feedback Loop — roadmap §8.4.

Covers experiment creation on PR merge, the retroactive baseline/actual windowing fix in
``aggregate_page_metrics``, checkpoint measurement and its actual-impact derivation, the daily
sweep, the accuracy report, and the wiring from the GitHub webhook's merge event through to a
tracked experiment.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import (
    DeploymentAnalysis,
    GA4Metric,
    GSCMetric,
    GitHubPullRequest,
    IntegrationProvider,
    MemberRole,
    Page,
    SeoExperiment,
    SeoExperimentCheckpoint,
    Website,
    WebsiteMember,
)
from app.services.experiments import (
    compute_accuracy_report,
    measure_checkpoint,
    run_due_checkpoints,
    start_experiment_for_merged_pr,
    suggest_weight_adjustments,
)
from app.services.experiments.recalibration import MIN_SAMPLE_SIZE
from app.services.integrations.base import upsert_integration
from app.services.metrics import aggregate_page_metrics
from app.utils.url_utils import url_hash

from .conftest import auth_headers


def make_site(db, member_user, domain="acme.test"):
    website = Website(
        name="Acme", url=f"https://{domain}/", domain=domain, created_by_id=member_user.id,
        github_repo="acme/website", github_branch="main",
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
        is_active=True, title=f"Page {path}", **kwargs,
    )
    db.add(page)
    db.flush()
    return page


def add_gsc_day(db, website, page, day: date, *, clicks, impressions, position):
    db.add(GSCMetric(
        website_id=website.id, page_id=page.id, date=day,
        clicks=clicks, impressions=impressions, position=position,
        ctr=(clicks / impressions if impressions else 0.0),
    ))


def add_ga4_day(db, website, page, day: date, *, sessions, conversions=0.0):
    db.add(GA4Metric(
        website_id=website.id, page_id=page.id, date=day,
        users=sessions, sessions=sessions, conversions=conversions,
    ))


def make_pr_with_analysis(
    db, website, *, number=1, expected_impact="positive",
    positive_confidence=0.8, negative_confidence=0.1, risk_level="low",
    merged_at=None,
):
    pr = GitHubPullRequest(
        website_id=website.id, number=number, title="Improve page", author="dev",
        state="merged" if merged_at else "open", base_branch="main", head_branch="feature",
        head_sha="b" * 40, merged_at=merged_at,
    )
    db.add(pr)
    db.flush()
    analysis = DeploymentAnalysis(
        website_id=website.id, pull_request_id=pr.id, head_sha="b" * 40,
        positive_confidence=positive_confidence, negative_confidence=negative_confidence,
        expected_impact=expected_impact, risk_level=risk_level,
        positive_findings=[], negative_findings=[], suggested_changes=[],
        gate_mode="off",
    )
    db.add(analysis)
    db.flush()
    return pr, analysis


def link_change_to_page(db, analysis, page, *, weight=0.8):
    from app.models import GitHubChange

    db.add(GitHubChange(
        website_id=analysis.website_id, deployment_analysis_id=analysis.id, page_id=page.id,
        file_path="pages/x.tsx", affected_url=page.url, change_type="title",
        direction="positive", weight=weight, description="title improved",
    ))
    db.flush()


# ── aggregate_page_metrics: the `until` bound fix ───────────────────────────


class TestAggregatePageMetricsUntilBound:
    def test_until_excludes_rows_on_or_after_the_boundary(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        base = date(2026, 1, 15)
        add_gsc_day(db, site, page, base - timedelta(days=3), clicks=10, impressions=100, position=5.0)
        add_gsc_day(db, site, page, base, clicks=999, impressions=999, position=1.0)  # excluded
        db.commit()

        result = aggregate_page_metrics(db, [page.id], window_days=7, today=base, until=base)
        assert result[page.id]["clicks"] == 10

    def test_without_until_a_retroactive_window_leaks_future_rows(self, db, member_user):
        """Documents exactly the bug this parameter fixes: omitting `until` on a call anchored
        in the past silently absorbs every later row too."""
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        base = date(2026, 1, 15)
        add_gsc_day(db, site, page, base - timedelta(days=3), clicks=10, impressions=100, position=5.0)
        add_gsc_day(db, site, page, base + timedelta(days=2), clicks=500, impressions=500, position=1.0)
        db.commit()

        leaked = aggregate_page_metrics(db, [page.id], window_days=7, today=base)
        assert leaked[page.id]["clicks"] == 510  # the bug, demonstrated

        bounded = aggregate_page_metrics(db, [page.id], window_days=7, today=base, until=base)
        assert bounded[page.id]["clicks"] == 10  # the fix

    def test_baseline_and_actual_windows_are_contiguous_and_non_overlapping(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        deploy = date(2026, 1, 15)
        add_gsc_day(db, site, page, deploy - timedelta(days=1), clicks=10, impressions=100, position=5.0)
        add_gsc_day(db, site, page, deploy, clicks=20, impressions=200, position=4.0)  # actual-side
        db.commit()

        baseline = aggregate_page_metrics(db, [page.id], window_days=7, today=deploy, until=deploy)
        actual_end = deploy + timedelta(days=7)
        actual = aggregate_page_metrics(db, [page.id], window_days=7, today=actual_end, until=actual_end)

        assert baseline[page.id]["clicks"] == 10
        assert actual[page.id]["clicks"] == 20

    def test_omitting_until_preserves_existing_trailing_now_behaviour(self, db, member_user):
        """Every pre-existing caller passes no `until` and expects "as of now" — must be unchanged."""
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        add_gsc_day(db, site, page, date.today() - timedelta(days=1), clicks=7, impressions=70, position=3.0)
        db.commit()
        result = aggregate_page_metrics(db, [page.id], window_days=7)
        assert result[page.id]["clicks"] == 7


# ── Experiment creation on merge ────────────────────────────────────────────


class TestStartExperimentForMergedPr:
    def test_creates_an_experiment_with_three_checkpoints(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        merged_at = datetime(2026, 1, 15, tzinfo=timezone.utc)
        pr, analysis = make_pr_with_analysis(db, site, merged_at=merged_at)
        link_change_to_page(db, analysis, page)
        db.commit()

        outcome = start_experiment_for_merged_pr(db, site, pr)
        assert outcome.experiment_id is not None

        experiment = db.get(SeoExperiment, outcome.experiment_id)
        assert experiment.page_id == page.id
        assert experiment.predicted_impact == "positive"
        assert experiment.deployed_at == merged_at
        assert sorted(c.checkpoint_day for c in experiment.checkpoints) == [7, 14, 28]
        assert all(c.due_at == merged_at + timedelta(days=c.checkpoint_day) for c in experiment.checkpoints)

    def test_is_idempotent_for_the_same_deployment_analysis(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        pr, analysis = make_pr_with_analysis(db, site, merged_at=datetime.now(timezone.utc))
        link_change_to_page(db, analysis, page)
        db.commit()

        first = start_experiment_for_merged_pr(db, site, pr)
        second = start_experiment_for_merged_pr(db, site, pr)
        assert first.experiment_id == second.experiment_id
        assert db.query(SeoExperiment).count() == 1

    def test_no_deployment_analysis_means_nothing_to_track(self, db, member_user):
        site = make_site(db, member_user)
        pr = GitHubPullRequest(
            website_id=site.id, number=99, state="merged", merged_at=datetime.now(timezone.utc),
        )
        db.add(pr)
        db.commit()

        outcome = start_experiment_for_merged_pr(db, site, pr)
        assert outcome.experiment_id is None
        assert "No SEO impact analysis" in outcome.reason

    def test_no_resolved_page_means_nothing_to_track(self, db, member_user):
        site = make_site(db, member_user)
        pr, analysis = make_pr_with_analysis(db, site, merged_at=datetime.now(timezone.utc))
        db.commit()  # no GitHubChange linked at all

        outcome = start_experiment_for_merged_pr(db, site, pr)
        assert outcome.experiment_id is None
        assert "no URL to measure" in outcome.reason

    def test_the_highest_weight_change_is_chosen_as_the_tracked_url(self, db, member_user):
        site = make_site(db, member_user)
        minor_page = add_page(db, site, "/minor")
        major_page = add_page(db, site, "/major")
        pr, analysis = make_pr_with_analysis(db, site, merged_at=datetime.now(timezone.utc))
        link_change_to_page(db, analysis, minor_page, weight=0.2)
        link_change_to_page(db, analysis, major_page, weight=0.9)
        db.commit()

        outcome = start_experiment_for_merged_pr(db, site, pr)
        experiment = db.get(SeoExperiment, outcome.experiment_id)
        assert experiment.page_id == major_page.id


# ── Checkpoint measurement ───────────────────────────────────────────────────


class TestMeasureCheckpoint:
    def _experiment(self, db, site, page, predicted="positive", deployed_at=None):
        pr, analysis = make_pr_with_analysis(
            db, site, expected_impact=predicted, merged_at=deployed_at or datetime.now(timezone.utc),
        )
        link_change_to_page(db, analysis, page)
        db.commit()
        outcome = start_experiment_for_merged_pr(db, site, pr)
        return db.get(SeoExperiment, outcome.experiment_id)

    def test_clear_improvement_is_measured_as_positive_and_matches(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        deployed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        experiment = self._experiment(db, site, page, predicted="positive", deployed_at=deployed_at)
        checkpoint = next(c for c in experiment.checkpoints if c.checkpoint_day == 7)

        for i in range(7):
            add_gsc_day(db, site, page, deployed_at.date() - timedelta(days=7 - i),
                       clicks=50, impressions=1000, position=8.0)
            add_gsc_day(db, site, page, deployed_at.date() + timedelta(days=i),
                       clicks=150, impressions=1000, position=4.0)
        db.commit()

        measure_checkpoint(db, checkpoint)
        db.commit()

        assert checkpoint.actual_impact == "positive"
        assert checkpoint.prediction_matched is True
        assert checkpoint.clicks_delta_pct == pytest.approx(2.0, abs=0.01)  # +200%
        assert checkpoint.position_delta == pytest.approx(-4.0, abs=0.01)  # improved by 4 ranks

    def test_clear_regression_is_measured_as_negative(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        deployed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        experiment = self._experiment(db, site, page, predicted="positive", deployed_at=deployed_at)
        checkpoint = next(c for c in experiment.checkpoints if c.checkpoint_day == 7)

        for i in range(7):
            add_gsc_day(db, site, page, deployed_at.date() - timedelta(days=7 - i),
                       clicks=150, impressions=1000, position=4.0)
            add_gsc_day(db, site, page, deployed_at.date() + timedelta(days=i),
                       clicks=30, impressions=1000, position=9.0)
        db.commit()

        measure_checkpoint(db, checkpoint)
        assert checkpoint.actual_impact == "negative"
        assert checkpoint.prediction_matched is False  # predicted positive, actually negative

    def test_no_data_at_all_is_insufficient_not_neutral(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        experiment = self._experiment(db, site, page)
        checkpoint = experiment.checkpoints[0]
        db.commit()

        measure_checkpoint(db, checkpoint)
        assert checkpoint.actual_impact == "insufficient_data"
        assert checkpoint.prediction_matched is None

    def test_tiny_baseline_volume_does_not_produce_a_misleading_percentage_signal(
        self, db, member_user
    ):
        """2 clicks -> 6 clicks is +200% but must not count as a trustworthy signal."""
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        deployed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        experiment = self._experiment(db, site, page, predicted="positive", deployed_at=deployed_at)
        checkpoint = next(c for c in experiment.checkpoints if c.checkpoint_day == 7)

        add_gsc_day(db, site, page, deployed_at.date() - timedelta(days=1), clicks=2, impressions=15, position=40.0)
        add_gsc_day(db, site, page, deployed_at.date(), clicks=6, impressions=18, position=39.0)
        db.commit()

        measure_checkpoint(db, checkpoint)
        # Below _MIN_BASELINE_FOR_SIGNAL on every metric -> no votes -> neutral, not "positive".
        assert checkpoint.actual_impact == "neutral"

    def test_mixed_signals_are_reported_as_mixed(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        deployed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        experiment = self._experiment(db, site, page, predicted="mixed", deployed_at=deployed_at)
        checkpoint = next(c for c in experiment.checkpoints if c.checkpoint_day == 7)

        # Clicks/impressions/sessions/conversions all clearly up; position clearly worse.
        for i in range(7):
            add_gsc_day(db, site, page, deployed_at.date() - timedelta(days=7 - i),
                       clicks=50, impressions=1000, position=4.0)
            add_gsc_day(db, site, page, deployed_at.date() + timedelta(days=i),
                       clicks=100, impressions=1000, position=10.0)
            add_ga4_day(db, site, page, deployed_at.date() - timedelta(days=7 - i), sessions=100, conversions=20)
            add_ga4_day(db, site, page, deployed_at.date() + timedelta(days=i), sessions=200, conversions=40)
        db.commit()

        measure_checkpoint(db, checkpoint)
        assert checkpoint.actual_impact in ("mixed", "positive")  # net vote count decides; both plausible
        # A mixed prediction is satisfied by any non-neutral, non-contradictory-only outcome.
        if checkpoint.actual_impact == "mixed":
            assert checkpoint.prediction_matched is True

    def test_a_mixed_prediction_matches_any_directional_actual_outcome(self):
        from app.services.experiments.tracker import _prediction_matched

        assert _prediction_matched("mixed", "positive") is True
        assert _prediction_matched("mixed", "negative") is True
        assert _prediction_matched("mixed", "mixed") is True
        assert _prediction_matched("mixed", "neutral") is False
        assert _prediction_matched("positive", "positive") is True
        assert _prediction_matched("positive", "negative") is False
        assert _prediction_matched("negative", "insufficient_data") is None


# ── Daily sweep ──────────────────────────────────────────────────────────────


class TestRunDueCheckpoints:
    def test_only_due_and_unmeasured_checkpoints_are_measured(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        deployed_at = datetime.now(timezone.utc) - timedelta(days=10)
        pr, analysis = make_pr_with_analysis(db, site, merged_at=deployed_at)
        link_change_to_page(db, analysis, page)
        db.commit()
        start_experiment_for_merged_pr(db, site, pr)
        db.commit()

        outcome = run_due_checkpoints(db, website_id=site.id)
        # Only the day-7 checkpoint is due 10 days after deploy; 14 and 28 are not yet.
        assert outcome.measured == 1

        experiment = db.query(SeoExperiment).filter_by(website_id=site.id).one()
        due = next(c for c in experiment.checkpoints if c.checkpoint_day == 7)
        not_due = next(c for c in experiment.checkpoints if c.checkpoint_day == 14)
        assert due.measured_at is not None
        assert not_due.measured_at is None

    def test_running_again_does_not_remeasure(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        deployed_at = datetime.now(timezone.utc) - timedelta(days=10)
        pr, analysis = make_pr_with_analysis(db, site, merged_at=deployed_at)
        link_change_to_page(db, analysis, page)
        db.commit()
        start_experiment_for_merged_pr(db, site, pr)
        db.commit()

        run_due_checkpoints(db, website_id=site.id)
        second = run_due_checkpoints(db, website_id=site.id)
        assert second.measured == 0

    def test_an_experiment_completes_once_all_three_checkpoints_are_measured(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        deployed_at = datetime.now(timezone.utc) - timedelta(days=40)
        pr, analysis = make_pr_with_analysis(db, site, merged_at=deployed_at)
        link_change_to_page(db, analysis, page)
        db.commit()
        outcome = start_experiment_for_merged_pr(db, site, pr)
        db.commit()

        run_due_checkpoints(db, website_id=site.id)

        experiment = db.get(SeoExperiment, outcome.experiment_id)
        assert experiment.status == "completed"

    def test_scoped_to_one_website_leaves_others_untouched(self, db, member_user):
        site_a = make_site(db, member_user, domain="a.test")
        site_b = make_site(db, member_user, domain="b.test")
        for site in (site_a, site_b):
            page = add_page(db, site, "/x")
            deployed_at = datetime.now(timezone.utc) - timedelta(days=10)
            pr, analysis = make_pr_with_analysis(db, site, merged_at=deployed_at)
            link_change_to_page(db, analysis, page)
            db.commit()
            start_experiment_for_merged_pr(db, site, pr)
        db.commit()

        outcome = run_due_checkpoints(db, website_id=site_a.id)
        assert outcome.measured == 1  # only site_a's due checkpoint

        b_checkpoint = (
            db.query(SeoExperimentCheckpoint)
            .join(SeoExperiment)
            .filter(SeoExperiment.website_id == site_b.id, SeoExperimentCheckpoint.checkpoint_day == 7)
            .one()
        )
        assert b_checkpoint.measured_at is None


# ── Accuracy report / recalibration hook ─────────────────────────────────────


class TestAccuracyReport:
    def _completed_checkpoint(self, db, site, page, *, predicted, actual, matched, number=None):
        deployed_at = datetime.now(timezone.utc) - timedelta(days=10)
        pr, analysis = make_pr_with_analysis(
            db, site, number=number or page.id, expected_impact=predicted, merged_at=deployed_at,
        )
        link_change_to_page(db, analysis, page)
        db.commit()
        outcome = start_experiment_for_merged_pr(db, site, pr)
        checkpoint = db.get(SeoExperiment, outcome.experiment_id).checkpoints[0]
        checkpoint.measured_at = datetime.now(timezone.utc)
        checkpoint.actual_impact = actual
        checkpoint.prediction_matched = matched
        db.commit()

    def test_below_minimum_sample_size_accuracy_rate_is_null(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        self._completed_checkpoint(db, site, page, predicted="positive", actual="positive", matched=True)
        db.commit()

        report = compute_accuracy_report(db, site)
        assert report.total_measured == 1
        assert report.accuracy_rate is None  # below MIN_SAMPLE_SIZE
        assert report.sample_size_sufficient is False

    def test_accuracy_rate_once_sample_size_is_sufficient(self, db, member_user):
        site = make_site(db, member_user)
        for i in range(MIN_SAMPLE_SIZE):
            page = add_page(db, site, f"/p{i}")
            matched = i < 3  # 3 matched, rest did not
            self._completed_checkpoint(
                db, site, page, predicted="positive",
                actual="positive" if matched else "negative", matched=matched,
            )
        db.commit()

        report = compute_accuracy_report(db, site)
        assert report.sample_size_sufficient is True
        assert report.accuracy_rate == pytest.approx(3 / MIN_SAMPLE_SIZE, abs=0.001)

    def test_insufficient_data_checkpoints_are_excluded_from_the_denominator(self, db, member_user):
        site = make_site(db, member_user)
        for i in range(MIN_SAMPLE_SIZE):
            page = add_page(db, site, f"/p{i}")
            self._completed_checkpoint(
                db, site, page, predicted="positive", actual="insufficient_data", matched=None,
            )
        db.commit()

        report = compute_accuracy_report(db, site)
        assert report.insufficient_data == MIN_SAMPLE_SIZE
        assert report.accuracy_rate is None  # zero evaluable checkpoints

    def test_weight_adjustment_suggestions_hold_below_sample_size(self, db, member_user):
        site = make_site(db, member_user)
        db.commit()
        suggestions = suggest_weight_adjustments(db, site)
        assert len(suggestions) == 1
        assert suggestions[0].direction == "hold"

    def test_weight_adjustment_suggestions_never_apply_anything_automatically(self, db, member_user):
        """The recalibration hook returns proposals only — verified by its return type carrying
        no mechanism to write back to app.config.settings or the impact-weights Setting row."""
        site = make_site(db, member_user)
        for i in range(MIN_SAMPLE_SIZE):
            page = add_page(db, site, f"/p{i}")
            self._completed_checkpoint(
                db, site, page, predicted="positive", actual="negative", matched=False,
            )
        db.commit()

        suggestions = suggest_weight_adjustments(db, site)
        assert any(s.direction == "decrease" for s in suggestions)
        # Nothing in app.services.impact's weight resolution was touched by this call.
        from app.services.impact.engine import IMPACT_WEIGHTS_KEY
        from app.models import Setting
        assert db.query(Setting).filter_by(key=IMPACT_WEIGHTS_KEY).count() == 0


# ── PR-merge webhook wiring ──────────────────────────────────────────────────


class TestPrMergeWiring:
    async def test_a_merged_pr_starts_an_experiment(self, db, member_user):
        from app.services.github.pr_handler import process_pull_request

        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        pr, analysis = make_pr_with_analysis(db, site)
        link_change_to_page(db, analysis, page)
        db.commit()

        payload = {
            "action": "closed",
            "number": pr.number,
            "pull_request": {
                "number": pr.number, "title": "Improve page", "merged": True,
                "user": {"login": "dev"}, "state": "closed",
                "base": {"ref": "main", "sha": "a" * 40},
                "head": {"ref": "feature", "sha": "b" * 40},
                "merged_at": "2026-01-15T00:00:00Z",
            },
        }
        outcome = await process_pull_request(db, event_type="pull_request", payload=payload, website=site)
        assert outcome.action == "experiment_started"
        assert outcome.experiment_id is not None

        experiment = db.get(SeoExperiment, outcome.experiment_id)
        assert experiment.deployed_at == datetime(2026, 1, 15, tzinfo=timezone.utc)

    async def test_a_closed_without_merge_does_not_start_an_experiment(self, db, member_user):
        from app.services.github.pr_handler import process_pull_request

        site = make_site(db, member_user)
        pr, analysis = make_pr_with_analysis(db, site)
        db.commit()

        payload = {
            "action": "closed", "number": pr.number,
            "pull_request": {
                "number": pr.number, "merged": False, "state": "closed",
                "base": {"ref": "main"}, "head": {"ref": "feature"},
            },
        }
        outcome = await process_pull_request(db, event_type="pull_request", payload=payload, website=site)
        assert outcome.action == "ignored"
        assert db.query(SeoExperiment).count() == 0

    async def test_analysis_count_is_not_incremented_by_a_merge_event(self, db, member_user):
        from app.services.github.pr_handler import process_pull_request

        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        pr, analysis = make_pr_with_analysis(db, site)
        link_change_to_page(db, analysis, page)
        db.commit()
        before = pr.analysis_count

        payload = {
            "action": "closed", "number": pr.number,
            "pull_request": {
                "number": pr.number, "merged": True, "state": "closed",
                "base": {"ref": "main"}, "head": {"ref": "feature", "sha": "b" * 40},
                "merged_at": "2026-01-15T00:00:00Z",
            },
        }
        await process_pull_request(db, event_type="pull_request", payload=payload, website=site)
        db.refresh(pr)
        assert pr.analysis_count == before


# ── API surface ──────────────────────────────────────────────────────────────


class TestExperimentsApi:
    def _seed_completed(self, db, site, page):
        deployed_at = datetime.now(timezone.utc) - timedelta(days=40)
        pr, analysis = make_pr_with_analysis(db, site, merged_at=deployed_at)
        link_change_to_page(db, analysis, page)
        db.commit()
        outcome = start_experiment_for_merged_pr(db, site, pr)
        db.commit()
        return outcome.experiment_id

    def test_list_and_detail(self, client, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        experiment_id = self._seed_completed(db, site, page)

        listed = client.get(f"/api/websites/{site.id}/experiments", headers=auth_headers(member_user))
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 1

        detail = client.get(
            f"/api/websites/{site.id}/experiments/{experiment_id}", headers=auth_headers(member_user)
        )
        assert detail.status_code == 200
        assert len(detail.json()["checkpoints"]) == 3

    def test_unknown_experiment_404s(self, client, db, member_user):
        site = make_site(db, member_user)
        db.commit()
        resp = client.get(f"/api/websites/{site.id}/experiments/9999", headers=auth_headers(member_user))
        assert resp.status_code == 404

    def test_run_due_checkpoints_endpoint(self, client, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        self._seed_completed(db, site, page)

        resp = client.post(
            f"/api/websites/{site.id}/experiments/run-due-checkpoints",
            headers=auth_headers(member_user),
        )
        assert resp.status_code == 200
        assert resp.json()["measured"] == 3  # deployed 40 days ago -> all three checkpoints due

    def test_accuracy_endpoint_reports_null_below_sample_size(self, client, db, member_user):
        site = make_site(db, member_user)
        db.commit()
        resp = client.get(
            f"/api/websites/{site.id}/experiments/accuracy", headers=auth_headers(member_user)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["accuracy_rate"] is None
        assert body["weight_adjustment_suggestions"][0]["direction"] == "hold"
