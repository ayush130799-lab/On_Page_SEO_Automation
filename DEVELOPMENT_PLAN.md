# DEVELOPMENT PLAN

Incremental delivery plan. **The application is runnable and the test suite green after every
phase.** Each phase ends with: run tests → run lint/build → fix errors → update docs.

Status legend: `[ ]` pending · `[~]` in progress · `[x]` complete

---

## Phase 1 — Existing-code audit + architecture `[x]`

* [x] Inspect the full repository; catalogue what works and what is missing
* [x] Verify the baseline test suite (23 tests passing)
* [x] Write `ARCHITECTURE.md` (reuse decisions, target design, data model, pipeline)
* [x] Write `DEVELOPMENT_PLAN.md`

**Outcome:** reuse map recorded; 12 concrete gaps identified.

---

## Phase 2 — Database + backend foundation `[x]`

* [x] Split `app/models.py` into a domain-organised `app/models/` package
* [x] Add all 17 required entities (users, website_members, websites, integrations, pages,
      crawl_runs, seo_audits, seo_issues, gsc/ga4/semrush metrics, priority_scores,
      ai_recommendations, github_events, jobs, historical_metrics, settings)
* [x] Alembic setup + initial migration (`0001_initial`); `create_all()` retained for SQLite tests
* [x] Legacy-safety: the initial migration parks pre-2.0 MVP tables as `legacy_*` instead of
      colliding with or dropping them
* [x] `core/crypto.py` — Fernet envelope encryption for credentials (HKDF-derived key)
* [x] `core/security.py` — bcrypt hashing, JWT access/refresh, signed OAuth state
* [x] `core/logging.py` — secret-redacting log filter
* [x] `core/errors.py` — typed error envelope + global handlers
* [x] `core/ratelimit.py` — Redis-backed fixed-window limiter with in-process fallback
* [x] `core/deps.py` — auth + website-authorization dependencies
* [x] Auth routes (register, login, refresh, me, config) and website CRUD + membership routes
* [x] Extend `config.py` with all new settings (weights defined once, never at call sites)

**Runnable:** `uvicorn app.main:app` serves `/health`, auth and website management. 68 tests pass.

**Carried into Phase 3:** the legacy `/api/audits` routes, `audit_service`, `pdf_service` and their
three tests were removed alongside the old schema; the equivalent (and broader) functionality is
rebuilt on crawl runs in Phase 3.

---

## Phase 3 — Crawler + SEO engine `[x]`

* [x] Refactored `services/crawler.py` into `services/crawler/` (robots, sitemap, fetcher,
      renderer, extractor, orchestrator) preserving the original behaviour
* [x] robots.txt parsed in-process: `Disallow`/`Allow` longest-match precedence, `*`/`$` patterns,
      per-agent blocks, `Crawl-delay`, `Sitemap:` directives
* [x] Recursive sitemap-index walking, gzip support, malformed-XML fallback
* [x] Redirect chains captured; per-host token-bucket rate limiting; retry with backoff and
      `Retry-After`; failure isolation per URL
* [x] Playwright rendering fallback, invoked only for thin/SPA pages, bounded by concurrency and a
      per-run page budget, degrading gracefully when Chromium is absent
* [x] Rule registry (`services/seo/registry.py`) — a new rule is one decorated function
* [x] Ported the original 14 rules with identical thresholds and severities
* [x] Added 10 rules: redirect chains/loops, canonical target mismatch, hreflang, viewport,
      duplicate title, duplicate meta description, duplicate content, orphan pages, image
      dimensions, external-link volume — **24 rules total**
* [x] Configurable scoring weights (registry defaults ← env ← per-website override)
* [x] Site-wide annotation pass so duplication rules run through the same registry
* [x] `services/pipeline.py`: crawl → audit → persist `Page`/`SEOAudit`/`SEOIssue`, stable page
      identity across crawls, incremental mode, website summary refresh
