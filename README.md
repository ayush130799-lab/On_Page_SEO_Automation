# AI-Powered On-Page SEO Automation Platform

Crawls every website your company builds, audits on-page SEO, enriches each page with Search
Console, GA4 and Semrush data, and ranks the work by **business priority** — then keeps the
analysis current automatically whenever the website's code changes.

> **The question it answers:**
> *"Across all websites developed by our company, which SEO problems should we fix first — based on
> technical severity, actual user activity, search performance and SEO opportunity?"*

---

## Why two scores

Most SEO tools give you one number and a list of everything wrong with everything. That does not
tell a developer what to do on Monday morning.

This platform keeps two scores deliberately separate:

| | Question it answers | Range |
|---|---|---|
| **SEO score** | How technically healthy is this page? | 0–100 |
| **Priority score** | How much does fixing it matter to the business? | 0–100 |

A page scoring **82** with 40 000 monthly users and 300 conversions ranks **above** a page scoring
**55** that nobody visits. That inversion is the whole point, and it is asserted directly in the
test suite (`tests/test_priority.py::TestBusinessValueOutranksTechnicalSeverity`).

```
priority_score = 100 × Σ( weightᵢ × percentile_rank(componentᵢ) )

  SEO severity        40%   how badly the page is broken
  User activity       30%   GA4 users, sessions, conversions, revenue
  Search performance  20%   GSC clicks, impressions, position, CTR gap
  Keyword opportunity 10%   Semrush striking-distance keywords
```

Weights are configuration, never literals in the code: environment defaults → a global settings
row → a per-website override, tunable in the UI with a live preview. Components are normalised by
**percentile rank within the website**, so a 200-page brochure site and a 10 000-page store both
produce a usable ranking. When an integration is missing its weight is **redistributed** across the
others rather than zero-filled — zero-filling would compress every page by the same amount and
destroy the ranking's resolution.

---

## The pipeline

The LLM is the last and narrowest stage, not the first.

```
All pages
  → 1. CRAWL       sitemap.xml → robots.txt → internal links → canonicals → redirects
                    (+ Playwright rendering only when the static HTML is insufficient)
  → 2. SEO AUDIT   24 pluggable rules → issues with severity and evidence
  → 3. SEO SCORE   weighted 0–100 technical health
  → 4. ENRICH      GSC + GA4 + Semrush, per page, per day, kept historically
  → 5. PRIORITY    0–100 business importance (see above)
  → 6. RANK        order by priority, not by severity
  → 7. AI          only the pages that earn the cost
```

**Pages are never sent to the model in bulk.** A page is analysed only when it is in the top *N*
by **priority** *and* scores below the threshold — or carries a `CRITICAL` issue at any score,
because a single `noindex` can sit on a page scoring 95. Unchanged pages reuse their previous
recommendation via a content hash. `GET /api/websites/{id}/ai/selection` shows exactly which pages
would be sent and why, **before** you pay for a run.

---

## Quick start

### With Docker (everything)

```bash
cp .env.example .env
```

Set `SECRET_KEY` to a strong random value:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then:

```bash
docker compose up -d
```

The dashboard is at <http://localhost:3000> and the API docs at <http://localhost:8000/docs>.
Migrations run automatically before anything else starts. The first account you register becomes
the administrator.

### Local development

Start just the infrastructure:

```bash
docker compose up -d db redis
```

Backend:

