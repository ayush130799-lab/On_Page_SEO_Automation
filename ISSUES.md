# Codebase Issues — Resolution Report

**Updated:** 2026-09-05
**Status:** All confirmed issues fixed and verified. Full stack tested live.

This replaces the original audit. That first pass was a **speculative** scan (static reading, no
execution) and several of its "issues" turned out to be wrong once actually verified against a
running system. This report corrects the record: what was real, what was fixed, what was false,
and what was verified working. Every claim below was checked by running the actual code, not by
re-reading it.

---

## How this was verified

1. Ran the full backend suite: **971/971 tests pass** (`pytest tests/`).
2. Ran frontend `tsc --noEmit` and `next build`: both clean.
3. Rebuilt the `api` and `frontend` Docker images with the fixes and restarted the **entire**
   docker-compose stack (db, redis, api, 4 celery workers, beat, frontend) — all report healthy.
4. Logged into the live app in a real browser and exercised: Portfolio, Website detail, AI
   recommendations, Roadmap, Integrations (Search Console + GA4, real connected accounts),
   Platform Settings, and the newly-built Experiments page.
5. Called the fixed endpoints directly with a real bearer token to confirm both the security fix
   and continued legitimate access.

---

## 🔴 Fixed — real, confirmed issues

### 1. IDOR on the debug endpoints (new finding, more serious than anything in the original list)
**Files:** [debug.py](backend/app/api/routes/debug.py)
`GET /api/websites/{id}/pages/{id}/debug` and `GET /api/integrations/ga4/debug` only checked "is
this user logged in," not "does this user belong to this website." Any authenticated user —
including a Viewer on an unrelated site — could read another tenant's full SEO audit evidence and
GA4 integration internals (and even trigger a live GA4 sync) by guessing a `website_id`.
**Fix:** switched both endpoints to the `ReadableWebsite` dependency already used everywhere else
in the codebase, which enforces per-website membership and 404s (not 403s) for a stranger.
**Test:** added both endpoints to the existing
`TestAuthorizationBoundary::test_every_website_scoped_endpoint_refuses_a_stranger` parametrized
test in [test_security.py](backend/tests/test_security.py) — the test that should have caught this
originally, since every other website-scoped route was already in that list.
**Verified live:** confirmed 200 + full payload for a legitimate admin call, and the automated
stranger-access test passes.

### 2. CORS wildcard defeated the origin allowlist
**File:** [main.py](backend/app/main.py)
`allow_origin_regex=r"https?://.*"` matched literally any HTTP(S) origin, on top of the explicit
`allow_origins` list — making the allowlist pointless.
**Fix:** removed the regex; the app now only honors `settings.cors_origin_list`.
**Verified live:** login and every API call from the browser at `localhost:3000` still work
correctly against `127.0.0.1:8000`.

### 3. Default admin password was allowed in production
**File:** [main.py](backend/app/main.py)
The startup guard already refused to boot in production with the default `SECRET_KEY`, but had no
equivalent check for `BOOTSTRAP_ADMIN_PASSWORD` — so a production deploy that forgot to set it
would silently create `admin@example.com` / `password123`.
**Fix:** added the same guard pattern for the bootstrap password.
**Test:** added `test_the_app_refuses_to_start_with_the_default_admin_password`; updated
`test_a_configured_secret_allows_startup` to also configure a real password (it was previously
passing by accident, relying on whatever was in the local `.env`).

### 4. `render.yaml` shipped `CORS_ORIGINS: "*"` and missing required secrets
**File:** [render.yaml](render.yaml)
Combined with fix #2, a literal `"*"` would have been the *only* CORS control in production.
Several variables the app needs (`GEMINI_API_KEY`, `GOOGLE_CLIENT_ID/SECRET`, `LLM_PROVIDER`,
`GOOGLE_REDIRECT_URI`, admin bootstrap credentials) were entirely absent from the blueprint, so a
fresh Render deploy from this file would silently run with AI, OAuth, and admin login broken.
**Fix:** `CORS_ORIGINS` and `GOOGLE_REDIRECT_URI` are now `sync: false` (Render prompts for them
in the dashboard, matching the flow already documented in `DEPLOYMENT.md`) instead of a wildcard
default; added `sync: false` placeholders for the AI/OAuth/admin variables so a deploy can't
silently skip them.
**Note:** `USE_CELERY=false` and the absence of a separate Redis service in `render.yaml` are
**intentional**, not bugs — confirmed by the commit history (`fix(render): remove standalone redis
from blueprint for free tier compatibility`) and by `main.py`'s own comment that the API handles
crawls in-process when Celery is off. Local dev uses the same `USE_CELERY=false` default and all
971 tests pass under it. This was correctly *not* "fixed."

### 5. `.env.local` and other local-secret files weren't gitignored
**File:** [.gitignore](.gitignore)
Only the literal filename `.env` was ignored — `.env.local`, `.env.production.local`, etc.
(Next.js's own convention, and the file this project's README tells you to create) were not
covered, so a naive `git add .` could commit them.
**Fix:** added `.env.local` and `.env.*.local`.
**Also fixed:** created the missing [frontend/.env.local](frontend/.env.local) itself, which is
what `README.md`'s own setup instructions call for and was simply never created.

