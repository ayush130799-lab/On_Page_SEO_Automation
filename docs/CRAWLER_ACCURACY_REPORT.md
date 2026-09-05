# SEO Crawler Accuracy — Audit & Rebuild Report

Scope: the crawling and extraction pipeline only. No changes were made to AI recommendations,
prioritisation, GSC, GA4 or Semrush except where a defect in shared code was corrupting stored
crawl data.

---

## 1. Root causes discovered

The audit ran the *existing* code against controlled HTML and printed pass/BUG per assertion
before any code was modified, so every finding below has a concrete reproduction. Twenty-one
defects were confirmed that way; three more surfaced later from the fixture suite and the
real-world run. The ones that materially changed reported SEO data:

### 1.1 A failed fetch was scored as a healthy page — the single largest source of false data

`app/services/seo/engine.py` guarded the audit with:

```python
status_code = getattr(page, "status_code", 200) or 200
```

`or 200` turns status `0` — a request that never completed — into `200`. The full rule set then
ran against an empty document and manufactured **13 issues** ("missing title", "missing H1",
"thin content", "no canonical", …) for a URL that was merely unreachable, reporting a score of
52.3. Every unreachable URL on a site produced a page of invented SEO findings.

**Fix.** `None` and `0` are now distinguished, and a page carries `is_usable`. When a page was
never successfully retrieved, only the status and redirect rules run; every other check returns
`SKIPPED`. A failed fetch now reports `http_status` alone.

### 1.2 Skipped checks were counted as passes

`RuleResult` had only `pass`/`warning`/`fail`. A check that could not run returned a neutral
`pass` at score 100, which *raised* the score of a broken page.

**Fix.** A fourth status `SKIPPED` was added; `scoring.calculate_score` excludes non-evaluated
results from the weighted mean entirely rather than counting them as either a pass or a zero.

### 1.3 Robots directives were substring-matched

`check_robots` tested `"none" in robots_directive`. The perfectly ordinary directive
`max-image-preview:none` contains the word "none" and was read as `robots: none`, marking a
healthy, indexable page as CRITICAL/noindex. `X-Robots-Tag` was not consulted at all, and a
header scoped to another crawler (`bingbot: noindex`) would have been applied to us.

**Fix.** New module `app/services/seo/robots_directives.py` tokenises directives, understands
valued directives (`max-snippet`, `max-image-preview`, `max-video-preview`, `unavailable_after`),
resolves meta and header sources together, records which source each directive came from, and
ignores groups addressed to other user agents.

### 1.4 Text measurement mutated one shared DOM tree

The three word-count passes decomposed elements from a single soup, so later measurements —
and the heading counts taken afterwards — saw a document earlier passes had already stripped.
H1 and H2 were counted against different DOM states.

**Fix.** `_measure_text` works on independent copies; all six heading levels are counted against
one unmodified document.

### 1.5 A hidden element with children zeroed every word count

`find_all(True)` returns a snapshot. Decomposing a hidden element also decomposes its
descendants, which remain in that list; reading `.attrs` on a decomposed tag raises `TypeError`.
The exception was caught, and all three word counts fell to 0.

This fired on **python.org** — a mainstream, entirely ordinary page — during the first debug run.

**Fix.** The loop skips already-decomposed nodes.

### 1.6 lxml discarded whole documents that opened with a stray closing tag

Verified directly: for `</span><!doctype html><html>…</html>`, lxml's recovery returns an empty
tree — no tags at all. A complete, valid document preceded by one stray tag extracted as title
`None`, h1 `None`, word count `0`. Browsers render such pages without complaint.

**Fix.** lxml remains the primary parser (it recovers unclosed tags better than the alternative),
but when it returns a document with no elements at all and the source was non-empty, the page is
reparsed with `html.parser`. The fallback is recorded in `extraction_errors`.

### 1.7 Real images were classified as tracking beacons

`_BEACON_PATH_RE` matched `/pixel` and `/beacon` anywhere, so `/photos/pixelated-art.jpg` matched
at `…photos/pixel…`. Genuine content images were removed from `image_count` and excluded from ALT
checking — under-counting images *and* hiding real missing-ALT problems.

**Fix.** Both alternatives now require a segment or extension boundary via lookahead.

### 1.8 A failed crawl overwrote good data with blanks

