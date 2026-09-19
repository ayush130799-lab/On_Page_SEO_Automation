"use client";

/**
 * The priority pages table: SEO score and priority score side by side with the traffic, search
 * and conversion numbers that justify the ranking, every column sortable and filterable
 * server-side against ``GET /api/websites/{id}/pages``.
 *
 * Extracted from the website dashboard so the same table/design can be reused wherever a filtered
 * slice of a website's pages needs showing — e.g. the "Most common issues" → affected-pages
 * drilldown (``rule_id`` prop), without duplicating the table markup.
 */

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  AiBadge,
  BandBadge,
  Card,
  EmptyState,
  ErrorNote,
  IntentBadge,
  ScoreBadge,
  SeverityBadge,
  Spinner,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { displayPath, formatNumber, formatPercent, truncate } from "@/lib/format";
import type { PageListItem } from "@/lib/types";

const PAGE_SIZE = 50;

type SortKey =
  | "priority_score"
  | "seo_score"
  | "traffic_potential_score"
  | "lead_potential_score"
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

export interface PriorityPagesTableProps {
  websiteId: number;
  /** Restrict the table to pages carrying this unresolved SEOIssue rule_id (the issue drilldown). */
  ruleId?: string;
  /** Card header title. Defaults to "Priority pages". */
  title?: string;
  /** Bump to force a refetch from a parent action (e.g. a crawl just completed). */
  reloadToken?: number;
  /** Shown as the empty-state action when there are no pages at all and no filters are active.
   *  Omitted (as on the issue drilldown) when starting a crawl doesn't make sense on that view. */
  onStartCrawl?: () => void;
  /** Called with the current sort/filter query (no pagination) whenever it changes, so a parent
   *  action outside this table — e.g. the issue drilldown's "Export to Excel" — can request the
   *  same filtered/sorted set the table is currently showing. */
  onQueryChange?: (query: Record<string, string | number>) => void;
}

export function PriorityPagesTable({
  websiteId,
  ruleId,
  title = "Priority pages",
  reloadToken = 0,
  onStartCrawl,
  onQueryChange,
}: PriorityPagesTableProps) {
  const [pages, setPages] = useState<PageListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState<SortKey>("priority_score");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [searchInput, setSearchInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // A different issue (or website) means a stale offset could point past the new result set.
  useEffect(() => {
    setOffset(0);
  }, [ruleId, websiteId]);

  const loadPages = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api.pages.list(websiteId, {
        limit: PAGE_SIZE,
        offset,
        sort,
        order,
        ...(ruleId ? { rule_id: ruleId } : {}),
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
      setLoading(false);
    }
  }, [websiteId, ruleId, offset, sort, order, filters]);

  useEffect(() => {
    void loadPages();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadPages, reloadToken]);

  // Let a parent action (e.g. an "Export to Excel" button outside this table) know the current
  // sort/filters — no pagination — so it can request the same set the table is showing.
  useEffect(() => {
    onQueryChange?.({
      sort,
      order,
      ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value !== "")),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sort, order, filters]);

  // Debounce the search box so typing does not fire a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => {
      setFilters((current) => ({ ...current, search: searchInput }));
      setOffset(0);
    }, 350);
    return () => clearTimeout(timer);
  }, [searchInput]);

  const toggleSort = (key: SortKey) => {
    if (sort === key) {
      setOrder(order === "desc" ? "asc" : "desc");
    } else {
      setSort(key);
      setOrder("desc");
    }
    setOffset(0);
  };

  const activeFilterCount = useMemo(
    () => Object.values(filters).filter((value) => value !== "").length,
    [filters],
  );

  const noCrawlYet = total === 0 && activeFilterCount === 0 && !ruleId;
  const emptyDescription =
    total === 0 && activeFilterCount === 0
      ? ruleId
        ? "No pages currently have this issue. It may have been resolved by a more recent crawl."
        : "Run a crawl to discover and audit this website's pages."
      : "Try relaxing the filters.";

  return (
    <Card
      title={title}
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
      {error && (
        <div className="mb-4">
          <ErrorNote error={error} onRetry={() => void loadPages()} />
        </div>
      )}

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

      {loading && pages.length === 0 ? (
        <div className="py-10">
          <Spinner label="Loading pages…" />
        </div>
      ) : pages.length === 0 ? (
        <EmptyState
          title="No pages match"
          description={emptyDescription}
          action={
            noCrawlYet && onStartCrawl ? (
              <button type="button" onClick={onStartCrawl} className="btn-primary">
                Crawl now
              </button>
            ) : undefined
          }
        />
      ) : (
        <>
          <div className="table-wrap -mx-4">
            <table className="data compact">
              <thead>
                <tr>
                  <SortHeader
                    label="URL"
                    column="url"
                    sort={sort}
                    order={order}
                    onSort={toggleSort}
                    className="pl-4"
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
                    label="Traffic"
                    column="traffic_potential_score"
                    sort={sort}
                    order={order}
                    onSort={toggleSort}
                    align="right"
                  />
                  <SortHeader
                    label="Leads"
                    column="lead_potential_score"
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
                  <th className="text-right">CTR</th>
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
                  <th className="max-w-[150px]">Major issues</th>
                  <th className="whitespace-nowrap">Intent</th>
                  <th className="min-w-[85px] whitespace-nowrap pr-4">AI</th>
                </tr>
              </thead>
              <tbody>
                {pages.map((page) => (
                  <tr key={page.id}>
                    <td className="max-w-[160px] pl-4">
                      <Link
                        href={`/pages/${page.id}`}
                        className="block truncate font-medium text-slate-200 hover:text-sky-400"
                        title={page.url}
                      >
                        {displayPath(page.url)}
                      </Link>
                      <div className="truncate text-xs text-slate-500" title={page.title ?? ""}>
                        {truncate(page.title, 55)}
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
                    <td className="text-right">
                      <ScoreBadge score={page.traffic_potential_score} />
                    </td>
                    <td className="text-right">
                      <ScoreBadge score={page.lead_potential_score} />
                    </td>
                    <td>
                      <SeverityBadge severity={page.highest_severity} />
                    </td>
                    <td className="tnum text-right">{formatNumber(page.users)}</td>
                    <td className="tnum text-right">{formatNumber(page.clicks)}</td>
                    <td className="tnum text-right">{formatNumber(page.impressions)}</td>
                    <td className="tnum text-right">
                      {page.ctr !== null ? formatPercent(page.ctr, 2) : "—"}
                    </td>
                    <td className="tnum text-right">{formatNumber(page.conversions)}</td>
                    <td className="tnum text-right">{page.issue_count}</td>
                    <td className="max-w-[150px]">
                      <span
                        className="line-clamp-2 text-xs text-slate-400"
                        title={page.top_issues.join(" · ")}
                      >
                        {page.top_issues.length > 0 ? page.top_issues.join(" · ") : "—"}
                      </span>
                    </td>
                    <td className="whitespace-nowrap">
                      <div className="flex items-center gap-1">
                        <IntentBadge intent={page.search_intent} />
                        {page.intent_mismatch && (
                          <span title="Intent mismatch detected" className="text-amber-400 text-xs">
                            ⚠
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="min-w-[85px] whitespace-nowrap pr-4">
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
  className = "",
}: {
  label: string;
  column: SortKey;
  sort: SortKey;
  order: "asc" | "desc";
  onSort: (key: SortKey) => void;
  align?: "left" | "right";
  className?: string;
}) {
  const active = sort === column;
  return (
    <th className={`${align === "right" ? "text-right" : ""} ${className}`}>
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