* [x] API: crawl trigger/poll/cancel, page list (priority table) with filter+sort, page detail,
      issue list and issue summary, rule catalogue
* [x] Verified against live sites (fastapi.tiangolo.com, example.com); fixed a trailing-slash
      redirect false positive found only by that real-traffic check

**Runnable:** a crawl of a real website produces pages, audits, issues and SEO scores. 183 tests pass.

---

## Phase 4 — Google Search Console `[x]`

* [x] Google OAuth 2.0 authorization-code flow with a signed, expiring `state` token
* [x] Refresh tokens stored Fernet-encrypted; access tokens refreshed lazily with an expiry skew
* [x] Search Analytics client over the REST API (no heavyweight client library), paginated to the
      25 000-row response cap
* [x] Two queries per sync: `[date, page]` for the daily series, `[page, query]` for top queries
* [x] Property auto-detection across `sc-domain:` / `https://` / `www` variants
* [x] Normalise + upsert into `gsc_metrics`; impression-weighted position when URLs merge
* [x] Backfill + incremental windows, rate-limit retries, failures recorded with secrets redacted

## Phase 5 — Google Analytics 4 `[x]`

* [x] GA4 Data API `runReport` client reusing the same OAuth grant
* [x] Positional header/row response format parsed into named metrics
* [x] Property discovery via the Admin API `accountSummaries`
* [x] Users, new users, sessions, page views, engaged sessions, engagement rate, engagement time,
      bounce rate, conversions and revenue
* [x] `purchaseRevenue` preferred with a `totalRevenue` fallback, so non-ecommerce properties still
      contribute a business signal
* [x] Session-weighted rate merging when several paths collapse onto one page

## Phase 6 — Semrush `[x]`

* [x] Semrush Analytics API client (semicolon CSV, key-in-query) with error-line detection
* [x] `domain_organic_unique` first to find pages with real visibility, then per-URL `url_organic`
      — budget-aware rather than querying every crawled URL
* [x] Striking-distance extraction (positions 4-20) plus aggregate opportunity volume
* [x] Backlinks/referring domains, degrading gracefully when that subscription is absent
* [x] Normalise + upsert into `semrush_metrics`; API-unit balance surfaced on connect
* [x] Site-wide keyword-opportunity endpoint ranked by unlockable volume

**Cross-cutting for 4-6:** `PageResolver` indexes a website's pages once per sync and matches
provider URLs by hash, path, protocol and `www` variants; unmatched rows are counted and sampled
rather than silently dropped. `services/metrics.py` aggregates all three providers in SQL.

**Runnable:** connect endpoints, OAuth callback, property selection, manual and queued syncs.
230 tests pass.

---

## Phase 7 — Priority engine `[x]`

* [x] Four component extractors: SEO severity, GA4 activity, GSC search, Semrush opportunity
* [x] Within-website **percentile-rank** normalisation, so a 200-page brochure site and a
      10 000-page store both produce a usable ranking; log-scaling first so a handful of outliers
      cannot flatten the middle of the distribution
* [x] Weight resolution env → global settings row → per-website override; **no weight literal
      appears at any call site**, and a test greps the engine source to keep it that way
* [x] Weight **redistribution** when a provider has no data, rather than zero-filling — zero-filling
      would compress every page equally and destroy the ranking's resolution
* [x] Persist `PriorityScore` with components, raw inputs, contributing sources and the weight
      vector, so a score stays explainable after the weights are retuned
* [x] P0–P3 banding relative to the site's own distribution, with absolute bands for small sites
* [x] API: rescore, live weight preview without saving, per-website and global weight settings
* [x] **The required behaviour is asserted directly**: a page at SEO 82 with 42 000 users and 980
      conversions outranks a page at SEO 41 with 3 users — and inverts back when severity is
      weighted at 100%, proving the weights drive it rather than the data

---

