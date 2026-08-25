/**
 * Typed API client.
 *
 * Handles the two things every call needs and no component should repeat: attaching the bearer
 * token, and transparently refreshing it once when the access token has expired. A concurrent
 * burst of requests during a refresh shares one in-flight refresh rather than firing several.
 */

import type {
  ApiErrorBody,
  CrawlRun,
  IntegrationSummary,
  PageDetailResponse,
  PageListItem,
  Paginated,
  PortfolioOverview,
  RecommendationListItem,
  SelectionDecision,
  TokenPair,
  TrendPoint,
  User,
  Website,
  WebsiteOverview,
  WeightResponse,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";

const ACCESS_KEY = "seo.access_token";
const REFRESH_KEY = "seo.refresh_token";

export class ApiError extends Error {
  status: number;
  code: string;
  details?: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  get isAuthError() {
    return this.status === 401;
  }
}

// ── Token storage ──────────────────────────────────────────────────────────
// localStorage keeps the dashboard a pure static bundle that can be served from anywhere; the
// tokens are short-lived and the API is the only thing that trusts them.

export const tokens = {
  access: () => (typeof window === "undefined" ? null : localStorage.getItem(ACCESS_KEY)),
  refresh: () => (typeof window === "undefined" ? null : localStorage.getItem(REFRESH_KEY)),
  set(pair: TokenPair) {
    localStorage.setItem(ACCESS_KEY, pair.access_token);
    localStorage.setItem(REFRESH_KEY, pair.refresh_token);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
  get isAuthenticated() {
    return Boolean(tokens.access());
  },
};

let refreshInFlight: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  // Share one refresh across concurrent 401s so a page load with six requests does not fire six.
  if (refreshInFlight) return refreshInFlight;

  const refreshToken = tokens.refresh();
  if (!refreshToken) return false;

  refreshInFlight = (async () => {
    try {
      const response = await fetch(`${API_BASE}/api/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!response.ok) {
        tokens.clear();
        return false;
      }
      tokens.set((await response.json()) as TokenPair);
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  skipAuth?: boolean;
  retryOnAuthFailure?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, query, skipAuth = false, retryOnAuthFailure = true } = options;

  const url = new URL(`${API_BASE}${path}`);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }

  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (!skipAuth) {
    const token = tokens.access();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  let response: Response;
  try {
    response = await fetch(url.toString(), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    // Retry once after 2.5s in case Render backend is waking up from free tier sleep
    try {
      await new Promise((resolve) => setTimeout(resolve, 2500));
      response = await fetch(url.toString(), {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      throw new ApiError(
        0,
        "network_error",
        `Could not reach the API at ${API_BASE}. Is the backend running?`,
      );
    }
  }

  if (response.status === 401 && retryOnAuthFailure && !skipAuth) {
    if (await refreshAccessToken()) {
      return request<T>(path, { ...options, retryOnAuthFailure: false });
    }
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const parsed = text ? safeJson(text) : null;

  if (!response.ok) {
    const envelope = parsed as ApiErrorBody | null;
    throw new ApiError(
      response.status,
      envelope?.error?.code ?? "http_error",
      envelope?.error?.message ?? `Request failed with HTTP ${response.status}.`,
      envelope?.error?.details,
    );
  }

  return parsed as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

// ── Endpoints ──────────────────────────────────────────────────────────────

export const api = {
  auth: {
    config: () => request<{ registration_enabled: boolean }>("/api/auth/config", { skipAuth: true }),
    register: (email: string, password: string, fullName?: string) =>
      request<User>("/api/auth/register", {
        method: "POST",
        body: { email, password, full_name: fullName || null },
        skipAuth: true,
      }),
    login: async (email: string, password: string) => {
      const pair = await request<TokenPair>("/api/auth/login", {
        method: "POST",
        body: { email, password },
        skipAuth: true,
      });
      tokens.set(pair);
      return pair;
    },
    me: () => request<User>("/api/auth/me"),
    logout: () => tokens.clear(),
  },

  dashboard: {
    overview: (windowDays?: number) =>
      request<PortfolioOverview>("/api/dashboard/overview", {
        query: { window_days: windowDays },
      }),
    website: (id: number, windowDays?: number) =>
      request<WebsiteOverview>(`/api/dashboard/websites/${id}`, {
        query: { window_days: windowDays },
      }),
    trends: (id: number, days = 90) =>
      request<{ days: number; points: TrendPoint[] }>(
        `/api/dashboard/websites/${id}/trends`,
        { query: { days } },
      ),
  },

  websites: {
    list: (query?: { search?: string; limit?: number; offset?: number }) =>
      request<Paginated<Website>>("/api/websites", { query }),
    get: (id: number) => request<Website>(`/api/websites/${id}`),
    create: (payload: Record<string, unknown>) =>
      request<Website>("/api/websites", { method: "POST", body: payload }),
    update: (id: number, payload: Record<string, unknown>) =>
      request<Website>(`/api/websites/${id}`, { method: "PATCH", body: payload }),
    remove: (id: number) =>
      request<{ message: string }>(`/api/websites/${id}`, { method: "DELETE" }),
  },

  crawls: {
    start: (websiteId: number, payload: Record<string, unknown> = { mode: "full" }) =>
      request<CrawlRun>(`/api/websites/${websiteId}/crawls`, { method: "POST", body: payload }),
    list: (websiteId: number, limit = 20) =>
      request<Paginated<CrawlRun>>(`/api/websites/${websiteId}/crawls`, { query: { limit } }),
    get: (crawlRunId: number) => request<CrawlRun>(`/api/crawls/${crawlRunId}`),
    cancel: (crawlRunId: number) =>
      request<CrawlRun>(`/api/crawls/${crawlRunId}/cancel`, { method: "POST" }),
  },

  pages: {
    list: (
      websiteId: number,
      query: Record<string, string | number | boolean | undefined> = {},
    ) => request<Paginated<PageListItem>>(`/api/websites/${websiteId}/pages`, { query }),
    detail: (pageId: number, historyDays = 90) =>
      request<PageDetailResponse>(`/api/pages/${pageId}`, {
        query: { history_days: historyDays },
      }),
    issues: (websiteId: number, query: Record<string, string | number | undefined> = {}) =>
      request<{ total: number; items: Record<string, unknown>[] }>(
        `/api/websites/${websiteId}/issues`,
        { query },
      ),
  },

  integrations: {
    list: (websiteId: number) =>
      request<IntegrationSummary[]>(`/api/websites/${websiteId}/integrations`),
    authorizeGoogle: (websiteId: number, provider: "gsc" | "ga4") =>
      request<{ authorization_url: string }>(
        `/api/websites/${websiteId}/integrations/${provider}/authorize`,
        { method: "POST" },
      ),
    gscProperties: (websiteId: number) =>
      request<{ selected: string | null; properties: { site_url: string }[] }>(
        `/api/websites/${websiteId}/integrations/gsc/properties`,
      ),
    selectGscProperty: (websiteId: number, siteUrl: string) =>
      request(`/api/websites/${websiteId}/integrations/gsc/property`, {
        method: "PUT",
        body: { site_url: siteUrl },
      }),
    ga4Properties: (websiteId: number) =>
      request<{
        selected: string | null;
        properties: { property_id: string; display_name: string; account: string }[];
      }>(`/api/websites/${websiteId}/integrations/ga4/properties`),
    selectGa4Property: (websiteId: number, propertyId: string) =>
      request(`/api/websites/${websiteId}/integrations/ga4/property`, {
        method: "PUT",
        body: { property_id: propertyId },
      }),
    connectServiceAccount: (
      websiteId: number,
      provider: "gsc" | "ga4",
      payload: { key: string; site_url?: string; property_id?: string },
    ) =>
      request(`/api/websites/${websiteId}/integrations/${provider}/service-account`, {
        method: "POST",
        body: payload,
      }),
    connectSemrush: (websiteId: number, payload: Record<string, unknown>) =>
      request(`/api/websites/${websiteId}/integrations/semrush`, {
        method: "POST",
        body: payload,
      }),
    connectGithub: (websiteId: number, payload: Record<string, unknown>) =>
      request(`/api/websites/${websiteId}/integrations/github`, {
        method: "POST",
        body: payload,
      }),
    sync: (websiteId: number, provider: string, backfill = false) =>
      request<{ provider: string; status: string }>(
        `/api/websites/${websiteId}/integrations/${provider}/sync`,
        { method: "POST", body: { backfill } },
      ),
    syncAll: (websiteId: number, backfill = false) =>
      request<{ provider: string; status: string }[]>(
        `/api/websites/${websiteId}/integrations/sync-all`,
        { method: "POST", body: { backfill } },
      ),
    disconnect: (websiteId: number, provider: string) =>
      request<{ message: string }>(`/api/websites/${websiteId}/integrations/${provider}`, {
        method: "DELETE",
      }),
    keywordOpportunities: (websiteId: number, limit = 50) =>
      request<Record<string, unknown>[]>(
        `/api/websites/${websiteId}/integrations/semrush/opportunities`,
        { query: { limit } },
      ),
  },

  priority: {
    weights: (websiteId: number) =>
      request<WeightResponse>(`/api/websites/${websiteId}/priority/weights`),
    setWeights: (websiteId: number, weights: Record<string, number>) =>
      request<WeightResponse>(`/api/websites/${websiteId}/priority/weights`, {
        method: "PUT",
        body: weights,
      }),
    rescore: (websiteId: number) =>
      request<{ pages_scored: number; weights: Record<string, number> }>(
        `/api/websites/${websiteId}/priority/score`,
        { method: "POST" },
      ),
    preview: (websiteId: number, weights: Record<string, number>) =>
      request<{ top_pages: Record<string, unknown>[]; weights: Record<string, number> }>(
        `/api/websites/${websiteId}/priority/preview`,
        { query: weights },
      ),
  },

  ai: {
    providers: () =>
      request<{
        enabled: boolean;
        active: string;
        configured: string[];
        max_pages_per_run: number;
        seo_score_threshold: number;
      }>("/api/ai/providers"),
    selection: (websiteId: number) =>
      request<{
        selected_count: number;
        considered_count: number;
        decisions: SelectionDecision[];
      }>(`/api/websites/${websiteId}/ai/selection`),
    analyse: (websiteId: number, payload: Record<string, unknown> = {}) =>
      request<{ status: string; analysed: number; skipped: number; cached: number }>(
        `/api/websites/${websiteId}/ai/analyse`,
        { method: "POST", body: payload },
      ),
    recommendations: (websiteId: number, limit = 50) =>
      request<Paginated<RecommendationListItem>>(
        `/api/websites/${websiteId}/recommendations`,
        { query: { limit } },
      ),
  },

  github: {
    events: (websiteId: number, limit = 20) =>
      request<{ items: Record<string, unknown>[] }>(
        `/api/websites/${websiteId}/github/events`,
        { query: { limit } },
      ),
    simulate: (websiteId: number, files: string[]) =>
      request<{
        requires_full_recrawl: boolean;
        reason: string;
        affected_paths: string[];
        mapped_files: Record<string, string>;
        unmapped_files: string[];
        ignored_files: string[];
      }>(`/api/websites/${websiteId}/github/simulate`, { method: "POST", body: files }),
  },

  seo: {
    rules: () =>
      request<
        { id: string; check_type: string; category: string; title: string; weight: number }[]
      >("/api/seo/rules"),
  },
};
