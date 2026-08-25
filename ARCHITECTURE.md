# ARCHITECTURE — AI-Powered On-Page SEO Automation Platform

> **The question this platform answers:**
> *"Across all websites developed by our company, which SEO problems should we fix first — based on
> technical severity, actual user activity, search performance and SEO opportunity — and how does
> that analysis update automatically whenever the website code changes?"*

---

## 1. Existing-code audit (baseline before this work)

The repository already contained a working single-site SEO audit MVP. It was **kept and extended**,
not rewritten.

### 1.1 What existed and was reused

| Area | File(s) | Verdict |
|---|---|---|
| FastAPI app + CORS + lifespan | `backend/app/main.py` | **Reused**, extended with auth, error handlers, structured logging, new routers |
| Settings via pydantic-settings | `backend/app/config.py` | **Reused**, extended (weights, secrets, provider keys, integration config) |
| SQLAlchemy 2.0 engine + SQLite dev fallback | `backend/app/db.py` | **Reused**, `create_all()` bootstrap replaced by Alembic (fallback retained for tests) |
| Async httpx crawler w/ queue, retries, backoff, sitemap+robots discovery, SSRF guard | `backend/app/services/crawler.py` | **Reused as the core fetch engine**, refactored into `services/crawler/` with a persistent frontier, redirect chains, Playwright rendering and per-host rate limiting |
| BeautifulSoup/lxml page extractor | `backend/app/services/extractor.py` | **Reused**, extended (hreflang, viewport, og/twitter, JSON-LD parsing, word count, content hash, rel=nofollow) |
| 14 deterministic SEO rules | `backend/app/services/seo_rules.py` | **Logic reused verbatim**, restructured into a pluggable rule registry (`services/seo/rules/`) so new rules are one decorated function |
| Weighted 0–100 scoring | `backend/app/services/scoring.py` | **Reused**, weights moved into configurable settings |
| Groq AI engine w/ JSON mode, retries, rate-limit backoff | `backend/app/services/ai_engine.py` | **Reused** as the Groq implementation behind a new `LLMProvider` abstraction |
| Celery + Redis task app w/ retry | `backend/app/celery_app.py` | **Reused**, extended to a multi-queue worker topology |
| ReportLab PDF export | `backend/app/services/pdf_service.py` | **Reused** |
| SSRF-safe URL utilities | `backend/app/utils/url_utils.py` | **Reused**, extended (URL hashing, path-pattern extraction) |
| Next.js 15 / React 19 dashboard | `frontend/app/**` | **Reused as the frontend stack**; migrated to TypeScript and expanded from a single-audit view to a multi-website portfolio dashboard |
| Test suite (23 tests, all passing) | `backend/tests/**` | **Reused**, updated for the new schema and heavily extended |
| Docker Compose (postgres + redis) | `docker-compose.yml` | **Reused**, extended with api / worker / beat / frontend services |

### 1.2 Gaps that this work closes

1. **Single-site only.** No concept of a portfolio of company websites, and no users/tenancy.
2. **No authentication or authorization.** Every endpoint was public.
3. **No integrations.** GSC, GA4, Semrush and GitHub were entirely absent, as was any secure
   credential/token storage.
4. **No business signal.** Prioritisation used technical severity only — a broken page with zero
   traffic outranked a converting page with a fixable issue.
5. **No priority score.** `seo_score` doubled as both health and importance.
6. **Weights and thresholds hard-coded** throughout the pipeline.
7. **AI provider hard-wired to Groq**; no abstraction.
8. **No page identity across crawls.** Pages were children of an audit, so nothing could be tracked
   over time and no historical metrics were possible.
9. **Playwright was a dependency but never invoked** — JS-rendered pages were audited as empty.
10. **No migrations** (`create_all()` plus ad-hoc `ALTER TABLE` probing in `init_db`).
11. **No GitHub webhooks**, no code-change-triggered re-audit.
12. **No job tracking**, no incremental audits, no scheduled syncs.

---

## 2. System overview

```
                          ┌──────────────────────────────────────────┐
                          │   Next.js 15 + TypeScript Dashboard      │
                          │   portfolio · website · page · settings  │
                          └───────────────────┬──────────────────────┘
                                              │ REST (JWT bearer)
                          ┌───────────────────▼──────────────────────┐
                          │            FastAPI API layer             │
                          │  auth · websites · integrations · pages  │
                          │  priority · recommendations · webhooks   │
                          └───────┬───────────────────────┬──────────┘
                                  │ enqueue               │ read
                       ┌──────────▼─────────┐   ┌─────────▼──────────┐
                       │  Redis (broker +   │   │    PostgreSQL      │
                       │  cache + locks)    │   │  SQLAlchemy 2.0    │
                       └──────────┬─────────┘   └─────────▲──────────┘
                                  │                       │
              ┌───────────────────▼───────────────────────┴─────────────────┐
              │                   Celery workers (4 queues)                 │
              │  crawl · sync · score · ai                                  │
              └──┬────────────┬─────────────┬──────────────┬────────────────┘
                 │            │             │              │
        ┌────────▼───┐ ┌──────▼──────┐ ┌────▼─────┐ ┌──────▼────────┐
        │  Crawler   │ │ Integration │ │ Priority │ │ AI Recommend. │
        │ httpx +    │ │ connectors  │ │  engine  │ │  engine       │
        │ Playwright │ │ GSC GA4 SEM │ │          │ │ (LLM abstr.)  │
        └────────────┘ └─────────────┘ └──────────┘ └───────────────┘
                 ▲
                 │  GitHub push webhook (HMAC-SHA256 verified)
        ┌────────┴──────────────────────────────────────────────────┐
        │  github.com  →  /api/webhooks/github  →  re-audit trigger │
        └───────────────────────────────────────────────────────────┘
```

