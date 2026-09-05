"""Turning a verified GitHub pull-request webhook into a Pre-Deployment SEO Prediction — and,
once merged, into a tracked Post-Deployment Validation experiment (§8.4).

    pull_request (opened/synchronize/reopened)
        → upsert the tracked PR
        → fetch changed files + patches (GitHub API, needs a stored access token)
        → diff-analyse each file for SEO-relevant changes
        → cross-reference title/H1 changes against each page's known target keyword (Step 2)
        → predict positive/negative impact and risk (§8.2)
        → persist DeploymentAnalysis + GitHubChange rows
        → post the PR comment (§8.3)
        → apply the configured deployment gate (§8.2's optional block/warn)

    pull_request (closed, merged=true)
        → §8.4: the merge *is* the deploy event for a GitHub-flow repository — start a
          SeoExperiment tracking that PR's prediction against real GSC/GA4 outcomes at
          7/14/28 days (app.services.experiments.tracker)

A repository connected only for push-triggered re-crawls (no access token stored) is unaffected —
this whole path is additive and every step that needs the token degrades to "skipped, recorded
why" rather than failing the webhook delivery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    DeploymentAnalysis,
    GitHubChange,
    GitHubPullRequest,
    Page,
    PageIntentProfile,
    Website,
)
from ..pipeline import resolve_incremental_urls
from .api_client import (
    GitHubApiError,
    get_access_token,
    list_pull_request_files,
    post_commit_status,
    post_pr_comment,
)
from .diff_analyzer import DetectedChange, analyse_pr_diff
from .prediction import format_pr_comment, predict_deployment_impact, refine_with_keywords

logger = logging.getLogger(__name__)

PR_ANALYSIS_ACTIONS = {"opened", "synchronize", "reopened"}
PR_MERGE_ACTIONS = {"closed"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PrAnalysisOutcome:
    action: str
    reason: str
    website_id: int | None = None
    pull_request_id: int | None = None
    deployment_analysis_id: int | None = None
    risk_level: str | None = None
    expected_impact: str | None = None
    comment_posted: bool = False
    #: Set only when this delivery was a merge that started a §8.4 experiment.
    experiment_id: int | None = None


def _parse_github_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _upsert_pull_request(db: Session, website: Website, payload: dict[str, Any]) -> GitHubPullRequest:
    pr_payload = payload.get("pull_request") or {}
    number = payload.get("number") or pr_payload.get("number")

    pr = db.scalar(
        select(GitHubPullRequest).where(
            GitHubPullRequest.website_id == website.id, GitHubPullRequest.number == number
        )
    )
    if pr is None:
        pr = GitHubPullRequest(website_id=website.id, number=number)
        db.add(pr)

    pr.title = pr_payload.get("title")
    pr.author = (pr_payload.get("user") or {}).get("login")
    pr.state = "merged" if pr_payload.get("merged") else (pr_payload.get("state") or "open")
    pr.base_branch = (pr_payload.get("base") or {}).get("ref")
    pr.head_branch = (pr_payload.get("head") or {}).get("ref")
    pr.base_sha = (pr_payload.get("base") or {}).get("sha")
    pr.head_sha = (pr_payload.get("head") or {}).get("sha")
    pr.html_url = pr_payload.get("html_url")
    if pr_payload.get("merged_at"):
        pr.merged_at = _parse_github_timestamp(pr_payload.get("merged_at"))
    if pr_payload.get("closed_at"):
        pr.closed_at = _parse_github_timestamp(pr_payload.get("closed_at"))
    if pr_payload.get("created_at") and pr.opened_at is None:
        pr.opened_at = _parse_github_timestamp(pr_payload.get("created_at"))
    db.flush()
    return pr


def _keywords_by_url(db: Session, website: Website, urls: list[str]) -> dict[str, list[str]]:
    """Primary + secondary target keywords for known pages, lower-cased for matching."""
    if not urls:
        return {}
    pages = db.scalars(
        select(Page).where(Page.website_id == website.id, Page.url.in_(urls))
    ).all()
    by_id = {p.id: p.url for p in pages}
    if not by_id:
        return {}
    profiles = db.scalars(
        select(PageIntentProfile).where(PageIntentProfile.page_id.in_(by_id.keys()))
    ).all()
    result: dict[str, list[str]] = {}
    for profile in profiles:
        url = by_id.get(profile.page_id)
        if not url:
            continue
        keywords = [
            kw.lower()
            for kw in [*(profile.primary_keywords or []), *(profile.secondary_keywords or [])]
        ]
        if keywords:
            result[url] = keywords
    return result


def _page_for_url(db: Session, website: Website, url: str) -> Page | None:
    return db.scalar(
        select(Page).where(Page.website_id == website.id, Page.url == url)
    )


def _deployment_gate_mode(db: Session, website: Website) -> str:
    from ...config import settings
    from ...models import Integration, IntegrationProvider

    integration = db.scalar(
        select(Integration).where(
            Integration.website_id == website.id,
            Integration.provider == IntegrationProvider.GITHUB,
        )
    )
    if integration and isinstance(integration.config, dict):
        mode = integration.config.get("deployment_gate")
        if mode in ("off", "warn", "block"):
            return mode
    return settings.github_deployment_gate_default


def _process_pr_close(
    db: Session, website: Website, payload: dict[str, Any],
) -> PrAnalysisOutcome:
    """§8.4: a merge is the deploy event. A close-without-merge just updates PR state."""
    from ...services.experiments import start_experiment_for_merged_pr

    pr = _upsert_pull_request(db, website, payload)
    db.commit()

    merged = bool((payload.get("pull_request") or {}).get("merged"))
    if not merged:
        return PrAnalysisOutcome(
            action="ignored", reason="Pull request was closed without merging.",
            website_id=website.id, pull_request_id=pr.id,
        )

    outcome = start_experiment_for_merged_pr(db, website, pr)
    if outcome.experiment_id is None:
        return PrAnalysisOutcome(
            action="merge_no_experiment", reason=outcome.reason,
            website_id=website.id, pull_request_id=pr.id,
        )

    return PrAnalysisOutcome(
        action="experiment_started", reason=outcome.reason or "Post-deployment tracking started.",
        website_id=website.id, pull_request_id=pr.id, experiment_id=outcome.experiment_id,
    )


async def process_pull_request(
    db: Session, *, event_type: str, payload: dict[str, Any], website: Website | None,
) -> PrAnalysisOutcome:
    """Analyse one pull_request webhook delivery. Caller has already verified the signature."""
    action = payload.get("action") or ""

    if website is None:
        return PrAnalysisOutcome(action="unmatched_repository", reason="No website maps to this repository.")

    if action in PR_MERGE_ACTIONS:
        return _process_pr_close(db, website, payload)

    if action not in PR_ANALYSIS_ACTIONS:
        return PrAnalysisOutcome(
            action="ignored", reason=f"pull_request action '{action}' is not analysed.",
            website_id=website.id,
        )

    pr = _upsert_pull_request(db, website, payload)
    pr.analysis_count = (pr.analysis_count or 0) + 1
    db.commit()

    repo = website.github_repo
    token = get_access_token(db, website)
    if not token or not repo:
        reason = (
            "No GitHub access token is configured for this website, so the PR diff cannot be "
            "fetched or commented on. Push-triggered re-crawls are unaffected."
        )
        logger.info("Skipping PR analysis for website %s PR #%s: %s", website.id, pr.number, reason)
        return PrAnalysisOutcome(
            action="skipped_no_token", reason=reason, website_id=website.id,
            pull_request_id=pr.id,
        )

    try:
        files = await list_pull_request_files(token, repo, pr.number)
    except GitHubApiError as exc:
        logger.warning("Could not fetch PR files for %s#%d: %s", repo, pr.number, exc)
        return PrAnalysisOutcome(
            action="error", reason=str(exc), website_id=website.id, pull_request_id=pr.id,
        )

    changes = analyse_pr_diff(
        files, framework=website.github_framework, path_map=website.github_path_map
    )

    # Resolve each change's path to an absolute URL and cross-reference target keywords.
    # resolve_incremental_urls de-duplicates its *output*, so zipping it positionally against
    # the input list breaks whenever two distinct paths happen to normalise to the same URL
    # (trailing-slash variants, for instance) — not just when the raw paths repeat. Calling it
    # once per distinct path (cheap: pure string handling, no I/O) sidesteps that entirely.
    distinct_paths = sorted({c.affected_url for c in changes if c.affected_url})
    path_to_url = {path: resolve_incremental_urls(website, [path])[0] for path in distinct_paths}

    for change in changes:
        if change.affected_url in path_to_url:
            change.affected_url = path_to_url[change.affected_url]

    urls = sorted(set(path_to_url.values()))
    keywords_by_url = _keywords_by_url(db, website, urls)
    changes = refine_with_keywords(changes, keywords_by_url)

    prediction = predict_deployment_impact(changes)
    gate_mode = _deployment_gate_mode(db, website)

    analysis = DeploymentAnalysis(
        website_id=website.id,
        pull_request_id=pr.id,
        head_sha=pr.head_sha,
        positive_confidence=prediction.positive_confidence,
        negative_confidence=prediction.negative_confidence,
        expected_impact=prediction.expected_impact,
        risk_level=prediction.risk_level,
        positive_findings=prediction.positive_findings,
        negative_findings=prediction.negative_findings,
        recommendation=prediction.recommendation,
        suggested_changes=prediction.suggested_changes,
        gate_mode=gate_mode,
        analysed_at=_now(),
    )
    db.add(analysis)
    db.flush()

    for change in changes:
        page = _page_for_url(db, website, change.affected_url) if change.affected_url else None
        db.add(GitHubChange(
            website_id=website.id,
            deployment_analysis_id=analysis.id,
            page_id=page.id if page else None,
            file_path=change.file_path,
            affected_url=change.affected_url,
            change_type=change.change_type,
            before_value=change.before_value,
            after_value=change.after_value,
            direction=change.direction,
            weight=change.weight,
            description=change.description,
        ))

    comment_body = format_pr_comment(
        pr.number, changes, prediction, affected_urls=sorted(set(urls)) or None
    )
    analysis.comment_body = comment_body

    comment_id = await post_pr_comment(token, repo, pr.number, comment_body)
    analysis.comment_posted = comment_id is not None
    if comment_id is None:
        analysis.comment_error = "Comment post failed or returned no id; see logs."

    if gate_mode != "off" and pr.head_sha:
        should_block = gate_mode == "block" and prediction.risk_level in ("high", "critical")
        state = "failure" if should_block else "success"
        description = (
            f"SEO impact: {prediction.expected_impact} (risk: {prediction.risk_level})"
        )
        posted = await post_commit_status(
            token, repo, pr.head_sha, state=state, description=description,
            target_url=pr.html_url,
        )
        analysis.gate_status_posted = state if posted else None

    db.commit()

    logger.info(
        "PR analysis for %s#%d (website %s): impact=%s risk=%s comment_posted=%s gate=%s",
        repo, pr.number, website.id, prediction.expected_impact, prediction.risk_level,
        analysis.comment_posted, gate_mode,
    )

    return PrAnalysisOutcome(
        action="analysed",
        reason=f"{len(changes)} SEO-relevant change(s) detected across {len(files)} file(s).",
        website_id=website.id,
        pull_request_id=pr.id,
        deployment_analysis_id=analysis.id,
        risk_level=prediction.risk_level,
        expected_impact=prediction.expected_impact,
        comment_posted=analysis.comment_posted,
    )
