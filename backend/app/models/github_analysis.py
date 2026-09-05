"""GitHub Change Analysis data model — roadmap §8 and §11.

Four tables, deliberately *not* five: the roadmap also lists ``github_repositories``, but this
codebase already models "one repository per website" on ``Website`` (``github_repo``,
``github_branch``, ``github_framework``) and its GitHub ``Integration`` row (credentials, config).
A second table holding the same repo/branch/token facts would just be a second place for them to
drift out of sync — the exact class of bug this platform's crawler work spent a long time fixing
elsewhere. Repo-level GitHub App settings (the deployment gate mode) live on the existing
``Integration.config`` JSON instead.

``GitHubPullRequest``  — one row per tracked PR, upserted on every ``opened``/``synchronize`` event.
``GitHubCommit``       — commits belonging to a PR or a push, for the audit trail (§8.1: "authors,
                         timestamps").
``GitHubChange``       — one row per SEO-relevant tag change detected in one file's diff (§8.1's
                         <title>/<h1>/canonical/robots/schema/content-length/internal-links list).
``DeploymentAnalysis``  — the §8.2 Pre-Deployment SEO Prediction for one PR: positive/negative
                         assessments with confidence, risk level, the posted PR comment, and the
                         gate decision. Also the anchor Step 5's post-deployment validation will
                         attach actual-vs-predicted results to.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .page import Page
    from .website import Website

PR_STATES = ("open", "closed", "merged")
DEPLOYMENT_GATE_MODES = ("off", "warn", "block")
RISK_LEVELS = ("low", "medium", "high", "critical")
EXPECTED_IMPACTS = ("positive", "negative", "mixed", "neutral")


class GitHubPullRequest(TimestampMixin, Base):
    """One tracked pull request."""

    __tablename__ = "github_pull_requests"
    __table_args__ = (
        UniqueConstraint("website_id", "number", name="uq_github_pr_website_number"),
        Index("ix_github_pr_website_state", "website_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    author: Mapped[str | None] = mapped_column(String(255))
    #: open | closed | merged
    state: Mapped[str] = mapped_column(String(10), default="open", nullable=False)
    base_branch: Mapped[str | None] = mapped_column(String(255))
    head_branch: Mapped[str | None] = mapped_column(String(255))
    base_sha: Mapped[str | None] = mapped_column(String(64))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    html_url: Mapped[str | None] = mapped_column(String(500))
    opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    merged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: Bumped on every synchronize — how many times this PR's diff has been (re-)analysed.
    analysis_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    website: Mapped["Website"] = relationship()
    commits: Mapped[list["GitHubCommit"]] = relationship(
        back_populates="pull_request", cascade="all, delete-orphan"
    )
    analyses: Mapped[list["DeploymentAnalysis"]] = relationship(
        back_populates="pull_request", cascade="all, delete-orphan"
    )


class GitHubCommit(Base):
    """One commit belonging to a tracked PR or a push."""

    __tablename__ = "github_commits"
    __table_args__ = (
        Index("ix_github_commits_website", "website_id"),
        Index("ix_github_commits_pr", "pull_request_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    pull_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("github_pull_requests.id", ondelete="CASCADE")
    )
    sha: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    author_name: Mapped[str | None] = mapped_column(String(255))
    author_login: Mapped[str | None] = mapped_column(String(255))
    committed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    pull_request: Mapped["GitHubPullRequest | None"] = relationship(back_populates="commits")


class GitHubChange(Base):
    """One SEO-relevant change detected in one file's diff — §8.1's change list.

    Detected from the unified diff GitHub returns for a changed file (the same text a human
    reviewer sees in "Files changed"), not from fetching and fully parsing the rendered document.
    A diff is line-based text, not a DOM, so this is deliberately a pattern-matching scan over
    added/removed lines rather than the crawler's DOM extractor — using DOM parsing on a diff
    fragment would be pretending to a precision the input does not support. ``extraction_method``
    records this plainly rather than implying more confidence than the input allows.
    """

    __tablename__ = "github_changes"
    __table_args__ = (
        Index("ix_github_changes_deployment_analysis", "deployment_analysis_id"),
        Index("ix_github_changes_website", "website_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    deployment_analysis_id: Mapped[int] = mapped_column(
        ForeignKey("deployment_analyses.id", ondelete="CASCADE"), index=True, nullable=False
    )
    page_id: Mapped[int | None] = mapped_column(ForeignKey("pages.id", ondelete="SET NULL"))

    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    #: The URL this file maps to, when the existing file->page mapping resolved one.
    affected_url: Mapped[str | None] = mapped_column(String(2048))

    #: title | h1 | canonical | robots | schema | content_length | internal_links
    change_type: Mapped[str] = mapped_column(String(30), nullable=False)
    before_value: Mapped[str | None] = mapped_column(Text)
    after_value: Mapped[str | None] = mapped_column(Text)
    #: positive | negative | neutral — this one signal's direction, before aggregation.
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    #: How much this signal counted in the prediction, from the impact catalog's ceiling for the
    #: corresponding check_type — the same table Step 1's scoring engine reads, so a "content"
    #: change is not weighted the same as an "image_alt" change here either.
    weight: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    #: diff_heuristic — always, currently. Reserved so a future full-content DOM comparison can
    #: be distinguished from this one without a schema change.
    extraction_method: Mapped[str] = mapped_column(
        String(20), default="diff_heuristic", nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text)

    page: Mapped["Page | None"] = relationship()


class DeploymentAnalysis(TimestampMixin, Base):
    """The §8.2 Pre-Deployment SEO Prediction for one pull request analysis run."""

    __tablename__ = "deployment_analyses"
    __table_args__ = (
        Index("ix_deployment_analyses_website", "website_id"),
        Index("ix_deployment_analyses_pr", "pull_request_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    pull_request_id: Mapped[int] = mapped_column(
        ForeignKey("github_pull_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The commit this analysis was run against — a synchronize event re-runs it at a new SHA.
    head_sha: Mapped[str | None] = mapped_column(String(64))

    # ── §8.2 dual assessment ─────────────────────────────────────────────────
    positive_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    negative_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: positive | negative | mixed | neutral
    expected_impact: Mapped[str] = mapped_column(String(10), nullable=False)
    #: low | medium | high | critical
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False)
    positive_findings: Mapped[list[str]] = mapped_column(JSONColumn, default=list, nullable=False)
    negative_findings: Mapped[list[str]] = mapped_column(JSONColumn, default=list, nullable=False)
    recommendation: Mapped[str | None] = mapped_column(Text)
    suggested_changes: Mapped[list[str]] = mapped_column(JSONColumn, default=list, nullable=False)

    # ── §8.3 posted PR comment ───────────────────────────────────────────────
    comment_body: Mapped[str | None] = mapped_column(Text)
    comment_posted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    comment_error: Mapped[str | None] = mapped_column(Text)

    # ── Deployment gate (§8.2's optional flag/block) ─────────────────────────
    #: off | warn | block — the mode that was active when this analysis ran.
    gate_mode: Mapped[str] = mapped_column(String(10), default="off", nullable=False)
    #: The commit-status state actually posted, when gate_mode != "off": success | failure.
    gate_status_posted: Mapped[str | None] = mapped_column(String(10))

    analysed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    website: Mapped["Website"] = relationship()
    pull_request: Mapped["GitHubPullRequest"] = relationship(back_populates="analyses")
    changes: Mapped[list["GitHubChange"]] = relationship(cascade="all, delete-orphan")
