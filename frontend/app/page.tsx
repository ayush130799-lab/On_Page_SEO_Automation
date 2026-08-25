"use client";

/**
 * Portfolio overview.
 *
 * The screen that answers "across all the websites we build, where should we start?" — so it is
 * sorted by outstanding P0 work rather than alphabetically, and every row shows business volume
 * next to technical health.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import {
  Card,
  EmptyState,
  ErrorNote,
  PageHeader,
  ProgressBar,
  ScoreBadge,
  Spinner,
  Stat,
  StatusBadge,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import {
  PROVIDER_LABELS,
  formatCurrency,
  formatNumber,
  formatRelative,
} from "@/lib/format";
import type { PortfolioOverview } from "@/lib/types";

export default function PortfolioPage() {
  return (
    <AuthGate>
      <Portfolio />
    </AuthGate>
  );
}

function Portfolio() {
  const [data, setData] = useState<PortfolioOverview | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await api.dashboard.overview());
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load the portfolio.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll while any crawl is running so progress bars advance without a manual refresh.
  useEffect(() => {
    const running = data?.websites.some((website) => website.active_crawl);
    if (!running) return;
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [data, load]);

  if (loading && !data) {
    return (
      <div className="py-16">
        <Spinner label="Loading your portfolio…" />
      </div>
    );
  }

  if (error && !data) {
    return <ErrorNote error={error} onRetry={load} />;
  }

  const totals = data?.totals;
  const websites = data?.websites ?? [];

  return (
    <>
      <PageHeader
        title="Portfolio"
        subtitle={
          totals
            ? `${totals.websites} website${totals.websites === 1 ? "" : "s"} · ${formatNumber(
                totals.pages,
              )} pages · metrics over the last ${data?.window_days} days`
            : undefined
        }
        actions={
          <>
            <button type="button" onClick={load} className="btn-secondary">
              Refresh
            </button>
            <Link href="/websites/new" className="btn-primary">
              Add website
            </Link>
          </>
        }
      />

      {error && (
        <div className="mb-4">
          <ErrorNote error={error} onRetry={load} />
        </div>
      )}

      {totals && (
        <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <Stat label="Websites" value={formatNumber(totals.websites)} />
          <Stat label="Pages tracked" value={formatNumber(totals.pages)} />
          <Stat
            label="Avg SEO score"
            value={totals.average_seo_score?.toFixed(1) ?? "—"}
            tone={
              totals.average_seo_score === null
                ? "default"
                : totals.average_seo_score > 90
                  ? "good"
                  : totals.average_seo_score >= 75
                    ? "warn"
                    : "bad"
            }
          />
          <Stat
            label="Critical issues"
            value={formatNumber(totals.critical_issues)}
            tone={totals.critical_issues > 0 ? "bad" : "good"}
          />
          <Stat
            label="High-priority pages"
            value={formatNumber(totals.high_priority_pages)}
            hint="P0 and P1"
            tone={totals.high_priority_pages > 0 ? "warn" : "good"}
          />
          <Stat
            label="Conversions"
            value={formatNumber(totals.conversions)}
            hint={totals.revenue ? formatCurrency(totals.revenue) : undefined}
          />
        </div>
      )}

      {websites.length === 0 ? (
        <EmptyState
          title="No websites yet"
          description="Add the first website your company builds, connect its data sources, and run a crawl."
          action={
            <Link href="/websites/new" className="btn-primary">
              Add your first website
            </Link>
          }
        />
      ) : (
        <Card
          title="Websites"
          action={<span className="text-xs text-slate-500">Most urgent work first</span>}
        >
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Website</th>
                  <th className="text-right">Pages</th>
                  <th className="text-right">SEO</th>
                  <th className="text-right">P0</th>
                  <th className="text-right">Critical</th>
                  <th className="text-right">Users</th>
                  <th className="text-right">Clicks</th>
                  <th className="text-right">Conversions</th>
                  <th>Integrations</th>
                  <th>Last crawl</th>
                </tr>
              </thead>
              <tbody>
                {websites.map((website) => (
                  <tr key={website.id}>
                    <td>
                      <Link
                        href={`/websites/${website.id}`}
                        className="font-medium text-slate-100 hover:text-sky-400"
                      >
                        {website.name}
                      </Link>
                      <div className="text-xs text-slate-500">{website.domain}</div>
                      {website.active_crawl && (
                        <div className="mt-1.5 w-40">
                          <ProgressBar
                            value={website.active_crawl.progress}
                            label={`Crawling · ${website.active_crawl.stage ?? website.active_crawl.status}`}
                          />
                        </div>
                      )}
                    </td>
                    <td className="tnum text-right">{formatNumber(website.total_pages)}</td>
                    <td className="text-right">
                      <ScoreBadge score={website.average_seo_score} />
                    </td>
                    <td className="tnum text-right">
                      {website.p0_pages > 0 ? (
                        <span className="font-semibold text-rose-400">{website.p0_pages}</span>
                      ) : (
                        <span className="text-slate-600">0</span>
                      )}
                    </td>
                    <td className="tnum text-right">
                      {website.critical_issues > 0 ? (
                        <span className="text-rose-400">{website.critical_issues}</span>
                      ) : (
                        <span className="text-slate-600">0</span>
                      )}
                    </td>
                    <td className="tnum text-right">{formatNumber(website.traffic.users)}</td>
                    <td className="tnum text-right">{formatNumber(website.search.clicks)}</td>
                    <td className="tnum text-right">
                      {formatNumber(website.traffic.conversions)}
                    </td>
                    <td>
                      <div className="flex flex-wrap gap-1">
                        {Object.entries(website.integrations).map(([provider, status]) => (
                          <StatusBadge
                            key={provider}
                            status={status}
                            label={PROVIDER_LABELS[provider] ?? provider}
                          />
                        ))}
                      </div>
                    </td>
                    <td className="whitespace-nowrap text-xs text-slate-400">
                      {formatRelative(website.last_crawled_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}
