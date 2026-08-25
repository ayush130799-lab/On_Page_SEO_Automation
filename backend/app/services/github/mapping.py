"""Mapping changed source files to the pages they affect.

The specification allows starting with "if precise mapping is hard, re-audit the whole site", and
that remains the fallback — but a full re-crawl of a 10 000-page site after a one-line copy change
is expensive enough that it is worth resolving what can be resolved.

The strategy is deliberately conservative:

1. An explicit ``github_path_map`` on the website always wins.
2. A file that can affect *many* pages (layout, template, config, component, stylesheet) forces a
   full re-audit — narrowing to a handful of pages there would silently under-report.
3. Otherwise, framework routing conventions turn a route file into a URL path.
4. Anything unrecognised is reported as unmapped; if nothing at all maps, we fall back to a full
   re-audit rather than doing nothing.

Resolvers are registered in a dict so a new framework is one function, and the webhook path never
changes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

#: Changing any of these can alter every rendered page, so they force a full re-audit.
GLOBAL_IMPACT_PATTERNS = (
    r"(^|/)(_app|_document|_layout|layout|template|root)\.[jt]sx?$",
    r"(^|/)layouts?/",
    r"(^|/)components?/",
    r"(^|/)partials?/",
    r"(^|/)includes?/",
    r"(^|/)theme/",
    r"(^|/)(head|seo|meta|metadata|sitemap|robots)\.[jt]sx?$",
    r"(^|/)(next|nuxt|astro|gatsby|svelte|vite|remix)\.config\.[jtm]?s$",
    r"(^|/)(config|_config)\.(ya?ml|toml|json|js|ts)$",
    r"(^|/)(tailwind|postcss)\.config\.[jtm]?s$",
    r"(^|/)package\.json$",
    r"\.(css|scss|sass|less)$",
    r"(^|/)middleware\.[jt]s$",
    r"(^|/)robots\.txt$",
    r"(^|/)sitemap\.xml$",
    r"(^|/)\.htaccess$",
    r"(^|/)(netlify|vercel)\.toml$",
    r"(^|/)nginx\.conf$",
)

#: Files that cannot affect rendered output at all — ignored entirely.
#:
#: Markdown is deliberately **not** ignored as a class: on Hugo, Jekyll, Astro and every content
#: collection, a `.md` file *is* the page. Only repository documentation is excluded, and only
#: where it lives at the repository root, so `content/docs/guide.md` stays a page while
#: `docs/architecture.md` does not.
IGNORED_PATTERNS = (
    r"^\.github/",
    r"(^|/)(tests?|__tests__|spec|e2e|cypress)/",
    r"\.(test|spec)\.[jt]sx?$",
    r"^docs?/",
    r"^(README|CHANGELOG|CONTRIBUTING|LICENSE|CODE_OF_CONDUCT|SECURITY|AUTHORS)(\.[a-z]+)?$",
    r"(^|/)(\.gitignore|\.editorconfig|\.prettierrc|\.eslintrc|\.dockerignore)",
    r"(^|/)(Dockerfile|docker-compose\.ya?ml)$",
    r"\.(lock|log)$",
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$",
    r"(^|/)(migrations?|alembic)/",
)

#: Extensions that can define a route in one framework or another.
ROUTE_EXTENSIONS = (
    ".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte", ".astro",
    ".html", ".htm", ".md", ".mdx", ".markdown", ".php", ".liquid", ".njk", ".hbs",
)

INDEX_NAMES = {"index", "page", "_index", "home", "default"}


@dataclass
class MappingResult:
    """What a set of changed files means for the crawl."""

    affected_paths: list[str] = field(default_factory=list)
    mapped_files: dict[str, str] = field(default_factory=dict)
    unmapped_files: list[str] = field(default_factory=list)
    ignored_files: list[str] = field(default_factory=list)
    requires_full_recrawl: bool = False
    reason: str = ""

    @property
    def has_targets(self) -> bool:
        return bool(self.affected_paths)


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, path, re.IGNORECASE) for pattern in patterns)


def is_ignorable(path: str) -> bool:
    """True for files that cannot change what a crawler would see."""
    return _matches_any(path, IGNORED_PATTERNS)


def has_global_impact(path: str) -> bool:
    """True for files whose change could alter every page on the site."""
    return _matches_any(path, GLOBAL_IMPACT_PATTERNS)


# ── Path normalisation shared by the resolvers ──────────────────────────────


def _clean_route(segments: list[str]) -> str | None:
    """Turn route segments into a URL path, or ``None`` if the route is not addressable."""
    cleaned: list[str] = []
    for segment in segments:
        if not segment:
            continue
        # Route groups: Next.js "(marketing)", SvelteKit "(app)" — organisational, not in the URL.
        if segment.startswith("(") and segment.endswith(")"):
            continue
        # Private/partial directories.
        if segment.startswith("_") and segment.strip("_") not in INDEX_NAMES:
            return None
        # Dynamic segments ([slug], :id, $param, ...) cannot be resolved to one URL from the
        # filename alone. Report the parent collection instead of guessing.
        if re.fullmatch(r"[\[\{].*[\]\}]", segment) or segment.startswith((":", "$")):
            return "/" + "/".join(cleaned) if cleaned else "/"
        cleaned.append(segment)

    if not cleaned:
        return "/"
    return "/" + "/".join(cleaned)


def _strip_extension(filename: str) -> str:
    for extension in sorted(ROUTE_EXTENSIONS, key=len, reverse=True):
        if filename.lower().endswith(extension):
            return filename[: -len(extension)]
    return filename


def _route_from_directory(path: str, roots: tuple[str, ...]) -> str | None:
    """Resolve a file under one of ``roots`` into a URL path."""
    normalised = path.strip("/").replace("\\", "/")
    parts = normalised.split("/")

    root_index = None
    for index, part in enumerate(parts[:-1]):
        if part.lower() in roots:
            root_index = index
            break
    if root_index is None:
        return None

    segments = parts[root_index + 1 :]
    if not segments:
        return None

    # SvelteKit names route files "+page.svelte", "+layout.svelte", "+server.ts" — the leading
    # "+" is a marker, not part of the URL.
    filename = _strip_extension(segments[-1]).lstrip("+")
    body = segments[:-1] if filename.lower() in INDEX_NAMES else [*segments[:-1], filename]
    return _clean_route(body)


# ── Framework resolvers ─────────────────────────────────────────────────────


def resolve_next(path: str) -> str | None:
    """Next.js: both the `pages/` router and the `app/` router."""
    return _route_from_directory(path, ("pages", "app"))


def resolve_nuxt(path: str) -> str | None:
    return _route_from_directory(path, ("pages",))


def resolve_astro(path: str) -> str | None:
    return _route_from_directory(path, ("pages",))


def resolve_sveltekit(path: str) -> str | None:
    return _route_from_directory(path, ("routes",))


def resolve_remix(path: str) -> str | None:
    """Remix flat routes: `routes/blog.post.tsx` -> `/blog/post`."""
    normalised = path.strip("/").replace("\\", "/")
    if "routes/" not in normalised:
        return None
    filename = _strip_extension(normalised.rsplit("/", 1)[-1])
    if filename.lower() in INDEX_NAMES or filename == "_index":
        return "/"
    return _clean_route(filename.split("."))


def resolve_hugo(path: str) -> str | None:
    return _route_from_directory(path, ("content",))


def resolve_jekyll(path: str) -> str | None:
    """Jekyll posts carry a date prefix that is not part of the permalink by default."""
    normalised = path.strip("/").replace("\\", "/")
    if "_posts/" in normalised:
        filename = _strip_extension(normalised.rsplit("/", 1)[-1])
        slug = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", filename)
        return f"/{slug}" if slug else None
    return _route_from_directory(path, ("pages", "_pages"))


def resolve_gatsby(path: str) -> str | None:
    return _route_from_directory(path, ("pages",))


def resolve_static(path: str) -> str | None:
    """Plain HTML sites, and the common `public/` or `dist/` output directory."""
    normalised = path.strip("/").replace("\\", "/")
    if not normalised.lower().endswith((".html", ".htm")):
        return None

    parts = normalised.split("/")
    for root in ("public", "dist", "build", "static", "www", "src", "site", "_site"):
        if parts and parts[0].lower() == root:
            parts = parts[1:]
            break
    if not parts:
        return None

    filename = _strip_extension(parts[-1])
    body = parts[:-1] if filename.lower() in INDEX_NAMES else [*parts[:-1], filename]
    return _clean_route(body)


RESOLVERS: dict[str, Callable[[str], str | None]] = {
    "next": resolve_next,
    "nextjs": resolve_next,
    "nuxt": resolve_nuxt,
    "astro": resolve_astro,
    "sveltekit": resolve_sveltekit,
    "svelte": resolve_sveltekit,
    "remix": resolve_remix,
    "hugo": resolve_hugo,
    "jekyll": resolve_jekyll,
    "gatsby": resolve_gatsby,
    "static": resolve_static,
    "html": resolve_static,
}

#: Tried in order when the website declares no framework.
GENERIC_ORDER = (
    resolve_next, resolve_sveltekit, resolve_astro, resolve_hugo,
    resolve_jekyll, resolve_remix, resolve_static,
)


def resolve_file(path: str, framework: str | None = None) -> str | None:
    """Resolve one changed file to a URL path."""
    if framework:
        resolver = RESOLVERS.get(framework.strip().lower())
        if resolver is not None:
            return resolver(path)

    for resolver in GENERIC_ORDER:
        resolved = resolver(path)
        if resolved is not None:
            return resolved
    return None


# ── Entry point ─────────────────────────────────────────────────────────────


def map_changed_files(
    changed_files: list[str],
    *,
    framework: str | None = None,
    path_map: dict[str, str] | None = None,
    max_targets: int = 200,
) -> MappingResult:
    """Decide what a push should re-audit."""
    result = MappingResult()

    relevant: list[str] = []
    for path in changed_files:
        if is_ignorable(path):
            result.ignored_files.append(path)
        else:
            relevant.append(path)

    if not relevant:
        result.reason = "No changed file can affect rendered output."
        return result

    explicit = {k.strip("/"): v for k, v in (path_map or {}).items()}

    seen: set[str] = set()
    for path in relevant:
        # 1. An explicit mapping is authoritative.
        mapped = explicit.get(path.strip("/"))

        # 2. A globally-scoped file forces a full re-audit, even if it would also resolve.
        if mapped is None and has_global_impact(path):
            result.requires_full_recrawl = True
            result.reason = (
                f"'{path}' is shared across pages (layout, template, component or config), "
                "so its effect cannot be limited to specific URLs."
            )
            return result

        # 3. Framework routing conventions.
        if mapped is None:
            mapped = resolve_file(path, framework)

        if mapped is None:
            result.unmapped_files.append(path)
            continue

        result.mapped_files[path] = mapped
        if mapped not in seen:
            seen.add(mapped)
            result.affected_paths.append(mapped)

    if not result.affected_paths:
        result.requires_full_recrawl = True
        result.reason = (
            f"None of the {len(relevant)} changed files could be mapped to a URL, "
            "so the whole site is re-audited to stay correct."
        )
        return result

    if len(result.affected_paths) > max_targets:
        result.requires_full_recrawl = True
        result.reason = (
            f"{len(result.affected_paths)} pages changed, which exceeds the "
            f"{max_targets}-page incremental limit; a full crawl is cheaper."
        )
        return result

    result.reason = (
        f"{len(result.affected_paths)} page(s) resolved from {len(relevant)} changed file(s)."
    )
    return result


def extract_changed_files(payload: dict) -> list[str]:
    """Collect every added, modified and removed path from a push payload.

    GitHub caps each commit's file lists at 3 000 entries and omits them entirely for very large
    pushes — an empty result therefore means "unknown", which the caller turns into a full
    re-audit rather than a no-op.
    """
    files: list[str] = []
    seen: set[str] = set()

    for commit in payload.get("commits") or []:
        for key in ("added", "modified", "removed"):
            for path in commit.get(key) or []:
                if path not in seen:
                    seen.add(path)
                    files.append(path)

    head = payload.get("head_commit") or {}
    for key in ("added", "modified", "removed"):
        for path in head.get(key) or []:
            if path not in seen:
                seen.add(path)
                files.append(path)

    return files
