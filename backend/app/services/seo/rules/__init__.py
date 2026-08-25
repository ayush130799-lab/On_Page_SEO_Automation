"""Importing this package registers every SEO rule on the shared registry.

Add a new rule by creating (or extending) a module here and importing it below — nothing else in
the system needs to change.
"""

from . import duplication, indexability, media_links, metadata, structure, technical  # noqa: F401

__all__ = [
    "duplication",
    "indexability",
    "media_links",
    "metadata",
    "structure",
    "technical",
]