---

## 3. The core pipeline

The mandated ordering is enforced end-to-end; the LLM is the **last** and **narrowest** stage.

```
All pages of a website
   │
   ├─ 1. CRAWL          sitemap.xml → robots.txt → internal links → canonicals → redirects
   │                     (+ Playwright render only when static HTML is insufficient)
   │
   ├─ 2. SEO AUDIT      pluggable rule registry → SEOIssue rows with severity
   │
   ├─ 3. SEO SCORE      weighted 0–100 technical health per page
   │
   ├─ 4. ENRICHMENT     GSC + GA4 + Semrush page-level metrics (historical, normalised)
   │
   ├─ 5. PRIORITY SCORE 0–100 business importance = weighted blend of
   │                     severity(40) · GA4 activity(30) · GSC search(20) · Semrush opp.(10)
   │
   ├─ 6. RANK           order pages by priority score, descending
   │
   └─ 7. AI ANALYSIS    only for selected pages (see §7) → structured JSON recommendation
```

**Why the two scores are separate.** `seo_score` answers *"how technically healthy is this page?"*.
`priority_score` answers *"how much does fixing it matter to the business?"*. A page at SEO 82 with
40 000 monthly users and 300 conversions ranks **above** a page at SEO 55 with 3 users — exactly the
behaviour required. See §6.

---

## 4. Data model

Persistent **page identity** is the backbone: a `Page` belongs to a `Website` (not to a crawl), so
audits, metrics, scores and recommendations accumulate against a stable row over time.

```
User ──< WebsiteMember >── Website ──< Integration
                              │
                              ├──< CrawlRun ──< SEOAudit >── Page
                              │                    │
                              │                    └──< SEOIssue
                              ├──< Page ──< GSCMetric
                              │      ├──< GA4Metric
                              │      ├──< SemrushMetric
                              │      ├──< PriorityScore
                              │      ├──< AIRecommendation
                              │      └──< HistoricalMetric
                              ├──< GitHubEvent
                              └──< Job
Setting (global / per-website key-value, JSON)
```

| Table | Purpose |
|---|---|
| `users` | Auth principals; bcrypt password hash, role (`admin` / `member` / `viewer`) |
| `website_members` | Per-website authorization grants |
| `websites` | A site built by the company: base URL, domain, GitHub repo/branch, crawl config |
| `integrations` | One row per (website, provider). Encrypted credential blob + status + last sync |
| `pages` | Stable page identity: `(website_id, url_hash)` unique. Latest snapshot columns for fast listing |
| `crawl_runs` | One crawl execution: trigger, status, counters, progress, timings |
| `seo_audits` | Per-page result of one crawl run: score, category, severity, priority band, extracted snapshot |
| `seo_issues` | One row per detected problem: rule id, severity, description, evidence JSON |
| `gsc_metrics` | Page × date: clicks, impressions, ctr, position (+ `queries` JSON) |
| `ga4_metrics` | Page × date: users, sessions, engagement rate/duration, conversions, revenue |
| `semrush_metrics` | Page × date: organic keywords/traffic/cost, backlinks, keyword opportunities JSON |
| `priority_scores` | Page × computed_at: final score + per-component breakdown + weights snapshot |
| `ai_recommendations` | Structured LLM output (JSON), provider/model, tokens, confidence, status |
| `github_events` | Received webhook deliveries: delivery id, event, commits, changed files, action taken |
| `jobs` | Unified job tracking across queues: type, status, progress, error, timings, payload |
| `historical_metrics` | Daily rollups (page and website scope) so trends survive metric retention windows |
| `settings` | Configurable weights/thresholds — global defaults overridable per website |

All hot query paths are indexed: `(website_id, url_hash)`, `(page_id, date)` on every metric table,
`(website_id, priority_score DESC)`, `(crawl_run_id, severity)`.

---

## 5. Crawler

Located in `backend/app/services/crawler/`.

* **Discovery order:** `robots.txt` (`Sitemap:` directives, `Disallow` rules honoured) →
  `sitemap.xml` / `sitemap_index.xml` (recursive, gzip-aware) → internal `<a href>` links →
  canonical targets → redirect destinations.
