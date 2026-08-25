"""robots.txt fetching and rule evaluation.

Python's ``urllib.robotparser`` is avoided because it performs its own blocking fetch and does not
expose ``Sitemap:`` directives. This implementation parses the text we already fetched
asynchronously and applies the longest-match precedence rule that Google documents.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

import httpx

logger = logging.getLogger(__name__)


@dataclass
class RobotsRules:
    """Parsed robots.txt for one origin."""

    allow: list[str] = field(default_factory=list)
    disallow: list[str] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    #: True when robots.txt was missing or unreadable — everything is then permitted.
    fetched: bool = False

    def is_allowed(self, url: str) -> bool:
        """Longest-matching rule wins; ties resolve in favour of Allow."""
        if not self.fetched or not self.disallow:
            return True

        path = unquote(urlparse(url).path or "/")
        query = urlparse(url).query
        if query:
            path = f"{path}?{query}"

        best_allow = max(
            (len(p) for p in self.allow if _path_matches(path, p)), default=-1
        )
        best_disallow = max(
            (len(p) for p in self.disallow if _path_matches(path, p)), default=-1
        )

        if best_disallow < 0:
            return True
        return best_allow >= best_disallow


def _path_matches(path: str, pattern: str) -> bool:
    """Match a path against a robots pattern supporting ``*`` and a trailing ``$``."""
    if pattern == "":
        return False
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]

    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    return bool(re.match(regex + ("$" if anchored else ""), path))


def parse_robots(text: str, user_agent: str = "*") -> RobotsRules:
    """Parse robots.txt, preferring a block for ``user_agent`` over the ``*`` block."""
    rules = RobotsRules(fetched=True)

    ua_token = user_agent.split("/")[0].strip().lower()
    groups: dict[str, RobotsRules] = {}
    current_agents: list[str] = []
    previous_was_agent = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()

        if directive == "sitemap":
            if value:
                rules.sitemaps.append(value)
            continue

        if directive == "user-agent":
            # Consecutive user-agent lines share one rule block.
            if not previous_was_agent:
                current_agents = []
            current_agents.append(value.lower())
            groups.setdefault(value.lower(), RobotsRules(fetched=True))
            previous_was_agent = True
            continue

        previous_was_agent = False
        if not current_agents:
            continue

        for agent in current_agents:
            group = groups[agent]
            if directive == "disallow":
                group.disallow.append(value)
            elif directive == "allow":
                group.allow.append(value)
            elif directive == "crawl-delay":
                try:
                    group.crawl_delay = float(value)
                except ValueError:
                    pass

    # A block naming our crawler wins; otherwise fall back to the wildcard block.
    chosen = None
    for agent, group in groups.items():
        if agent and agent != "*" and agent in ua_token:
            chosen = group
            break
    if chosen is None:
        chosen = groups.get("*")

    if chosen is not None:
        rules.allow = chosen.allow
        # An empty Disallow value means "allow everything" and must not block the crawl.
        rules.disallow = [p for p in chosen.disallow if p]
        rules.crawl_delay = chosen.crawl_delay

    return rules


async def fetch_robots(
    client: httpx.AsyncClient, origin: str, user_agent: str = "*", timeout: float = 8.0
) -> RobotsRules:
    """Fetch and parse ``{origin}/robots.txt``.

    A missing or failed robots.txt yields permissive rules — the standard interpretation, and the
    safe one for a site the company owns.
    """
    url = f"{origin.rstrip('/')}/robots.txt"
    try:
        response = await client.get(url, timeout=timeout)
        if response.status_code == 200 and response.text:
            rules = parse_robots(response.text, user_agent)
            logger.debug(
                "robots.txt for %s: %d disallow rules, %d sitemaps",
                origin, len(rules.disallow), len(rules.sitemaps),
            )
            return rules
        logger.debug("robots.txt at %s returned HTTP %s", url, response.status_code)
    except Exception as exc:
        logger.debug("Could not fetch robots.txt at %s: %s", url, exc)
    return RobotsRules(fetched=False)
