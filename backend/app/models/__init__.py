"""SQLAlchemy models for the SEO automation platform.

Importing this package registers every mapper against ``Base.metadata``, which is what Alembic
autogeneration and the SQLite test bootstrap both rely on.
"""

from .audit import SEOAudit, SEOIssue
from .base import Base, JSONColumn, TimestampMixin, utcnow
from .crawl import CrawlRun
from .enums import (
    SEVERITY_RANK,
    AIStatus,
    CrawlMode,
    CrawlTrigger,
    IntegrationProvider,
    IntegrationStatus,
    IssueCategory,
    JobType,
    MemberRole,
    RunStatus,
    Severity,
    UserRole,
    severity_rank,
)
from .github import GitHubEvent
from .integration import Integration
from .job import Job
from .metrics import GA4Metric, GSCMetric, HistoricalMetric, SemrushMetric
from .page import Page
from .priority import PriorityScore
from .recommendation import AIRecommendation
from .setting import SETTING_KEYS, Setting
from .user import User, WebsiteMember
from .website import Website

__all__ = [
    "SEVERITY_RANK",
    "AIRecommendation",
    "AIStatus",
    "Base",
    "CrawlMode",
    "CrawlRun",
    "CrawlTrigger",
    "GA4Metric",
    "GSCMetric",
    "GitHubEvent",
    "HistoricalMetric",
    "Integration",
    "IntegrationProvider",
    "IntegrationStatus",
    "IssueCategory",
    "JSONColumn",
    "Job",
    "JobType",
    "MemberRole",
    "Page",
    "PriorityScore",
    "RunStatus",
    "SETTING_KEYS",
    "SEOAudit",
    "SEOIssue",
    "SemrushMetric",
    "Setting",
    "Severity",
    "TimestampMixin",
    "User",
    "UserRole",
    "Website",
    "WebsiteMember",
    "severity_rank",
    "utcnow",
]
