"use client";

/**
 * Website-Level SEO Planning & Opportunity Roadmap.
 *
 * Provides a 3-week chronological sprint plan (Phase 3) aggregating individual URL priority scores,
 * search intent opportunities, and user activity signals, alongside the full live Priority Matrix.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import {
  BandBadge,
  Card,
  EmptyState,
  ErrorNote,
  PageHeader,
  Spinner,
  Stat,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { displayPath, formatPercent, formatRelative } from "@/lib/format";
import type { PriorityMatrixRow, RoadmapResponse, RoadmapWeek } from "@/lib/types";

export default function WebsiteRoadmapRoute() {
  return (
    <AuthGate>
      <WebsiteRoadmapView />
    </AuthGate>
  );
}

function WebsiteRoadmapView() {
  const params = useParams<{ id: string }>();
  const websiteId = Number(params.id);

  const [roadmap, setRoadmap] = useState<RoadmapResponse | null>(null);
  const [matrix, setMatrix] = useState<PriorityMatrixRow[]>([]);
  const [activeTab, setActiveTab] = useState<"sprints" | "matrix">("sprints");
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [roadmapData, matrixData] = await Promise.all([
        api.roadmap.get(websiteId),
        api.roadmap.priorityMatrix(websiteId).catch(() => ({ website_id: websiteId, total: 0, items: [] })),
      ]);
      setRoadmap(roadmapData);
      setMatrix(matrixData.items ?? []);
      setError("");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load the roadmap.");
    } finally {
      setLoading(false);
    }
  }, [websiteId]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleGenerate = async () => {
    setGenerating(true);
    setError("");
    setNotice("");
    try {
      const result = await api.roadmap.generate(websiteId);
      setRoadmap(result);
      setNotice("New 3-week SEO sprint roadmap generated successfully!");
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Failed to generate roadmap.");
    } finally {
      setGenerating(false);
    }
  };

  if (loading && !roadmap) {
    return (
      <div className="py-16">
        <Spinner label="Loading SEO growth roadmap…" />
      </div>
    );
  }

  const overview = roadmap?.overview;
  const weeks: RoadmapWeek[] = roadmap?.weeks ?? [];
  const priorityCounts = overview?.priority_counts ?? { P0: 0, P1: 0, P2: 0, P3: 0 };

  return (
    <>
      <PageHeader
        breadcrumb={[
          { href: "/", label: "Portfolio" },
          { href: `/websites/${websiteId}`, label: "Website" },
        ]}
        title="SEO Growth Roadmap & Sprint Planning"
        subtitle={
          roadmap?.generated_at
            ? `Latest sprint snapshot generated ${formatRelative(roadmap.generated_at)}`
            : "Transform technical audit findings into a ranked, multi-week execution plan."
        }
        actions={
          <div className="flex items-center gap-2">
            <Link href={`/websites/${websiteId}`} className="btn-secondary">
              Back to overview
            </Link>
            <button
              type="button"
              onClick={() => void handleGenerate()}
              disabled={generating}
              className="btn-primary"
            >
              {generating ? "Generating…" : "Generate Sprint Roadmap"}
            </button>
          </div>
        }
      />

      {error && (
        <div className="mb-4">
          <ErrorNote error={error} onRetry={load} />
        </div>
      )}

      {notice && (
        <div className="mb-4 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-200">
          {notice}
        </div>
      )}

      {/* Top Opportunity KPIs */}
      {overview && (
        <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
          <Stat
            label="Overall SEO Opportunity"
            value={overview.overall_seo_opportunity != null ? `${Math.round(Number(overview.overall_seo_opportunity))}/100` : "—"}
            tone={Number(overview.overall_seo_opportunity) > 70 ? "good" : "warn"}
            hint="Sitewide potential"
          />
          <Stat
            label="Organic Growth Potential"
            value={
              typeof overview.organic_growth_opportunity === "number"
                ? `${Math.round(overview.organic_growth_opportunity)}/100`
                : overview.organic_growth_opportunity || "—"
            }
            tone={
              overview.organic_growth_opportunity === "HIGH" || Number(overview.organic_growth_opportunity) > 60
                ? "good"
                : overview.organic_growth_opportunity === "MEDIUM"
                ? "warn"
                : "default"
            }
            hint="Ranking & traffic lift"
          />
          <Stat
            label="User Activity Opportunity"
            value={
              typeof overview.user_activity_opportunity === "number"
                ? `${Math.round(overview.user_activity_opportunity)}/100`
                : overview.user_activity_opportunity || "—"
            }
            tone={
              overview.user_activity_opportunity === "HIGH" || Number(overview.user_activity_opportunity) > 60
                ? "good"
                : overview.user_activity_opportunity === "MEDIUM"
                ? "warn"
                : "default"
            }
            hint="Engagement & conversion lift"
          />
          <Stat
            label="Critical (P0)"
            value={priorityCounts.P0}
            tone={priorityCounts.P0 > 0 ? "bad" : "good"}
            hint="Immediate sprint action"
          />
          <Stat
            label="High Growth (P1)"
            value={priorityCounts.P1}
            tone={priorityCounts.P1 > 0 ? "warn" : "default"}
            hint="High-volume opportunities"
          />
          <Stat
            label="Optimization (P2)"
            value={priorityCounts.P2}
            hint="Content depth & keywords"
          />
          <Stat
            label="Maintenance (P3)"
            value={priorityCounts.P3}
            hint="Minor technical cleanup"
          />
        </div>
      )}

      {/* View Switcher Tabs */}
      <div className="mb-4 flex border-b border-slate-800 text-sm">
        <button
          type="button"
          onClick={() => setActiveTab("sprints")}
          className={`px-4 py-2.5 font-medium border-b-2 transition-colors ${
            activeTab === "sprints"
              ? "border-sky-500 text-sky-400"
              : "border-transparent text-slate-400 hover:text-slate-200"
          }`}
        >
          Sprint Roadmaps ({weeks.length} Weeks)
        </button>
        <button
          type="button"
          onClick={() => setActiveTab("matrix")}
          className={`px-4 py-2.5 font-medium border-b-2 transition-colors ${
            activeTab === "matrix"
              ? "border-sky-500 text-sky-400"
              : "border-transparent text-slate-400 hover:text-slate-200"
          }`}
        >
          Website Priority Matrix ({matrix.length} URLs)
        </button>
      </div>

      {/* TAB 1: SPRINT ROADMAPS */}
      {activeTab === "sprints" && (
        <div>
          {!roadmap?.generated || weeks.length === 0 ? (
            <EmptyState
              title="No roadmap generated yet"
              description="Click 'Generate Sprint Roadmap' above to analyze your website's priority scores and build a ranked, multi-week execution plan."
              action={
                <button
                  type="button"
                  onClick={() => void handleGenerate()}
                  disabled={generating}
                  className="btn-primary"
                >
                  {generating ? "Generating…" : "Generate Sprint Roadmap"}
                </button>
              }
            />
          ) : (
            <div className="space-y-6">
              {weeks.map((week) => {
                const title = week.label || week.title || "Sprint Focus";
                const focus = week.focus || week.label || "Priority Optimisations";
                const tasks = week.tasks || week.items || [];
                return (
                  <Card
                    key={week.week}
                    title={`Week ${week.week} — ${title}`}
                    action={
                      <span className="chip bg-sky-500/15 text-sky-300 ring-sky-500/30 text-xs">
                        Focus: {focus}
                      </span>
                    }
                  >
                    {tasks.length === 0 ? (
                      <p className="text-sm text-slate-400">No tasks allocated for this sprint.</p>
                    ) : (
                      <div className="space-y-3">
                        {tasks.map((task, idx) => {
                          const priority = task.priority_level || task.priority || "P1";
                          const actionTitle = task.title || task.action || "SEO Optimization";
                          const rationale = task.reason || task.rationale;
                          return (
                            <div
                              key={idx}
                              className="rounded-lg border border-slate-800 bg-slate-900/40 p-4 transition-colors hover:border-slate-700"
                            >
                              <div className="flex flex-wrap items-start justify-between gap-3">
                                <div className="min-w-0 flex-1">
                                  <div className="flex flex-wrap items-center gap-2">
                                    <span
                                      className={`chip text-xs font-semibold ${
                                        priority === "P0"
                                          ? "bg-rose-500/15 text-rose-300 ring-rose-500/30"
                                          : priority === "P1"
                                          ? "bg-orange-500/15 text-orange-300 ring-orange-500/30"
                                          : priority === "P2"
                                          ? "bg-amber-500/15 text-amber-300 ring-amber-500/30"
                                          : "bg-slate-500/15 text-slate-400 ring-slate-500/30"
                                      }`}
                                    >
                                      {priority}
                                    </span>
                                    {task.overall_priority != null && (
                                      <span className="chip bg-sky-500/15 text-sky-300 ring-sky-500/30 text-xs">
                                        Score: {Math.round(task.overall_priority)}
                                      </span>
                                    )}
                                    {task.effort && (
                                      <span className="chip bg-slate-800 text-slate-300 ring-slate-700 text-xs">
                                        {task.effort} effort
                                      </span>
                                    )}
                                    <h4 className="text-sm font-semibold text-slate-200">
                                      {actionTitle}
                                    </h4>
                                  </div>

                                  {task.url && (
                                    <div className="mt-1.5">
                                      <Link
                                        href={task.page_id ? `/pages/${task.page_id}` : "#"}
                                        className="text-xs font-mono text-sky-400 hover:underline"
                                      >
                                        {displayPath(task.url)}
                                      </Link>
                                    </div>
                                  )}

                                  {rationale && (
                                    <p className="mt-2 text-xs text-slate-300 leading-relaxed">
                                      <span className="font-semibold text-slate-400">Why: </span>
                                      {rationale}
                                    </p>
                                  )}

                                  {task.expected_outcome && (
                                    <p className="mt-1 text-xs text-emerald-400/90 leading-relaxed">
                                      <span className="font-semibold text-emerald-500">Expected Outcome: </span>
                                      {task.expected_outcome}
                                    </p>
                                  )}

                                  <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                                    {task.search_impact_score != null && (
                                      <span className="text-slate-400">
                                        Search Impact: <strong className="text-slate-200">{Math.round(task.search_impact_score)}</strong>
                                      </span>
                                    )}
                                    {task.user_activity_score != null && (
                                      <span className="text-slate-400">
                                        • User Activity: <strong className="text-slate-200">{Math.round(task.user_activity_score)}</strong>
                                      </span>
                                    )}
                                  </div>

                                  {task.target_keywords && task.target_keywords.length > 0 && (
                                    <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                                      <span className="text-slate-500">Target Keywords:</span>
                                      {task.target_keywords.map((kw) => (
                                        <span
                                          key={kw}
                                          className="chip bg-indigo-500/15 text-indigo-300 ring-indigo-500/30 text-xs"
                                        >
                                          {kw}
                                        </span>
                                      ))}
                                    </div>
                                  )}
                                </div>

                                {task.page_id && (
                                  <Link
                                    href={`/pages/${task.page_id}`}
                                    className="shrink-0 btn-secondary text-xs"
                                  >
                                    View page details →
                                  </Link>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </Card>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* TAB 2: PRIORITY MATRIX */}
      {activeTab === "matrix" && (
        <Card
          title="Website Priority Matrix"
          action={
            <span className="text-xs text-slate-500">
              Live URL ranking combining SEO Opportunity, Traffic Activity, and Business Value
            </span>
          }
        >
          {matrix.length === 0 ? (
            <EmptyState
              title="Matrix empty"
              description="No priority matrix rows available. Crawl the website to compute priority scores."
            />
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Priority</th>
                    <th>URL</th>
                    <th className="text-right">SEO Opp</th>
                    <th className="text-right">Activity Opp</th>
                    <th className="text-right">Business Value</th>
                    <th className="text-right">Severity</th>
                    <th className="text-right">Overall Score</th>
                    <th>Top Action</th>
                  </tr>
                </thead>
                <tbody>
                  {matrix.map((row) => (
                    <tr key={row.page_id}>
                      <td>
                        <span
                          className={`chip text-xs font-semibold ${
                            row.priority_level === "P0"
                              ? "bg-rose-500/15 text-rose-300 ring-rose-500/30"
                              : row.priority_level === "P1"
                              ? "bg-orange-500/15 text-orange-300 ring-orange-500/30"
                              : row.priority_level === "P2"
                              ? "bg-amber-500/15 text-amber-300 ring-amber-500/30"
                              : "bg-slate-500/15 text-slate-400 ring-slate-500/30"
                          }`}
                        >
                          {row.priority_level}
                        </span>
                      </td>
                      <td className="max-w-xs truncate font-mono text-xs">
                        <Link
                          href={`/pages/${row.page_id}`}
                          className="text-slate-200 hover:text-sky-400"
                        >
                          {displayPath(row.url)}
                        </Link>
                      </td>
                      <td className="tnum text-right text-xs text-slate-300">
                        {Math.round(row.seo_opportunity)}%
                      </td>
                      <td className="tnum text-right text-xs text-slate-300">
                        {Math.round(row.user_activity_opportunity)}%
                      </td>
                      <td className="tnum text-right text-xs text-slate-300">
                        {Math.round(row.business_value)}%
                      </td>
                      <td className="tnum text-right text-xs text-slate-300">
                        {Math.round(row.technical_severity)}%
                      </td>
                      <td className="tnum text-right text-xs font-bold text-emerald-400">
                        {Math.round(row.overall_priority)}
                      </td>
                      <td className="max-w-xs truncate text-xs text-slate-400">
                        {row.top_recommendation || "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </>
  );
}
