# Deployment Guide: Render (Backend) & Vercel (Frontend)

This guide provides step-by-step instructions to deploy the **SEO Automation Platform** with:
- **Backend Stack on Render**: FastAPI Web API, Celery Worker, PostgreSQL Database, and Redis Instance.
- **Frontend Stack on Vercel**: Next.js App.

---

## Architecture Overview

```
┌─────────────────────────┐               ┌─────────────────────────────────────┐
│     Vercel Frontend     │               │           Render Services           │
│    (Next.js App)        │               │                                     │
│  NEXT_PUBLIC_API_URL────┼──────────────►│ ┌─────────────────────────────────┐ │
│                         │   HTTP / REST │ │  FastAPI Web Service              │ │
└─────────────────────────┘               │ └────────────────┬────────────────┘ │
                                          │                  │                  │
                                          │     ┌────────────┴────────────┐     │
                                          │     ▼                         ▼     │
                                          │ ┌───────┐                 ┌───────┐ │
                                          │ │Postgre│                 │ Redis │ │
                                          │ └───────┘                 └───┬───┘ │
                                          │                               │     │
                                          │ ┌─────────────────────────────▼───┐ │
                                          │ │ Celery Background Worker        │ │
                                          │ └─────────────────────────────────┘ │
                                          └─────────────────────────────────────┘
```

---

## Step 1: Push Code to GitHub

Ensure your project is committed and pushed to a GitHub repository:
```bash
git add .
git commit -m "Prepare for Vercel and Render deployment"
git push origin main
```

---

## Step 2: Deploy Backend on Render

Render will host the FastAPI API, Celery worker, PostgreSQL, and Redis.

### Option A: 1-Click Deployment via Render Blueprint (Recommended)

The repository includes a pre-configured `render.yaml` in the root directory.

1. Go to the [Render Dashboard](https://dashboard.render.com/) and click **New +** -> **Blueprint**.
2. Connect your GitHub repository.
3. Render will scan `render.yaml` and provision:
   - `seo-postgres` (PostgreSQL Database)
   - `seo-redis` (Redis Cache & Queue Broker)
   - `seo-automation-api` (FastAPI Web Service built with `./backend/Dockerfile`)
   - `seo-automation-worker` (Celery Worker built with `./backend/Dockerfile`)
4. Set required Environment Variables on the Web Service (`seo-automation-api`) & Worker (`seo-automation-worker`):

| Key | Description / Recommended Value |
|---|---|
| `ENVIRONMENT` | `production` |
| `SECRET_KEY` | Generate with `openssl rand -hex 32` |
| `CORS_ORIGINS` | `https://<your-vercel-app>.vercel.app` |
| `FRONTEND_BASE_URL` | `https://<your-vercel-app>.vercel.app` |
| `PUBLIC_BASE_URL` | `https://seo-automation-api.onrender.com` |
| `GROQ_API_KEY` | Your Groq API Key (if AI feature enabled) |
| `BOOTSTRAP_ADMIN_EMAIL` | Admin login email (e.g. `admin@example.com`) |
| `BOOTSTRAP_ADMIN_PASSWORD` | Admin login password |

5. Click **Apply**. Render will build and launch all 4 services.

---

### Option B: Manual Setup on Render

If you prefer to configure services manually on Render:

1. **PostgreSQL Database**:
   - **New +** -> **PostgreSQL**.
   - Name: `seo-postgres`
   - Copy the **Internal Database URL**.

2. **Redis Instance**:
   - **New +** -> **Redis**.
   - Name: `seo-redis`
   - Copy the **Internal Redis URL**.

3. **FastAPI Web Service**:
   - **New +** -> **Web Service** -> Connect Repo.
   - Name: `seo-automation-api`
   - Environment: `Docker`
   - Dockerfile Path: `./backend/Dockerfile`
   - Docker Context: `./backend`
   - Set Environment Variables (`DATABASE_URL`, `REDIS_URL`, `ENVIRONMENT=production`, `SECRET_KEY`, etc.).
   - Health Check Path: `/health`

4. **Celery Background Worker**:
   - **New +** -> **Background Worker** -> Connect Repo.
   - Name: `seo-automation-worker`
   - Environment: `Docker`
   - Dockerfile Path: `./backend/Dockerfile`
   - Docker Context: `./backend`
   - Command: `celery -A app.celery_app.celery_app worker -c 4 --loglevel=info`
   - Set Environment Variables (`DATABASE_URL`, `REDIS_URL`, `USE_CELERY=true`, `SECRET_KEY`).

---

## Step 3: Run Database Migrations on Render

Run Alembic schema migrations on the deployed PostgreSQL database:

1. In Render Dashboard, open the **`seo-automation-api`** service.
2. Click **Shell** tab to open an interactive terminal.
3. Run:
   ```bash
   alembic upgrade head
   ```

---

## Step 4: Deploy Frontend on Vercel

1. Log in to [Vercel](https://vercel.com/) and click **Add New Project**.
2. Import your GitHub repository.
3. Configure the deployment settings:
   - **Framework Preset**: `Next.js`
   - **Root Directory**: Select `frontend`
   - **Build Command**: `npm run build` (default)
   - **Output Directory**: `.next` (default)
4. Add **Environment Variables**:
   - `NEXT_PUBLIC_API_URL` = `https://seo-automation-api.onrender.com` (Your Render API URL)
5. Click **Deploy**.
6. Once deployed, Vercel will give you a domain URL (e.g. `https://seo-automation-frontend.vercel.app`).

---

## Step 5: Final CORS & Environment Sync

1. Copy your Vercel URL (e.g., `https://seo-automation-frontend.vercel.app`).
2. Go back to Render Dashboard -> **`seo-automation-api`** -> **Environment**.
3. Set/Update:
   - `CORS_ORIGINS` = `https://seo-automation-frontend.vercel.app`
   - `FRONTEND_BASE_URL` = `https://seo-automation-frontend.vercel.app`
4. Click **Save Changes** (Render will automatically redeploy the backend API).

---

## Step 6: Verification

1. **Verify Backend Health**:
   Open `https://seo-automation-api.onrender.com/health` in your browser. Expected response:
   ```json
   { "status": "ok", "version": "2.0.0", "environment": "production" }
   ```
2. **Verify Frontend**:
   Navigate to `https://seo-automation-frontend.vercel.app`.
   Sign in using your bootstrap admin credentials or register a new user.
3. Test initiating a website crawl to verify communication between Frontend -> API -> Redis -> Celery Worker -> PostgreSQL.
