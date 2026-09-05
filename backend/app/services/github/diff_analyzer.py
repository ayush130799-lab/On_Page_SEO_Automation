"""SEO-relevant diff analysis — roadmap §8.1's change list, detected from unified diff patches.

Works from the ``patch`` text GitHub's PR-files API already returns for each changed file — the
same unified-diff a human reviewer sees in "Files changed" — rather than fetching and fully
parsing the rendered document at two commits. That is a deliberate choice, not a shortcut taken
for convenience: a diff patch is line-based text, and running a DOM parser against a text fragment
that is not a complete, well-formed document would produce a false sense of precision. Pattern
matching against added/removed lines is the tool that actually fits this input, and it works
uniformly whether the changed file is raw HTML, Markdown, or a JSX/Vue/Astro component — a diff
patch looks the same shape regardless of the source language, whereas a full-DOM parse of a JSX
component is not meaningful at all.

**Known limitation, stated plainly rather than hidden**: a diff patch only shows lines with
changed context. A `<title>` that moves from a JSX conditional branch without any line in its
immediate vicinity changing will not be caught. This is reported to the reader as
``extraction_method="diff_heuristic"`` on every finding, and is exactly the class of limitation
the crawler accuracy work insisted on stating outright rather than glossing over.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..impact import catalog
from .api_client import PullRequestFile
from .mapping import is_ignorable, resolve_file

#: A unified diff line: leading +/-, never ++/-- (the file headers), the rest is content.
_ADDED_LINE = re.compile(r"^\+(?!\+\+)(.*)$", re.MULTILINE)
_REMOVED_LINE = re.compile(r"^-(?!--)(.*)$", re.MULTILINE)

_TITLE_RE = re.compile(
    r"<title[^>]*>\s*(.*?)\s*</title>|(?<![-\w])title\s*:\s*[\"'`]([^\"'`]{1,300})[\"'`]",
    re.IGNORECASE,
)
_H1_RE = re.compile(r"<h1[^>]*>\s*(.*?)\s*</h1>", re.IGNORECASE | re.DOTALL)
_CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']'
    r'|<link[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\']canonical["\']'
    r'|canonical\s*:\s*[\"\'`]([^\"\'`]{1,500})[\"\'`]',
    re.IGNORECASE,
)
_ROBOTS_META_RE = re.compile(
    r'<meta[^>]+name=["\']robots["\'][^>]*content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']robots["\']',
    re.IGNORECASE,
)
_NOINDEX_RE = re.compile(r"\bnoindex\b", re.IGNORECASE)
_JSON_LD_RE = re.compile(r'application/ld\+json', re.IGNORECASE)
_SCHEMA_TYPE_RE = re.compile(r'"@type"\s*:\s*"([^"]+)"')
_INTERNAL_HREF_RE = re.compile(r'href=["\']\/(?!\/)[^"\']*["\']', re.IGNORECASE)


@dataclass(slots=True)
class DetectedChange:
    file_path: str
    affected_url: str | None
    change_type: str  # title | h1 | canonical | robots | schema | content_length | internal_links
    before_value: str | None
    after_value: str | None
    direction: str  # positive | negative | neutral
    weight: float
    description: str


def _lines(pattern: re.Pattern[str], patch: str) -> str:
    return "\n".join(m.group(1) for m in pattern.finditer(patch))


def _first_match(text: str, pattern: re.Pattern[str]) -> str | None:
    m = pattern.search(text)
    if not m:
        return None
    for group in m.groups():
        if group:
            return re.sub(r"\s+", " ", group).strip()
    return None


def _weight_for(check_type: str) -> float:
    """Reuse the Step 1 impact catalog's ceiling so a title change is weighted the way the
    scoring engine already treats title changes elsewhere, rather than inventing a second,
    disagreeing notion of how much each tag matters."""
    return catalog.get(check_type).ceiling


def _tag_change(
    file_path: str,
    url: str | None,
    change_type: str,
    added_text: str,
    removed_text: str,
    pattern: re.Pattern[str],
    *,
    positive_when_added: bool = True,
) -> DetectedChange | None:
    """Generic before/after diff for a single-value tag (title, canonical, …)."""
    before = _first_match(removed_text, pattern)
    after = _first_match(added_text, pattern)

    if before is None and after is None:
        return None
    if before == after:
        return None

    weight = _weight_for(change_type)
    if before and not after:
        direction = "negative"
        description = f"{change_type.replace('_', ' ').title()} removed: was \"{before[:120]}\"."
    elif after and not before:
        direction = "positive" if positive_when_added else "negative"
        description = f"{change_type.replace('_', ' ').title()} added: \"{after[:120]}\"."
    else:
        # Both present but different — a rewrite. Direction is genuinely ambiguous from text
        # alone (a rewritten title could be better or worse), so this is reported neutral with
        # low weight rather than guessed at.
        direction = "neutral"
        weight *= 0.4
        description = (
            f"{change_type.replace('_', ' ').title()} changed from \"{(before or '')[:80]}\" "
            f"to \"{(after or '')[:80]}\"."
        )

    return DetectedChange(
        file_path=file_path, affected_url=url, change_type=change_type,
        before_value=before, after_value=after, direction=direction,
        weight=weight, description=description,
    )


def _robots_change(
    file_path: str, url: str | None, added_text: str, removed_text: str
) -> DetectedChange | None:
    before = _first_match(removed_text, _ROBOTS_META_RE)
    after = _first_match(added_text, _ROBOTS_META_RE)
    before_noindex = bool(before and _NOINDEX_RE.search(before))
    after_noindex = bool(after and _NOINDEX_RE.search(after))

    if before_noindex == after_noindex and before == after:
        return None

    weight = _weight_for("robots")
    if after_noindex and not before_noindex:
        return DetectedChange(
            file_path, url, "robots", before, after, "negative", weight,
            "A noindex directive was added — this page would be removed from search results.",
        )
    if before_noindex and not after_noindex:
        return DetectedChange(
            file_path, url, "robots", before, after, "positive", weight,
            "A noindex directive was removed — this page becomes indexable again.",
        )
    if before != after:
        return DetectedChange(
            file_path, url, "robots", before, after, "neutral", weight * 0.3,
            f"Robots meta content changed from \"{before}\" to \"{after}\".",
        )
    return None


def _schema_change(
    file_path: str, url: str | None, added_text: str, removed_text: str
) -> DetectedChange | None:
    had_json_ld = bool(_JSON_LD_RE.search(removed_text))
    has_json_ld = bool(_JSON_LD_RE.search(added_text))
    before_types = sorted(set(_SCHEMA_TYPE_RE.findall(removed_text)))
    after_types = sorted(set(_SCHEMA_TYPE_RE.findall(added_text)))

    if not had_json_ld and not has_json_ld and before_types == after_types:
        return None

    weight = _weight_for("structured_data")
    if has_json_ld and not had_json_ld:
        return DetectedChange(
            file_path, url, "schema", None, ", ".join(after_types) or "structured data",
            "positive", weight,
            f"Structured data added ({', '.join(after_types) or 'JSON-LD block'}).",
        )
    if had_json_ld and not has_json_ld:
        return DetectedChange(
            file_path, url, "schema", ", ".join(before_types) or "structured data", None,
            "negative", weight,
            f"Structured data removed ({', '.join(before_types) or 'JSON-LD block'}).",
        )
    if before_types != after_types:
        removed = set(before_types) - set(after_types)
        added = set(after_types) - set(before_types)
        direction = "negative" if removed and not added else "neutral"
        return DetectedChange(
            file_path, url, "schema", ", ".join(before_types), ", ".join(after_types),
            direction, weight * (1.0 if direction == "negative" else 0.3),
            f"Structured data types changed: removed {sorted(removed) or 'none'}, "
            f"added {sorted(added) or 'none'}.",
        )
    return None


def _content_length_change(
    file_path: str, url: str | None, file: PullRequestFile
) -> DetectedChange | None:
    """Uses GitHub's own additions/deletions counts rather than re-deriving them from the patch
    text — those are exact line counts for the whole file change, which a partial patch (GitHub
    truncates very large diffs) might not fully represent if parsed by hand."""
    total = file.additions + file.deletions
    if total < 10:
        return None
    net = file.additions - file.deletions
    ratio = file.deletions / total if total else 0.0

    if ratio >= 0.6 and file.deletions >= 15:
        weight = _weight_for("content")
        return DetectedChange(
            file_path, url, "content_length",
            f"{file.deletions} lines", f"{file.additions} lines", "negative",
            weight * min(1.0, ratio),
            f"Content reduced substantially: {file.deletions} lines removed, "
            f"{file.additions} added (net {net:+d}).",
        )
    return None


def _internal_links_change(
    file_path: str, url: str | None, added_text: str, removed_text: str
) -> DetectedChange | None:
    before_count = len(_INTERNAL_HREF_RE.findall(removed_text))
    after_count = len(_INTERNAL_HREF_RE.findall(added_text))
    delta = after_count - before_count
    if abs(delta) < 2:
        return None

    weight = _weight_for("internal_links")
    direction = "negative" if delta < 0 else "positive"
    return DetectedChange(
        file_path, url, "internal_links", str(before_count), str(after_count), direction,
        weight * min(1.0, abs(delta) / 5.0),
        f"Internal links {'decreased' if delta < 0 else 'increased'} by {abs(delta)} "
        f"({before_count} -> {after_count}).",
    )


def analyse_file_diff(
    file: PullRequestFile, *, framework: str | None, path_map: dict[str, str] | None
) -> list[DetectedChange]:
    """Every SEO-relevant change detected in one file's patch."""
    if not file.patch or is_ignorable(file.filename):
        return []

    added = _lines(_ADDED_LINE, file.patch)
    removed = _lines(_REMOVED_LINE, file.patch)

    explicit = {k.strip("/"): v for k, v in (path_map or {}).items()}
    url = explicit.get(file.filename.strip("/")) or resolve_file(file.filename, framework)

    changes: list[DetectedChange] = []
    for change in (
        _tag_change(file.filename, url, "title", added, removed, _TITLE_RE),
        _tag_change(file.filename, url, "h1", added, removed, _H1_RE),
        _tag_change(file.filename, url, "canonical", added, removed, _CANONICAL_RE),
        _robots_change(file.filename, url, added, removed),
        _schema_change(file.filename, url, added, removed),
        _content_length_change(file.filename, url, file),
        _internal_links_change(file.filename, url, added, removed),
    ):
        if change is not None:
            changes.append(change)
    return changes


def analyse_pr_diff(
    files: list[PullRequestFile], *, framework: str | None, path_map: dict[str, str] | None
) -> list[DetectedChange]:
    """Every SEO-relevant change across an entire PR."""
    changes: list[DetectedChange] = []
    for file in files:
        changes.extend(analyse_file_diff(file, framework=framework, path_map=path_map))
    return changes