```bash
cd backend
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Frontend:

```bash
cd frontend
npm install
cp .env.local.example .env.local
npm run dev
```

Workers (optional locally — the API falls back to in-process background tasks when
`USE_CELERY=false`):

```bash
cd backend
celery -A app.celery_app.celery_app worker -Q crawl,sync,score,ai --loglevel=info
celery -A app.celery_app.celery_app beat --loglevel=info
```

**No PostgreSQL?** Leave `DATABASE_URL` unset and the app falls back to a local SQLite file, so a
fresh clone runs with nothing provisioned.

**No LLM key?** Everything except the AI stage works. Crawling, auditing, scoring, prioritisation
and the whole dashboard are unaffected — AI is an enrichment layer, not a dependency.

---

## Using it

1. **Add a website** — name, URL, optionally the GitHub repo, branch and framework.
2. **Connect data sources** on the integrations screen:
   - *Search Console* and *GA4* via Google OAuth (one consent each, then pick a property) —
     step-by-step Cloud setup and a self-checking verifier are in
     [`docs/GOOGLE_INTEGRATION_SETUP.md`](docs/GOOGLE_INTEGRATION_SETUP.md);
   - *Semrush* with an API key, verified against the live API before it is stored;
   - *GitHub* with a repository and webhook secret — the screen shows the payload URL to paste
     into GitHub, and a simulator that dry-runs the file→page mapping before a real deploy.
3. **Crawl.** Progress streams into the dashboard.
4. **Sync** the metrics, then **rescore**. (Both run nightly on their own once workers are up.)
5. **Work the priority table.** Sort and filter by any column; open a page for its issues,
   metrics, priority reasoning, history and AI recommendation.

After that it maintains itself: nightly syncs and rescoring, and a re-audit on every push.

---

## Automatic re-audit on code change

```
git push
  → GitHub webhook (HMAC-SHA256, verified in constant time over the raw body)
  → delivery recorded, idempotent by delivery id
  → website resolved by repository + branch
  → changed files mapped to pages
      ├─ route file changed  → incremental re-audit of just those URLs
      └─ layout/component/config/CSS changed, mapping failed, or too many files
                             → full re-audit
  → SEO + priority recomputed → dashboard updated
```

File→page mapping understands Next.js (both routers), Nuxt, Astro, SvelteKit, Remix, Gatsby, Hugo,
Jekyll and static HTML, and accepts an explicit `path_map` when a codebase does something unusual.
Files that cannot affect rendered output (tests, CI config, README) are ignored entirely.

The webhook is the only unauthenticated write path, so it fails closed: no secret, no signature, a
SHA-1 signature or a modified body are all rejected with an identical 401 that does not reveal
which check failed.

---

## Architecture

```
Next.js 15 + TypeScript dashboard
        │ REST (JWT)
FastAPI ─┼─ PostgreSQL 16 (SQLAlchemy 2.0, Alembic)
        └─ Redis ── Celery workers ── crawl │ sync │ score │ ai
                                        │
                    httpx + Playwright, provider connectors, LLM abstraction
```

`docs/GOOGLE_INTEGRATION_SETUP.md` walks through connecting Search Console and GA4 for real.
`ARCHITECTURE.md` covers the design in depth — the reuse decisions, data model, crawler internals,
priority engine and security model. `DEVELOPMENT_PLAN.md` records what was built in each phase.

### Layout

```
backend/
  alembic/                    migrations
  app/
    api/routes/               auth, websites, crawls, pages, integrations,
                              priority, recommendations, webhooks, dashboard, jobs
    core/                     crypto, security, deps, errors, logging, rate limiting
    models/                   17 tables, domain-organised
    schemas/                  Pydantic request/response contracts
    services/
      crawler/                robots, sitemap, fetcher, renderer, extractor, orchestrator
      seo/                    rule registry + 24 rules, scoring, audit engine
      integrations/           Google OAuth, GSC, GA4, Semrush, page matching
      priority/               components, weights, engine
      ai/                     schema, prompts, providers (Groq/Anthropic/OpenAI), recommender
      github/                 signature, file→page mapping, webhook handler
      jobs/                   Celery tasks, job tracking, distributed locks
      pipeline.py             crawl → audit → persist
      metrics.py, rollup.py
  tests/                      452 tests
frontend/
  app/                        portfolio, website, page detail, integrations, settings, login
  components/, lib/
