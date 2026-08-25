"""Matching provider rows back to crawled pages.

Providers report URLs in shapes the crawler never produced: GA4 returns bare paths, Search Console
returns absolute URLs that may differ in trailing slash, ``www.`` prefix or protocol. This resolver
indexes a website's pages once per sync and then answers lookups from memory — a per-row database
query would dominate a 10 000-page sync.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Page
from ...utils.url_utils import normalize_url, url_hash, url_path

logger = logging.getLogger(__name__)


@dataclass
class PageResolver:
    """In-memory index of one website's pages, keyed several ways."""

    website_id: int
    base_url: str
    by_hash: dict[str, int] = field(default_factory=dict)
    by_path: dict[str, int] = field(default_factory=dict)
    matched: int = 0
    unmatched: int = 0
    _unmatched_samples: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, db: Session, website_id: int, base_url: str) -> "PageResolver":
        resolver = cls(website_id=website_id, base_url=base_url.rstrip("/"))
        rows = db.execute(
            select(Page.id, Page.url, Page.url_hash, Page.path).where(
                Page.website_id == website_id
            )
        ).all()
        for page_id, url, digest, path in rows:
            resolver.by_hash[digest] = page_id
            resolver.by_path.setdefault(_normalise_path(path), page_id)
            # Also index the final URL's path, which can differ after a redirect.
            resolver.by_path.setdefault(_normalise_path(url_path(url)), page_id)
        logger.debug("PageResolver indexed %d pages for website %s.", len(rows), website_id)
        return resolver

    def resolve(self, raw: str) -> int | None:
        """Return the page id for a provider-supplied URL or path, or ``None``."""
        if not raw:
            self.unmatched += 1
            return None

        candidate = raw.strip()
        if candidate.startswith(("http://", "https://")):
            page_id = self.by_hash.get(url_hash(candidate))
            if page_id is not None:
                self.matched += 1
                return page_id
            # Fall back to the path: protocol or www differences are common between providers.
            candidate = url_path(candidate)
        elif not candidate.startswith("/"):
            candidate = "/" + candidate

        page_id = self.by_path.get(_normalise_path(candidate))
        if page_id is not None:
            self.matched += 1
            return page_id

        # Last resort: rebuild an absolute URL against the site root and hash it.
        page_id = self.by_hash.get(url_hash(f"{self.base_url}{candidate}"))
        if page_id is not None:
            self.matched += 1
            return page_id

        self.unmatched += 1
        if len(self._unmatched_samples) < 10:
            self._unmatched_samples.append(raw)
        return None

    @property
    def summary(self) -> dict[str, object]:
        return {
            "matched": self.matched,
            "unmatched": self.unmatched,
            "unmatched_samples": self._unmatched_samples,
        }


def _normalise_path(path: str) -> str:
    """Collapse the trailing-slash and case differences providers disagree about."""
    if not path:
        return "/"
    path = path.split("#", 1)[0]
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return path.lower()


def site_url_variants(website_url: str) -> list[str]:
    """Plausible Search Console property identifiers for a website URL.

    Search Console properties come in three flavours (domain property, https prefix, http prefix)
    and only an exact match works, so every variant is offered when auto-detecting.
    """
    normalised = normalize_url(website_url)
    from ...utils.url_utils import domain_of

    host = domain_of(normalised)
    bare = host.removeprefix("www.")

    return list(
        dict.fromkeys(
            [
                f"sc-domain:{bare}",
                f"https://{host}/",
                f"https://www.{bare}/",
                f"https://{bare}/",
                f"http://{host}/",
            ]
        )
    )
