"""SQLAlchemy models for the SEO automation platform.

Importing this package registers every mapper against ``Base.metadata``, which is what Alembic
autogeneration and the SQLite test bootstrap both rely on.
"""

from .audit import SEOAudit, SEOIssue
from .intent import KeywordOpportunity, PageIntentProfile
from .base import Base, JSONColumn, TimestampMixin, utcnow
from .competitor import FETCH_STATUSES, CompetitorAnalysis, CompetitorResult
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
from .experiment import (
    ACTUAL_IMPACT_VALUES,
    CHECKPOINT_DAYS,
    EXPERIMENT_STATUSES,
    SeoExperiment,
    SeoExperimentCheckpoint,
)
from .github import GitHubEvent
from .github_analysis import (
    DEPLOYMENT_GATE_MODES,
    EXPECTED_IMPACTS,
    PR_STATES,
    RISK_LEVELS,
    DeploymentAnalysis,
    GitHubChange,
    GitHubCommit,
    GitHubPullRequest,
)
from .integration import Integration
from .job import Job
from .metrics import GA4Metric, GSCMetric, HistoricalMetric, SemrushMetric
from .page import Page
from .priority import PriorityScore
from .recommendation import AIRecommendation
from .recommendation_score import (
    PRIORITY_LEVELS,
    RECOMMENDATION_STATUSES,
    RecommendationScore,
)
from .roadmap import SeoRoadmap
from .setting import SETTING_KEYS, Setting
from .user import User, WebsiteMember
from .website import Website

__all__ = [
    "ACTUAL_IMPACT_VALUES",
    "CHECKPOINT_DAYS",
    "DEPLOYMENT_GATE_MODES",
    "EXPECTED_IMPACTS",
    "EXPERIMENT_STATUSES",
    "FETCH_STATUSES",
    "CompetitorAnalysis",
    "CompetitorResult",
    "SeoExperiment",
    "SeoExperimentCheckpoint",
    "KeywordOpportunity",
    "PRIORITY_LEVELS",
    "PR_STATES",
    "PageIntentProfile",
    "RECOMMENDATION_STATUSES",
    "RISK_LEVELS",
    "RecommendationScore",
    "SEVERITY_RANK",
    "AIRecommendation",
    "AIStatus",
    "Base",
    "CrawlMode",
    "CrawlRun",
    "CrawlTrigger",
    "DeploymentAnalysis",
    "GA4Metric",
    "GSCMetric",
    "GitHubChange",
    "GitHubCommit",
    "GitHubEvent",
    "GitHubPullRequest",
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
    "SeoRoadmap",
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