* **Concurrency:** bounded worker pool over an `asyncio.Queue` frontier with a shared
  `httpx.AsyncClient` (keep-alive pooling), per-host token-bucket rate limiting, exponential-backoff
  retries and **failure isolation** — a single exception, timeout or 5xx never aborts the run.
* **Redirects:** the full chain is captured (`redirect_chain`, `final_url`) so chains and loops are
  detectable as issues rather than silently followed.
* **JS rendering:** static HTML first. Playwright/Chromium is invoked **only** when a page looks
  under-rendered (body text below threshold, or an SPA root element with no content), or the site is
  configured `render_mode = "always"`. Rendering runs in a separately-bounded pool because it is
  ~50× the cost of an HTTP fetch.
* **Scale:** designed for 10 000+ pages per site — batched inserts, URL-key-only in-memory state,
  `max_pages` and time-budget caps, resumable frontier state on the `crawl_runs` row.

## 6. Priority engine

`backend/app/services/priority/engine.py`. **Weights are never hard-coded at call sites** — they are
resolved through `Settings` → global `settings` table → per-website override, and every computed
`PriorityScore` row stores the weight vector that produced it, so historical scores stay explainable.

```
priority_score = 100 × Σ( wᵢ × normalise(componentᵢ) )

default weights:  seo_severity 0.40 · ga4_activity 0.30 · gsc_search 0.20 · semrush_opportunity 0.10
```

Each component is normalised to 0–1 **relative to the website's own distribution** (percentile rank
over the site's pages), so a 200-page brochure site and a 10 000-page e-commerce site both produce
usable rankings. Missing integrations are handled by *weight redistribution*, not by zero-filling —
a site without GA4 renormalises the remaining weights instead of penalising every page equally.

## 7. AI recommendation engine

`backend/app/services/ai/`. A provider abstraction (`LLMProvider`) with Groq, Anthropic and OpenAI
implementations, selected by `LLM_PROVIDER`. All responses are **structured JSON**, schema-validated
with Pydantic before persistence, with a bounded repair retry on malformed output.

**Selection gate — pages must earn an LLM call:**

1. Page must be in the top `AI_MAX_PAGES` by **priority score** (not by SEO score), and
2. `seo_score` below `AI_SEO_SCORE_THRESHOLD` (default 90) **or** carrying a `CRITICAL` issue, and
3. content hash changed since the last recommendation (cache hit otherwise).

Healthy, high-scoring pages are skipped with `ai_status = "skipped"`. Each recommendation returns
issue explanation, why it matters, recommended fix, concrete suggested SEO changes, expected impact,
priority, confidence and developer-facing implementation guidance.

## 8. GitHub integration

```
push → HMAC-SHA256 signature verify (constant-time) → GitHubEvent row (idempotent by delivery id)
     → resolve website by repo full name + branch
     → collect changed files
     → map files → pages   (route conventions, content collections, explicit path_map)
     → matched: incremental re-audit of affected pages
     → unmatched/broad change (config, layout, template): full site re-audit
     → recompute SEO + priority → refresh dashboard
```

Signature verification is mandatory; the secret is stored encrypted and never logged or returned by
the API. File→page mapping is a pluggable resolver so precision can improve without touching the
webhook path.

## 9. Security

* JWT access/refresh tokens; bcrypt password hashing; role + per-website membership authorization.
* All third-party credentials encrypted at rest with Fernet (AES-128-CBC + HMAC) using a key derived
  from `SECRET_KEY`; ciphertext never leaves the server — the API returns status and metadata only.
* Google OAuth 2.0 authorization-code flow with a signed, expiring `state` parameter.
* SSRF protection on every outbound crawl target (scheme allowlist + DNS resolution + private/
  loopback/link-local/reserved IP rejection).
* A redacting log filter strips tokens, keys and `Authorization` headers from all log records.
* Per-IP and per-user rate limiting on auth and audit-trigger endpoints.
* Strict Pydantic validation on every request body; typed error envelopes; no stack traces to clients.

## 10. Technology choices

| Concern | Choice | Reason |
|---|---|---|
| Backend | Python 3.11+ / FastAPI | Already in place; async-native, matches the crawler's I/O profile |
| ORM / DB | SQLAlchemy 2.0 / PostgreSQL 16 | Already in place; JSON columns for evidence and metric payloads |
| Migrations | Alembic | Replaces `create_all()` + ad-hoc `ALTER TABLE` |
| Queue / cache | Celery + Redis | Already in place; four queues isolate crawl / sync / score / ai |
| Crawl | httpx + BeautifulSoup/lxml + Playwright fallback | Already in place; rendering added |
| Frontend | Next.js 15 + React 19 + TypeScript + Tailwind | Already in place; migrated to TS |
| AI | Provider abstraction (Groq default, Anthropic, OpenAI) | Removes vendor lock-in from the existing Groq-only path |
| Deploy | Docker + Docker Compose | Extends the existing compose file |
