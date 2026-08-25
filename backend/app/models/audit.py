"""Per-page SEO audit results and the individual issues they contain."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime
from .enums import Severity

if TYPE_CHECKING:
    from .crawl import CrawlRun
    from .page import Page


class SEOAudit(TimestampMixin, Base):
    """The result of running the rule engine against one page during one crawl run."""

    __tablename__ = "seo_audits"
    __table_args__ = (
        UniqueConstraint("crawl_run_id", "page_id", name="uq_seo_audit_run_page"),
        Index("ix_seo_audits_page_created", "page_id", "created_at"),
        Index("ix_seo_audits_run_score", "crawl_run_id", "seo_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    crawl_run_id: Mapped[int] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True, nullable=False
    )

    seo_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    category: Mapped[str | None] = mapped_column(String(30), index=True)
    highest_severity: Mapped[str] = mapped_column(
        String(20), default=Severity.NONE, nullable=False, index=True
    )
    issue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    critical_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    high_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    medium_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    low_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Per-check results: ``[{"check": "title", "status": "pass", "score": 100, "details": …}]``.
    checks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONColumn)
    #: The weight vector used, so a historical score stays explainable after a config change.
    weights_snapshot: Mapped[dict[str, float] | None] = mapped_column(JSONColumn)

    status_code: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    audited_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    crawl_run: Mapped["CrawlRun"] = relationship(back_populates="audits")
    page: Mapped["Page"] = relationship(back_populates="audits")
    issues: Mapped[list["SEOIssue"]] = relationship(
        back_populates="audit", cascade="all, delete-orphan"
    )


class SEOIssue(TimestampMixin, Base):
    """One detected problem. ``rule_id`` links back to the rule registry entry."""

    __tablename__ = "seo_issues"
    __table_args__ = (
        Index("ix_seo_issues_page_severity", "page_id", "severity"),
        Index("ix_seo_issues_audit_severity", "seo_audit_id", "severity"),
        Index("ix_seo_issues_rule", "rule_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    seo_audit_id: Mapped[int] = mapped_column(
        ForeignKey("seo_audits.id", ondelete="CASCADE"), index=True, nullable=False
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True, nullable=False
    )

    rule_id: Mapped[str] = mapped_column(String(100), nullable=False)
    check_type: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str | None] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str | None] = mapped_column(Text)
    #: Rule-specific supporting data (offending values, counts, sample URLs).
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    source: Mapped[str] = mapped_column(String(30), default="rule_engine", nullable=False)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    audit: Mapped["SEOAudit"] = relationship(back_populates="issues")
    page: Mapped["Page"] = relationship(back_populates="issues")
