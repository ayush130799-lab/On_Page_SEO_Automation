"use client";

/**
 * Website overview and the priority pages table.
 *
 * The table is the product's centrepiece: SEO score and priority score sit side by side with the
 * traffic, search and conversion numbers that justify the ranking, and every column can be sorted
 * and filtered server-side.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import {
  AiBadge,
  BandBadge,
  Card,
  DistributionBar,
  EmptyState,
  ErrorNote,
  IntentBadge,
  PageHeader,
  ProgressBar,
  ScoreBadge,
  SeverityBadge,
  Spinner,
  Stat,
  StatusBadge,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import {
  PROVIDER_LABELS,
  displayPath,
  formatCurrency,
  formatNumber,
  formatPercent,
  formatRelative,
  truncate,
} from "@/lib/format";
import type { CrawlRun, PageListItem, WebsiteOverview } from "@/lib/types";

const PAGE_SIZE = 50;

type SortKey =
  | "priority_score"
  | "seo_score"
  | "issue_count"
  | "users"
  | "clicks"
  | "impressions"
  | "conversions"
  | "severity"
  | "url";

interface Filters {
  search: string;
  severity: string;
  priority_band: string;
  seo_category: string;
  ai_status: string;
  has_issues: string;
}

const EMPTY_FILTERS: Filters = {
  search: "",
  severity: "",
  priority_band: "",
  seo_category: "",
  ai_status: "",
  has_issues: "",
};

export default function WebsitePage() {
  return (
    <AuthGate>
      <WebsiteDashboard />
    </AuthGate>
  );
}

function WebsiteDashboard() {
  const params = useParams<{ id: string }>();
  const websiteId = Number(params.id);

  const [overview, setOverview] = useState<WebsiteOverview | null>(null);
  const [pages, setPages] = useState<PageListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState<SortKey>("priority_score");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [searchInput, setSearchInput] = useState("");

  const [loadingOverview, setLoadingOverview] = useState(true);
  const [loadingPages, setLoadingPages] = useState(true);
  const [error, setError] = useState("");
  const [action, setAction] = useState("");
  const [activeCrawl, setActiveCrawl] = useState<CrawlRun | null>(null);

  const loadOverview = useCallback(async () => {
    try {
      const data = await api.dashboard.website(websiteId);
      setOverview(data);
      setError("");
      const running = data.recent_crawls.find(
        (run) => run.status === "running" || run.status === "queued",
      );
      setActiveCrawl((running as CrawlRun) ?? null);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load this website.");
    } finally {
      setLoadingOverview(false);
    }
  }, [websiteId]);

  const loadPages = useCallback(async () => {
    setLoadingPages(true);
    try {
      const result = await api.pages.list(websiteId, {
        limit: PAGE_SIZE,
        offset,
        sort,
        order,
        ...Object.fromEntries(
          Object.entries(filters).filter(([, value]) => value !== ""),
        ),
      });
      setPages(result.items);
      setTotal(result.total);
      setError("");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load pages.");
    } finally {
      setLoadingPages(false);
    }
  }, [websiteId, offset, sort, order, filters]);

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    void loadPages();
  }, [loadPages]);

  // Debounce the search box so typing does not fire a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => {
      setFilters((current) => ({ ...current, search: searchInput }));
      setOffset(0);
    }, 350);
    return () => clearTimeout(timer);
  }, [searchInput]);

  // Follow a running crawl and refresh the table when it finishes.
  useEffect(() => {
    if (!activeCrawl) return;
    const timer = setInterval(async () => {
      try {
        const run = await api.crawls.get(activeCrawl.id);
        setActiveCrawl(run.status === "running" || run.status === "queued" ? run : null);
        if (run.status === "completed") {
          void loadOverview();
          void loadPages();
        }
      } catch {
        setActiveCrawl(null);
      }
    }, 3000);
    return () => clearInterval(timer);
  }, [activeCrawl, loadOverview, loadPages]);

  const toggleSort = (key: SortKey) => {
    if (sort === key) {
      setOrder(order === "desc" ? "asc" : "desc");
    } else {
      setSort(key);
      setOrder("desc");
    }
    setOffset(0);
  };

  const runAction = async (label: string, fn: () => Promise<unknown>) => {
    setAction(label);
    setError("");
    try {
      await fn();
      await loadOverview();
      await loadPages();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : `${label} failed.`);
    } finally {
      setAction("");
    }
  };

  const startCrawl = () =>
    runAction("Starting crawl", async () => {
      const run = await api.crawls.start(websiteId, { mode: "full" });
      setActiveCrawl(run);
    });

  const activeFilterCount = useMemo(
    () => Object.values(filters).filter((value) => value !== "").length,
    [filters],
  );

  if (loadingOverview && !overview) {
    return (
      <div className="py-16">
        <Spinner label="Loading website…" />
      </div>
    );
  }

  if (!overview) {
    return <ErrorNote error={error || "Website not found."} onRetry={loadOverview} />;
  }

  const { website, summary, distribution, traffic, search, integrations, top_issues } = overview;

  return (
    <>
      <PageHeader
        breadcrumb={[{ href: "/", label: "Portfolio" }]}
        title={website.name}
        subtitle={
          <a
            href={website.url}
            target="_blank"
            rel="noreferrer noopener"
            className="hover:text-sky-400"
          >
            {website.url}
          </a>
        }
        actions={
          <>
            <Link href={`/websites/${websiteId}/roadmap`} className="btn-secondary text-sky-300 font-medium">
              Roadmap
            </Link>
            <Link href={`/websites/${websiteId}/recommendations`} className="btn-secondary">
              AI recommendations
            </Link>
            <Link href={`/websites/${websiteId}/experiments`} className="btn-secondary">
              Experiments
            </Link>
            <Link href={`/websites/${websiteId}/integrations`} className="btn-secondary">
              Integrations
            </Link>
            <Link href={`/websites/${websiteId}/settings`} className="btn-secondary">
              Settings
            </Link>
            <button
              type="button"
              onClick={() => void runAction("Syncing", () => api.integrations.syncAll(websiteId))}
              disabled={Boolean(action)}
              className="btn-secondary"
            >
              Sync data
            </button>
            <button
              type="button"
              onClick={() => void runAction("Scoring", () => api.priority.rescore(websiteId))}
              disabled={Boolean(action)}
              className="btn-secondary"
            >
              Rescore
            </button>
            <button
              type="button"
              onClick={() => void startCrawl()}
              disabled={Boolean(action) || Boolean(activeCrawl)}
              className="btn-primary"
            >
              {activeCrawl ? "Crawling…" : "Crawl now"}
            </button>
          </>
        }
      />

      {error && (
        <div className="mb-4">
          <ErrorNote error={error} />
        </div>
      )}

      {activeCrawl && (
        <div className="mb-4">
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="min-w-64 flex-1">
                <ProgressBar
                  value={activeCrawl.progress_percent}
                  label={`${activeCrawl.stage ?? activeCrawl.status} · ${formatNumber(
                    activeCrawl.pages_crawled,
                  )} of ${formatNumber(activeCrawl.urls_discovered)} URLs crawled`}
                />
              </div>
              <button
                type="button"
                onClick={() =>
                  void runAction("Cancelling", () => api.crawls.cancel(activeCrawl.id))
                }
                className="btn-ghost"
              >
                Cancel
              </button>
            </div>
          </Card>
        </div>
      )}

      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
        <Stat label="Pages" value={formatNumber(summary.total_pages)} />
        <Stat
          label="Avg SEO score"
          value={summary.average_seo_score?.toFixed(1) ?? "—"}
          tone={
            summary.average_seo_score === null
              ? "default"
              : summary.average_seo_score > 90
                ? "good"
                : summary.average_seo_score >= 75
                  ? "warn"
                  : "bad"
          }
        />
        <Stat
          label="Critical issues"
          value={formatNumber(summary.critical_issues)}
          tone={summary.critical_issues > 0 ? "bad" : "good"}
        />
        <Stat
          label="High priority"
          value={formatNumber(summary.high_priority_pages)}
          hint="P0 + P1 pages"
          tone={summary.high_priority_pages > 0 ? "warn" : "good"}
        />
        <Stat label="Users" value={formatNumber(traffic.users)} hint={`${overview.window_days}d`} />
        <Stat
          label="Clicks"
          value={formatNumber(search.clicks)}
          hint={search.ctr !== null ? `CTR ${formatPercent(search.ctr, 2)}` : undefined}
        />
        <Stat
          label="Conversions"
          value={formatNumber(traffic.conversions)}
          hint={traffic.revenue ? formatCurrency(traffic.revenue) : undefined}
        />
      </div>

      <div className="mb-6 grid gap-4 lg:grid-cols-3">
        <Card title="SEO health distribution">
          <DistributionBar
            segments={[
              {
                label: "Healthy (>90)",
                value: distribution.seo_category["LOW ISSUES"] ?? 0,
                className: "bg-emerald-500",
              },
              {
                label: "Needs work (75–90)",
                value: distribution.seo_category["MEDIUM ISSUES"] ?? 0,
                className: "bg-amber-500",
              },
              {
                label: "Poor (<75)",
                value: distribution.seo_category["HIGH ISSUES"] ?? 0,
                className: "bg-rose-500",
              },
            ]}
          />
          <div className="mt-4">
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">
              Business priority
            </p>
            <DistributionBar
              segments={[
                { label: "P0", value: distribution.priority_band.P0 ?? 0, className: "bg-rose-500" },
                {
                  label: "P1",
                  value: distribution.priority_band.P1 ?? 0,
                  className: "bg-orange-500",
                },
                {
                  label: "P2",
                  value: distribution.priority_band.P2 ?? 0,
                  className: "bg-amber-500",
                },
                {
                  label: "P3",
                  value: distribution.priority_band.P3 ?? 0,
                  className: "bg-slate-600",
                },
              ]}
            />
          </div>
        </Card>

        <Card title="Most common issues">
          {top_issues.length === 0 ? (
            <p className="text-sm text-slate-500">No outstanding issues.</p>
          ) : (
            <ul className="space-y-2">
              {top_issues.slice(0, 6).map((issue) => (
                <li key={issue.rule_id} className="flex items-center justify-between gap-3">
                  <span className="flex min-w-0 items-center gap-2">
                    <SeverityBadge severity={issue.severity} />
                    <span className="truncate text-sm text-slate-300">{issue.title}</span>
                  </span>
                  <span className="tnum shrink-0 text-sm text-slate-400">
                    {formatNumber(issue.page_count)} pages
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card
          title="Data sources"
          action={
            <Link
              href={`/websites/${websiteId}/integrations`}
              className="text-xs text-sky-400 hover:underline"
            >
              Manage
            </Link>
          }
        >
          <ul className="space-y-2.5">
            {integrations.map((integration) => (
              <li key={integration.provider} className="flex items-center justify-between gap-3">
                <span className="text-sm text-slate-300">
                  {PROVIDER_LABELS[integration.provider] ?? integration.provider}
                </span>
                <span className="flex items-center gap-2">
                  {integration.last_sync_at && (
                    <span className="text-xs text-slate-500">
                      {formatRelative(integration.last_sync_at)}
                    </span>
                  )}
                  <StatusBadge status={integration.status} />
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-3 border-t border-slate-800 pt-3 text-xs text-slate-500">
            Priority is computed from: {overview.data_sources.join(", ")}
          </p>
        </Card>
      </div>

      <Card
        title="Priority pages"
        action={
          <div className="flex items-center gap-2">
            {activeFilterCount > 0 && (
              <button
                type="button"
                onClick={() => {
                  setFilters(EMPTY_FILTERS);
                  setSearchInput("");
                  setOffset(0);
                }}
                className="text-xs text-sky-400 hover:underline"
              >
                Clear {activeFilterCount} filter{activeFilterCount === 1 ? "" : "s"}
              </button>
            )}
            <span className="text-xs text-slate-500">{formatNumber(total)} pages</span>
          </div>
        }
      >
        <div className="mb-4 flex flex-wrap gap-2">
          <input
            className="input max-w-xs"
            placeholder="Search URL or title…"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
          />
          <FilterSelect
            value={filters.priority_band}
            onChange={(value) => {
              setFilters({ ...filters, priority_band: value });
              setOffset(0);
            }}
            label="All priorities"
            options={[
              ["P0", "P0 — critical"],
              ["P1", "P1 — high"],
              ["P2", "P2 — medium"],
              ["P3", "P3 — low"],
            ]}
          />
          <FilterSelect
            value={filters.severity}
            onChange={(value) => {
              setFilters({ ...filters, severity: value });
              setOffset(0);
            }}
            label="All severities"
            options={[
              ["CRITICAL", "Critical"],
              ["HIGH", "High"],
              ["MEDIUM", "Medium"],
              ["LOW", "Low"],
              ["NONE", "None"],
            ]}
          />
          <FilterSelect
            value={filters.seo_category}
            onChange={(value) => {
              setFilters({ ...filters, seo_category: value });
              setOffset(0);
            }}
            label="All SEO health"
            options={[
              ["HIGH ISSUES", "Poor (<75)"],
              ["MEDIUM ISSUES", "Needs work (75–90)"],
              ["LOW ISSUES", "Healthy (>90)"],
            ]}
          />
          <FilterSelect
            value={filters.ai_status}
            onChange={(value) => {
              setFilters({ ...filters, ai_status: value });
              setOffset(0);
            }}
            label="All AI states"
            options={[
              ["completed", "Analysed"],
              ["cached", "Cached"],
              ["skipped", "Skipped"],
              ["pending", "Pending"],
              ["failed", "Failed"],
            ]}
          />
          <FilterSelect
            value={filters.has_issues}
            onChange={(value) => {
              setFilters({ ...filters, has_issues: value });
              setOffset(0);
            }}
            label="With and without issues"
            options={[
              ["true", "Has issues"],
              ["false", "No issues"],
            ]}
          />
        </div>

        {loadingPages && pages.length === 0 ? (
          <div className="py-10">
            <Spinner label="Loading pages…" />
          </div>
        ) : pages.length === 0 ? (
          <EmptyState
            title="No pages match"
            description={
              total === 0 && activeFilterCount === 0
                ? "Run a crawl to discover and audit this website's pages."
                : "Try relaxing the filters."
            }
            action={
              total === 0 && activeFilterCount === 0 ? (
                <button type="button" onClick={() => void startCrawl()} className="btn-primary">
                  Crawl now
                </button>
              ) : undefined
            }
          />
        ) : (
          <>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <SortHeader
                      label="URL"
                      column="url"
                      sort={sort}
                      order={order}
                      onSort={toggleSort}
                    />
                    <SortHeader
                      label="Priority"
                      column="priority_score"
                      sort={sort}
                      order={order}
                      onSort={toggleSort}
                      align="right"
                    />
                    <SortHeader
                      label="SEO"
                      column="seo_score"
                      sort={sort}
                      order={order}
                      onSort={toggleSort}
                      align="right"
                    />
                    <SortHeader
                      label="Severity"
                      column="severity"
                      sort={sort}
                      order={order}
                      onSort={toggleSort}
                    />
                    <SortHeader
                      label="Users"
                      column="users"
                      sort={sort}
                      order={order}
                      onSort={toggleSort}
                      align="right"
                    />
                    <SortHeader
                      label="Clicks"
                      column="clicks"
                      sort={sort}
                      order={order}
                      onSort={toggleSort}
                      align="right"
                    />
                    <SortHeader
                      label="Impr."
                      column="impressions"
                      sort={sort}
                      order={order}
                      onSort={toggleSort}
                      align="right"
                    />
                    <SortHeader
                      label="Conv."
                      column="conversions"
                      sort={sort}
                      order={order}
                      onSort={toggleSort}
                      align="right"
                    />
                    <SortHeader
                      label="Issues"
                      column="issue_count"
                      sort={sort}
                      order={order}
                      onSort={toggleSort}
                      align="right"
                    />
                    <th>Major issues</th>
                    <th>Intent</th>
                    <th>AI</th>
                  </tr>
                </thead>
                <tbody>
                  {pages.map((page) => (
                    <tr key={page.id}>
                      <td className="max-w-xs">
                        <Link
                          href={`/pages/${page.id}`}
                          className="block truncate font-medium text-slate-200 hover:text-sky-400"
                          title={page.url}
                        >
                          {displayPath(page.url)}
                        </Link>
                        <div className="truncate text-xs text-slate-500" title={page.title ?? ""}>
                          {truncate(page.title, 70)}
                        </div>
                      </td>
                      <td className="text-right">
                        <div className="flex items-center justify-end gap-1.5">
                          <span className="tnum text-sm font-semibold text-slate-100">
                            {page.priority_score?.toFixed(1) ?? "—"}
                          </span>
                          <BandBadge band={page.priority_band} />
                        </div>
                      </td>
                      <td className="text-right">
                        <ScoreBadge score={page.seo_score} />
                      </td>
                      <td>
                        <SeverityBadge severity={page.highest_severity} />
                      </td>
                      <td className="tnum text-right">{formatNumber(page.users)}</td>
                      <td className="tnum text-right">{formatNumber(page.clicks)}</td>
                      <td className="tnum text-right">{formatNumber(page.impressions)}</td>
                      <td className="tnum text-right">{formatNumber(page.conversions)}</td>
                      <td className="tnum text-right">{page.issue_count}</td>
                      <td className="max-w-xs">
                        <span className="text-xs text-slate-400">
                          {page.top_issues.length > 0 ? page.top_issues.join(" · ") : "—"}
                        </span>
                      </td>
                      <td>
                        <div className="flex items-center gap-1">
                          <IntentBadge intent={page.search_intent} />
                          {page.intent_mismatch && (
                            <span title="Intent mismatch detected" className="text-amber-400 text-xs">
                              ⚠
                            </span>
                          )}
                        </div>
                      </td>
                      <td>
                        <AiBadge status={page.ai_status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="mt-4 flex items-center justify-between text-sm text-slate-400">
              <span className="tnum">
                {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {formatNumber(total)}
              </span>
              <span className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  disabled={offset === 0}
                  className="btn-secondary"
                >
                  Previous
                </button>
                <button
                  type="button"
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                  disabled={offset + PAGE_SIZE >= total}
                  className="btn-secondary"
                >
                  Next
                </button>
              </span>
            </div>
          </>
        )}
      </Card>
    </>
  );
}

function FilterSelect({
  value,
  onChange,
  label,
  options,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
  options: [string, string][];
}) {
  return (
    <select
      className="input max-w-[13rem]"
      value={value}
      onChange={(event) => onChange(event.target.value)}
      aria-label={label}
    >
      <option value="">{label}</option>
      {options.map(([optionValue, optionLabel]) => (
        <option key={optionValue} value={optionValue}>
          {optionLabel}
        </option>
      ))}
    </select>
  );
}

function SortHeader({
  label,
  column,
  sort,
  order,
  onSort,
  align = "left",
}: {
  label: string;
  column: SortKey;
  sort: SortKey;
  order: "asc" | "desc";
  onSort: (key: SortKey) => void;
  align?: "left" | "right";
}) {
  const active = sort === column;
  return (
    <th className={align === "right" ? "text-right" : ""}>
      <button
        type="button"
        onClick={() => onSort(column)}
        className={`sortable inline-flex items-center gap-1 uppercase ${
          active ? "text-sky-400" : ""
        }`}
      >
        {label}
        <span aria-hidden className="text-[10px]">
          {active ? (order === "desc" ? "▼" : "▲") : "↕"}
        </span>
      </button>
    </th>
  );
}
