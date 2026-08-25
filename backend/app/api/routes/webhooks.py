"""Inbound webhooks.

This is the only unauthenticated write path in the API, so the signature check is the entire
security boundary. It runs before anything in the payload is trusted, and a failure returns 401
without revealing whether the repository or secret was the problem.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Header, Request, Response, status

from ...core.deps import DbSession, WritableWebsite
from ...core.errors import ValidationError
from ...db import SessionLocal
from ...models import GitHubEvent, RunStatus
from ...services.github.handler import (
    candidate_secrets,
    find_website,
    branch_from_ref,
    process_push,
)
from ...services.github.mapping import map_changed_files
from ...services.github.signature import verify_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["webhooks"])

MAX_PAYLOAD_BYTES = 25 * 1024 * 1024  # GitHub's documented ceiling


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


@router.get("/websites/{website_id}/github/events")
def list_github_events(
    website: WritableWebsite,
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