`_snapshot_page` wrote every content field unconditionally. One timeout or 500 on a re-crawl
blanked the stored title, meta description, H1, headings, word count, images and links of a page
that was perfectly healthy — a failed crawl silently becoming fabricated "everything missing"
SEO data on the next dashboard load.

**Fix.** `ExtractedPage.has_document` distinguishes an observation that actually parsed a
document (2xx *and* a parsed body) from one that did not. Response facts — status, error,
timing, redirect chain, quality — are always current; content signals are written only when a
document was retrieved. New column `pages.content_captured_at` makes the age of those signals
explicit: older than `last_crawled_at` means the latest crawl carried no document.

### 1.9 Sitemaps were parsed with regex

Namespaces and CDATA were mishandled, and `<changefreq>` inside a `<url>` block was mis-parsed as
a nested sitemap reference. `lastmod`, `changefreq` and `priority` were discarded entirely.

**Fix.** `sitemap.py` was rewritten around the namespace-aware XML parser, returning structured
`SitemapEntry` records with full metadata. The regex sweep survives only as a fallback for
documents that are not well-formed XML — a great many production sitemaps are not, and refusing
to read them would throw away the best URL source a site offers. Which path was used is reported
(`used_fallback_parser`), as are per-sitemap failures and skip reasons.

### 1.10 Smaller confirmed defects

| Defect | Effect | Fix |
|---|---|---|
| `<svg><title>` read as the page title | Icon labels reported as titles | Title restricted to `<head>`, SVG explicitly excluded |
| Only one `<title>` / `<link rel=canonical>` observed | Duplicate-tag defects invisible | `title_count`, `meta_description_count`, `meta_robots_count`, `canonical_count` recorded; two new rules |
| Canonical stored as a single field | Could not distinguish absent / empty / relative / cross-domain | `canonical_raw`, `canonical_url`, `canonical_count`, `canonical_status` stored separately |
| Only H1–H3 counted | H4–H6 and empty headings invisible | All six levels plus `empty_heading_count`; two new rules |
| Missing ALT and `alt=""` conflated | Decorative images reported as defects | `missing_alt_count` and `empty_alt_count` counted separately |
| `X-Robots-Tag` never reached the extractor | Header-level noindex missed entirely | Orchestrator passes `headers=` into `extract_page` |
| Discovery suppressed link expansion on non-canonical pages | Whole site sections never discovered | Removed; hreflang alternates added as a discovery source |
| Render failure silently fell back to static HTML | Client-rendered pages reported as genuinely thin | `render_error` recorded, `crawl_quality="render_failed"` |
| Trailing-slash redirects reported as CRITICAL loops | 11 false positives on one real site | Loop detection requires a repeated hop or a ≥2-hop return |

---

## 2. Exact files changed

**New**

| File | Purpose |
|---|---|
| `backend/app/services/seo/robots_directives.py` | Tokenised robots-directive parsing and resolution |
| `backend/app/api/routes/validate.py` | Crawler debugging mode (rewritten from a stub) |
| `backend/scripts/validate_crawler.py` | Real-website validation harness with an independent oracle |
| `backend/tests/test_crawler_fixtures.py` | The 32 fixture scenarios |
| `backend/alembic/versions/0005_crawler_accuracy_v2.py` | 19 new `pages` columns |
| `backend/alembic/versions/0006_content_captured_at.py` | `pages.content_captured_at` |
| `docs/CRAWLER_ACCURACY_REPORT.md` | This report |

**Modified**

