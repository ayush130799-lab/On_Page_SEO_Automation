"""GitHub integration: webhook verification, file-to-page mapping and re-audit triggering."""

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
from .signature import compute_signature, verify_signature

__all__ = [
    "MappingResult",
    "WebhookOutcome",
    "branch_from_ref",
    "candidate_secrets",
    "compute_signature",
    "extract_changed_files",
    "find_website",
    "has_global_impact",
    "is_ignorable",
    "map_changed_files",
    "process_push",
    "resolve_file",
    "resolve_secret",
    "verify_signature",
]