```

---

## Adding an SEO rule

One decorated function. Scoring, persistence, the dashboard, the API and the AI prompt all read
from the registry, so nothing else changes:

```python
@rule(
    id="favicon",
    check_type="favicon",
    category=IssueCategory.METADATA,
    weight=1.0,
    title="Favicon",
    fix_hint="Add a <link rel=\"icon\"> so the browser tab and search results show your mark.",
)
def check_favicon(page):
    if page.has_favicon:
        return ok("Favicon is present.")
    return warn("No favicon declared.", score=70.0, severity=Severity.LOW)
```

---

## Testing

```bash
cd backend
pytest                          # 452 tests
pytest -k "priority or ai"      # a subset
pytest tests/test_scale.py      # 10 000-page scale characteristics (slower)
```

```bash
cd frontend
npm run typecheck
npm run build
```

Coverage includes the rule engine and scoring bands, crawler internals (robots precedence, sitemap
indexes, redirect chains, retries, rendering fallback), provider parsing against realistically
shaped payloads, the priority inversion the product depends on, the AI selection gate, webhook
signature verification and file mapping, the authorization boundary, credential encryption, log
redaction, and behaviour at 10 000 pages.

---

## Security

* JWT access/refresh tokens, bcrypt password hashing, role plus per-website membership checks.
  A website you cannot see returns **404, not 403** — the API does not confirm that an id exists.
* Integration credentials are Fernet-encrypted at rest with an HKDF-derived key, and are never
  returned by any endpoint, written to a log, or placed in a job payload.
* A redacting log filter strips tokens, keys and `Authorization` headers from every record before
  it reaches a handler.
* SSRF protection on every crawl target: scheme allowlist plus DNS resolution, rejecting private,
  loopback, link-local and reserved addresses.
* Webhook signatures verified with `hmac.compare_digest` over the exact bytes received.
* Per-IP and per-user rate limiting; strict Pydantic validation; typed error envelopes with no
  stack traces.
* `SECRET_KEY` must be set in production — the app refuses to start with the development default.

---

## Configuration

Every setting lives in `backend/.env.example` with an explanation. The ones that matter most:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Signs JWTs and derives the credential-encryption key. **Required in production.** Changing it invalidates sessions and makes stored credentials unreadable. |
| `DATABASE_URL` | PostgreSQL. Falls back to SQLite when unset or unreachable. |
| `REDIS_URL` / `USE_CELERY` | Broker, and whether work is queued or run in-process. |
| `MAX_PAGES`, `CONCURRENT_WORKERS`, `RATE_LIMIT_PER_SECOND` | Crawl scale and politeness. |
| `RENDER_ENABLED`, `RENDER_MAX_PAGES` | JavaScript rendering budget. |
| `PRIORITY_WEIGHT_*` | Priority engine defaults. |
| `LLM_PROVIDER` + `GROQ_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Model backend. |
| `AI_MAX_PAGES`, `AI_SEO_SCORE_THRESHOLD` | How many pages reach the model, and when. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Search Console and GA4 OAuth. |
| `SEMRUSH_API_KEY` | Optional global fallback; per-website keys are set in the UI. |
| `GITHUB_WEBHOOK_SECRET` | Optional global fallback; per-website secrets are set in the UI. |

---

## Operations

* `GET /health` — liveness, unauthenticated.
* `GET /api/system/health` — job counts, stuck jobs, broker reachability, recent failures (admin).
* `GET /api/websites/{id}/jobs` — what ran, when, and why it failed.
* `GET /docs` — full OpenAPI reference.

Nightly, in order: provider syncs → priority rescoring → history rollup. Scheduled crawling is
off by default (`SCHEDULED_CRAWL_ENABLED`) because most sites are better re-audited by their own
deploys.

### Upgrading from the 1.x MVP

The initial migration detects the old schema and renames its tables to `legacy_*` rather than
dropping or colliding with them. Nothing is lost; drop the parked tables once you are satisfied:

```sql
DROP TABLE legacy_recommendations, legacy_ai_analysis, legacy_issues,
           legacy_seo_checks, legacy_pages, legacy_audits, legacy_websites;
```