| File | Change |
|---|---|
| `backend/app/services/crawler/extractor.py` | Rewritten: three-tier word counting, per-field provenance, link/image records, all six heading levels, canonical status, structured-data formats, parser fallback |
| `backend/app/services/crawler/sitemap.py` | Rewritten around XML parsing with structured entries and reported failures |
| `backend/app/services/crawler/orchestrator.py` | Passes response headers to extraction; records render failures; discovery no longer suppressed on non-canonical pages |
| `backend/app/services/seo/engine.py` | Status-0 guard; unusable pages skip content rules |
| `backend/app/services/seo/registry.py` | `SKIPPED` status; `was_evaluated` |
| `backend/app/services/seo/scoring.py` | Non-evaluated rules excluded from the weighted mean |
| `backend/app/services/seo/rules/indexability.py` | Robots rule uses the tokenised resolver; canonical rules use `canonical_count` |
| `backend/app/services/seo/rules/metadata.py` | `title_multiple`, `meta_description_multiple` |
| `backend/app/services/seo/rules/structure.py` | `empty_headings`, `heading_depth` |
| `backend/app/services/seo/rules/media_links.py` | Missing vs empty ALT separated |
| `backend/app/models/page.py` | 19 accuracy columns + `content_captured_at` |
| `backend/app/services/pipeline.py` | Persists all new fields; failed observations no longer blank content |
| `backend/app/api/routes/debug.py` | Freshness, methodology and multiplicity fields exposed |
| `backend/app/utils/url_utils.py` | Single normalisation entry point hardened |
| `backend/tests/test_crawler_accuracy.py` | Updated to the new sitemap API |
| `backend/tests/test_pipeline.py` | Stale-data regression tests |

---

## 3. Crawler components changed

```
fetcher.py        unchanged interface; every response fact already recorded
                  (final URL, status, headers, redirect chain, timing, bytes, attempts)
renderer.py       unchanged; render failures are now surfaced by the orchestrator
orchestrator.py   headers plumbed into extraction; render errors recorded;
                  discovery unblocked; hreflang alternates discovered
extractor.py      rewritten — the bulk of the accuracy work
sitemap.py        rewritten — XML-first with a reported fallback
robots.py         unchanged; verified by fixture tests
robots_directives.py   new — meta + header directive resolution
seo/engine.py     unusable pages no longer audited as if retrieved
pipeline.py       failed crawls no longer overwrite good content
```

---

## 4. Extraction methodology

### URL identity
One function, `app/utils/url_utils.normalize_url`, is the only normaliser in the codebase
(verified: a single definition, imported by every module that establishes URL identity). It
lowercases scheme and host, removes default ports, strips fragments, removes known tracking
parameters, sorts remaining query parameters, and normalises trailing slashes while preserving
the root path. Path case is preserved — most servers are case-sensitive and lowering it would
merge genuinely different pages. Content-bearing parameters (`?id=7`) are kept, so `?id=7` and
`?id=8` remain distinct URLs.

### Word count — three measures, all stored
| Measure | Definition |
|---|---|
| `raw_word_count` | Body text after removing `script`, `style`, `template`, `svg`, `iframe`, `noscript` and comments |
| `visible_word_count` | Raw minus `hidden`, `aria-hidden="true"`, inline `display:none` / `visibility:hidden` |
| `main_content_word_count` (= `word_count`) | Visible minus `nav`, `header`, `footer`, `aside`, `form`, `figcaption`, `dialog`; scoped to `<main>`/`<article>` when present |

`content_scope` records which container was used. Counting is whitespace tokenisation of
extracted text — never `len(html)`, never a split of markup. All three are stored so a
disagreement with another tool is explained by methodology rather than argued about.

### Canonical
`canonical_raw` (the href as written), `canonical_url` (resolved absolute), `canonical_count`,
and `canonical_status` ∈ {`missing`, `empty`, `self`, `other`, `relative`, `invalid`, `multiple`}
are stored separately. Google's *selected* canonical is not observable by a crawler and is never
inferred — only what the page declares is reported.

### Images
Counted from `<img>` elements only. CSS background images and inline SVG are not `<img>`
elements and are never counted. Tracking pixels are excluded from `image_count` and counted in
`tracking_pixel_count`. A missing `alt` attribute and `alt=""` are counted separately, because
`alt=""` is a valid declaration that an image is decorative.

### Links
Classified as internal or external by comparing normalised registrable hosts — never string
containment, so `example.com.evil.test` is correctly external. Anchor text and all `rel` tokens
(`nofollow`, `sponsored`, `ugc`) are captured. `mailto:`, `tel:`, `javascript:`, `data:`,
fragment-only and malformed hrefs are counted in `non_http_link_count` and never become crawl
targets. Stored internal/external lists are de-duplicated by normalised URL.

### Structured data
JSON-LD, Microdata and RDFa are each detected and reported in `structured_data_formats`. Invalid
JSON-LD sets `structured_data_invalid` and `json_ld_error` rather than being silently ignored,
and one broken block does not discard a valid one in the same document.

