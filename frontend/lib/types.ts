/**
 * Types mirroring the backend's response schemas.
 *
 * Hand-written rather than generated so the dashboard stays readable, and narrow enough that a
 * backend field rename fails `tsc` instead of rendering `undefined` in the UI.
 */

export type Severity = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "NONE";
export type PriorityBand = "P0" | "P1" | "P2" | "P3";
export type SeoCategory = "LOW ISSUES" | "MEDIUM ISSUES" | "HIGH ISSUES";
export type IntegrationProvider = "gsc" | "ga4" | "semrush" | "github";
export type IntegrationStatus =
  | "not_connected"
  | "connected"
  | "error"
  | "expired"
  | "syncing";
export type RunStatus = "queued" | "running" | "completed" | "failed" | "cancelled";
export type AiStatus = "pending" | "queued" | "completed" | "failed" | "skipped" | "cached";

export interface Paginated<T> {
  total: number;
  limit: number;
  offset: number;
  items: T[];
}

export interface User {
  id: number;
  email: string;
  full_name: string | null;
  role: "admin" | "member" | "viewer";
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface IntegrationSummary {
  provider: IntegrationProvider;
  status: IntegrationStatus;
  account_label?: string | null;
  last_sync_at?: string | null;
  last_error?: string | null;
}

export interface Website {
  id: number;
  name: string;
  url: string;
  domain: string;
  is_active: boolean;
  github_repo: string | null;
  github_branch: string | null;
  github_framework: string | null;
  github_path_map?: Record<string, string> | null;
  max_pages: number | null;
  render_mode: string;
  respect_robots_txt: boolean;
  include_patterns: string[] | null;
  exclude_patterns: string[] | null;
  total_pages: number;
  average_seo_score: number | null;
  critical_issue_count: number;
  high_priority_page_count: number;
  last_crawled_at: string | null;
  last_synced_at: string | null;
  last_scored_at: string | null;
  created_at: string;
  integrations?: IntegrationSummary[];
}

export interface PortfolioWebsite {
  id: number;
  name: string;
  url: string;
  domain: string;
  is_active: boolean;
  total_pages: number;
  average_seo_score: number | null;
  critical_issues: number;
  high_issues: number;
  total_issues: number;
  p0_pages: number;
  p1_pages: number;
  high_priority_pages: number;
  last_crawled_at: string | null;
  last_synced_at: string | null;
  last_scored_at: string | null;
  integrations: Record<string, IntegrationStatus>;
  traffic: { users: number; sessions: number; conversions: number; revenue: number };
  search: { clicks: number; impressions: number };
  active_crawl: { id: number; status: RunStatus; progress: number; stage: string | null } | null;
  github_repo: string | null;
}

export interface PortfolioOverview {
  window_days: number;
  totals: {
    websites: number;
    pages: number;
    critical_issues: number;
    high_priority_pages: number;
    total_issues: number;
    users: number;
    conversions: number;
    revenue: number;
    clicks: number;
    impressions: number;
    average_seo_score: number | null;
  };
  websites: PortfolioWebsite[];
}

export interface CrawlRun {
  id: number;
  website_id: number;
  status: RunStatus;
  trigger: string;
  mode: string;
  stage: string | null;
  progress_percent: number;
  urls_discovered: number;
  pages_queued: number;
  pages_crawled: number;
  pages_rendered: number;
  pages_analysed: number;
  pages_failed: number;
  ai_completed: number;
  ai_failed: number;
  ai_skipped: number;
  average_seo_score: number | null;
  critical_issue_count: number;
  total_issue_count: number;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  error: string | null;
  created_at: string;
}

export interface WebsiteOverview {
  website: {
    id: number;
    name: string;
    url: string;
    domain: string;
    is_active: boolean;
    github_repo: string | null;
    github_branch: string | null;
    render_mode: string;
    max_pages: number | null;
  };
  window_days: number;
  summary: {
    total_pages: number;
    average_seo_score: number | null;
    critical_issues: number;
    high_issues: number;
    total_issues: number;
    high_priority_pages: number;
    ai_recommendations: number;
    last_crawled_at: string | null;
    last_synced_at: string | null;
    last_scored_at: string | null;
  };
  distribution: {
    seo_category: Record<string, number>;
    priority_band: Record<string, number>;
    ai_status: Record<string, number>;
    status_code: Record<string, number>;
    issues_by_severity: Record<string, number>;
  };
  top_issues: { rule_id: string; title: string; severity: Severity; page_count: number }[];
  traffic: { users: number; sessions: number; conversions: number; revenue: number };
  search: {
    clicks: number;
    impressions: number;
    average_position: number | null;
    ctr: number | null;
  };
  integrations: IntegrationSummary[];
  data_sources: string[];
  recent_crawls: Partial<CrawlRun>[];
}

export interface TrendPoint {
  date: string;
  clicks: number;
  impressions: number;
  users: number;
  sessions: number;
  conversions: number;
  revenue: number;
  seo_score: number | null;
  issue_count: number;
  critical_count: number;
}

export interface PageListItem {
  id: number;
  url: string;
  path: string;
  title: string | null;
  status_code: number | null;
  seo_score: number | null;
  seo_category: SeoCategory | null;
  highest_severity: Severity | null;
  issue_count: number;
  priority_score: number | null;
  priority_band: PriorityBand | null;
  priority_rank: number | null;
  ai_status: AiStatus;
  last_crawled_at: string | null;
  users: number;
  sessions: number;
  conversions: number;
  revenue: number;
  clicks: number;
  impressions: number;
  ctr: number | null;
  position: number | null;
  top_issues: string[];
}

export interface IssueSummary {
  id: number;
  rule_id: string;
  check_type: string;
  category: string | null;
  severity: Severity;
  title: string;
  description: string;
  recommendation: string | null;
  evidence: Record<string, unknown> | null;
}

export interface MetricSummary {
  window_days: number;
  users: number;
  sessions: number;
  engagement_rate: number | null;
  conversions: number;
  revenue: number;
  clicks: number;
  impressions: number;
  ctr: number | null;
  position: number | null;
  organic_keywords: number;
  organic_traffic: number;
  striking_distance_keywords: number;
  backlinks: number;
}

export interface PriorityBreakdown {
  score: number;
  band: PriorityBand | null;
  rank: number | null;
  components: Record<string, number>;
  weights: Record<string, number>;
  breakdown: Record<string, unknown>;
  data_sources: string[];
  computed_at: string | null;
}

export interface HistoryPoint {
  date: string;
  seo_score: number | null;
  priority_score: number | null;
  issue_count: number;
  clicks: number;
  impressions: number;
  users: number;
  sessions: number;
  conversions: number;
  revenue: number;
}

export interface AiFinding {
  issue: string;
  explanation: string;
  why_it_matters: string;
  recommended_fix: string;
  implementation: string | null;
  expected_impact: string | null;
  priority: "critical" | "high" | "medium" | "low";
  effort: "trivial" | "small" | "medium" | "large";
  confidence: number;
}

export interface AiSuggestedChange {
  field: string;
  current: string | null;
  suggested: string;
  rationale: string | null;
}

export interface AiRecommendationPayload {
  summary: string;
  search_intent: string | null;
  content_quality_score: number;
  topic_coverage_score: number;
  findings: AiFinding[];
  suggested_changes: AiSuggestedChange[];
  expected_impact: string | null;
  priority: string;
  confidence: number;
  implementation_notes: string | null;
}

export interface PageDetail {
  id: number;
  website_id: number;
  url: string;
  path: string;
  final_url: string | null;
  is_active: boolean;
  status_code: number | null;
  redirect_chain: string[] | null;
  title: string | null;
  meta_description: string | null;
  h1: string | null;
  h1_count: number;
  h2_count: number;
  h3_count: number;
  canonical_url: string | null;
  robots_directive: string | null;
  lang: string | null;
  hreflang: Record<string, string>[] | null;
  has_viewport: boolean;
  has_structured_data: boolean;
  structured_data_types: string[] | null;
  has_open_graph: boolean;
  word_count: number;
  content_hash: string | null;
  image_count: number;
  missing_alt_count: number;
  internal_link_count: number;
  external_link_count: number;
  broken_link_count: number;
  inbound_internal_links: number;
  was_rendered: boolean;
  response_time_ms: number | null;
  crawl_status: string;
  crawl_error: string | null;
  seo_score: number | null;
  seo_category: SeoCategory | null;
  highest_severity: Severity | null;
  issue_count: number;
  priority_score: number | null;
  priority_band: PriorityBand | null;
  priority_rank: number | null;
  ai_status: AiStatus;
  ai_analysed_at: string | null;
  first_seen_at: string | null;
  last_crawled_at: string | null;
}

export interface GithubChange {
  id: number;
  repository: string | null;
  branch: string | null;
  after_sha: string | null;
  pusher: string | null;
  commit_messages: string[];
  changed_files: string[];
  action_taken: string | null;
  created_at: string;
}

export interface PageDetailResponse {
  page: PageDetail;
  issues: IssueSummary[];
  checks: { rule_id: string; check: string; status: string; score: number; details: string }[];
  metrics: MetricSummary;
  priority: PriorityBreakdown | null;
  history: HistoryPoint[];
  recommendation: {
    id: number;
    provider: string;
    model: string;
    status: string;
    summary: string | null;
    payload: AiRecommendationPayload | null;
    analysed_at: string | null;
  } | null;
  github_changes: GithubChange[];
}

export interface WeightResponse {
  scope: string;
  weights: Record<string, number>;
  effective_weights: Record<string, number> | null;
  data_sources: string[];
  connected_providers: string[];
}

export interface SelectionDecision {
  page_id: number;
  url: string;
  rank: number | null;
  selected: boolean;
  reason: string;
}

export interface RecommendationListItem {
  id: number;
  page_id: number;
  url: string;
  provider: string;
  model: string;
  status: string;
  summary: string | null;
  search_intent: string | null;
  priority: string | null;
  confidence: number | null;
  expected_impact: string | null;
  suggested_title: string | null;
  suggested_meta_description: string | null;
  finding_count: number;
  seo_score_at_analysis: number | null;
  priority_score_at_analysis: number | null;
  analysed_at: string | null;
}

export interface ApiErrorBody {
  error: { code: string; message: string; details?: unknown };
}
