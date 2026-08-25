"""String enumerations shared by models, services and API schemas.

Plain ``str`` enums are used (rather than native DB enums) so that adding a value never requires a
migration — important for the rule registry and integration providers, which are designed to grow.
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class MemberRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class IntegrationProvider(StrEnum):
    GSC = "gsc"
    GA4 = "ga4"
    SEMRUSH = "semrush"
    GITHUB = "github"


class IntegrationStatus(StrEnum):
    NOT_CONNECTED = "not_connected"
    CONNECTED = "connected"
    ERROR = "error"
    EXPIRED = "expired"
    SYNCING = "syncing"


class CrawlTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    GITHUB_PUSH = "github_push"
    API = "api"


class CrawlMode(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


class IssueCategory(StrEnum):
    INDEXABILITY = "indexability"
    METADATA = "metadata"
    HEADINGS = "headings"
    CONTENT = "content"
    IMAGES = "images"
    LINKS = "links"
    STRUCTURED_DATA = "structured_data"
    PERFORMANCE = "performance"
    INTERNATIONAL = "international"


class AIStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CACHED = "cached"


class JobType(StrEnum):
    CRAWL = "crawl"
    INCREMENTAL_CRAWL = "incremental_crawl"
    GSC_SYNC = "gsc_sync"
    GA4_SYNC = "ga4_sync"
    SEMRUSH_SYNC = "semrush_sync"
    PRIORITY_SCORING = "priority_scoring"
    AI_ANALYSIS = "ai_analysis"
    ROLLUP = "rollup"


#: Numeric ranking used for ordering and for the severity component of the priority score.
SEVERITY_RANK: dict[str, int] = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.NONE: 0,
}


def severity_rank(severity: str | None) -> int:
    """Return the numeric rank of a severity string (0 for unknown/none)."""
    return SEVERITY_RANK.get((severity or "").upper(), 0)