### Provenance
Every headline value records how it was obtained — the selector, how many nodes matched, the raw
attribute text before normalisation, and the decision taken. This is produced during extraction,
so the debug endpoint reports what the crawler actually did rather than re-deriving it through a
second implementation that could disagree.

---

## 5. Crawler debugging mode

```
GET /api/validate/page?url=<URL>&render=auto|always|never&html=true&check_robots=true
```

Returns, for one URL, in a single request:

- complete response facts — final URL, redirect chain and hop count, every response header,
  content type, charset, timing, byte count, attempts, transport error
- the rendering decision and *why* it was taken, plus any render error
- `is_usable` / `crawl_quality` / `extraction_errors`, stated explicitly
- resolved robots directives with the source of each, and the robots.txt verdict for our agent
- canonical: declared raw, resolved, count, status
- every extracted signal, including all three word counts
- **provenance for each value**
- image and link samples with per-item ALT state and `rel` values
- every rule result with status, score, weight and evidence
- optionally the raw and rendered HTML for hand-diffing

It calls the same fetch, render, extraction and rule functions a crawl calls, so it cannot
disagree with the crawl that produced the stored data. (The previous version re-implemented the
robots check with substring matching and *could* — precisely the class of bug this endpoint
exists to find.)

`GET /api/websites/{id}/pages/{page_id}/debug` does the same for already-stored data and now
also reports content freshness.

---

## 6. Tests added

`backend/tests/test_crawler_fixtures.py` — **114 tests** covering all 32 required scenarios,
each asserting actual extracted values:

| # | Scenario | # | Scenario |
|---|---|---|---|
| 1 | Normal HTML | 17 | Internal links |
| 2 | JavaScript-rendered content | 18 | External links |
| 3 | Missing canonical | 19 | noindex |
| 4 | Multiple canonicals | 20 | X-Robots-Tag |
| 5 | Self canonical | 21 | 301 redirect |
| 6 | Cross canonical | 22 | Redirect chain |
| 7 | Relative canonical | 23 | 404 |
| 8 | Missing title | 24 | robots.txt restrictions |
| 9 | Multiple titles | 25 | sitemap |
| 10 | Missing meta description | 26 | sitemap index |
| 11 | Multiple H1 | 27 | duplicate URLs |
| 12 | No H1 | 28 | URL parameters |
| 13 | Hidden content | 29 | trailing slash variants |
| 14 | Images with ALT | 30 | malformed HTML |
| 15 | Images without ALT | 31 | JSON-LD |
| 16 | Images with empty ALT | 32 | invalid JSON-LD |

Scenarios 21–26 drive the real fetcher and sitemap code over `httpx.MockTransport`, so the code
under test is the code a live crawl runs.

Also added: 4 end-to-end pipeline tests proving a failed re-crawl (timeout and 500) preserves the
previous content signals, that `content_captured_at` lags `last_crawled_at` after a failure, and
that a successful re-crawl still updates content.

## 7. Test results

```
777 passed in 316s
```

Full backend suite, PostgreSQL 18. No skips, no xfails, no warnings suppressed.
(659 pre-existing + 114 fixture scenarios + 4 stale-data regressions, with pre-existing tests
updated where the sitemap API changed.)

Migrations `0005` and `0006` applied cleanly to the populated production database; the 13,903
preserved `legacy_*` rows are intact. Alembic autogenerate proposes dropping those legacy tables
on every revision because they are deliberately absent from the ORM metadata — that proposal must
never be accepted, and this is noted in `0006`'s docstring.

---

## 8. Real-world validation

`python scripts/validate_crawler.py` fetches each URL once and measures every metric twice: with
the production extractor, and with an **independent oracle** in the harness that uses a different
tree builder (`html.parser` rather than lxml) and reimplements every count from scratch. Agreement
is therefore evidence rather than tautology.

**24 URLs, 23 metrics each. All 24 agree on every metric; 0 unexplained differences.**