## Phase 8 — AI recommendations `[x]`

* [x] `LLMProvider` abstraction with Groq (SDK), Anthropic (Messages API + assistant prefill for
      JSON) and OpenAI (chat completions, JSON mode) implementations
* [x] `get_provider()` returns `None` rather than raising when no key is set — AI is an enrichment
      layer, so a deployment without a key still crawls, audits, scores and prioritises
* [x] Structured `PageRecommendation` Pydantic schema: findings carry issue, explanation, why it
      matters, recommended fix, implementation guidance, expected impact, priority, effort and
      confidence; plus concrete `suggested_changes`
* [x] Tolerant validators for the shapes models actually return (P0/urgent priorities, 0-100
      confidences, 0-1 scores, fenced JSON, prose preambles)
* [x] One bounded repair retry that feeds the validation errors back to the model
* [x] Selection gate: top-N **by priority score** (not SEO score), below the score threshold, with
      a CRITICAL override, plus a content-hash cache so unchanged pages are never re-billed
* [x] Skipped pages are marked `skipped` so the dashboard can explain an empty cell
* [x] Failure isolation — one page failing never stops the run
* [x] Prompts carry the real rule findings and real GSC/GA4/Semrush numbers, never speculation
* [x] `GET /api/websites/{id}/ai/selection` exposes the gate's reasoning *before* a paid run
* [x] Persist `AIRecommendation` with tokens, latency and the scores at analysis time

**Runnable:** selection preview, synchronous and queued analysis, recommendation list and detail.
335 tests pass.

---

## Phase 9 — GitHub webhooks `[x]`

* [x] `POST /api/webhooks/github` verifying HMAC-SHA256 over the **raw** body with a constant-time
      comparison; SHA-1 and unprefixed signatures refused; missing secret fails closed
* [x] Secret resolution: per-website (encrypted on the integration) → global env → every configured
      secret when the delivery names no repository (organisation hooks, pings)
* [x] Uniform 401 that does not reveal whether the repository or the secret was wrong
* [x] Idempotent `GitHubEvent` recording by delivery id — GitHub retries never double-crawl
* [x] Website resolution by repository (case-insensitive) and branch
* [x] Changed-file collection across every commit and add/modify/remove list
* [x] Pluggable file→page mapping: explicit `path_map` → global-impact detection (layouts,
      components, config, CSS, middleware) → framework routing conventions for Next (pages + app
      router), Nuxt, Astro, SvelteKit, Remix, Hugo, Jekyll, Gatsby and static HTML
* [x] Incremental re-audit when files map; full re-audit when they do not, when a shared file
      changed, when the change set is too large, or when GitHub omitted the file list
* [x] Unmapped repositories still recorded, with an actionable reason
* [x] `POST /websites/{id}/github/simulate` dry-runs the mapping before a real deploy
* [x] `GET /websites/{id}/github/events` shows what each deploy triggered

**Runnable:** a signed push triggers a scoped re-audit end to end. 426 tests pass.

---

## Phase 10 — Dashboard `[x]`

* [x] Frontend migrated to TypeScript (strict), `tsc --noEmit` clean, production build green
* [x] Typed API client with bearer auth and a shared, de-duplicated token refresh
* [x] Login/registration screen; `AuthGate` shell with redirect, nav and session handling
* [x] Portfolio overview across every website the user can see, ordered by outstanding P0 work,
      with live crawl-progress polling
* [x] Website overview: pages, SEO health, avg score, critical issues, high-priority pages,
      last crawl/sync, integration status, health and priority distributions, top issues
* [x] Priority pages table — URL, SEO score, priority score, users, clicks, impressions,
      conversions, major issues, AI status — server-side sort on every column and six filters
* [x] Page detail: issues with evidence, extracted values, provider metrics, the priority
      breakdown ("why this priority"), history sparklines, AI recommendation, GitHub changes