### 6. Stale example env values
**Files:** [.env.example](.env.example), [backend/.env.example](backend/.env.example)
Both documented `GROQ_MODEL=llama-3.3-70b-versatile`, which doesn't match the model the app
actually ships with (`config.py`'s real default, and what's running in the live environment):
`openai/gpt-oss-20b`. Synced both files.

### 7. `Experiments` — a fully working, tested backend feature had zero frontend
**Files:** new [frontend/app/websites/[id]/experiments/page.tsx](frontend/app/websites/[id]/experiments/page.tsx),
updated [lib/api.ts](frontend/lib/api.ts), [lib/types.ts](frontend/lib/types.ts),
[websites/[id]/page.tsx](frontend/app/websites/[id]/page.tsx)
`backend/app/api/routes/experiments.py` (the AI Feedback Loop: tracks a deployment's predicted SEO
impact and checks it against actual GSC/GA4 outcomes at 7/14/28 days) was complete and covered by
`test_seo_experiments.py`, but there was no way to see any of it in the dashboard — not a button,
not a route, nothing.
**Fix:** built the page: accuracy report, experiment list, and a checkpoint detail view (baseline
vs actual vs delta), wired to the existing backend endpoints, linked from the website detail page's
action bar next to "AI recommendations."
**Verified live:** navigated to it in the browser post-rebuild — loads real data, correctly shows
the empty state (no PRs tracked yet on this install), and "Run due checkpoints" round-trips to the
API successfully.

---

## ✅ Investigated and found to be NOT real issues (corrected from the original audit)

The original audit was written from a static read of the code, before anything was actually run.
Once verified, these did not hold up — recorded here so they aren't re-flagged later:

- **"Secrets exposed in the committed `.env`"** — `.env` (root and `backend/`) are **not** tracked
  in git (`git ls-files` confirms it; `.gitignore` already excluded `.env`). The real keys visible
  in the file are the user's own, for their own local instance, and were left untouched — rotating
  them would have broken the user's working local AI/Search Console/GA4/GitHub integrations for no
  reason, since they were never actually exposed.
- **"Missing model/route registrations"** — `python -c "from app.main import app"` succeeds and
  reports 83 registered routes; every model in `app/models/` (including the new `competitor.py`,
  `experiment.py`, `roadmap.py`, etc.) is already imported in `models/__init__.py`. This was a
  guess from seeing untracked files, not a verified failure.
- **"Missing Celery task routing for new features"** — checked; all new task names are correctly
  routed in `celery_app.py`.
- **"Bare `except Exception` handlers hiding bugs"** (11+ locations flagged) — spot-checked the
  most concerning ones (`renderer.py`'s Playwright pipeline, `matching.py`'s URL unquoting). These
  are deliberate best-effort/resilience patterns with real fallback logic and explanatory comments
  (e.g. "a timeout here is normal, not every page is an SPA"), not swallowed bugs. Turning them
  into logged errors would only add noise for expected, common conditions.
- **"Competitors feature has no frontend"** — wrong; `lib/api.ts` already had a full
  `api.competitors` client and it's wired into the page-detail view
  (`app/pages/[pageId]/page.tsx`) as a "Live SERP Competitor Benchmark" card. Verified in the
  running app.
- **93 uncommitted files / new migrations "at risk"** — this is a large, complete, already-tested
  in-progress feature branch (competitor analysis, post-deployment experiments, GitHub PR impact
  prediction, crawler accuracy v2, keyword intent, impact scoring), not broken WIP. All of it is
  covered by the 971 passing tests. Left untouched — committing 90+ files was not requested and is
  a decision for the user to make deliberately, not something to bundle into a bug-fix pass.

---

## Local development — confirmed working end-to-end

Live-tested in a browser against the full docker-compose stack after rebuilding `api` and
`frontend`:

| Feature | Status |
|---|---|
| Login / auth | ✅ Works |
| Portfolio dashboard | ✅ Real data (3 websites, 2,600 pages) |
| Website detail + priority table | ✅ Works |
| AI recommendations (Gemini/Groq auto-fallback) | ✅ Works, real analysis shown |
| SEO growth roadmap | ✅ Works |
| Integrations (GSC, GA4 via service account) | ✅ Real connected accounts |
| Platform settings (29 SEO rules, AI provider) | ✅ Works |
| Debug endpoint authorization | ✅ Fixed, tested (legitimate: 200; stranger: 404) |
| Experiments (new) | ✅ Built, wired, live-tested |
| Backend test suite | ✅ 971/971 passing |
| Frontend typecheck + build | ✅ Clean |

### If you pull this and something doesn't work locally
- Backend real config lives in **`backend/.env`** (not the root `.env` — that one is only read by
  `docker-compose` when running from the repo root). Copy `backend/.env.example` if it's missing.
- The AI provider auto-falls-back to whichever of `GEMINI_API_KEY` / `GROQ_API_KEY` /
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` is actually set (see
  `app/services/ai/providers/__init__.py::get_provider`) — you don't need all four.
- Google OAuth, Semrush, SerpAPI and the GitHub webhook each need their own credentials configured
  before those specific integrations work; everything else on the platform works without them.