| URL | HTTP | hops | title | canon | h1 | h2 | img | noalt | int | ext | hreflang | words |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| example.com/ | 200 | 0 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 19 |
| www.python.org/about/ | 200 | 0 | 1 | 0 | 1 | 7 | 1 | 0 | 60 | 38 | 0 | 345 |
| docs.python.org/3/tutorial/index.html | 200 | 0 | 1 | 1 | 1 | 0 | 3 | 0 | 30 | 4 | 0 | 1019 |
| fastapi.tiangolo.com/ | 200 | 0 | 1 | 1 | 1 | 11 | 43 | 1 | 163 | 53 | 13 | 2272 |
| developer.mozilla.org/en-US/docs/Web/HTML | 200 | 0 | 1 | 1 | 1 | 7 | 0 | 0 | 356 | 16 | 0 | 1129 |
| en.wikipedia.org/wiki/Search_engine_optimization | 403 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| blog.rust-lang.org/ | 200 | 0 | 1 | 0 | 0 | 0 | 6 | 0 | 394 | 18 | 0 | 2585 |
| news.ycombinator.com/ | 200 | 0 | 1 | 0 | 0 | 0 | 3 | 3 | 158 | 30 | 0 | 706 |
| github.com/pallets/flask | 200 | 0 | 1 | 0 | 1 | 15 | 1 | 0 | 90 | 21 | 0 | 330 |
| schema.org/Product | 200 | 0 | 1 | 1 | 1 | 0 | 9 | 0 | 156 | 16 | 0 | 6118 |
| www.w3.org/TR/html52/ | 200 | 2 | 1 | 0 | 1 | 2 | 1 | 0 | 65 | 10 | 0 | 5688 |
| web.dev/articles/lcp | 200 | 0 | 1 | 1 | 1 | 5 | 10 | 0 | 73 | 60 | 44 | 2218 |
| developers.google.com/…/robots/intro | 200 | 0 | 1 | 1 | 1 | 3 | 8 | 0 | 176 | 24 | 20 | 775 |
| react.dev/ | 200 | 0 | 1 | 1 | 2 | 15 | 43 | 0 | 22 | 34 | 8 | 1336 |
| nextjs.org/docs | 200 | 0 | 1 | 1 | 1 | 7 | 22 | 0 | 305 | 22 | 0 | 453 |
| vercel.com/ | 200 | 0 | 1 | 1 | 1 | 17 | 17 | 0 | 76 | 13 | 0 | 103 |
| stripe.com/ | 200 | 1 | 1 | 1 | 2 | 5 | 31 | 0 | 112 | 18 | 89 | 1299 |
| www.djangoproject.com/ | 200 | 0 | 1 | 0 | 1 | 5 | 1 | 0 | 19 | 25 | 0 | 121 |
| httpbin.org/status/404 | 404 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| httpbin.org/redirect/2 | 200 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| www.iana.org/domains/reserved | 200 | 0 | 1 | 0 | 1 | 5 | 1 | 0 | 34 | 7 | 0 | 250 |
| peps.python.org/pep-0008/ | 200 | 0 | 1 | 1 | 2 | 12 | 0 | 0 | 9 | 7 | 0 | 7396 |
| curl.se/docs/manpage.html | 200 | 0 | 1 | 0 | 1 | 20 | 1 | 0 | 54 | 28 | 0 | 41011 |
| www.rfc-editor.org/rfc/rfc9110.html | 200 | 0 | 1 | 0 | 2 | 28 | 0 | 0 | 107 | 23 | 0 | 69513 |

The corpus deliberately spans server-rendered documentation, client-rendered React/Next
applications, news, reference specifications, marketing pages, pages with heavy `hreflang` sets
(stripe.com: 89), pages with Microdata (schema.org), redirects, a 404, and a host that blocks our
user agent (Wikipedia, 403).

### Disagreements found and resolved

Two runs produced disagreements. In both, the page source was inspected to decide which
implementation was technically correct.

**www.w3.org/TR/html52/ — title.** Crawler: `HTML Standard`. Oracle: `None`.
The URL redirects (2 hops) to `html.spec.whatwg.org/multipage/`, which **omits the optional
`<head>` tag entirely** — verified from the raw bytes: `<title>` appears at offset 209 and
`</head>` appears nowhere in the 154 KB document. This is legal HTML5; the `head` element is
implied. lxml synthesises it, `html.parser` does not.
**The crawler was correct; the oracle was fixed** to fall back to document scope when no `head`
element exists.

**vercel.com — word count 103 from a 522 KB page** looked suspicious. Checked against a real
Chrome DOM: `document.querySelector('main').innerText` yields 57 words and `document.body` 60.
The page is genuinely text-sparse. Our higher figure counts DOM text that Chrome's `innerText`
omits because it is hidden by CSS *classes* — the documented static-HTML limitation, not a defect.

