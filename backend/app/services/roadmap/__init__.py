"""Website-Level SEO Planning — roadmap Feature 4 (§7).

Aggregation only: the priority matrix and roadmap are built entirely from what
``app.services.impact`` and ``app.services.intent`` already scored and persisted. No new external
integrations, no new AI calls.
"""

from .generator import generate_roadmap, latest_roadmap
from .matrix import PriorityMatrixEntry, compute_priority_matrix

__all__ = [
    "PriorityMatrixEntry",
    "compute_priority_matrix",
    "generate_roadmap",
    "latest_roadmap",
]
