"""Application configuration.

Every tunable value lives here and is overridable through the environment. In particular the
priority-engine weights are defined **once** in this module and resolved at runtime through
``app.services.priority.weights`` (env -> global settings row -> per-website override) so that no
call site ever hard-codes them.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Core ────────────────────────────────────────────────────────────────
    app_name: str = "SEO Automation Platform"
    environment: str = "development"
    debug: bool = False

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/seo_automation"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800

    redis_url: str = "redis://localhost:6379/0"
    use_celery: bool = False

    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    public_base_url: str = "http://127.0.0.1:8000"
    frontend_base_url: str = "http://localhost:3000"

    # ── Security ────────────────────────────────────────────────────────────
    # MUST be overridden in production. Used for JWT signing and to derive the
    # credential-encryption key.
    secret_key: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 14
    allow_registration: bool = True
    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "password123"

    rate_limit_enabled: bool = True
    rate_limit_auth_per_minute: int = 10
    rate_limit_default_per_minute: int = 120

    # ── Crawler ─────────────────────────────────────────────────────────────
    max_pages: int = 5000
    request_timeout: int = 15
    crawl_delay: float = 0.0
    concurrent_workers: int = 25
    max_retries: int = 3
    rate_limit_per_second: float = 20.0
    crawl_time_budget_seconds: int = 3600
    respect_robots_txt: bool = True
    user_agent: str = (
        "Mozilla/5.0 (compatible; SEO-Automation-Crawler/2.0; +https://example.com/bot)"
    )
    allow_local_crawl: bool = False
    crawl_persist_batch_size: int = 200

    # JavaScript rendering (Playwright) — fallback only, never the default path.
    render_enabled: bool = True
    render_concurrency: int = 5
    render_timeout_ms: int = 6000
    render_min_text_length: int = 400
    render_max_pages: int = 150

    # ── SEO scoring weights (per rule check id) ─────────────────────────────
    # Consumed by app.services.seo.scoring. Overridable per website via settings.
    seo_weights: dict[str, float] = {
        "http_status": 10.0,
        "robots": 10.0,
        "canonical": 5.0,
        "title": 8.0,
        "meta_description": 7.0,
        "h1": 10.0,
        "heading_structure": 5.0,
        "content": 20.0,
        "image_alt": 10.0,
        "internal_links": 5.0,
        "broken_links": 5.0,
        "url_structure": 2.0,
        "structured_data": 1.5,
        "open_graph": 1.5,
        "redirect_chain": 4.0,
        "canonical_target": 4.0,
        "hreflang": 1.0,
        "viewport": 1.0,
        "duplicate_title": 3.0,
        "duplicate_meta_description": 2.0,
        "duplicate_content": 4.0,
        "orphan_page": 2.0,
        "external_links": 1.0,
    }

    # Score bands (0-100). 90 is deliberately the top of the MEDIUM band.
    seo_band_low_issues: float = 90.0    # score > this  -> "LOW ISSUES"
    seo_band_medium_issues: float = 75.0  # score >= this -> "MEDIUM ISSUES"

    # ── Priority engine weights ─────────────────────────────────────────────
    # Defaults mandated by the spec. Never referenced directly outside
    # app.services.priority.weights.
    priority_weight_seo_severity: float = 0.40
    priority_weight_ga4_activity: float = 0.30
    priority_weight_gsc_search: float = 0.20
    priority_weight_semrush_opportunity: float = 0.10

    # Sub-weights inside the GA4 activity component.
    ga4_weight_users: float = 0.35
    ga4_weight_sessions: float = 0.15
    ga4_weight_conversions: float = 0.30
    ga4_weight_revenue: float = 0.20

    # Sub-weights inside the GSC search component.
    gsc_weight_clicks: float = 0.45
    gsc_weight_impressions: float = 0.30
    gsc_weight_position: float = 0.15
    gsc_weight_ctr_gap: float = 0.10

    # Sub-weights inside the Semrush opportunity component.
    semrush_weight_keywords: float = 0.30
    semrush_weight_traffic: float = 0.25
    semrush_weight_striking_distance: float = 0.30
    semrush_weight_backlinks: float = 0.15

    # Metric lookback window used when computing priority components.
    priority_metric_window_days: int = 90

    # ── AI ──────────────────────────────────────────────────────────────────
    ai_enabled: bool = True
    llm_provider: str = "gemini"  # gemini | groq | anthropic | openai

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    ai_max_pages: int = 5000
    ai_concurrency: int = 5
    ai_max_content_length: int = 8000
    ai_max_retries: int = 3
    ai_timeout_seconds: int = 90
    # A page is sent to the LLM when its SEO score is at or below this threshold
    # (set to 100.0 to analyze all pages regardless of SEO score).
    ai_seo_score_threshold: float = 100.0
    ai_reuse_when_unchanged: bool = True

    # ── Integrations ────────────────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8000/api/integrations/google/callback"
    google_oauth_state_ttl_seconds: int = 600

    semrush_api_key: str = ""
    semrush_database: str = "us"
    semrush_api_base: str = "https://api.semrush.com"

    integration_sync_backfill_days: int = 90
    integration_sync_window_days: int = 3
    integration_http_timeout: int = 60
    integration_max_retries: int = 4

    github_webhook_secret: str = ""
    github_api_base: str = "https://api.github.com"

    # ── Jobs / scheduling ───────────────────────────────────────────────────
    daily_sync_hour_utc: int = 4
    scheduled_crawl_enabled: bool = False
    scheduled_crawl_hour_utc: int = 5

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False
    )

    # ── Derived helpers ─────────────────────────────────────────────────────
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def resolved_google_redirect_uri(self) -> str:
        if self.google_redirect_uri and not self.google_redirect_uri.startswith("http://127.0.0.1:8000"):
            return self.google_redirect_uri
        if self.public_base_url and not self.public_base_url.startswith("http://127.0.0.1:8000"):
            return f"{self.public_base_url.rstrip('/')}/api/integrations/google/callback"
        return self.google_redirect_uri

    @property
    def default_priority_weights(self) -> dict[str, float]:
        return {
            "seo_severity": self.priority_weight_seo_severity,
            "ga4_activity": self.priority_weight_ga4_activity,
            "gsc_search": self.priority_weight_gsc_search,
            "semrush_opportunity": self.priority_weight_semrush_opportunity,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
