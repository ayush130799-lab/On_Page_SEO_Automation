"use client";

/**
 * Website overview and the priority pages table.
 *
 * The table itself (SEO score and priority score side by side with the traffic, search and
 * conversion numbers that justify the ranking, every column sortable/filterable server-side)
 * lives in ``PriorityPagesTable`` so the same table/design is reused by the "Most common issues"
 * → affected-pages drilldown at ``/websites/[id]/issues/[ruleId]``.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import { PriorityPagesTable } from "@/components/PriorityPagesTable";
import {
  Card,
  DistributionBar,
  ErrorNote,
  PageHeader,
  ProgressBar,
  SeverityBadge,
  Spinner,
  Stat,
  StatusBadge,
} from "@/components/ui";
import { ApiError, api, saveBlob } from "@/lib/api";
import {
  PROVIDER_LABELS,
  formatCurrency,
  formatNumber,
  formatPercent,
  formatRelative,
} from "@/lib/format";
import type { CrawlRun, WebsiteOverview } from "@/lib/types";

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
  // Bumped to force PriorityPagesTable to refetch after an action changes the underlying data
  // (a crawl finishing, a rescore, etc.) — the table owns its own paging/sorting/filter state.
  const [pagesReloadToken, setPagesReloadToken] = useState(0);

  const [loadingOverview, setLoadingOverview] = useState(true);
  const [error, setError] = useState("");
  const [action, setAction] = useState("");
  const [activeCrawl, setActiveCrawl] = useState<CrawlRun | null>(null);

  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");

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

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);

  // Follow a running crawl and refresh the overview + pages table when it finishes.
  useEffect(() => {
    if (!activeCrawl) return;
    const crawlId = activeCrawl.id;
    const timer = setInterval(async () => {
      try {
        const run = await api.crawls.get(crawlId);
        if (run.status === "running" || run.status === "queued") {
          setActiveCrawl(run);
        } else {
          setActiveCrawl(null);
          if (run.status === "completed") {
            void loadOverview();
            setPagesReloadToken((token) => token + 1);
          } else if (run.status === "failed") {
            setError(run.error || "Crawl did not complete successfully.");
            void loadOverview();
          }
        }
      } catch {
        setActiveCrawl(null);
      }
    }, 2500);
    return () => clearInterval(timer);
  }, [activeCrawl?.id, loadOverview]);

  const runAction = async (label: string, fn: () => Promise<unknown>) => {
    setAction(label);
    setError("");
    try {
      await fn();
      await loadOverview();
      setPagesReloadToken((token) => token + 1);
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

  const exportExcel = async () => {
    if (exporting) return; // guard against a double-click firing a second download
    setExporting(true);
    setExportError("");
    try {
      const { blob, filename } = await api.seo.exportExcel(websiteId);
      saveBlob(blob, filename);
    } catch (caught) {
      setExportError(
        caught instanceof ApiError ? caught.message : "Could not generate the Excel report.",
      );
    } finally {
      setExporting(false);
    }
  };

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
              onClick={() => void runAction("Analysing intent", () => api.intent.analyse(websiteId))}
              disabled={Boolean(action)}
              className="btn-secondary"
              title="Classify search intent for any crawled page that doesn't have it yet"
            >
              Analyse intent
            </button>
            <button
              type="button"
              onClick={() => void exportExcel()}
              disabled={exporting}
              className="btn-secondary"
              title="Download SEO Summary, Page Overview, SEO Issues, AI Recommendations and Keywords as a .xlsx file"
            >
              {exporting ? "Generating Excel…" : "Export to Excel"}
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

      {exportError && (
        <div className="mb-4">
          <ErrorNote error={exportError} onRetry={() => void exportExcel()} />
        </div>
      )}

      {activeCrawl && (
        <div className="mb-4">
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="min-w-64 flex-1">
                <ProgressBar
                  value={activeCrawl.progress_percent}
                  label={
                    activeCrawl.urls_discovered > 0
                      ? `${activeCrawl.stage ?? activeCrawl.status} · ${formatNumber(
                          activeCrawl.pages_crawled,
                        )} of ${formatNumber(activeCrawl.urls_discovered)} URLs crawled`
                      : `${activeCrawl.stage ?? activeCrawl.status} · discovering sitemap & URLs…`
                  }
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

      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-5">
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
          label="Avg traffic potential"
          value={summary.average_traffic_potential_score?.toFixed(1) ?? "—"}
          hint="0-100 · additional organic traffic opportunity"
        />
        <Stat
          label="Avg lead potential"
          value={summary.average_lead_potential_score?.toFixed(1) ?? "—"}
          hint="0-100 · business/lead opportunity"
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
        <Stat label="Clicks" value={formatNumber(search.clicks)} />
        <Stat
          label="CTR"
          value={search.ctr !== null ? formatPercent(search.ctr, 2) : "—"}
          hint={
            search.average_position !== null
              ? `Avg position ${search.average_position.toFixed(1)}`
              : undefined
          }
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
            <ul className="space-y-1">
              {top_issues.slice(0, 6).map((issue) => (
                <li key={issue.rule_id}>
                  <Link
                    href={{
                      pathname: `/websites/${websiteId}/issues/${encodeURIComponent(issue.rule_id)}`,
                      query: { title: issue.title, severity: issue.severity },
                    }}
                    className="flex items-center justify-between gap-3 rounded-md px-2 py-1.5 -mx-2 transition-colors hover:bg-slate-800/60"
                    title={`View all ${issue.page_count} pages affected by "${issue.title}"`}
                  >
                    <span className="flex min-w-0 items-center gap-2">
                      <SeverityBadge severity={issue.severity} />
                      <span className="truncate text-sm text-slate-300">{issue.title}</span>
                    </span>
                    <span className="tnum shrink-0 text-sm text-sky-400">
                      {formatNumber(issue.page_count)} pages
                    </span>
                  </Link>
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

      <PriorityPagesTable
        websiteId={websiteId}
        reloadToken={pagesReloadToken}
        onStartCrawl={() => void startCrawl()}
      />
    </>
  );
}
