# Development Guide

Practical notes for working on the platform. For *what* it does and how to run it, see
[`README.md`](../README.md); for *why* it is shaped this way, see
[`ARCHITECTURE.md`](../ARCHITECTURE.md).

---

## Getting set up

Python **3.11 or newer** is required (`enum.StrEnum`, PEP 604 unions evaluated at runtime).

```bash
docker compose up -d db redis          # infrastructure only

cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

```bash
cd frontend
npm install
cp .env.local.example .env.local
npm run dev
```

Tests need nothing running — they use an in-memory SQLite database and mock every outbound call.

---

## Where things live

```
app/
  api/routes/        HTTP only: validate, authorize, delegate, serialise
  core/              cross-cutting: crypto, security, deps, errors, logging, rate limiting
  models/            SQLAlchemy, one module per domain area
  schemas/           Pydantic request/response contracts
  services/          all the behaviour
  utils/             pure helpers with no app dependencies
```

The rule that keeps this navigable: **routes contain no logic**. If an endpoint is doing more than
validating input, checking authorization and calling a service, the logic belongs in `services/`.

---

## Adding an SEO rule

One decorated function, in any module under `services/seo/rules/`:

```python
@rule(
    id="favicon",                      # stable; stored on every issue row
    check_type="favicon",              # scoring key; several rules may share one
    category=IssueCategory.METADATA,
    weight=1.0,                        # relative contribution to the SEO score
    title="Favicon",
    fix_hint="Add a <link rel=\"icon\"> so the tab and search results show your mark.",
)
def check_favicon(page):
    if page.has_favicon:
        return ok("Favicon is present.")
    return warn("No favicon declared.", score=70.0, severity=Severity.LOW)
```

Import the module in `services/seo/rules/__init__.py` and you are done. Scoring, persistence, the
dashboard, the API and the AI prompt all read from the registry.

Return `ok`, `warn` or `fail`, or `None` for "nothing to say" (treated as a pass). A rule that
raises is logged and degrades to a pass — one buggy rule must never lose a whole page's audit.

**Site-wide rules** (duplicates, orphans) read attributes that `engine.annotate_site` computes
across the crawl, so they run through the same registry rather than a parallel path. Mark them
`site_wide=True` and add the annotation in `annotate_site`.

**Needs a new signal?** Add the field to `ExtractedPage` and populate it in
`crawler/extractor.py`, then add the column to `models/page.py` and `pipeline._snapshot_page` if
it should persist.

---

## Adding an integration

1. A module in `services/integrations/` exposing `sync(db, website, **kwargs)`.
2. Use `base.request_with_retry` for HTTP — it handles rate limits, backoff and turns provider
   errors into messages that are safe to show a user (provider bodies often echo the API key).
3. Use `PageResolver` to map provider URLs to pages. Never guess: count unmatched rows instead.
4. Store credentials with `upsert_integration(credentials={...})`; they are encrypted for you.
   **Never** put a credential in a log line, an API response or a Celery payload.
5. Add the provider to `IntegrationProvider`, the sync task, and the priority component if it
   contributes a signal.

Upserts must be idempotent — a re-sync of an overlapping window is routine.

---

## Adding an LLM provider

Subclass `LLMProvider`, implement `complete()`, register it in `providers/__init__.py` and add its
key to `provider_credentials`. Everything above it — prompts, schema validation, the repair retry,
the selection gate, persistence — is provider-agnostic.

---

## Testing

```bash
pytest                        # everything
pytest -k priority            # one area
pytest tests/test_scale.py    # slow; 10 000-page characteristics
pytest -x -q                  # stop at the first failure
```

Conventions worth keeping:

* **Test the behaviour, not the implementation.** `test_the_high_value_page_ranks_above_the_broken_low_traffic_one`
  is the product requirement; `test_compute_priorities_calls_percentile_ranks` is not.
* **Mock at the transport boundary.** `httpx.MockTransport` with payloads shaped like the real API
  — GA4's positional header/row format, Semrush's semicolon CSV — so the parsing code is genuinely
  exercised.
* **Assert the negative case.** A security test that only proves the happy path proves nothing.
* Fixtures that depend on "now" must build dates relative to `date.today()`; a pinned literal
  silently falls outside the metric window and the test stops testing anything.

---

## Database changes

```bash
alembic revision --autogenerate -m "add favicon column"
alembic upgrade head
alembic downgrade -1
```

Autogenerate against **PostgreSQL**, not SQLite: SQLite cannot express JSONB, most `ALTER` forms,
or several constraint types, so a migration generated there will be wrong in production.

Then check the generated file. Autogenerate does not detect renames (it emits a drop plus an add,
which loses data), server-default changes, or `CHECK` constraints.

Circular foreign keys have no valid creation order — `crawl_runs` ↔ `github_events` is resolved by
declaring the FK in one direction only.

---

## Debugging

**A crawl finds nothing.** Check `robots.txt` (`respect_robots_txt`), that the sitemap is reachable,
and `exclude_patterns`. `is_safe_url` refuses private addresses — set `ALLOW_LOCAL_CRAWL=true` to
crawl a local server, never in production.

**Pages look empty.** Client-rendered. Confirm Chromium is installed
(`playwright install chromium`) and `render_mode` is not `never`. Rendering falls back silently
when the browser is missing — the log line says so.

**A sync matches nothing.** The sync summary reports `unmatched` and sample URLs. Usually the
Search Console property is a different variant (`sc-domain:` vs `https://`), or GA4 paths carry a
prefix the crawler never saw.

**Priority is all severity.** The engine only weights signals that have *stored rows*, not merely a
connected integration. Run a sync first; `GET /api/websites/{id}/priority/weights` shows the
effective weights and contributing sources.

**AI skips everything.** By design when pages are healthy.
`GET /api/websites/{id}/ai/selection` gives the reason for each page.

**A webhook 401s.** The signature covers the exact bytes sent — any proxy that re-serialises the
body breaks it. Verify the secret matches and the content type is `application/json`.

**Work is not running.** `GET /api/system/health` reports broker reachability, stuck jobs and
recent failures. With `USE_CELERY=false` work runs in-process and dies with the API.

---

## Conventions

* Type hints everywhere; `from __future__ import annotations` at the top of every module.
* Comments explain *why*. The code already says what.
* Log with `%s` placeholders, never f-strings — the redaction filter operates on `record.args`.
* Never catch bare `Exception` without logging it and having a reason to continue.
* Failure isolation: one bad page, rule or website must never abort a batch.
* Configuration over constants. A number a user might reasonably want to change belongs in
  `config.py`, and if it is per-website, in `settings`.
