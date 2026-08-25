"""End-to-end verification of the Google Search Console and GA4 integrations.

Walks the real OAuth flow against Google, then runs a real sync and reports what landed. Every
step is checked separately and failures are translated into the specific thing to fix, because
Google's own errors ("403 Forbidden") do not say *which* of five setup steps was missed.

    # 1. Check configuration and reachability without connecting anything
    python -m scripts.verify_google check

    # 2. Connect a provider for a website (opens your browser)
    python -m scripts.verify_google connect --website 1 --provider gsc

    # 3. Sync and inspect what came back
    python -m scripts.verify_google sync --website 1 --provider gsc --days 28

    # 2b. Or connect with a service account instead - no browser, no token expiry
    python -m scripts.verify_google service-account --website 1 --key sa-key.json

    # 4. Everything at once
    python -m scripts.verify_google all --website 1

The API must be running (it owns the OAuth callback URL). Run it with FRONTEND_BASE_URL="" so
the callback returns JSON in the browser instead of redirecting to a dashboard you may not have
running:

    FRONTEND_BASE_URL= uvicorn app.main:app --reload
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import time
import webbrowser
from datetime import date, timedelta
from typing import Any

import httpx

from app.config import settings
from app.core.errors import IntegrationError
from app.db import SessionLocal
from app.models import (
    GA4Metric,
    GSCMetric,
    Integration,
    IntegrationProvider,
    IntegrationStatus,
    Page,
    Website,
)
from app.services.integrations import ga4, google_oauth, gsc
from app.services.integrations.base import read_credentials, upsert_integration

GREEN, RED, YELLOW, BLUE, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m",
)


def ok(message: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {message}")


def fail(message: str, fix: str = "") -> None:
    print(f"  {RED}FAIL{RESET}  {message}")
    if fix:
        for line in fix.strip().splitlines():
            print(f"        {DIM}{line.strip()}{RESET}")


def warn(message: str) -> None:
    print(f"  {YELLOW}WARN{RESET}  {message}")


def info(message: str) -> None:
    print(f"  {DIM}{message}{RESET}")


def heading(message: str) -> None:
    print(f"\n{BLUE}{message}{RESET}")


# ── Step 1: configuration ───────────────────────────────────────────────────


def check_config(require_oauth: bool = True) -> bool:
    """Verify the OAuth client is configured and the API is reachable."""
    heading("Configuration")
    healthy = True

    if require_oauth:
        if settings.google_client_id and settings.google_client_secret:
            ok(f"OAuth client configured ({settings.google_client_id[:20]}...)")
        else:
            healthy = False
            fail(
                "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set",
                """
                Google Cloud Console -> APIs & Services -> Credentials
                -> Create credentials -> OAuth client ID -> Web application
                Copy both values into backend/.env
                """,
            )
    else:
        info("Service account mode — skipping OAuth client credential check.")

    if not settings.google_client_id.endswith(".apps.googleusercontent.com"):
        if settings.google_client_id:
            warn(
                "GOOGLE_CLIENT_ID does not look like a Google client id "
                "(it should end in .apps.googleusercontent.com)"
            )

    print(f"  {DIM}Redirect URI: {settings.google_redirect_uri}{RESET}")
    info(
        "This must appear *character for character* under 'Authorised redirect URIs' on the "
        "OAuth client. Google treats localhost and 127.0.0.1 as different hosts."
    )

    base = settings.google_redirect_uri.split("/api/")[0]
    try:
        response = httpx.get(f"{base}/health", timeout=5)
        if response.status_code == 200:
            ok(f"API is reachable at {base}")
        else:
            healthy = False
            fail(f"API at {base} returned HTTP {response.status_code}")
    except Exception:
        healthy = False
        fail(
            f"API is not running at {base}",
            'Start it with:  FRONTEND_BASE_URL= uvicorn app.main:app --reload',
        )

    if settings.frontend_base_url:
        info(
            f"FRONTEND_BASE_URL is {settings.frontend_base_url} - after consent your browser "
            "will be redirected there. Set it empty to see the raw JSON result instead."
        )

    # The script talks to the database directly, so a missing schema must be reported as such
    # rather than surfacing as a SQLAlchemy traceback.
    db = SessionLocal()
    try:
        db.query(Website).first()
        ok(f"Database reachable ({settings.database_url.split('@')[-1]})")
    except Exception as exc:
        healthy = False
        fail(
            f"Database is not usable: {type(exc).__name__}",
            """
            Run the migrations first:  alembic upgrade head
            And confirm DATABASE_URL matches the one the API is using.
            """,
        )
    finally:
        db.close()

    return healthy


def check_website(website_id: int) -> Website | None:
    """The website must exist and have crawled pages, or nothing can be matched."""
    heading(f"Website {website_id}")
    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            fail(
                f"Website {website_id} does not exist",
                "Create one in the dashboard, or: POST /api/websites",
            )
            return None

        ok(f"{website.name} - {website.url}")

        page_count = db.query(Page).filter(Page.website_id == website.id).count()
        if page_count == 0:
            fail(
                "The website has no crawled pages",
                """
                Provider rows are matched to pages by URL. With no pages, every row is
                unmatched and nothing is stored. Run a crawl first:
                  POST /api/websites/<id>/crawls  {"mode": "full"}
                """,
            )
            return None

        ok(f"{page_count} pages available to match against")
        sample = db.query(Page).filter(Page.website_id == website.id).limit(3).all()
        for page in sample:
            info(f"  {page.path}")
        return website
    finally:
        db.close()


# ── Step 2: connect ─────────────────────────────────────────────────────────


def connect(website_id: int, provider: str, timeout_seconds: int = 300) -> bool:
    """Run the real OAuth consent flow and wait for the callback to land."""
    heading(f"Connecting {provider.upper()} for website {website_id}")

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            fail(f"Website {website_id} does not exist")
            return False

        existing = (
            db.query(Integration)
            .filter(Integration.website_id == website_id, Integration.provider == provider)
            .first()
        )
        if existing is None:
            db.add(Integration(website_id=website_id, provider=provider))
            db.commit()

        try:
            url = google_oauth.build_authorization_url(website_id, provider, user_id=0)
        except Exception as exc:
            fail(f"Could not build the consent URL: {exc}")
            return False

        print(f"\n  Open this URL and grant access:\n\n  {url}\n")
        try:
            webbrowser.open(url)
            info("(opened in your default browser)")
        except Exception:
            pass

        info(f"Waiting up to {timeout_seconds}s for the callback...")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            db.expire_all()
            integration = (
                db.query(Integration)
                .filter(
                    Integration.website_id == website_id, Integration.provider == provider
                )
                .first()
            )
            if integration and integration.status == IntegrationStatus.CONNECTED:
                ok(f"Connected as {integration.account_label or 'unknown account'}")

                credentials = read_credentials(integration)
                if google_oauth.is_service_account(credentials):
                    ok("Service-account auth - no token expiry, no reconsent")
                    return True
                if credentials.get("refresh_token"):
                    ok("Refresh token stored (encrypted)")
                else:
                    fail(
                        "No refresh token was returned",
                        """
                        Revoke the app at https://myaccount.google.com/permissions
                        and connect again - Google only returns a refresh token on first consent.
                        """,
                    )

                target = (integration.config or {}).get(
                    "site_url" if provider == "gsc" else "property_id"
                )
                if target:
                    ok(f"Property selected automatically: {target}")
                else:
                    warn("No property was auto-selected - choose one before syncing")
                return True

            if integration and integration.status == IntegrationStatus.ERROR:
                fail(f"Authorisation failed: {integration.last_error}")
                return False

            time.sleep(2)

        fail("Timed out waiting for the callback")
        info("If the browser showed an error, the message there names the cause.")
        return False
    finally:
        db.close()



def connect_service_account(website_id: int, provider: str, key_path: str) -> bool:
    """Connect with a service-account key file - no browser, no token expiry."""
    heading(f"Connecting {provider.upper()} with a service account")

    try:
        raw = io.open(key_path, encoding="utf-8").read()
    except OSError as exc:
        fail(f"Could not read {key_path}: {exc}")
        return False

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            fail(f"Website {website_id} does not exist")
            return False

        try:
            credentials = google_oauth.parse_service_account_key(raw)
        except Exception as exc:
            fail(f"The key file was rejected: {exc}")
            return False

        ok(f"Key parsed for {credentials['client_email']}")

        try:
            token = asyncio.run(
                google_oauth.verify_service_account(credentials, provider)
            )
            ok("Google accepted the service account")
        except Exception as exc:
            fail(f"Google rejected it: {exc}")
            info(
                "Check the key is current and that the Cloud project has the APIs enabled."
            )
            return False

        config = {"auth_mode": "service_account"}
        try:
            if provider == IntegrationProvider.GSC:
                entries = asyncio.run(gsc.list_sites(token))
                if not entries:
                    fail(
                        "The service account cannot see any Search Console property",
                        f"""
                        Add {credentials['client_email']} as a user of the property:
                        Search Console -> Settings -> Users and permissions -> Add user
                        """,
                    )
                    return False
                detected = asyncio.run(gsc.detect_site_url(token, website))
                if detected:
                    config["site_url"] = detected
                    ok(f"Property auto-detected: {detected}")
                else:
                    warn("Could not auto-detect the property; select one before syncing")
                    for entry in entries[:10]:
                        info(f"  {entry.get('siteUrl')}")
            else:
                properties = asyncio.run(ga4.list_properties(token))
                if not properties:
                    fail(
                        "The service account cannot see any GA4 property",
                        f"""
                        Add {credentials['client_email']} as a Viewer:
                        Analytics -> Admin -> Property access management -> Add users
                        """,
                    )
                    return False
                if len(properties) == 1:
                    config["property_id"] = properties[0]["property_id"]
                    ok(
                        f"Property auto-selected: {properties[0]['property_id']} "
                        f"({properties[0]['display_name']})"
                    )
                else:
                    warn("Several properties visible; select one before syncing")
                    for prop in properties[:10]:
                        info(f"  {prop['property_id']}  {prop['display_name']}")
        except Exception as exc:
            fail(f"Property discovery failed: {exc}")
            return False

        upsert_integration(
            db, website, provider, credentials=credentials, config=config,
            account_label=credentials["client_email"],
            status=IntegrationStatus.CONNECTED,
        )
        ok("Stored (encrypted at rest)")
        return True
    finally:
        db.close()


# ── Step 3: API access ──────────────────────────────────────────────────────


async def check_api_access(website_id: int, provider: str) -> bool:
    """Call the provider's list endpoint and translate the failure into the fix."""
    heading(f"{provider.upper()} API access")

    db = SessionLocal()
    try:
        integration = (
            db.query(Integration)
            .filter(Integration.website_id == website_id, Integration.provider == provider)
            .first()
        )
        if integration is None or not integration.credentials_encrypted:
            fail(f"{provider} is not connected - run the connect step first")
            return False

        try:
            token = await google_oauth.get_access_token(db, integration)
            ok("Access token obtained (refreshed if it had expired)")
        except IntegrationError as exc:
            fail(f"Could not obtain an access token: {exc.message}")
            return False

        try:
            if provider == IntegrationProvider.GSC:
                entries = await gsc.list_sites(token)
                if not entries:
                    fail(
                        "The account has no Search Console properties",
                        """
                        Add the site at https://search.google.com/search-console
                        and make sure the Google account you authorised has at least
                        'Restricted' access to it.
                        """,
                    )
                    return False
                ok(f"{len(entries)} Search Console propert(ies) visible:")
                for entry in entries[:10]:
                    marker = (
                        "  <- selected"
                        if entry.get("siteUrl") == (integration.config or {}).get("site_url")
                        else ""
                    )
                    info(f"  {entry.get('siteUrl')}  [{entry.get('permissionLevel')}]{marker}")
            else:
                properties = await ga4.list_properties(token)
                if not properties:
                    fail(
                        "The account has no GA4 properties",
                        """
                        Confirm the account can see a property at https://analytics.google.com
                        and that the Google Analytics Admin API is enabled in your Cloud project.
                        """,
                    )
                    return False
                ok(f"{len(properties)} GA4 propert(ies) visible:")
                selected = (integration.config or {}).get("property_id")
                for prop in properties[:10]:
                    marker = "  <- selected" if prop["property_id"] == selected else ""
                    info(
                        f"  {prop['property_id']}  {prop['display_name']} "
                        f"({prop['account']}){marker}"
                    )
            return True

        except IntegrationError as exc:
            fail(f"API call failed: {exc.message}")
            _explain_api_error(provider, exc)
            return False
    finally:
        db.close()