### Third-oracle cross-check against a real browser DOM

Three pages were additionally measured in a live Chrome DOM via `document.querySelectorAll`:

| Metric | developers.google.com (browser / crawler) | vercel.com (browser / crawler) |
|---|---|---|
| title | identical | identical |
| `<title>` count | 1 / 1 | 1 / 1 |
| canonical count | 1 / 1 | 1 / 1 |
| h1 / h2 / h3 | 1, 3, 3 / 1, 3, 3 | 1, 17, 6 / 1, 17, 6 |
| images | 8 / 8 | 17 / 17 |
| images missing ALT | 0 / 0 | 0 / 0 |
| images `alt=""` | — | 1 / 1 |
| lang | en / en | en / en |
| hreflang | 20 / 20 | 0 / 0 |
| meta robots | (none) / `None` | `index, max-image-preview:large` (correctly read as permissive) |

Exact agreement on every structural metric against a real browser.

---

## 9. JetOctopus comparison

**Not performed — I have no access to a JetOctopus account, and I will not invent its numbers.**

Fabricating that column would be worse than leaving it empty, and hardcoding our output to match
an assumed JetOctopus result is exactly what the brief forbids. What is in place instead:

1. `scripts/validate_crawler.py` produces our value and an independent DOM-derived value for
   every metric, which is the half of the comparison that establishes ground truth.
2. `GET /api/validate/page?url=…` gives per-value provenance for any single URL, so any specific
   JetOctopus disagreement can be traced to the exact selector, match count and raw attribute
   text in seconds.

To complete the comparison: export a JetOctopus crawl for the same URLs, and for each metric
where the two disagree, call the validate endpoint on that URL. The provenance block states what
we found and why; the decision then rests on the page source, not on either tool's authority.
The two disagreements encountered during this work were both resolved that way, and in both the
page source — not the tool — settled it.

---

## 10. Performance and scale

Accuracy was not traded for speed, and no accuracy shortcut was taken for throughput. Existing
controls remain in force: bounded concurrency (25 workers), per-host rate limiting, request
reuse through a shared `AsyncClient`, retries with backoff, per-request and whole-crawl time
budgets, batched persistence (200 rows), and a bounded render budget so Playwright is a fallback
rather than the default path.

`tests/test_scale.py` (17 tests, all passing) exercises 10,000-page auditing, 10,000-page
scoring, bulk upsert and re-upsert, chunked metric aggregation, deep pagination, a 1,000-page
crawl end to end, and the page-limit and time-budget guard rails. Duplicate detection is
asserted not to degrade quadratically.

---

## 11. Remaining limitations

These are genuine, known, and documented in the code rather than hidden:

1. **CSS-class-driven visibility is not resolved.** Text hidden by a class (rather than the
   `hidden` attribute, `aria-hidden`, or an inline style) counts toward `visible_word_count`.
   Resolving it requires computed styles from a browser for every page. This is the cause of the
   vercel.com word-count gap above.
2. **JetOctopus comparison is outstanding** (§9) — blocked on account access, not on code.
3. **`json_ld_blocks` is a format flag, not a block count.** We record which structured-data
   formats and types are present, not how many JSON-LD `<script>` elements exist. Presence is
   what the rules need; if a per-block count is wanted it is a small addition.
4. **Rendering is a bounded fallback.** With `render_max_pages` exhausted, later client-rendered
   pages are extracted from static HTML. This is recorded (`render_error`,
   `crawl_quality="render_failed"`) and never silently presented as a thin page, but those pages
   are less complete than a rendered crawl would make them.
5. **Content preserved across a failed crawl is stale by design.** `content_captured_at` exposes
   the age, but the dashboard does not yet surface a "signals from an earlier crawl" badge — the
   data is available to it, the UI treatment is not built.
6. **A 403 or bot-block is reported honestly as a no-document response** (Wikipedia in the corpus
   above) rather than worked around. Crawling a site that blocks our user agent needs a
   configuration decision, not a crawler change.
7. **`is_probably_page` uses extension heuristics** to skip assets in sitemaps. An
   extensionless URL serving a PDF would still be queued; it is then rejected by content type at
   fetch time, costing one request.