* [x] Onboarding form, integration connection UI (OAuth, Semrush key, GitHub + mapping simulator)
* [x] Per-website weight settings with a live, unsaved ranking preview
* [x] Backend aggregate endpoints (`/api/dashboard/*`) so each screen makes one request
* [x] Dependency-free SVG sparklines and distribution bars rather than a charting library

**Verified live**, not just built: registered a user, onboarded fastapi.tiangolo.com, crawled 12
pages, scored them, and walked the portfolio → website → page-detail → integrations screens in a
browser with no console errors. Two defects were found only by doing this and fixed:

* timestamps returned naive from SQLite were parsed by the browser as local time, so a crawl that
  had just finished rendered as "6 hours ago" — fixed with a `UTCDateTime` column type applied to
  every timestamp, plus regression tests;
* a rule firing at two different severities was listed twice in "top issues" with partial counts —
  now grouped per rule, keeping the highest severity.

**Runnable:** the full dashboard. 435 tests pass.

---

## Phase 11 — Scalability & optimisation `[x]`

* [x] Batched page/audit/issue writes; streaming crawl persistence; connection pooling
* [x] Redis distributed locks so a second crawl, sync or AI run for the same website is skipped
      rather than duplicating outbound traffic and racing writes (degrades to a no-op lock when
      Redis is absent, since a single process cannot race itself)
* [x] Durable `Job` tracking — Celery's own state is ephemeral and invisible to the dashboard
* [x] Indexes on every hot path; metric aggregation done in SQL, chunked to stay under SQLite's
      bound-parameter cap; list endpoints enrich only the rows they return
* [x] Four Celery queues (crawl/sync/score/ai), late acks, prefetch 1, per-queue scaling
* [x] Beat schedule in dependency order: syncs → rescoring → history rollup
* [x] Daily rollups into `historical_metrics`, idempotent per day, per-page series capped at 500
      pages per site so a large portfolio does not accrue millions of rows a year
* [x] `GET /api/system/health` — job counts, stuck jobs, broker reachability, recent failures
* [x] Verified at scale (`tests/test_scale.py`): 10 000 pages audited, 10 000 scored, 1 000
      crawled, 2 500-page metric aggregation, deep pagination, 25-website portfolio, and proof
      that duplicate detection is not quadratic

A defect surfaced only by the scale tests: when *discovery alone* exceeded `max_pages`, the workers
never reached their own limit check, so a partially-crawled site reported a complete crawl. The
frontier now records truncation at the point of refusal.

---

## Phase 12 — Testing & production hardening `[x]`

* [x] **552 tests** across rules, scoring, crawler internals, provider parsing, priority inversion,
      the AI gate, webhooks, jobs, rollups, authorization, crypto, redaction and scale
* [x] Security suite: SSRF (cloud metadata, private ranges, non-HTTP schemes, unresolvable hosts),
      the full authorization boundary across every website-scoped endpoint, 404-not-403 for
      non-members, credential confidentiality end to end, log redaction, input validation, and the
      production guard that refuses to start with the development `SECRET_KEY`
* [x] Dockerfiles for backend (Playwright base, non-root, healthcheck) and frontend (multi-stage,
      standalone output, non-root, healthcheck)
* [x] Production compose: postgres, redis, a one-shot migration gate every service waits on, API,
      four queue workers, beat and the dashboard
* [x] Documentation refreshed: `README.md`, `ARCHITECTURE.md`, this plan

Two further defects were caught by the security pass and fixed:

* `GET /api/websites/{id}` raised a validation error for **any** website with a connected
  integration — the schema's `integrations` field collided with the ORM relationship of the same
  name. The response is now built explicitly.
* The backend image was built on the Playwright `jammy` tag, which ships Python 3.10 and lacks
  `enum.StrEnum`; migrations failed on first container start. Moved to the `noble` (Python 3.12)
  base and recorded the minimum version in `requirements.txt`.
