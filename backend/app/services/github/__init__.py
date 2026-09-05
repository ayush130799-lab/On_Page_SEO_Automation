"""GitHub integration: webhook verification, file-to-page mapping, re-audit triggering, and
pull-request SEO impact prediction (roadmap §8)."""

from .diff_analyzer import DetectedChange, analyse_file_diff, analyse_pr_diff
from .handler import (
    WebhookOutcome,
    branch_from_ref,
    candidate_secrets,
    find_website,
    process_push,
    resolve_secret,
)
from .mapping import (
    MappingResult,
    extract_changed_files,
    has_global_impact,
    is_ignorable,
    map_changed_files,
    resolve_file,
)
from .pr_handler import PrAnalysisOutcome, process_pull_request
from .prediction import DeploymentPrediction, format_pr_comment, predict_deployment_impact
from .signature import compute_signature, verify_signature

__all__ = [
    "DeploymentPrediction",
    "DetectedChange",
    "MappingResult",
    "PrAnalysisOutcome",
    "WebhookOutcome",
    "analyse_file_diff",
    "analyse_pr_diff",
    "branch_from_ref",
    "candidate_secrets",
    "compute_signature",
    "extract_changed_files",
    "find_website",
    "format_pr_comment",
    "has_global_impact",
    "is_ignorable",
    "map_changed_files",
    "predict_deployment_impact",
    "process_pull_request",
    "process_push",
    "resolve_file",
    "resolve_secret",
    "verify_signature",
]
