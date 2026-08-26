"use client";

/**
 * AI recommendations for a website.
 *
 * The selection panel is shown first on purpose: it explains which pages will be sent to the model
 * and why, so the cost of a run is visible before it is incurred.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import { Card, EmptyState, ErrorNote, PageHeader, Spinner, Stat } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { displayPath, formatPercent, formatRelative, truncate } from "@/lib/format";
import type { RecommendationListItem, SelectionDecision } from "@/lib/types";

export default function RecommendationsPage() {
  return (
    <AuthGate>
      <Recommendations />
    </AuthGate>
  );
}

function Recommendations() {
  const params = useParams<{ id: string }>();
  const websiteId = Number(params.id);

  const [items, setItems] = useState<RecommendationListItem[]>([]);
  const [selection, setSelection] = useState<{
    selected_count: number;
    considered_count: number;
    decisions: SelectionDecision[];
  } | null>(null);
  const [providers, setProviders] = useState<{
    enabled: boolean;
    active: string;
    configured: string[];
    max_pages_per_run: number;
    seo_score_threshold: number;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showSelection, setShowSelection] = useState(false);

  const load = useCallback(async () => {
    try {
      const [recommendations, selectionData, providerData] = await Promise.all([
        api.ai.recommendations(websiteId),
        api.ai.selection(websiteId),
        api.ai.providers(),
      ]);
      setItems(recommendations.items);
      setSelection(selectionData);
      setProviders(providerData);
      setError("");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load recommendations.");
    } finally {
      setLoading(false);
    }
  }, [websiteId]);

  useEffect(() => {
    void load();
  }, [load]);

  const analyse = async () => {
    setRunning(true);
    setError("");
    setNotice("");
    try {
      const result = await api.ai.analyse(websiteId, { wait: true });
      setNotice(
        `Analysed ${result.analysed} page(s); ${result.cached} reused a cached result and ` +
          `${result.skipped} were skipped as healthy.`,
      );
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "AI analysis failed.");
    } finally {
      setRunning(false);
    }
  };

  if (loading) {
    return (
      <div className="py-16">
        <Spinner label="Loading recommendations…" />
      </div>
    );
  }

  const aiUnavailable = providers && (!providers.enabled || providers.configured.length === 0);

  return (
    <>
      <PageHeader
        breadcrumb={[
          { href: "/", label: "Portfolio" },
          { href: `/websites/${websiteId}`, label: "Website" },
        ]}
        title="AI recommendations"
        subtitle="Only pages that earn the cost are sent to the model."
        actions={
          <button
            type="button"
            onClick={() => void analyse()}
            disabled={running || Boolean(aiUnavailable)}
            className="btn-primary"
          >
            {running ? "Analysing…" : "Run analysis"}
          </button>
        }
      />

      {error && (
        <div className="mb-4">
          <ErrorNote error={error} />
        </div>
      )}
      {notice && (
        <div className="mb-4 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-200">
          {notice}
        </div>
      )}
      {aiUnavailable && (
        <div className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
          AI analysis is unavailable: no model provider is configured. Set{" "}
          <code>GEMINI_API_KEY</code>, <code>GROQ_API_KEY</code>, <code>ANTHROPIC_API_KEY</code> or <code>OPENAI_API_KEY</code>{" "}
          and restart the API. Everything else on this platform works without it.
        </div>
      )}

      {selection && providers && (
        <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat
            label="Would be analysed"
            value={selection.selected_count}
            hint={`of ${selection.considered_count} pages`}
          />
          <Stat label="Existing recommendations" value={items.length} />
          <Stat label="Provider" value={providers.active} hint={providers.configured.join(", ") || "not configured"} />
          <Stat
            label="Skip threshold"
            value={`SEO > ${providers.seo_score_threshold}`}
            hint={`Top ${providers.max_pages_per_run} by priority`}
          />
        </div>
      )}

      {selection && (
        <Card
          title="Selection"
          action={
            <button
              type="button"
              onClick={() => setShowSelection(!showSelection)}
              className="text-xs text-sky-400 hover:underline"
            >
              {showSelection ? "Hide" : "Show"} reasoning
            </button>
          }
          className="mb-4"
        >
          <p className="text-sm text-slate-400">
            {selection.selected_count} of {selection.considered_count} pages qualify. Healthy,
            high-scoring pages are skipped; a page carrying a CRITICAL issue is always included even
            if its score is high.
          </p>

          {showSelection && (
            <div className="table-wrap mt-4">
              <table className="data">
                <thead>
                  <tr>
                    <th>Rank</th>
                    <th>URL</th>
                    <th>Decision</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {selection.decisions.slice(0, 50).map((decision) => (
                    <tr key={decision.page_id}>
                      <td className="tnum text-slate-500">{decision.rank}</td>
                      <td className="max-w-xs truncate">
                        <Link
                          href={`/pages/${decision.page_id}`}
                          className="text-slate-300 hover:text-sky-400"
                        >
                          {displayPath(decision.url)}
                        </Link>
                      </td>
                      <td>
                        <span
                          className={`chip ${
                            decision.selected
                              ? "bg-violet-500/15 text-violet-300 ring-violet-500/30"
                              : "bg-slate-500/15 text-slate-400 ring-slate-500/30"
                          }`}
                        >
                          {decision.selected ? "Analyse" : "Skip"}
                        </span>
                      </td>
                      <td className="text-xs text-slate-400">{decision.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {items.length === 0 ? (
        <EmptyState
          title="No recommendations yet"
          description="Run an analysis to generate structured, developer-ready fixes for the pages that need them most."
        />
      ) : (
        <Card title={`Recommendations (${items.length})`}>
          <ul className="divide-y divide-slate-800">
            {items.map((item) => (
              <li key={item.id} className="py-4 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <Link
                      href={`/pages/${item.page_id}`}
                      className="font-medium text-slate-200 hover:text-sky-400"
                    >
                      {displayPath(item.url)}
                    </Link>
                    <p className="mt-1 text-sm text-slate-400">{item.summary}</p>

                    {item.suggested_title && (
                      <p className="mt-2 text-sm">
                        <span className="text-xs uppercase tracking-wide text-slate-500">
                          Suggested title:{" "}
                        </span>
                        <span className="text-emerald-300">{truncate(item.suggested_title, 90)}</span>
                      </p>
                    )}
                    {item.expected_impact && (
                      <p className="mt-1 text-xs text-slate-500">{item.expected_impact}</p>
                    )}
                  </div>

                  <div className="flex shrink-0 flex-col items-end gap-1.5 text-xs">
                    <span
                      className={`chip ${
                        item.priority === "critical"
                          ? "bg-rose-500/15 text-rose-300 ring-rose-500/30"
                          : item.priority === "high"
                            ? "bg-orange-500/15 text-orange-300 ring-orange-500/30"
                            : "bg-slate-500/15 text-slate-400 ring-slate-500/30"
                      }`}
                    >
                      {item.priority}
                    </span>
                    <span className="text-slate-500">{item.finding_count} findings</span>
                    {item.confidence !== null && (
                      <span className="text-slate-500">
                        {formatPercent(item.confidence, 0)} confidence
                      </span>
                    )}
                    <span className="text-slate-600">{formatRelative(item.analysed_at)}</span>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}
