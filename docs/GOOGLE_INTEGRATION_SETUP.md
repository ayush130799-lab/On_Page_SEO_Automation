# Connecting Search Console and GA4 for real

Everything in this guide happens in **your** Google account — the platform only stores the tokens
you grant it. Budget about 15 minutes for the first time.

There is a verification tool that checks each step and tells you exactly what is wrong when
something fails, rather than leaving you with Google's generic `403`:

```bash
cd backend
python -m scripts.verify_google check
```

---

## What you need first

- A Google account that can **already see** the data in the products' own UIs:
  - the site at <https://search.google.com/search-console> (any permission level), and/or
  - a GA4 property at <https://analytics.google.com>.
  Read access is granted through those products, not through the Cloud project. If the account
  cannot see the property there, no amount of Cloud configuration will help.
- A website already onboarded **and crawled** in the platform. Provider rows are matched to pages
  by URL, so with no pages every row is unmatched and nothing is stored.

---

## 1. Create a Google Cloud project

<https://console.cloud.google.com/projectcreate> — any name. If you already have a project, reuse
it; nothing here is billable.

## 2. Enable the APIs

This is the single most common cause of a `403` later. Enable **all three** — the Admin API is
separate from the Data API and is what lists your GA4 properties:

| API | Needed for | Link |
|---|---|---|
| Google Search Console API | GSC clicks, impressions, position, queries | [enable](https://console.cloud.google.com/apis/library/searchconsole.googleapis.com) |
| Google Analytics Data API | GA4 users, sessions, conversions, revenue | [enable](https://console.cloud.google.com/apis/library/analyticsdata.googleapis.com) |
| Google Analytics Admin API | Listing your GA4 properties | [enable](https://console.cloud.google.com/apis/library/analyticsadmin.googleapis.com) |

A newly enabled API can take a minute to propagate.

## 3. Configure the OAuth consent screen

**APIs & Services → OAuth consent screen**

- **User type:** *External* (unless you are on Google Workspace and only your own domain needs
  access, in which case *Internal* skips the test-user step).
- Fill in app name and support email. Nothing else is required.
- **Scopes:** you can leave this empty — the app requests scopes at runtime.
- **Test users:** add the Google account you will authorise with. **In Testing mode only listed
  test users can grant access**, and this step is easy to miss.

> **Testing-mode caveat:** while the consent screen is in *Testing*, Google expires refresh tokens
> after **7 days**. The integration will start returning 401 and need reconnecting. That is fine
> for evaluation; publish the consent screen before relying on it. `analytics.readonly` and
> `webmasters.readonly` are "sensitive" scopes, so publishing externally requires Google's
> verification review — an Internal app avoids that entirely.

## 4. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID**

- **Application type:** *Web application*
- **Authorised redirect URIs:** add exactly

  ```
  http://127.0.0.1:8000/api/integrations/google/callback
  ```

  Google matches this **character for character** and treats `127.0.0.1` and `localhost` as
  different hosts. Use whatever `GOOGLE_REDIRECT_URI` is set to — if you change one, change both.
  Plain `http` is allowed for loopback addresses; for a deployed instance it must be `https`.

Copy the client ID and client secret.

## 5. Configure the platform

In `backend/.env`:

```bash
GOOGLE_CLIENT_ID=123456789-abcdef.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-your-secret
GOOGLE_REDIRECT_URI=http://127.0.0.1:8000/api/integrations/google/callback
```

Restart the API afterwards — settings are read once at startup.

---

## 6. Connect and verify

Start the API. Setting `FRONTEND_BASE_URL` empty makes the OAuth callback return JSON in the
browser instead of redirecting to a dashboard you may not have running:

```bash
cd backend
FRONTEND_BASE_URL= uvicorn app.main:app --reload
```

Then, in another terminal:

```bash
cd backend
python -m scripts.verify_google all --website 1
```

It will:

1. check the configuration, the API and the database;
2. confirm the website has crawled pages to match against;
3. open your browser for consent, and wait for the callback;
4. confirm a refresh token was stored and a property auto-selected;
5. list every property the account can see;
6. run a **real sync** and report rows fetched, metrics stored, and matched vs unmatched;
7. print the actual stored rows so you can compare them against the Google UI;
8. rescore priority and show how the ranking changed.

Or drive it through the dashboard instead — **Website → Integrations → Connect with Google** does
the same thing, then use **Sync now**.

---

## Alternative: connect with a service account (no browser, no expiry)

Everything above uses the OAuth consent flow, which needs a human at a browser and — while the
consent screen is unverified — hands out refresh tokens that **expire after 7 days**. For a
platform whose whole point is unattended nightly syncs, a **service account** is the better fit:
it authenticates with a signed assertion, so there is no consent screen, no browser, and no
expiry to manage.

The trade-off: access is granted **per property inside Search Console and Analytics**, not by
consent. Enabling the APIs is not enough on its own.

### 1. Create the service account and key

**Google Cloud Console → IAM & Admin → Service Accounts → Create service account**

Give it a name (no roles are needed — Cloud IAM roles do not grant Search Console or Analytics
access). Then **Keys → Add key → Create new key → JSON** and download the file.

Note the account's email, which looks like:

```
seo-bot@your-project.iam.gserviceaccount.com
```

The APIs from step 2 above still need to be enabled in the project.

### 2. Grant it access to your data

This is the step people miss — the key alone can see nothing.

- **Search Console:** open the property → *Settings → Users and permissions → Add user* → paste
  the service-account email → *Restricted* is enough.
- **GA4:** *Admin → Property access management → +  → Add users* → paste the email → *Viewer*.

### 3. Connect

Through the dashboard: **Integrations → "Use a service account instead"**, paste the JSON.

Through the verifier:

```bash
python -m scripts.verify_google service-account --website 1 --key ~/Downloads/sa-key.json
```

Through the API — the key goes in as a JSON string, so let a tool do the escaping:

```bash
# Get a token
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login -H "Content-Type: application/json" -d '{"email":"you@example.com","password":"your-password"}' | jq -r .access_token)

# Wrap the key file and post it
jq -n --slurpfile k sa-key.json '{key: $k[0]}' > payload.json
curl -X POST http://127.0.0.1:8000/api/websites/1/integrations/gsc/service-account -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" --data-binary @payload.json
rm payload.json
```

The endpoint accepts the key either as a JSON object (as above) or as a raw JSON string.


The key is validated, exchanged for a live token, checked against the property, and only then
stored encrypted. If the grant from step 2 is missing you get a message naming the exact screen
to fix it on, and nothing is saved.

### Which should you use?

| | OAuth consent | Service account |
|---|---|---|
| Setup | Consent screen + test users | Grant an email on each property |
| Browser needed | Yes, per connection | No |
| Token lifetime | 7 days unverified, else until revoked | No expiry — minted per request |
| Suits | A person connecting their own account | Scheduled syncs, CI, servers |
| Access model | Everything the person can see | Only properties explicitly shared |

Both use identical sync, storage and encryption paths; only the token acquisition differs.

---

## When something fails

| Symptom | Cause | Fix |
|---|---|---|
| `redirect_uri_mismatch` on the consent screen | The URI in Cloud Console differs from `GOOGLE_REDIRECT_URI` | Make them identical, including `http` vs `https` and `127.0.0.1` vs `localhost` |
| `403: access_denied` before consent | Your account is not a test user | Add it under OAuth consent screen → Test users |
| `403` when listing properties | The API is not enabled, or the account cannot see the property | Enable all three APIs in step 2; confirm access in the product's own UI |
| `401` after it previously worked | Testing-mode refresh token expired (7 days) | Reconnect, or switch to a service account (no expiry) |
| Service account sees no properties | The email was never granted on the property | Add it in Search Console *Users and permissions* / GA4 *Property access management* |
| `invalid_grant` on a service account | Key revoked, or the clock is skewed | Re-download the key; assertions are time-signed, so check the host clock |
| "Google did not return a refresh token" | Google only issues one on first consent | Revoke at [Account permissions](https://myaccount.google.com/permissions) and reconnect |
| Rows fetched, none matched | URL-shape mismatch | For GSC, pick the right property variant — a `sc-domain:` property reports different URLs than an `https://` prefix property. The tool prints unmatched samples next to your page paths |
| 0 rows fetched, no error | No data in the window, or wrong property | GSC lags 2–3 days; widen with `--days 90`. Verify the selected property is the one with traffic |
| GA4 has data in the UI but not here | Wrong property, or path mismatch | GA4 reports `pagePath` — if your site is served under a path prefix the crawler never saw, the paths will not line up |

---

## Notes on the data

- **Search Console lags 2–3 days.** Syncs end three days back by default, so today and yesterday
  will always look empty. That is Google, not the platform.
- **Re-syncing an overlapping window is safe.** Metrics are upserted on `(page, date)`.
- **Backfill** pulls 90 days: `python -m scripts.verify_google sync --website 1 --days 90`, or the
  "Backfill 90 days" button in the dashboard.
- **Revenue:** GA4 `purchaseRevenue` is used where present, falling back to `totalRevenue`, so
  non-ecommerce properties still contribute a business signal.
- **Only signals with stored rows are weighted.** A connected integration that has never synced
  contributes nothing, and its weight is redistributed across the others. Check with
  `GET /api/websites/{id}/priority/weights`.

## Production

- Use an `https` redirect URI on your real domain and add it to the same OAuth client.
- Publish the consent screen (Internal avoids Google's verification review; External with
  sensitive scopes requires it).
- Set `GOOGLE_REDIRECT_URI` and `PUBLIC_BASE_URL` to the deployed host.
- Nightly syncs then run themselves — see the beat schedule in `app/celery_app.py`.
