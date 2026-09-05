"""Authenticated GitHub REST API calls: PR file diffs, PR comments, commit statuses.

Every call needs write access (posting a comment, setting a commit status) or read access beyond
what a webhook payload carries (a PR's changed files), so this requires a token — unlike the
webhook receiver, which needs only the shared signing secret.

**Scope note on "GitHub App".** A true GitHub App (JWT-signed app identity, per-installation
access tokens, comments posted as the app's own bot user) requires the app to be registered in
GitHub's UI first — an app ID, a generated private key, and an installation step the user must
perform there; there is no way to do that from here. What is built instead is the same
architecture the roadmap asks for — webhook-driven, one repo maps to one website, no per-repo CI
YAML — authenticated with a personal access token (or fine-grained token) stored the same
encrypted way the webhook secret already is. Upgrading to true GitHub App installation tokens
later is a drop-in change to :func:`get_access_token` alone; nothing else in this module or its
callers would need to change.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ...config import settings
from ...models import Integration, IntegrationProvider, Website
from ..integrations.base import read_credentials

logger = logging.getLogger(__name__)


class GitHubApiError(Exception):
    """A GitHub API call failed in a way the caller should know about."""


@dataclass(slots=True)
class PullRequestFile:
    filename: str
    status: str  # added | modified | removed | renamed
    patch: str | None  # unified diff text; absent for binary files or very large diffs
    additions: int
    deletions: int


def get_access_token(db, website: Website) -> str | None:
    """The stored token for this website's GitHub integration, or ``None`` if unconfigured.

    Never raises — a missing token is a normal, expected state (many repos are connected for
    push-triggered re-crawls only, without PR analysis), and every caller here treats it as
    "skip the API-dependent step" rather than a failure.
    """
    integration = db.query(Integration).filter(
        Integration.website_id == website.id,
        Integration.provider == IntegrationProvider.GITHUB,
    ).one_or_none()
    if integration is None or not integration.credentials_encrypted:
        return None
    try:
        return read_credentials(integration).get("access_token") or None
    except Exception as exc:
        logger.warning("Could not read GitHub access token for website %s: %s", website.id, exc)
        return None


def _client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.github_api_base,
        timeout=settings.github_api_timeout,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "seo-automation-platform",
        },
    )


async def list_pull_request_files(
    token: str, repo: str, number: int, *, max_files: int = 300
) -> list[PullRequestFile]:
    """Every changed file in a PR, with its unified diff patch where GitHub provides one."""
    files: list[PullRequestFile] = []
    page = 1
    async with _client(token) as client:
        while len(files) < max_files:
            resp = await client.get(
                f"/repos/{repo}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            )
            if resp.status_code != 200:
                raise GitHubApiError(
                    f"Listing files for {repo}#{number} failed: HTTP {resp.status_code}"
                )
            batch = resp.json()
            if not batch:
                break
            for item in batch:
                files.append(PullRequestFile(
                    filename=item["filename"],
                    status=item.get("status", "modified"),
                    patch=item.get("patch"),
                    additions=item.get("additions", 0),
                    deletions=item.get("deletions", 0),
                ))
            if len(batch) < 100:
                break
            page += 1
    return files[:max_files]


async def get_pull_request(token: str, repo: str, number: int) -> dict[str, Any]:
    async with _client(token) as client:
        resp = await client.get(f"/repos/{repo}/pulls/{number}")
        if resp.status_code != 200:
            raise GitHubApiError(f"Fetching PR {repo}#{number} failed: HTTP {resp.status_code}")
        return resp.json()


async def get_file_content(
    token: str, repo: str, path: str, ref: str
) -> str | None:
    """A file's text content at a specific ref, or ``None`` if it doesn't exist there.

    Used sparingly — the diff analyzer works from PR-file patches, not full file fetches — but
    kept available for the cases a patch is absent (binary flag, GitHub's own patch-size cutoff).
    """
    async with _client(token) as client:
        resp = await client.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise GitHubApiError(
                f"Fetching {path}@{ref[:8]} in {repo} failed: HTTP {resp.status_code}"
            )
        data = resp.json()
        if data.get("encoding") != "base64" or "content" not in data:
            return None
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception:
            return None


async def post_pr_comment(token: str, repo: str, number: int, body: str) -> int | None:
    """Post a PR comment; returns the comment id, or ``None`` if posting failed.

    Failure is logged and swallowed rather than raised — a broken comment post must never turn
    an otherwise-successful analysis into a failed webhook delivery.
    """
    try:
        async with _client(token) as client:
            resp = await client.post(
                f"/repos/{repo}/issues/{number}/comments", json={"body": body}
            )
            if resp.status_code not in (200, 201):
                logger.warning(
                    "Posting PR comment to %s#%d failed: HTTP %d %s",
                    repo, number, resp.status_code, resp.text[:300],
                )
                return None
            return resp.json().get("id")
    except httpx.HTTPError as exc:
        logger.warning("Posting PR comment to %s#%d failed: %s", repo, number, exc)
        return None


async def post_commit_status(
    token: str, repo: str, sha: str, *, state: str, description: str, target_url: str | None,
    context: str = "seo-automation/impact-prediction",
) -> bool:
    """Set a commit status GitHub's branch protection can require. Returns success as bool."""
    try:
        async with _client(token) as client:
            resp = await client.post(
                f"/repos/{repo}/statuses/{sha}",
                json={
                    "state": state,
                    "description": description[:140],
                    "context": context,
                    **({"target_url": target_url} if target_url else {}),
                },
            )
            if resp.status_code not in (200, 201):
                logger.warning(
                    "Posting commit status to %s@%s failed: HTTP %d %s",
                    repo, sha[:8], resp.status_code, resp.text[:300],
                )
                return False
            return True
    except httpx.HTTPError as exc:
        logger.warning("Posting commit status to %s@%s failed: %s", repo, sha[:8], exc)
        return False
