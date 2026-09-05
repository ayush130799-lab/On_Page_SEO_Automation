"""Validate crawler extraction against real websites.

    python scripts/validate_crawler.py                 # the built-in 24-URL corpus
    python scripts/validate_crawler.py URL [URL ...]   # ad-hoc URLs
    python scripts/validate_crawler.py --json out.json # machine-readable results

For every URL this fetches the page once and measures each metric **twice**:

* ``crawler`` — the value the production extractor produces.
* ``source_dom`` — an independent oracle in this file, written deliberately differently: it uses
  the ``html.parser`` tree builder rather than lxml, and reimplements each count from scratch. It
  shares no code with the extractor beyond BeautifulSoup itself, so agreement is evidence rather
  than tautology.

A disagreement is printed with both values so the page can be inspected and the technically
correct answer established from the markup. The oracle is not authoritative — it is a second
opinion, and where the two differ for a documented reason (main-content word scoping, tracking
pixel exclusion) the difference is expected and labelled as such.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.crawler.extractor import empty_page, extract_page  # noqa: E402
from app.services.crawler.fetcher import fetch_url  # noqa: E402
from app.utils.url_utils import domain_of  # noqa: E402

#: Deliberately varied: server-rendered docs, client-rendered apps, news, reference, commerce,
#: pages with structured data, pages with hreflang, redirects and error statuses.
DEFAULT_URLS = [
    "https://example.com/",
    "https://www.python.org/about/",
    "https://docs.python.org/3/tutorial/index.html",
    "https://fastapi.tiangolo.com/",
    "https://developer.mozilla.org/en-US/docs/Web/HTML",
    "https://en.wikipedia.org/wiki/Search_engine_optimization",
    "https://blog.rust-lang.org/",
    "https://news.ycombinator.com/",
    "https://github.com/pallets/flask",
    "https://schema.org/Product",
    "https://www.w3.org/TR/html52/",
    "https://web.dev/articles/lcp",
    "https://developers.google.com/search/docs/crawling-indexing/robots/intro",
    "https://react.dev/",
    "https://nextjs.org/docs",
    "https://vercel.com/",
    "https://stripe.com/",
    "https://www.djangoproject.com/",
    "https://httpbin.org/status/404",
    "https://httpbin.org/redirect/2",
    "https://www.iana.org/domains/reserved",
    "https://peps.python.org/pep-0008/",
    "https://curl.se/docs/manpage.html",
    "https://www.rfc-editor.org/rfc/rfc9110.html",
]

METRICS = [
    "status_code", "title", "title_count", "meta_description_present", "meta_description_count",
    "canonical_count", "canonical_href", "h1_count", "h1_text", "h2_count", "h3_count",
    "img_count", "img_missing_alt", "img_empty_alt", "internal_links", "external_links",
    "nofollow_links", "meta_robots", "lang", "hreflang_count", "json_ld_blocks",
    "microdata_nodes", "word_count",
]

#: Metrics where the two implementations are *expected* to differ, with the reason. These are
#: documented methodology differences, not defects.
EXPECTED_DIVERGENCE = {
    "word_count": (
        "Oracle counts all visible body text; the crawler reports main-content words "
        "(nav/header/footer/aside removed). The crawler figure is <= the oracle figure."
    ),
    "img_count": (
        "The crawler excludes tracking pixels from the image count; the oracle counts every "
        "<img> element."
    ),
    "internal_links": (
        "The crawler de-duplicates by normalised URL; the oracle counts anchor elements."
    ),
    "external_links": (
        "The crawler de-duplicates by normalised URL; the oracle counts anchor elements."
    ),
}


def oracle(url: str, html: str, base_domain: str, status_code: int, headers: dict) -> dict[str, Any]:
    """Independent measurement of the same page. Shares no logic with the extractor."""
    soup = BeautifulSoup(html or "", "html.parser")

    # <head> is an optional tag: html.spec.whatwg.org omits it entirely and the element is
    # implied. html.parser does not synthesise implied elements (lxml does), so when there is no
    # head element the whole document is the right place to look for the title.
    head = soup.find("head")
    scope = head if head is not None else soup
    titles = [t for t in scope.find_all("title") if not t.find_parent("svg")]

    metas = soup.find_all("meta")

    def meta_by_name(name: str) -> list:
        return [m for m in metas if (m.get("name") or "").lower() == name]

    descs = meta_by_name("description")
    robots = meta_by_name("robots")

    canonicals = [
        link for link in soup.find_all("link")
        if "canonical" in [r.lower() for r in (link.get("rel") or [])]
    ]

    headings = {f"h{n}": len(soup.find_all(f"h{n}")) for n in range(1, 7)}
    h1_tags = soup.find_all("h1")
    h1_text = " | ".join(
        " ".join(t.get_text(" ", strip=True).split()) for t in h1_tags
        if t.get_text(strip=True)
    ) or None

    images = soup.find_all("img")
    missing_alt = sum(1 for i in images if i.get("alt") is None)
    empty_alt = sum(1 for i in images if (i.get("alt") or "").strip() == "" and i.get("alt") is not None)

    host = re.sub(r"^www\.", "", base_domain.lower())
    internal = external = nofollow = 0
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        rels = [r.lower() for r in (a.get("rel") or [])]
        if "nofollow" in rels:
            nofollow += 1
        if href.startswith("//"):
            link_host = href[2:].split("/")[0]
        elif "://" in href:
            link_host = href.split("://", 1)[1].split("/")[0]
        else:
            internal += 1
            continue
        link_host = re.sub(r"^www\.", "", link_host.lower().split(":")[0])
        if link_host == host:
            internal += 1
        else:
            external += 1

    alternates = [
        link for link in soup.find_all("link")
        if "alternate" in [r.lower() for r in (link.get("rel") or [])] and link.get("hreflang")
    ]

    json_ld = soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)})
    microdata = soup.find_all(attrs={"itemtype": True})

    # Visible body text: drop the elements that never render, then tokenise on whitespace.
    body = soup.find("body") or soup
    text_soup = BeautifulSoup(str(body), "html.parser")
    for tag in text_soup(["script", "style", "template", "noscript", "svg", "iframe"]):
        tag.decompose()
    words = len(" ".join(text_soup.get_text(" ").split()).split())

    html_tag = soup.find("html")

    return {
        "status_code": status_code,
        "title": " ".join(titles[0].get_text(" ", strip=True).split()) if titles else None,
        "title_count": len(titles),
        "meta_description_present": bool(descs and (descs[0].get("content") or "").strip()),
        "meta_description_count": len(descs),
        "canonical_count": len(canonicals),
        "canonical_href": (canonicals[0].get("href") or "").strip() if canonicals else None,
        "h1_count": headings["h1"],
        "h1_text": h1_text,
        "h2_count": headings["h2"],
        "h3_count": headings["h3"],
        "img_count": len(images),
        "img_missing_alt": missing_alt,
        "img_empty_alt": empty_alt,
        "internal_links": internal,
        "external_links": external,
        "nofollow_links": nofollow,
        "meta_robots": (robots[0].get("content") or "").strip() if robots else None,
        "lang": (html_tag.get("lang") or "").strip() or None if html_tag else None,
        "hreflang_count": len(alternates),
        "json_ld_blocks": len(json_ld),
        "microdata_nodes": len(microdata),
        "word_count": words,
    }


def crawler_view(page) -> dict[str, Any]:
    """The same metric set, read off the production ExtractedPage."""
    return {
        "status_code": page.status_code,
        "title": page.title,
        "title_count": page.title_count,
        "meta_description_present": bool(page.meta_description),
        "meta_description_count": page.meta_description_count,
        "canonical_count": page.canonical_count,
        "canonical_href": page.canonical_raw,
        "h1_count": page.h1_count,
        "h1_text": page.h1,
        "h2_count": page.h2_count,
        "h3_count": page.h3_count,
        "img_count": page.image_count,
        "img_missing_alt": page.missing_alt_count,
        "img_empty_alt": page.empty_alt_count,
        "internal_links": page.internal_link_count,
        "external_links": page.external_link_count,
        "nofollow_links": page.nofollow_link_count,
        "meta_robots": page.meta_robots,
        "lang": page.lang,
        "hreflang_count": len(page.hreflang),
        "json_ld_blocks": len([f for f in page.structured_data_formats if f == "json-ld"]),
        "microdata_nodes": len([f for f in page.structured_data_formats if f == "microdata"]),
        "word_count": page.word_count,
    }


async def check(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    result = await fetch_url(client, url, max_retries=2, timeout=25.0)
    base_domain = domain_of(result.final_url or url)

    # Mirror the orchestrator: a response with no HTML body carries no document, and must be
    # recorded as such rather than parsed into a page of zeroes labelled "ok".
    if result.status_code == 0 or (not result.html and not result.ok):
        page = empty_page(
            result.final_url or url,
            result.status_code or 0,
            result.error or f"No HTML body (HTTP {result.status_code}).",
        )
    else:
        page = extract_page(
            result.final_url or url, result.html or "", base_domain,
            result.status_code, headers=result.headers,
        )
    page.final_url = result.final_url
    page.redirect_chain = result.redirect_chain

    ours = crawler_view(page)
    theirs = oracle(
        result.final_url or url, result.html or "", base_domain,
        result.status_code, result.headers,
    )

    mismatches = []
    for metric in METRICS:
        a, b = ours.get(metric), theirs.get(metric)
        if a == b:
            continue
        # json_ld_blocks/microdata_nodes: the crawler reports formats present, the oracle counts
        # elements. Presence agreement is what matters.
        if metric in ("json_ld_blocks", "microdata_nodes"):
            if bool(a) == bool(b):
                continue
        mismatches.append({
            "metric": metric,
            "crawler": a,
            "source_dom": b,
            "expected": metric in EXPECTED_DIVERGENCE,
            "reason": EXPECTED_DIVERGENCE.get(metric),
        })

    return {
        "url": url,
        "final_url": result.final_url,
        "status_code": result.status_code,
        "redirect_hops": len(result.redirect_chain),
        "fetch_error": result.error,
        "html_bytes": result.content_bytes,
        "elapsed_ms": result.elapsed_ms,
        "crawl_quality": page.crawl_quality,
        "is_usable": page.is_usable,
        "has_document": page.has_document,
        "extraction_errors": page.extraction_errors,
        "crawler": ours,
        "source_dom": theirs,
        "mismatches": mismatches,
        "unexpected_mismatches": [m for m in mismatches if not m["expected"]],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", default=None)
    parser.add_argument("--json", dest="json_out")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    urls = args.urls or DEFAULT_URLS
    semaphore = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; SEO-Automation-Crawler/2.0; validation)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:

        async def guarded(u):
            async with semaphore:
                try:
                    return await check(client, u)
                except Exception as exc:  # a harness failure must not look like a crawler failure
                    return {"url": u, "harness_error": f"{type(exc).__name__}: {exc}"}

        results = await asyncio.gather(*(guarded(u) for u in urls))

    unexpected_total = 0
    for r in results:
        if "harness_error" in r:
            print(f"\n{r['url']}\n  HARNESS ERROR: {r['harness_error']}")
            continue
        flag = "OK " if not r["unexpected_mismatches"] else "DIFF"
        print(f"\n[{flag}] {r['url']}")
        print(f"       HTTP {r['status_code']}  hops={r['redirect_hops']}  "
              f"{r['html_bytes']}B  {r['elapsed_ms']}ms  quality={r['crawl_quality']}  "
              f"document={r['has_document']}")
        if r["fetch_error"]:
            print(f"       fetch error: {r['fetch_error']}")
        if r["extraction_errors"]:
            print(f"       extraction errors: {r['extraction_errors']}")
        c = r["crawler"]
        print(f"       title={str(c['title'])[:60]!r} h1={c['h1_count']} h2={c['h2_count']} "
              f"canon={c['canonical_count']} img={c['img_count']}(missing_alt={c['img_missing_alt']}) "
              f"links={c['internal_links']}/{c['external_links']} words={c['word_count']}")
        for m in r["mismatches"]:
            marker = "expected" if m["expected"] else "UNEXPECTED"
            print(f"       - {m['metric']}: crawler={m['crawler']!r} dom={m['source_dom']!r} [{marker}]")
            if m["reason"]:
                print(f"         reason: {m['reason']}")
        unexpected_total += len(r["unexpected_mismatches"])

    checked = [r for r in results if "harness_error" not in r]
    clean = [r for r in checked if not r["unexpected_mismatches"]]
    print(f"\n{'=' * 78}")
    print(f"URLs checked: {len(checked)}/{len(urls)}   "
          f"agreeing on every metric: {len(clean)}   "
          f"unexplained metric differences: {unexpected_total}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
        print(f"Full results written to {args.json_out}")

    return 0 if unexpected_total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
