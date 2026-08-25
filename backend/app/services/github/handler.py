"""Turning a verified GitHub push into a re-audit.

    push → verify signature → record the delivery (idempotent) → resolve the website
         → collect changed files → map files to pages
         → incremental re-audit when mapping succeeded, full re-audit otherwise

Every delivery is recorded, including ones that map to no website, because "the webhook fires but
nothing happens" is otherwise impossible to diagnose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import (
    CrawlMode,
    CrawlTrigger,
    GitHubEvent,
    Integration,
    IntegrationProvider,
    Website,
)
from ..integrations.base import read_credentials
from ..pipeline import create_crawl_run, resolve_incremental_urls
from .mapping import extract_changed_files, map_changed_files

logger = logging.getLogger(__name__)

#: Only the payload keys worth keeping for diagnostics. The rest is large and uninteresting.
RETAINED_PAYLOAD_KEYS = ("ref", "before", "after", "created", "deleted", "forced", "compare")

MAX_STORED_FILES = 500
#: Cap on how many secrets are tried when a delivery names no repository.
MAX_SECRET_CANDIDATES = 200
MAX_STORED_COMMIT_MESSAGES = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class WebhookOutcome:
    """What the platform did with one delivery."""

    event_id: int
    action: str
    reason: str
    website_id: int | None = None
    crawl_run_id: int | None = None
    affected_urls: list[str] | None = None
    duplicate: bool = False


# ── Website resolution ──────────────────────────────────────────────────────


def branch_from_ref(ref: str | None) -> str | None:
    """``refs/heads/main`` → ``main``. Tags and other refs return ``None``."""
    if not ref or not ref.startswith("refs/heads/"):
        return None
    return ref[len("refs/heads/") :]


def find_website(db: Session, repository: str | None, branch: str | None) -> Website | None:
    """Match a repository (and branch) to a website.

    Repository names are compared case-insensitively because GitHub preserves the owner's casing
    but treats it as insensitive, and an operator will inevitably type it differently.
    """
    if not repository:
        return None

    candidates = db.scalars(
        select(Website).where(func.lower(Website.github_repo) == repository.lower())
    ).all()
    if not candidates:
        return None

    if branch:
        for website in candidates:
            if (website.github_branch or "main").lower() == branch.lower():
                return website
        # The repository is known but this branch is not the one being monitored.
        return None

    return candidates[0]


def resolve_secret(db: Session, website: Website | None) -> str:
    """The signing secret for a website, falling back to the global one.

    A per-website secret is stored encrypted on its GitHub integration; the environment-level
    ``GITHUB_WEBHOOK_SECRET`` covers a single-repo deployment.
    """
    from ...config import settings

    if website is not None:
        integration = db.scalar(
            select(Integration).where(
                Integration.website_id == website.id,
                Integration.provider == IntegrationProvider.GITHUB,
            )
        )
        if integration is not None and integration.credentials_encrypted:
            try:
                secret = read_credentials(integration).get("webhook_secret")
                if secret:
                    return secret
            except Exception as exc:
                logger.warning(
                    "Could not read the stored webhook secret for website %s: %s",
                    website.id, exc,
                )

    return settings.github_webhook_secret


def candidate_secrets(db: Session, repository: str | None) -> list[tuple[Website | None, str]]:
    """Every (website, secret) pair that could have signed a delivery for this repository.

    The signature must be checked *before* the payload is trusted, but the payload is what names
    the repository. The way out is to try each configured secret for the claimed repository: a
    forged repository name simply will not match any real secret.
    """
    from ...config import settings

    pairs: list[tuple[Website | None, str]] = []

    if repository:
        for website in db.scalars(
            select(Website).where(func.lower(Website.github_repo) == repository.lower())
        ):
            secret = resolve_secret(db, website)
            if secret:
                pairs.append((website, secret))

    if settings.github_webhook_secret:
        pairs.append((None, settings.github_webhook_secret))

    if not pairs:
        # Organisation-level webhooks and some ping deliveries carry no repository, so there is
        # nothing to look the secret up by. Trying every configured secret is safe — each is
        # compared in constant time, and a wrong guess simply fails — and it is the difference
        # between a webhook that verifies and one that silently 401s during setup.
        for website in db.scalars(
            select(Website)
            .join(Integration, Integration.website_id == Website.id)
            .where(
                Integration.provider == IntegrationProvider.GITHUB,
                Integration.credentials_encrypted.isnot(None),
            )
            .limit(MAX_SECRET_CANDIDATES)
        ):
            secret = resolve_secret(db, website)
            if secret:
                pairs.append((website, secret))

    return pairs


# ── Delivery recording ──────────────────────────────────────────────────────


def existing_delivery(db: Session, delivery_id: str) -> GitHubEvent | None:
    """A previously processed delivery, if GitHub is retrying."""
    return db.scalar(select(GitHubEvent).where(GitHubEvent.delivery_id == delivery_id))


def record_event(
    db: Session,
    *,
    delivery_id: str,
    event_type: str,
    payload: dict[str, Any],
    website: Website | None,
    changed_files: list[str],
) -> GitHubEvent:
    """Persist the delivery before any work is dispatched."""
    repository = (payload.get("repository") or {}).get("full_name")
    branch = branch_from_ref(payload.get("ref"))
    commits = payload.get("commits") or []
    pusher = (payload.get("pusher") or {}).get("name") or (
        payload.get("sender") or {}
    ).get("login")

    event = GitHubEvent(
        website_id=website.id if website else None,
        delivery_id=delivery_id,
        event_type=event_type,
        repository=repository,
        branch=branch,
        before_sha=payload.get("before"),
        after_sha=payload.get("after"),
        pusher=pusher,
        commit_count=len(commits),
        commit_messages=[
            (c.get("message") or "").splitlines()[0][:200]
            for c in commits[:MAX_STORED_COMMIT_MESSAGES]
        ],
        changed_files=changed_files[:MAX_STORED_FILES],
        changed_file_count=len(changed_files),
        raw_payload={k: payload.get(k) for k in RETAINED_PAYLOAD_KEYS if k in payload},
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def finalise_event(
    db: Session,
    event: GitHubEvent,
    *,
    action: str,
    reason: str,
    crawl_run_id: int | None = None,
    affected_urls: list[str] | None = None,
    error: str | None = None,
) -> None:
    event.action_taken = action
    event.action_reason = reason[:2000]
    event.crawl_run_id = crawl_run_id
    event.affected_urls = affected_urls
    event.error = error
    event.processed_at = _now()
    db.commit()


# ── Processing ──────────────────────────────────────────────────────────────


def process_push(
    db: Session,
    *,
    delivery_id: str,
    event_type: str,
    payload: dict[str, Any],
    website: Website | None,
) -> WebhookOutcome:
    """Decide and record what a push should trigger. Dispatch is the caller's job."""
    duplicate = existing_delivery(db, delivery_id)
    if duplicate is not None:
        logger.info("Ignoring duplicate GitHub delivery %s.", delivery_id)
        return WebhookOutcome(
            event_id=duplicate.id,
            action=duplicate.action_taken or "ignored",
            reason="This delivery was already processed.",
            website_id=duplicate.website_id,
            crawl_run_id=duplicate.crawl_run_id,
            duplicate=True,
        )

    changed_files = extract_changed_files(payload)
    event = record_event(
        db,
        delivery_id=delivery_id,
        event_type=event_type,
        payload=payload,
        website=website,
        changed_files=changed_files,
    )

    # ── Cases that do no work ───────────────────────────────────────────────
    if event_type == "ping":
        finalise_event(db, event, action="ignored", reason="Webhook ping received.")
        return WebhookOutcome(event.id, "ignored", "Webhook ping received.",
                              website_id=event.website_id)

    if event_type != "push":
        finalise_event(
            db, event, action="ignored", reason=f"'{event_type}' events are not acted on."
        )
        return WebhookOutcome(event.id, "ignored", f"'{event_type}' events are not acted on.")

    if website is None:
        repository = (payload.get("repository") or {}).get("full_name")
        branch = branch_from_ref(payload.get("ref"))
        reason = (
            f"No active website is mapped to {repository} on branch {branch}. "
            "Connect the repository under the website's integrations to enable re-audits."
        )
        finalise_event(db, event, action="unmatched_repository", reason=reason)
        logger.info("GitHub push for unmapped repository %s (%s).", repository, branch)
        return WebhookOutcome(event.id, "unmatched_repository", reason)

    if payload.get("deleted"):
        finalise_event(db, event, action="ignored", reason="Branch deletion, nothing to re-audit.")
        return WebhookOutcome(event.id, "ignored", "Branch deletion, nothing to re-audit.",
                              website_id=website.id)

    if not website.is_active:
        finalise_event(db, event, action="ignored", reason="The website is not active.")
        return WebhookOutcome(event.id, "ignored", "The website is not active.",
                              website_id=website.id)

    # ── Decide the crawl scope ──────────────────────────────────────────────
    if not changed_files:
        # GitHub omits file lists on very large pushes; unknown is not the same as empty.
        mode, target_urls = CrawlMode.FULL, None
        reason = (
            "The push carried no file list (GitHub omits it for very large pushes), "
            "so the whole site is re-audited."
        )
    else:
        mapping = map_changed_files(
            changed_files,
            framework=website.github_framework,
            path_map=website.github_path_map,
        )

        if not mapping.requires_full_recrawl and not mapping.has_targets:
            finalise_event(db, event, action="ignored", reason=mapping.reason)
            logger.info("GitHub push for website %s changed nothing crawlable.", website.id)
            return WebhookOutcome(event.id, "ignored", mapping.reason, website_id=website.id)

        if mapping.requires_full_recrawl:
            mode, target_urls = CrawlMode.FULL, None
            reason = mapping.reason
        else:
            mode = CrawlMode.INCREMENTAL
            target_urls = resolve_incremental_urls(website, mapping.affected_paths)
            reason = mapping.reason
            if mapping.unmapped_files:
                reason += f" {len(mapping.unmapped_files)} file(s) could not be mapped."

    run = create_crawl_run(
        db,
        website,
        trigger=CrawlTrigger.GITHUB_PUSH,
        mode=mode,
        target_urls=target_urls,
        github_event_id=event.id,
    )

    action = "incremental_crawl" if mode == CrawlMode.INCREMENTAL else "full_crawl"
    finalise_event(
        db, event, action=action, reason=reason,
        crawl_run_id=run.id, affected_urls=target_urls,
    )

    logger.info(
        "GitHub push for website %s triggered a %s crawl (run %s): %s",
        website.id, mode, run.id, reason,
    )
    return WebhookOutcome(
        event_id=event.id,
        action=action,
        reason=reason,
        website_id=website.id,
        crawl_run_id=run.id,
        affected_urls=target_urls,
    )