def _explain_api_error(provider: str, exc: IntegrationError) -> None:
    """Translate Google's generic errors into the setup step that was missed."""
    message = str(exc).lower()
    if "403" in message or "forbidden" in message or "denied" in message:
        api = (
            "Google Search Console API"
            if provider == IntegrationProvider.GSC
            else "Google Analytics Data API *and* Google Analytics Admin API"
        )
        fail(
            "403 usually means the API is not enabled, or the account lacks access",
            f"""
            1. Enable {api} in your Cloud project:
               https://console.cloud.google.com/apis/library
            2. Confirm the Google account you authorised can see the property in the
               product's own UI.
            3. Newly enabled APIs can take a minute to propagate.
            """,
        )
    elif "401" in message or "unauthoris" in message or "unauthoriz" in message:
        fail(
            "401 means the stored credentials were rejected",
            """
            If your OAuth consent screen is in 'Testing', refresh tokens expire after 7 days.
            Reconnect, or publish the consent screen.
            """,
        )


# ── Step 4: sync ────────────────────────────────────────────────────────────


async def run_sync(website_id: int, provider: str, days: int) -> bool:
    """Run a real sync and report what landed in the database."""
    heading(f"{provider.upper()} sync - last {days} days")

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            fail(f"Website {website_id} does not exist")
            return False

        module = gsc if provider == IntegrationProvider.GSC else ga4
        started = time.monotonic()
        try:
            summary = await module.sync(db, website, days=days)
        except IntegrationError as exc:
            fail(f"Sync failed: {exc.message}")
            _explain_api_error(provider, exc)
            return False
        except Exception as exc:
            fail(f"Sync failed: {type(exc).__name__}: {exc}")
            return False

        elapsed = time.monotonic() - started
        ok(f"Sync completed in {elapsed:.1f}s")

        info(f"Window:         {summary.get('start_date')} to {summary.get('end_date')}")
        info(f"Rows fetched:   {summary.get('rows_fetched', 0)}")
        info(f"Metrics stored: {summary.get('metrics_upserted', 0)}")
        info(f"Pages matched:  {summary.get('matched', 0)}")
        info(f"Unmatched:      {summary.get('unmatched', 0)}")

        if summary.get("rows_fetched", 0) == 0:
            warn("Google returned no rows at all")
            info(
                "Either the property genuinely has no data in this window, or the wrong "
                "property is selected. Search Console also lags 2-3 days."
            )
            return True

        unmatched = summary.get("unmatched", 0)
        matched = summary.get("matched", 0)
        if unmatched and matched == 0:
            fail(
                "Google returned data but none of it matched a crawled page",
                """
                Almost always a URL-shape mismatch. Compare the samples below with the page
                paths printed earlier. For Search Console check you picked the right property
                variant (sc-domain: vs https:// vs www).
                """,
            )
            for sample in summary.get("unmatched_samples", [])[:5]:
                info(f"  unmatched: {sample}")
            return False

        if unmatched:
            warn(f"{unmatched} rows did not match a page (usually fine - old or filtered URLs)")
            for sample in summary.get("unmatched_samples", [])[:3]:
                info(f"  unmatched: {sample}")

        _show_stored_rows(db, website_id, provider)
        return True
    finally:
        db.close()


