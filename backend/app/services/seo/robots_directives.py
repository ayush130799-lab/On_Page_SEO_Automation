"""Parsing of robots directives from the meta tag and the ``X-Robots-Tag`` header.

Substring matching does not work here and previously produced false positives that made healthy
sites look broken:

* ``max-image-preview:none`` contains ``none`` — it is not ``robots: none``;
* ``nosnippet`` and ``noarchive`` contain neither ``noindex`` nor ``nofollow``, but a naive
  ``in`` test over a concatenated string is one careless edit away from matching them;
* ``X-Robots-Tag: bingbot: noindex`` is scoped to another crawler and says nothing about how
  Google will treat the page.

So directives are tokenised, key/value pairs are separated from bare directives, and
user-agent-scoped groups are only applied when they address us (or all crawlers).

Reference: https://developers.google.com/search/docs/crawling-indexing/robots-meta-tag
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Directives that take a value; their values must never be matched as bare directives.
VALUED_DIRECTIVES = {
    "max-snippet",
    "max-image-preview",
    "max-video-preview",
    "unavailable_after",
}

#: Bare directives we recognise. Anything else is preserved but not acted on.
KNOWN_DIRECTIVES = {
    "all",
    "index",
    "noindex",
    "follow",
    "nofollow",
    "none",
    "noarchive",
    "nosnippet",
    "noimageindex",
    "notranslate",
    "nositelinkssearchbox",
    "indexifembedded",
}

#: User-agent tokens a directive group may be addressed to for it to bind our analysis. We report
#: what a general search crawler would do, so Google's tokens and the wildcard both count.
APPLICABLE_AGENTS = {"*", "robots", "googlebot", "googlebot-news", "google"}

_SPLIT_RE = re.compile(r"[,\n]")


@dataclass
class RobotsDirectives:
    """The effective directives for a page, merged from meta and header sources."""

    directives: set[str] = field(default_factory=set)
    values: dict[str, str] = field(default_factory=dict)
    #: Groups addressed to other crawlers, kept for reporting but not applied.
    ignored_agent_groups: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    raw: dict[str, str | None] = field(default_factory=dict)

    @property
    def noindex(self) -> bool:
        """``noindex`` or ``none`` — the page is excluded from the index."""
        return "noindex" in self.directives or "none" in self.directives

    @property
    def nofollow(self) -> bool:
        """``nofollow`` or ``none`` — links on the page pass no signal."""
        return "nofollow" in self.directives or "none" in self.directives

    @property
    def indexable(self) -> bool:
        return not self.noindex

    def as_evidence(self) -> dict[str, object]:
        """Everything needed to explain the verdict, for the issue's evidence payload."""
        return {
            "directives": sorted(self.directives),
            "values": self.values,
            "meta_robots": self.raw.get("meta"),
            "x_robots_tag": self.raw.get("header"),
            "applied_from": self.sources,
            "ignored_agent_groups": self.ignored_agent_groups,
        }


def _parse_tokens(value: str) -> tuple[set[str], dict[str, str]]:
    """Split one directive list into bare directives and key/value pairs."""
    directives: set[str] = set()
    values: dict[str, str] = {}

    for token in _SPLIT_RE.split(value):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            key, _, val = token.partition(":")
            key = key.strip().lower()
            if key in VALUED_DIRECTIVES:
                # A valued directive: its value is data, never a directive in its own right.
                values[key] = val.strip()
                continue
            # Not a known valued directive — treat the whole token as a bare directive so
            # unrecognised syntax is preserved rather than silently reinterpreted.
            directives.add(token.lower())
            continue
        directives.add(token.lower())

    return directives, values


def parse_directive_list(value: str | None, *, source: str) -> RobotsDirectives:
    """Parse a single ``robots`` meta content or ``X-Robots-Tag`` header value.

    An ``X-Robots-Tag`` may be user-agent scoped (``googlebot: noindex``). A group addressed to a
    crawler that is not us is recorded but not applied.
    """
    result = RobotsDirectives()
    if not value or not value.strip():
        return result

    result.raw[source] = value

    for chunk in value.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue

        agent = None
        payload = chunk

        # A leading "agent:" prefix scopes the group — but only when what follows the colon is
        # not itself a valued directive (so "max-snippet: -1" is never read as an agent).
        head, sep, tail = chunk.partition(":")
        head_token = head.strip().lower()
        if sep and head_token not in VALUED_DIRECTIVES and head_token not in KNOWN_DIRECTIVES:
            # Looks like a user-agent prefix rather than a directive.
            if head_token and " " not in head_token:
                agent = head_token
                payload = tail

        if agent is not None and agent not in APPLICABLE_AGENTS:
            result.ignored_agent_groups.append(agent)
            continue

        directives, values = _parse_tokens(payload)
        if directives or values:
            for directive in directives:
                result.sources.setdefault(directive, source)
        result.directives |= directives
        result.values.update(values)

    return result


def resolve(meta_robots: str | None, x_robots_tag: str | None) -> RobotsDirectives:
    """Merge the meta tag and the header into one effective directive set.

    Google treats the two as cumulative and applies the most restrictive outcome, so the union is
    taken rather than one overriding the other.
    """
    merged = RobotsDirectives()

    for value, source in ((meta_robots, "meta"), (x_robots_tag, "header")):
        parsed = parse_directive_list(value, source=source)
        merged.directives |= parsed.directives
        merged.values.update(parsed.values)
        merged.ignored_agent_groups.extend(parsed.ignored_agent_groups)
        merged.sources.update(parsed.sources)
        if value:
            merged.raw[source] = value

    merged.raw.setdefault("meta", meta_robots)
    merged.raw.setdefault("header", x_robots_tag)
    return merged


def describe(directives: RobotsDirectives) -> str:
    """A short human-readable summary for an issue description."""
    if not directives.directives and not directives.values:
        return "none"
    parts = sorted(directives.directives)
    parts += [f"{k}:{v}" for k, v in sorted(directives.values.items())]
    return ", ".join(parts)
