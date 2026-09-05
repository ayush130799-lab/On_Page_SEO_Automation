"""Inbound webhooks.

This is the only unauthenticated write path in the API, so the signature check is the entire
security boundary. It runs before anything in the payload is trusted, and a failure returns 401
without revealing whether the repository or secret was the problem.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Header, Request, Response, status

from ...config import settings
from ...core.deps import DbSession, ReadableWebsite, WritableWebsite
from ...core.errors import ValidationError
from ...db import SessionLocal
from ...models import GitHubEvent, RunStatus
from ...services.github.handler import (
    candidate_secrets,
    existing_delivery,
    find_website,
    finalise_event,
    branch_from_ref,
    process_push,
    record_event,
)
from ...services.github.mapping import map_changed_files
from ...services.github.pr_handler import PR_ANALYSIS_ACTIONS, PR_MERGE_ACTIONS
from ...services.github.signature import verify_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["webhooks"])

MAX_PAYLOAD_BYTES = 25 * 1024 * 1024  # GitHub's documented ceiling


def _execute_pr_analysis(website_id: int, event_type: str, payload: dict) -> None:
    """Run PR analysis in its own session (background task entry point)."""
    import asyncio

    from ...models import Website
    from ...services.github.pr_handler import process_pull_request

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            return
        asyncio.run(process_pull_request(db, event_type=event_type, payload=payload, website=website))
    except Exception:
        logger.exception("PR analysis did not complete for website %s.", website_id)
    finally:
        db.close()


def dispatch_pr_analysis(
    website_id: int, event_type: str, payload: dict, background_tasks: BackgroundTasks | None
) -> str:
    """Send a pull_request analysis to Celery when configured, otherwise a background task.

    Mirrors ``crawls.dispatch_crawl`` — the same reasoning applies: never block the webhook
    response on a GitHub API round trip, since GitHub treats a slow response as a near-failure
    and will retry the delivery, risking a duplicate PR comment.
    """
    if settings.use_celery:
        try:
            from ...services.jobs.tasks import run_github_pr_analysis_task

            run_github_pr_analysis_task.delay(website_id, event_type, payload)
            return "celery"
        except Exception as exc:
            logger.warning(
                "Celery dispatch failed for PR analysis on website %s; falling back: %s",
                website_id, exc,
            )

    if background_tasks is not None:
        background_tasks.add_task(_execute_pr_analysis, website_id, event_type, payload)
        return "background_task"

    _execute_pr_analysis(website_id, event_type, payload)
    return "inline"


@router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    response: Response,
    db: DbSession,
    background_tasks: BackgroundTasks,
    x_github_event: str = Header(default="", alias="X-GitHub-Event"),
    x_github_delivery: str = Header(default="", alias="X-GitHub-Delivery"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
):
    """Receive a GitHub webhook delivery and trigger the appropriate re-audit."""
    # The raw bytes are required: re-serialising parsed JSON changes whitespace and key order,
    # which would invalidate a legitimate signature.
    body = await request.body()
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ValidationError("The webhook payload is larger than GitHub's documented maximum.")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise ValidationError("The webhook payload is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValidationError("The webhook payload must be a JSON object.")

    repository = (payload.get("repository") or {}).get("full_name")
    branch = branch_from_ref(payload.get("ref"))

    # Try every secret configured for the claimed repository. A forged repository name cannot
    # match a real secret, so this is safe despite the name coming from the unverified body.
    verified_website = None
    verified = False
    for website, secret in candidate_secrets(db, repository):
        if verify_signature(secret, body, x_hub_signature_256):
            verified = True
            verified_website = website
            break

    if not verified:
        logger.warning(
            "Rejected an unverified GitHub delivery %s for repository %s.",
            x_github_delivery or "(no id)", repository or "(unknown)",
        )
        # Deliberately uniform: do not reveal whether the repository or the secret was wrong.
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return {"error": {"code": "invalid_signature",
                          "message": "The webhook signature could not be verified."}}

    if not x_github_delivery:
        raise ValidationError("The X-GitHub-Delivery header is required.")

    # A pull_request payload has no top-level `ref` (that field belongs to push payloads); its
    # branch lives at `pull_request.base.ref` as a bare name, not `refs/heads/<name>`.
    if (x_github_event or "").lower() == "pull_request":
        pr_branch = ((payload.get("pull_request") or {}).get("base") or {}).get("ref")
        website = verified_website or find_website(db, repository, pr_branch)
        return _handle_pull_request_event(
            db, background_tasks,
            delivery_id=x_github_delivery, payload=payload, website=website,
        )

    website = verified_website or find_website(db, repository, branch)

    outcome = process_push(
        db,
        delivery_id=x_github_delivery,
        event_type=x_github_event or "push",
        payload=payload,
        website=website,
    )

    if outcome.crawl_run_id and not outcome.duplicate:
        from .crawls import dispatch_crawl

        dispatch_crawl(outcome.crawl_run_id, background_tasks)

    return {
        "delivery_id": x_github_delivery,
        "event_id": outcome.event_id,
        "website_id": outcome.website_id,
        "action": outcome.action,
        "reason": outcome.reason,
        "crawl_run_id": outcome.crawl_run_id,
        "affected_urls": (outcome.affected_urls or [])[:50],
        "duplicate": outcome.duplicate,
    }


def _handle_pull_request_event(
    db, background_tasks: BackgroundTasks, *, delivery_id: str, payload: dict, website
) -> dict:
    """Record the delivery and, when it warrants it, dispatch SEO impact analysis (§8)."""
    duplicate = existing_delivery(db, delivery_id)
    if duplicate is not None:
        logger.info("Ignoring duplicate GitHub delivery %s.", delivery_id)
        return {
            "delivery_id": delivery_id, "event_id": duplicate.id,
            "website_id": duplicate.website_id, "action": duplicate.action_taken or "ignored",
            "reason": "This delivery was already processed.", "duplicate": True,
        }

    event = record_event(
        db, delivery_id=delivery_id, event_type="pull_request", payload=payload,
        website=website, changed_files=[],
    )

    action = (payload.get("action") or "").lower()
    pr_number = payload.get("number")

    if website is None:
        repository = (payload.get("repository") or {}).get("full_name")
        reason = (
            f"No active website is mapped to {repository}. Connect the repository under the "
            "website's integrations to enable pull-request analysis."
        )
        finalise_event(db, event, action="unmatched_repository", reason=reason)
        return {"delivery_id": delivery_id, "event_id": event.id, "action": "unmatched_repository",
                "reason": reason, "duplicate": False}

    if not website.is_active:
        finalise_event(db, event, action="ignored", reason="The website is not active.")
        return {"delivery_id": delivery_id, "event_id": event.id, "website_id": website.id,
                "action": "ignored", "reason": "The website is not active.", "duplicate": False}

    # PR_MERGE_ACTIONS ("closed") covers both a merge — which starts §8.4 post-deployment
    # tracking — and a close-without-merge, which process_pull_request itself resolves to a
    # cheap "ignored"; both still need dispatching to update the tracked PR's final state.
    if action not in PR_ANALYSIS_ACTIONS and action not in PR_MERGE_ACTIONS:
        reason = f"pull_request action '{action}' is not analysed."
        finalise_event(db, event, action="ignored", reason=reason)
        return {"delivery_id": delivery_id, "event_id": event.id, "website_id": website.id,
                "action": "ignored", "reason": reason, "duplicate": False}

    transport = dispatch_pr_analysis(website.id, "pull_request", payload, background_tasks)
    verb = "queued for post-deployment tracking" if action in PR_MERGE_ACTIONS else "queued for SEO impact analysis"
    reason = f"PR #{pr_number} {verb} (via {transport})."
    finalise_event(db, event, action="queued_pr_analysis", reason=reason)

    return {
        "delivery_id": delivery_id, "event_id": event.id, "website_id": website.id,
        "action": "queued_pr_analysis", "reason": reason, "duplicate": False,
    }


@router.get("/websites/{website_id}/github/events")
def list_github_events(
    website: ReadableWebsite,
    db: DbSession,
    limit: int = 20,
):
    """Recent deliveries for a website — the "what did the last deploy change?" view."""
    events = (
        db.query(GitHubEvent)
        .filter(GitHubEvent.website_id == website.id)
        .order_by(GitHubEvent.id.desc())
        .limit(min(limit, 100))
        .all()
    )
    return {
        "items": [
            {
                "id": event.id,
                "delivery_id": event.delivery_id,
                "event_type": event.event_type,
                "repository": event.repository,
                "branch": event.branch,
                "after_sha": (event.after_sha or "")[:8],
                "pusher": event.pusher,
                "commit_count": event.commit_count,
                "commit_messages": event.commit_messages or [],
                "changed_file_count": event.changed_file_count,
                "changed_files": (event.changed_files or [])[:50],
                "affected_urls": event.affected_urls or [],
                "action_taken": event.action_taken,
                "action_reason": event.action_reason,
                "crawl_run_id": event.crawl_run_id,
                "created_at": event.created_at,
                "processed_at": event.processed_at,
            }
            for event in events
        ]
    }


@router.get("/websites/{website_id}/github/pull-requests")
def list_pull_requests(
    website: WritableWebsite,
    db: DbSession,
    limit: int = 20,
):
    """Tracked pull requests with their latest SEO impact prediction — roadmap §8.2/§8.3."""
    from sqlalchemy import select

    from ...models import DeploymentAnalysis, GitHubPullRequest

    prs = db.scalars(
        select(GitHubPullRequest)
        .where(GitHubPullRequest.website_id == website.id)
        .order_by(GitHubPullRequest.id.desc())
        .limit(min(limit, 100))
    ).all()

    items = []
    for pr in prs:
        latest = db.scalar(
            select(DeploymentAnalysis)
            .where(DeploymentAnalysis.pull_request_id == pr.id)
            .order_by(DeploymentAnalysis.id.desc())
            .limit(1)
        )
        items.append({
            "id": pr.id,
            "number": pr.number,
            "title": pr.title,
            "author": pr.author,
            "state": pr.state,
            "base_branch": pr.base_branch,
            "head_branch": pr.head_branch,
            "html_url": pr.html_url,
            "analysis_count": pr.analysis_count,
            "latest_analysis": None if latest is None else {
                "id": latest.id,
                "expected_impact": latest.expected_impact,
                "risk_level": latest.risk_level,
                "positive_confidence": latest.positive_confidence,
                "negative_confidence": latest.negative_confidence,
                "comment_posted": latest.comment_posted,
                "gate_mode": latest.gate_mode,
                "gate_status_posted": latest.gate_status_posted,
                "analysed_at": latest.analysed_at,
            },
        })
    return {"items": items}


@router.get("/websites/{website_id}/github/pull-requests/{number}")
def get_pull_request_analysis(
    website: WritableWebsite,
    db: DbSession,
    number: int,
):
    """Full §8.2/§8.3 detail for one PR: every detected change, the prediction, and the posted
    comment text."""
    from sqlalchemy import select

    from ...core.errors import NotFoundError
    from ...models import DeploymentAnalysis, GitHubChange, GitHubPullRequest

    pr = db.scalar(
        select(GitHubPullRequest).where(
            GitHubPullRequest.website_id == website.id, GitHubPullRequest.number == number
        )
    )
    if pr is None:
        raise NotFoundError(f"No tracked pull request #{number} for website {website.id}.")

    latest = db.scalar(
        select(DeploymentAnalysis)
        .where(DeploymentAnalysis.pull_request_id == pr.id)
        .order_by(DeploymentAnalysis.id.desc())
        .limit(1)
    )
    changes = [] if latest is None else db.scalars(
        select(GitHubChange).where(GitHubChange.deployment_analysis_id == latest.id)
    ).all()

    return {
        "number": pr.number,
        "title": pr.title,
        "author": pr.author,
        "state": pr.state,
        "html_url": pr.html_url,
        "analysis": None if latest is None else {
            "expected_impact": latest.expected_impact,
            "risk_level": latest.risk_level,
            "positive_confidence": latest.positive_confidence,
            "negative_confidence": latest.negative_confidence,
            "positive_findings": latest.positive_findings,
            "negative_findings": latest.negative_findings,
            "recommendation": latest.recommendation,
            "suggested_changes": latest.suggested_changes,
            "comment_body": latest.comment_body,
            "comment_posted": latest.comment_posted,
            "comment_error": latest.comment_error,
            "gate_mode": latest.gate_mode,
            "gate_status_posted": latest.gate_status_posted,
            "analysed_at": latest.analysed_at,
        },
        "changes": [
            {
                "file_path": c.file_path,
                "affected_url": c.affected_url,
                "change_type": c.change_type,
                "before_value": c.before_value,
                "after_value": c.after_value,
                "direction": c.direction,
                "weight": c.weight,
                "extraction_method": c.extraction_method,
                "description": c.description,
            }
            for c in changes
        ],
    }


@router.post("/websites/{website_id}/github/simulate")
def simulate_mapping(
    website: WritableWebsite,
    changed_files: list[str],
):
    """Dry-run the file→page mapping for a website.

    Lets an operator verify that their framework and path-map configuration resolves the files they
    expect *before* a real deploy, instead of discovering it from an unexpected full re-crawl.
    """
    mapping = map_changed_files(
        changed_files,
        framework=website.github_framework,
        path_map=website.github_path_map,
    )
    return {
        "framework": website.github_framework or "auto-detected",
        "requires_full_recrawl": mapping.requires_full_recrawl,
        "reason": mapping.reason,
        "affected_paths": mapping.affected_paths,
        "mapped_files": mapping.mapped_files,
        "unmapped_files": mapping.unmapped_files,
        "ignored_files": mapping.ignored_files,
    }