def _show_stored_rows(db, website_id: int, provider: str) -> None:
    """Print what actually landed, so the numbers can be eyeballed against the Google UI."""
    print()
    if provider == IntegrationProvider.GSC:
        rows = (
            db.query(GSCMetric, Page.path)
            .join(Page, GSCMetric.page_id == Page.id)
            .filter(GSCMetric.website_id == website_id)
            .order_by(GSCMetric.clicks.desc())
            .limit(5)
            .all()
        )
        if not rows:
            warn("No gsc_metrics rows were written")
            return
        print(f"  {'PATH':<40} {'DATE':<12} {'CLICKS':>7} {'IMPR':>8} {'POS':>6}")
        for metric, path in rows:
            print(
                f"  {path[:38]:<40} {metric.date!s:<12} {metric.clicks:>7} "
                f"{metric.impressions:>8} {metric.position or 0:>6.1f}"
            )
        with_queries = (
            db.query(GSCMetric)
            .filter(GSCMetric.website_id == website_id, GSCMetric.queries.isnot(None))
            .first()
        )
        if with_queries and with_queries.queries:
            print("\n  Top queries stored for one page:")
            for query in with_queries.queries[:5]:
                info(
                    f"  \"{query['query']}\" - {query['clicks']} clicks, "
                    f"{query['impressions']} impressions, position {query['position']}"
                )
        else:
            warn("No top-query data was attached")
    else:
        rows = (
            db.query(GA4Metric, Page.path)
            .join(Page, GA4Metric.page_id == Page.id)
            .filter(GA4Metric.website_id == website_id)
            .order_by(GA4Metric.users.desc())
            .limit(5)
            .all()
        )
        if not rows:
            warn("No ga4_metrics rows were written")
            return
        print(f"  {'PATH':<38} {'DATE':<12} {'USERS':>6} {'SESS':>6} {'CONV':>6} {'REVENUE':>10}")
        for metric, path in rows:
            print(
                f"  {path[:36]:<38} {metric.date!s:<12} {metric.users:>6} "
                f"{metric.sessions:>6} {metric.conversions:>6.0f} {metric.revenue:>10.2f}"
            )


