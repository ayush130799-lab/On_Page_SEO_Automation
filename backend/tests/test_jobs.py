"""Background-job tracking, distributed locking, rollups and the Celery topology."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import (
    GA4Metric,
    GSCMetric,
    HistoricalMetric,
    Job,
    JobType,
    MemberRole,
    Page,
    RunStatus,
    SEOAudit,
    SEOIssue,
    Severity,
    Website,
    WebsiteMember,
)
from app.services.jobs.tracking import (
    fail_job,
    finish_job,
    start_job,
    tracked_job,
    update_progress,
    website_lock,
)
from app.services.rollup import prune_history, rollup_all, rollup_website
from app.utils.url_utils import url_hash, url_path

from .conftest import auth_headers

TODAY = date.today()


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


def add_page(db, site, path, *, seo_score=70.0, severity=Severity.HIGH, issues=3,
             priority_score=50.0):
    url = f"https://acme.test{path}"
    page = Page(
        website_id=site.id, url=url, url_hash=url_hash(url), path=url_path(url),
        is_active=True, seo_score=seo_score, highest_severity=severity,
        issue_count=issues, priority_score=priority_score, status_code=200,
    )
    db.add(page)
    db.flush()
    return page


# ── Job records ─────────────────────────────────────────────────────────────


class TestJobTracking:
    def test_a_job_records_its_lifecycle(self, db, site):
        job = start_job(db, JobType.CRAWL, website_id=site.id, queue="crawl", items_total=100)
        assert job.status == RunStatus.RUNNING
        assert job.started_at is not None

        update_progress(db, job, items_done=50, message="halfway")
        assert job.progress_percent == 50.0
        assert job.progress_message == "halfway"

        finish_job(db, job, {"pages": 100})
        assert job.status == RunStatus.COMPLETED
        assert job.progress_percent == 100.0
        assert job.result == {"pages": 100}
        assert job.duration_seconds is not None

    def test_a_failed_job_records_the_error(self, db, site):
        job = start_job(db, JobType.GSC_SYNC, website_id=site.id)
        fail_job(db, job, "RuntimeError: the provider exploded")
        assert job.status == RunStatus.FAILED
        assert "exploded" in job.error
        assert job.finished_at is not None

    def test_the_error_field_is_redacted(self, db, site):
        """Provider errors routinely echo the API key back in the URL."""
        job = start_job(db, JobType.SEMRUSH_SYNC, website_id=site.id)
        fail_job(db, job, "request to https://api.semrush.com/?key=super-secret-key failed")
        assert "super-secret-key" not in job.error
        assert "REDACTED" in job.error

    def test_the_context_manager_completes_a_successful_block(self, db, site):
        with tracked_job(db, JobType.PRIORITY_SCORING, website_id=site.id) as job:
            job_id = job.id
        db.expire_all()
        assert db.get(Job, job_id).status == RunStatus.COMPLETED

    def test_a_result_set_inside_the_block_survives(self, db, site):
        """Every task assigns `job.result` inside the block; completing must not wipe it."""
        with tracked_job(db, JobType.CRAWL, website_id=site.id) as job:
            job.result = {"pages_crawled": 120}
            job_id = job.id

        db.expire_all()
        finished = db.get(Job, job_id)
        assert finished.status == RunStatus.COMPLETED
        assert finished.result == {"pages_crawled": 120}

    def test_the_context_manager_records_a_raising_block(self, db, site):
        with pytest.raises(ValueError):
            with tracked_job(db, JobType.CRAWL, website_id=site.id) as job:
                job_id = job.id
                raise ValueError("something broke")

        db.expire_all()
        failed = db.get(Job, job_id)
        assert failed.status == RunStatus.FAILED
        assert "something broke" in failed.error

    def test_progress_without_a_total_does_not_divide_by_zero(self, db, site):
        job = start_job(db, JobType.ROLLUP, website_id=site.id)
        update_progress(db, job, items_done=5)
        assert job.progress_percent == 0.0


class TestLocking:
    def test_the_lock_yields_when_redis_is_absent(self):
        """Without Redis a single process cannot race with itself; work must still run."""
        with website_lock(1, "crawl") as acquired:
            assert acquired is True

    def test_the_lock_releases_on_an_exception(self):
        with pytest.raises(RuntimeError):
            with website_lock(1, "crawl"):
                raise RuntimeError("boom")
        # A second acquisition must still succeed.
        with website_lock(1, "crawl") as acquired:
            assert acquired is True


# ── Rollups ─────────────────────────────────────────────────────────────────


class TestRollups:
    def test_a_website_snapshot_captures_scores_and_traffic(self, db, site):
        page = add_page(db, site, "/a", seo_score=60.0, priority_score=80.0)
        add_page(db, site, "/b", seo_score=90.0, priority_score=20.0)
        db.add(
            GSCMetric(website_id=site.id, page_id=page.id, date=TODAY, clicks=120,
                      impressions=4000, ctr=0.03, position=7.0)
        )
        db.add(
            GA4Metric(website_id=site.id, page_id=page.id, date=TODAY, users=900,
                      sessions=1100, conversions=12, revenue=4500.0)
        )
        db.commit()

        summary = rollup_website(db, site, TODAY)

        row = db.query(HistoricalMetric).filter(HistoricalMetric.scope == "website").one()
        assert row.page_count == 2
        assert row.seo_score == 75.0
        assert row.clicks == 120
        assert row.users == 900
        assert row.revenue == 4500.0
        assert summary["pages"] == 2

    def test_critical_issues_are_counted(self, db, site):
        page = add_page(db, site, "/a", severity=Severity.CRITICAL)
        audit = SEOAudit(crawl_run_id=1, page_id=page.id, seo_score=40.0)
        db.add(audit)
        db.flush()
        db.add(
            SEOIssue(
                seo_audit_id=audit.id, page_id=page.id, rule_id="robots_directive",
                check_type="robots", severity=Severity.CRITICAL, title="Robots",
                description="noindex",
            )
        )
        db.commit()

        rollup_website(db, site, TODAY)
        row = db.query(HistoricalMetric).filter(HistoricalMetric.scope == "website").one()
        assert row.critical_count == 1
        assert row.issue_count == 1

    def test_per_page_rows_are_written_for_scored_pages(self, db, site):
        add_page(db, site, "/a", priority_score=90.0)
        add_page(db, site, "/b", priority_score=10.0)
        db.commit()

        rollup_website(db, site, TODAY)
        assert db.query(HistoricalMetric).filter(HistoricalMetric.scope == "page").count() == 2

    def test_rolling_up_the_same_day_twice_overwrites(self, db, site):
        add_page(db, site, "/a", seo_score=50.0)
        db.commit()
        rollup_website(db, site, TODAY)

        page = db.query(Page).first()
        page.seo_score = 95.0
        db.commit()
        rollup_website(db, site, TODAY)

        rows = db.query(HistoricalMetric).filter(HistoricalMetric.scope == "website").all()
        assert len(rows) == 1
        assert rows[0].seo_score == 95.0

    def test_rollup_all_survives_one_bad_website(self, db, site, monkeypatch):
        other = Website(name="Other", url="https://other.test/", domain="other.test")
        db.add(other)
        db.commit()

        original = rollup_website
        calls = {"n": 0}

        def flaky(session, website, day=None):
            calls["n"] += 1
            if website.id == site.id:
                raise RuntimeError("this one is broken")
            return original(session, website, day)

        monkeypatch.setattr("app.services.rollup.rollup_website", flaky)
        results = rollup_all(db, TODAY)

        assert calls["n"] == 2
        assert len(results) == 1  # the healthy website still rolled up

    def test_old_history_is_pruned(self, db, site):
        db.add(
            HistoricalMetric(
                website_id=site.id, date=TODAY - timedelta(days=900), scope="website"
            )
        )
        db.add(HistoricalMetric(website_id=site.id, date=TODAY, scope="website"))
        db.commit()

        assert prune_history(db, keep_days=730) == 1
        assert db.query(HistoricalMetric).count() == 1

    def test_an_empty_website_rolls_up_without_error(self, db, site):
        summary = rollup_website(db, site, TODAY)
        assert summary["pages"] == 0


# ── Celery topology ─────────────────────────────────────────────────────────


class TestCeleryConfiguration:
    def test_every_task_is_registered(self):
        from app.celery_app import celery_app
        import app.services.jobs.tasks  # noqa: F401

        registered = {name for name in celery_app.tasks if name.startswith("seo.")}
        assert {
            "seo.crawl.run",
            "seo.sync.provider",
            "seo.score.priority",
            "seo.ai.analyse",
            "seo.score.rollup",
            "seo.sync.daily_all",
            "seo.crawl.daily_all",
            "seo.score.daily_all",
        } <= registered

    def test_tasks_are_routed_to_separate_queues(self):
        """A multi-hour crawl must not block a two-second metric sync."""
        from app.celery_app import celery_app

        queues = {
            route["queue"] for route in celery_app.conf.task_routes.values()
        }
        assert queues == {"crawl", "sync", "score", "ai"}

    def test_the_nightly_schedule_runs_in_dependency_order(self):
        """Syncs must land before scoring, and scoring before the snapshot."""
        from app.celery_app import celery_app

        schedule = celery_app.conf.beat_schedule
        sync_hour = schedule["daily-provider-sync"]["schedule"].hour
        score_hour = schedule["daily-priority-scoring"]["schedule"].hour
        rollup_hour = schedule["daily-history-rollup"]["schedule"].hour

        assert min(sync_hour) < min(score_hour) < min(rollup_hour)

    def test_late_acknowledgement_is_enabled(self):
        """Losing a long crawl to an early ack would leave the run stuck at 'running'."""
        from app.celery_app import celery_app

        assert celery_app.conf.task_acks_late is True
        assert celery_app.conf.worker_prefetch_multiplier == 1


# ── API ─────────────────────────────────────────────────────────────────────


class TestJobsApi:
    def test_website_jobs_are_listed(self, client, db, site, member_user):
        start_job(db, JobType.CRAWL, website_id=site.id, queue="crawl")
        response = client.get(
            f"/api/websites/{site.id}/jobs", headers=auth_headers(member_user)
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["job_type"] == JobType.CRAWL

    def test_jobs_can_be_filtered_by_status(self, client, db, site, member_user):
        finished = start_job(db, JobType.CRAWL, website_id=site.id)
        finish_job(db, finished)
        start_job(db, JobType.GSC_SYNC, website_id=site.id)

        body = client.get(
            f"/api/websites/{site.id}/jobs?status=completed",
            headers=auth_headers(member_user),
        ).json()
        assert len(body["items"]) == 1
        assert body["items"][0]["status"] == RunStatus.COMPLETED

    def test_the_cross_website_list_respects_access(self, client, db, site, member_user):
        from .conftest import make_user

        start_job(db, JobType.CRAWL, website_id=site.id)
        stranger = make_user(db, email="jobs-stranger@example.com")

        assert client.get("/api/jobs", headers=auth_headers(member_user)).json()["items"]
        assert client.get("/api/jobs", headers=auth_headers(stranger)).json()["items"] == []

    def test_a_platform_job_is_admin_only(self, client, db, member_user, admin_user):
        job = start_job(db, JobType.ROLLUP)  # no website
        assert client.get(
            f"/api/jobs/{job.id}", headers=auth_headers(member_user)
        ).status_code == 403
        assert client.get(
            f"/api/jobs/{job.id}", headers=auth_headers(admin_user)
        ).status_code == 200

    def test_system_health_reports_the_operational_picture(
        self, client, db, site, admin_user
    ):
        failed = start_job(db, JobType.GSC_SYNC, website_id=site.id)
        fail_job(db, failed, "provider unavailable")

        stuck = start_job(db, JobType.CRAWL, website_id=site.id)
        stuck.started_at = datetime.now(timezone.utc) - timedelta(hours=5)
        db.commit()

        body = client.get("/api/system/health", headers=auth_headers(admin_user)).json()
        assert body["jobs_last_24h"][RunStatus.FAILED] == 1
        assert body["stuck_jobs"] == 1
        assert body["recent_failures"][0]["job_type"] == JobType.GSC_SYNC
        assert "reachable" in body["broker"]

    def test_system_health_is_not_public(self, client, member_user):
        assert client.get(
            "/api/system/health", headers=auth_headers(member_user)
        ).status_code == 403