# ── Step 5: the point of it all ─────────────────────────────────────────────


def check_priority_effect(website_id: int) -> None:
    """Confirm the freshly synced metrics actually change the ranking."""
    heading("Effect on priority")

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            return

        from app.services.priority import available_data_sources, score_website

        sources = available_data_sources(db, website_id)
        ok(f"Signals with data: {', '.join(sorted(sources))}")
        if sources == {"seo"}:
            warn("Still SEO-only - priority is pure technical severity until metrics land")
            return

        result = score_website(db, website)
        ok(f"Rescored {result.pages_scored} pages")
        info(f"Effective weights: {result.weights}")

        top = (
            db.query(Page)
            .filter(Page.website_id == website_id, Page.priority_score.isnot(None))
            .order_by(Page.priority_score.desc())
            .limit(5)
            .all()
        )
        print(f"\n  {'PATH':<38} {'PRIORITY':>9} {'BAND':>5} {'SEO':>6}")
        for page in top:
            print(
                f"  {page.path[:36]:<38} {page.priority_score or 0:>9.1f} "
                f"{page.priority_band or '-':>5} {page.seo_score or 0:>6.1f}"
            )
        info(
            "If a page here has a higher SEO score than one below it, the business signals are "
            "doing their job."
        )
    finally:
        db.close()


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the Google Search Console and GA4 integrations end to end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        choices=["check", "connect", "service-account", "sync", "all"],
        help="What to run.",
    )
    parser.add_argument("--website", type=int, default=1, help="Website id (default: 1).")
    parser.add_argument(
        "--provider", choices=["gsc", "ga4", "both"], default="both", help="Which provider."
    )
    parser.add_argument("--days", type=int, default=28, help="Sync window in days.")
    parser.add_argument(
        "--key",
        help="Path to a service-account JSON key (for the service-account command).",
    )
    args = parser.parse_args()

    providers = (
        [IntegrationProvider.GSC, IntegrationProvider.GA4]
        if args.provider == "both"
        else [args.provider]
    )

    print(f"{BLUE}Google integration verification{RESET}")

    if not check_config(require_oauth=(args.command not in ("service-account", "sync"))):
        print(f"\n{RED}Fix the configuration above before continuing.{RESET}")
        return 1

    if args.command == "check":
        check_website(args.website)
        for provider in providers:
            asyncio.run(check_api_access(args.website, provider))
        return 0

    if check_website(args.website) is None:
        return 1

    if args.command == "service-account":
        if not args.key:
            print(f"{RED}--key is required: path to the service-account JSON file.{RESET}")
            return 1
        failures = 0
        for provider in providers:
            if not connect_service_account(args.website, provider, args.key):
                failures += 1
                continue
            if not asyncio.run(check_api_access(args.website, provider)):
                failures += 1
        print()
        if failures:
            print(f"{RED}{failures} provider(s) failed.{RESET}")
            return 1
        print(f"{GREEN}Connected. Now run:  python -m scripts.verify_google sync{RESET}")
        return 0

    failures = 0
    for provider in providers:
        if args.command in ("connect", "all"):
            if not connect(args.website, provider):
                failures += 1
                continue

        if not asyncio.run(check_api_access(args.website, provider)):
            failures += 1
            continue

        if args.command in ("sync", "all"):
            if not asyncio.run(run_sync(args.website, provider, args.days)):
                failures += 1

    if args.command in ("sync", "all"):
        check_priority_effect(args.website)

    print()
    if failures:
        print(f"{RED}{failures} provider(s) failed - see above.{RESET}")
        return 1
    print(f"{GREEN}All checks passed.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
