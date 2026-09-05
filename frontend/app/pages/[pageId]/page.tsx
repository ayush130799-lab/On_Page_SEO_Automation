"use client";

/**
 * Page detail.
 *
 * Everything the platform knows about one URL: its issues, its provider metrics, why the priority
 * engine scored it the way it did, how it has trended, the AI recommendation, and which deploys
 * touched it.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import {
  AiBadge,
  BandBadge,
  Card,
  ErrorNote,
  PageHeader,
  ScoreBadge,
  SeverityBadge,
  Sparkline,
  Spinner,
  Stat,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import {
  COMPONENT_LABELS,
  displayPath,
  formatCurrency,
  formatDate,
  formatNumber,
  formatPercent,
  formatRelative,
} from "@/lib/format";
import type { AiFinding, CompetitorAnalysisResponse, PageDetailResponse } from "@/lib/types";

export default function PageDetailRoute() {
  return (
    <AuthGate>
      <PageDetailView />
    </AuthGate>
  );
}

function PageDetailView() {
  const params = useParams<{ pageId: string }>();
  const pageId = Number(params.pageId);

  const [data, setData] = useState<PageDetailResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [analysing, setAnalysing] = useState(false);

  // Competitor SERP Benchmark state
  const [competitor, setCompetitor] = useState<CompetitorAnalysisResponse | null>(null);
  const [competitorKeyword, setCompetitorKeyword] = useState("");
  const [analysingCompetitors, setAnalysingCompetitors] = useState(false);
  const [serpConfigured, setSerpConfigured] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const pageData = await api.pages.detail(pageId);
      setData(pageData);
      setError("");

      // Fetch live SERP competitor analysis & status in parallel
      try {
        const [serpStatus, compData] = await Promise.all([
          api.competitors.status(),
          api.competitors.get(pageData.page.website_id, pageId).catch(() => null),
        ]);
        setSerpConfigured(serpStatus.configured);
        if (compData && compData.available) {
          setCompetitor(compData);
          if (compData.keyword) setCompetitorKeyword(compData.keyword);
        }
      } catch {
        // Non-blocking for competitor data
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load this page.");
    } finally {
      setLoading(false);
    }
  }, [pageId]);

  useEffect(() => {
    void load();
  }, [load]);

  const analyse = async () => {
    if (!data) return;
    setAnalysing(true);
    setError("");
    try {
      await api.ai.analyse(data.page.website_id, {
        page_ids: [pageId],
        force: true,
        wait: true,
      });
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "AI analysis failed.");
    } finally {
      setAnalysing(false);
    }
  };

  const runCompetitorAnalysis = async () => {
    if (!data) return;
    setAnalysingCompetitors(true);
    setError("");
    try {
      const kw = competitorKeyword.trim() || undefined;
      await api.competitors.analyse(data.page.website_id, pageId, { keyword: kw, wait: true });
      const fresh = await api.competitors.get(data.page.website_id, pageId);
      if (fresh && fresh.available) {
        setCompetitor(fresh);
      }
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Competitor analysis failed.");
    } finally {
      setAnalysingCompetitors(false);
    }
  };

  if (loading && !data) {
    return (
      <div className="py-16">
        <Spinner label="Loading page…" />
      </div>
    );
  }

  if (!data) {
    return <ErrorNote error={error || "Page not found."} onRetry={load} />;
  }

  const { page, issues, metrics, priority, history, recommendation, github_changes, checks } = data;
  const recommendationPayload = recommendation?.payload ?? null;

  return (
    <>
      <PageHeader
        breadcrumb={[
          { href: "/", label: "Portfolio" },
          { href: `/websites/${page.website_id}`, label: "Website" },
        ]}
        title={displayPath(page.url)}
        subtitle={
          <a
            href={page.url}
            target="_blank"
            rel="noreferrer noopener"
            className="hover:text-sky-400"
          >
            {page.url}
          </a>
        }
        actions={
          <>
            <button type="button" onClick={load} className="btn-secondary">
              Refresh
            </button>
            <button
              type="button"
              onClick={() => void analyse()}
              disabled={analysing}
              className="btn-primary"
            >
              {analysing ? "Analysing…" : recommendation ? "Re-run AI analysis" : "Run AI analysis"}
            </button>
          </>
        }
      />

      {error && (
        <div className="mb-4">
          <ErrorNote error={error} />
        </div>
      )}

      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <Stat
          label="Priority score"
          value={
            <span className="flex items-center gap-2">
              {page.priority_score?.toFixed(1) ?? "—"}
              <BandBadge band={page.priority_band} />
            </span>
          }
          hint={page.priority_rank ? `Rank #${page.priority_rank} on this site` : undefined}
        />
        <Stat
          label="SEO score"
          value={page.seo_score?.toFixed(1) ?? "—"}
          hint={page.seo_category ?? undefined}
          tone={
            page.seo_score === null
              ? "default"
              : page.seo_score > 90
                ? "good"
                : page.seo_score >= 75
                  ? "warn"
                  : "bad"
          }
        />
        <Stat
          label="Issues"
          value={formatNumber(page.issue_count)}
          hint={page.highest_severity ?? undefined}
          tone={page.issue_count > 0 ? "warn" : "good"}
        />
        <Stat label="Users" value={formatNumber(metrics.users)} hint={`${metrics.window_days}d`} />
        <Stat
          label="Clicks"
          value={formatNumber(metrics.clicks)}
          hint={
            metrics.position !== null ? `Avg position ${metrics.position.toFixed(1)}` : undefined
          }
        />
        <Stat
          label="Conversions"
          value={formatNumber(metrics.conversions)}
          hint={metrics.revenue ? formatCurrency(metrics.revenue) : undefined}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          <Card
            title={`SEO issues (${issues.length})`}
            action={
              <span className="text-xs text-slate-500">
                {checks.length} checks run · HTTP {page.status_code ?? "—"}
              </span>
            }
          >
            {issues.length === 0 ? (
              <p className="text-sm text-emerald-300">
                No outstanding issues — this page passed every check.
              </p>
            ) : (
              <ul className="divide-y divide-slate-800">
                {issues.map((issue) => (
                  <li key={issue.id} className="py-3 first:pt-0 last:pb-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <SeverityBadge severity={issue.severity} />
                      <span className="text-sm font-medium text-slate-200">{issue.title}</span>
                      <code className="rounded bg-slate-800 px-1.5 py-0.5 text-xs text-slate-400">
                        {issue.rule_id}
                      </code>
                    </div>
                    <p className="mt-1.5 text-sm text-slate-400">{issue.description}</p>
                    {issue.recommendation && (
                      <p className="mt-1 text-sm text-sky-300/90">→ {issue.recommendation}</p>
                    )}
                    {issue.evidence && Object.keys(issue.evidence).length > 0 && (
                      <details className="mt-1.5">
                        <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-300">
                          Evidence
                        </summary>
                        <pre className="mt-1.5 overflow-x-auto rounded bg-slate-950 p-2 text-xs text-slate-400">
                          {JSON.stringify(issue.evidence, null, 2)}
                        </pre>
                      </details>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {recommendationPayload ? (
            <Card
              title="AI recommendation"
              action={
                <span className="text-xs text-slate-500">
                  {recommendation?.provider} · {recommendation?.model} ·{" "}
                  {formatRelative(recommendation?.analysed_at)}
                </span>
              }
            >
              <p className="text-sm text-slate-300">{recommendationPayload.summary}</p>

              {(recommendationPayload.reason || recommendation?.reason) && (
                <div className="mt-3 rounded-lg border border-indigo-500/30 bg-indigo-950/30 p-3 text-xs text-indigo-200">
                  <span className="font-semibold text-indigo-300">Why this recommendation matters: </span>
                  {recommendationPayload.reason || recommendation?.reason}
                </div>
              )}

              <div className="mt-3 flex flex-wrap gap-2 text-xs">
                {recommendationPayload.search_intent && (
                  <span className="chip bg-sky-500/15 text-sky-300 ring-sky-500/30">
                    Intent: {recommendationPayload.search_intent}
                  </span>
                )}
                {(recommendationPayload.search_impact_score ?? recommendation?.search_impact_score) !== undefined && (
                  <span className="chip bg-indigo-500/15 text-indigo-300 ring-indigo-500/30 font-medium">
                    Search Impact: {Math.round(recommendationPayload.search_impact_score ?? recommendation?.search_impact_score ?? 0)}/100
                  </span>
                )}
                {(recommendationPayload.user_activity_score ?? recommendation?.user_activity_score) !== undefined && (
                  <span className="chip bg-cyan-500/15 text-cyan-300 ring-cyan-500/30 font-medium">
                    User Activity Impact: {Math.round(recommendationPayload.user_activity_score ?? recommendation?.user_activity_score ?? 0)}/100
                  </span>
                )}
                {(recommendationPayload.impact_score ?? recommendation?.impact_score) !== undefined && (
                  <span className="chip bg-emerald-500/15 text-emerald-300 ring-emerald-500/30 font-semibold">
                    Overall Impact: {Math.round(recommendationPayload.impact_score ?? recommendation?.impact_score ?? 0)}/100
                  </span>
                )}
                <span className="chip bg-slate-500/15 text-slate-300 ring-slate-500/30">
                  Content quality {recommendationPayload.content_quality_score.toFixed(0)}
                </span>
                <span className="chip bg-slate-500/15 text-slate-300 ring-slate-500/30">
                  Topic coverage {recommendationPayload.topic_coverage_score.toFixed(0)}
                </span>
                <span className="chip bg-violet-500/15 text-violet-300 ring-violet-500/30">
                  Confidence {formatPercent(recommendationPayload.confidence, 0)}
                </span>
              </div>

              {recommendationPayload.expected_impact && (
                <p className="mt-3 rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-sm text-slate-300">
                  <span className="font-medium text-slate-200">Expected impact: </span>
                  {recommendationPayload.expected_impact}
                </p>
              )}

              {recommendationPayload.findings.length > 0 && (
                <div className="mt-4 space-y-3">
                  {recommendationPayload.findings.map((finding, index) => (
                    <FindingCard key={index} finding={finding} />
                  ))}
                </div>
              )}

              {recommendationPayload.suggested_changes.length > 0 && (
                <div className="mt-4">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
                    Suggested changes
                  </h3>
                  <ul className="space-y-2">
                    {recommendationPayload.suggested_changes.map((change, index) => (
                      <li key={index} className="rounded-lg border border-slate-800 p-3">
                        <div className="text-xs font-medium uppercase tracking-wide text-slate-500">
                          {change.field.replace(/_/g, " ")}
                        </div>
                        {change.current && (
                          <p className="mt-1.5 text-sm text-rose-300/80 line-through">
                            {change.current}
                          </p>
                        )}
                        <p className="mt-1 text-sm text-emerald-300">{change.suggested}</p>
                        {change.rationale && (
                          <p className="mt-1 text-xs text-slate-500">{change.rationale}</p>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {recommendationPayload.implementation_notes && (
                <p className="mt-4 border-t border-slate-800 pt-3 text-sm text-slate-400">
                  <span className="font-medium text-slate-300">Implementation: </span>
                  {recommendationPayload.implementation_notes}
                </p>
              )}
            </Card>
          ) : (
            <Card title="AI recommendation">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-sm text-slate-400">
                  {page.ai_status === "skipped"
                    ? "This page was skipped: it is healthy enough that an LLM call would not earn its cost."
                    : "No AI analysis has been run for this page yet."}
                </p>
                <AiBadge status={page.ai_status} />
              </div>
            </Card>
          )}

          {/* Live SERP & Competitor Benchmark Card (Phase 4) */}
          <Card
            title="Live SERP Competitor Benchmark (Google)"
            action={
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  placeholder="Target search keyword…"
                  value={competitorKeyword}
                  onChange={(e) => setCompetitorKeyword(e.target.value)}
                  className="rounded bg-slate-900 border border-slate-700 px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:border-sky-500 w-44 sm:w-60"
                />
                <button
                  type="button"
                  onClick={() => void runCompetitorAnalysis()}
                  disabled={analysingCompetitors || !serpConfigured}
                  className="btn-primary text-xs py-1"
                >
                  {analysingCompetitors ? "Analyzing SERP…" : "Analyze Competitors"}
                </button>
              </div>
            }
          >
            {!serpConfigured ? (
              <p className="text-sm text-slate-400">
                SERP competitor analysis requires a SerpApi key. Add <code className="text-sky-300">SERPAPI_KEY</code> to your <code className="text-sky-300">.env</code> file to enable live Google search benchmarks.
              </p>
            ) : !competitor ? (
              <div className="rounded-lg border border-dashed border-slate-800 p-6 text-center">
                <p className="text-sm text-slate-300 font-medium">
                  No competitor analysis has been run for this URL yet.
                </p>
                <p className="mt-1 text-xs text-slate-500">
                  Enter a target keyword above and click <strong>Analyze Competitors</strong> to benchmark against top 5 Google search results, extract People Also Ask questions, and detect content gaps.
                </p>
              </div>
            ) : (
              <div className="space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 pb-3 text-xs">
                  <div className="flex items-center gap-2">
                    <span className="text-slate-400">Target Keyword:</span>
                    <span className="font-semibold text-sky-400 bg-sky-500/10 px-2.5 py-0.5 rounded border border-sky-500/20">
                      {competitor.keyword}
                    </span>
                  </div>
                  <span className="text-slate-500">
                    Fetched {competitor.fetched_count} competitors · {formatRelative(competitor.analysed_at)}
                  </span>
                </div>

                {/* Content Gap Benchmarks */}
                {competitor.content_gap && (
                  <div className="grid gap-3 sm:grid-cols-2 rounded-lg border border-slate-800 bg-slate-900/40 p-3.5">
                    <div>
                      <span className="text-xs font-medium text-slate-400">Word Count Gap</span>
                      <div className="mt-1.5 flex items-baseline gap-2">
                        <span className="text-base font-bold text-slate-100">
                          {formatNumber(page.word_count)}
                        </span>
                        <span className="text-xs text-slate-500">words (your page) vs</span>
                        <span className="text-base font-bold text-amber-400">
                          {formatNumber(competitor.content_gap.competitor_median_word_count ?? 0)}
                        </span>
                        <span className="text-xs text-slate-500">competitor median</span>
                      </div>
                      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-800">
                        <div
                          className="h-full rounded-full bg-amber-500"
                          style={{
                            width: `${Math.min(
                              100,
                              Math.round(
                                ((page.word_count || 1) /
                                  (competitor.content_gap.competitor_median_word_count || 1)) *
                                  100,
                              ),
                            )}%`,
                          }}
                        />
                      </div>
                    </div>

                    <div>
                      <span className="text-xs font-medium text-slate-400">Heading Structure (H2)</span>
                      <div className="mt-1.5 flex items-baseline gap-2">
                        <span className="text-base font-bold text-slate-100">{page.h2_count}</span>
                        <span className="text-xs text-slate-500">H2s (your page) vs</span>
                        <span className="text-base font-bold text-indigo-400">
                          {competitor.content_gap.competitor_avg_h2_count?.toFixed(1) ?? "—"}
                        </span>
                        <span className="text-xs text-slate-500">competitor avg</span>
                      </div>
                      {competitor.content_gap.missing_subtopics && competitor.content_gap.missing_subtopics.length > 0 && (
                        <div className="mt-2 text-xs">
                          <span className="text-slate-500">Missing Subtopics: </span>
                          <span className="text-rose-300 font-medium">
                            {competitor.content_gap.missing_subtopics.join(", ")}
                          </span>
                        </div>
                      )}
                    </div>
                  </div>
                )}

                {/* Google PAA Questions */}
                {competitor.paa_questions && competitor.paa_questions.length > 0 && (
                  <div>
                    <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500 mb-2">
                      Google "People Also Ask" (PAA) Questions
                    </h4>
                    <div className="flex flex-wrap gap-1.5">
                      {competitor.paa_questions.map((q, i) => {
                        const questionText = typeof q === "string" ? q : (q as { question?: string })?.question || "";
                        if (!questionText) return null;
                        return (
                          <span
                            key={i}
                            className="chip bg-sky-500/10 text-sky-300 ring-sky-500/20 text-xs py-1"
                          >
                            ❓ {questionText}
                          </span>
                        );
                      })}
                    </div>
                  </div>
                )}

                {/* Competitors List */}
                {competitor.competitors && competitor.competitors.length > 0 && (
                  <div>
                    <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500 mb-2">
                      Top Ranking Competitors (Google SERP)
                    </h4>
                    <div className="space-y-2">
                      {competitor.competitors.map((comp) => (
                        <div
                          key={comp.position}
                          className="rounded border border-slate-800 p-2.5 text-xs bg-slate-950/40"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <div className="flex items-center gap-2 truncate">
                              <span className="font-bold text-slate-400">#{comp.position}</span>
                              <span className="font-medium text-slate-200 truncate">{comp.title || comp.domain}</span>
                              <a
                                href={comp.url}
                                target="_blank"
                                rel="noreferrer noopener"
                                className="text-sky-400 hover:underline font-mono truncate"
                              >
                                ({comp.domain})
                              </a>
                            </div>
                            <div className="flex items-center gap-3 shrink-0 text-slate-400">
                              <span>{formatNumber(comp.word_count)} words</span>
                              <span>{comp.h2_count ?? 0} H2s</span>
                            </div>
                          </div>
                          {comp.snippet && (
                            <p className="mt-1 text-slate-400 line-clamp-2">{comp.snippet}</p>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </Card>

          <Card title="Extracted values">
            <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
              <Field label="Title" value={page.title} sub={`${page.title?.length ?? 0} chars`} />
              <Field
                label="Meta description"
                value={page.meta_description}
                sub={`${page.meta_description?.length ?? 0} chars`}
              />
              <Field label="H1" value={page.h1} sub={`${page.h1_count} H1 on page`} />
              <Field label="Canonical" value={page.canonical_url} />
              <Field label="Robots" value={page.robots_directive ?? "none"} />
              <Field label="Language" value={page.lang ?? "not declared"} />
              <Field
                label="Headings"
                value={`${page.h2_count} H2 · ${page.h3_count} H3`}
              />
              <Field
                label="Content"
                value={`${formatNumber(page.word_count)} words`}
                sub={page.was_rendered ? "JavaScript-rendered" : "static HTML"}
              />
              <Field
                label="Images"
                value={`${page.image_count} images`}
                sub={`${page.missing_alt_count} without alt text`}
              />
              <Field
                label="Links"
                value={`${page.internal_link_count} internal · ${page.external_link_count} external`}
                sub={`${page.inbound_internal_links} inbound · ${page.broken_link_count} broken`}
              />
              <Field
                label="Structured data"
                value={
                  page.structured_data_types?.length
                    ? page.structured_data_types.join(", ")
                    : "none"
                }
              />
              <Field
                label="Response time"
                value={page.response_time_ms ? `${page.response_time_ms} ms` : "—"}
              />
            </dl>

            {page.redirect_chain && page.redirect_chain.length > 0 && (
              <div className="mt-4 border-t border-slate-800 pt-3">
                <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
                  Redirect chain
                </p>
                <p className="mt-1 break-all text-sm text-amber-300">
                  {[...page.redirect_chain, page.final_url ?? page.url].join(" → ")}
                </p>
              </div>
            )}
          </Card>
        </div>

        <div className="space-y-4">
          {priority && (
            <Card title="Why this priority">
              <p className="mb-3 text-sm text-slate-400">
                Score <span className="tnum font-semibold text-slate-100">{priority.score}</span> of
                100, rank #{priority.rank} on this website.
              </p>
              <ul className="space-y-3">
                {Object.entries(priority.components).map(([component, value]) => {
                  const weight = priority.weights[component] ?? 0;
                  return (
                    <li key={component}>
                      <div className="flex items-center justify-between text-xs">
                        <span className="text-slate-300">
                          {COMPONENT_LABELS[component] ?? component}
                        </span>
                        <span className="tnum text-slate-500">
                          {(value * 100).toFixed(0)}% × weight {(weight * 100).toFixed(0)}%
                        </span>
                      </div>
                      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-800">
                        <div
                          className="h-full rounded-full bg-sky-500"
                          style={{ width: `${Math.min(100, value * 100)}%` }}
                        />
                      </div>
                    </li>
                  );
                })}
              </ul>
              <p className="mt-3 border-t border-slate-800 pt-3 text-xs text-slate-500">
                Computed {formatRelative(priority.computed_at)} from{" "}
                {priority.data_sources.join(", ")}.
              </p>
            </Card>
          )}

          <Card title="Search & analytics">
            <dl className="space-y-2 text-sm">
              <MetricRow label="Clicks" value={formatNumber(metrics.clicks)} />
              <MetricRow label="Impressions" value={formatNumber(metrics.impressions)} />
              <MetricRow label="CTR" value={formatPercent(metrics.ctr, 2)} />
              <MetricRow
                label="Average position"
                value={metrics.position?.toFixed(1) ?? "—"}
              />
              <MetricRow label="Users" value={formatNumber(metrics.users)} />
              <MetricRow label="Sessions" value={formatNumber(metrics.sessions)} />
              <MetricRow
                label="Engagement rate"
                value={formatPercent(metrics.engagement_rate, 1)}
              />
              <MetricRow label="Conversions" value={formatNumber(metrics.conversions)} />
              <MetricRow label="Revenue" value={formatCurrency(metrics.revenue)} />
              <MetricRow
                label="Ranking keywords"
                value={formatNumber(metrics.organic_keywords)}
              />
              <MetricRow
                label="Striking distance"
                value={formatNumber(metrics.striking_distance_keywords)}
              />
              <MetricRow label="Backlinks" value={formatNumber(metrics.backlinks)} />
            </dl>
          </Card>

          {history.length > 1 && (
            <Card title="History">
              <div className="space-y-4">
                <Sparkline
                  points={history.map((point) => point.clicks)}
                  label="Search clicks"
                  color="rgb(56 189 248)"
                />
                <Sparkline
                  points={history.map((point) => point.users)}
                  label="Users"
                  color="rgb(52 211 153)"
                />
                <Sparkline
                  points={history
                    .map((point) => point.seo_score)
                    .filter((value): value is number => value !== null)}
                  label="SEO score"
                  color="rgb(251 191 36)"
                />
              </div>
            </Card>
          )}

          {github_changes.length > 0 && (
            <Card title="Recent code changes">
              <ul className="space-y-3">
                {github_changes.map((event) => (
                  <li key={event.id} className="border-b border-slate-800 pb-3 last:border-0 last:pb-0">
                    <div className="flex items-center justify-between gap-2 text-xs">
                      <code className="text-slate-400">{event.after_sha?.slice(0, 8)}</code>
                      <span className="text-slate-500">{formatRelative(event.created_at)}</span>
                    </div>
                    {event.commit_messages[0] && (
                      <p className="mt-1 text-sm text-slate-300">{event.commit_messages[0]}</p>
                    )}
                    <p className="mt-0.5 text-xs text-slate-500">
                      {event.pusher} · {event.action_taken?.replace(/_/g, " ")}
                    </p>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Card title="Crawl">
            <dl className="space-y-2 text-sm">
              <MetricRow label="Status" value={page.crawl_status} />
              <MetricRow label="First seen" value={formatDate(page.first_seen_at)} />
              <MetricRow label="Last crawled" value={formatRelative(page.last_crawled_at)} />
              {page.crawl_error && (
                <div className="pt-2 text-xs text-rose-300">{page.crawl_error}</div>
              )}
            </dl>
            <Link
              href={`/websites/${page.website_id}`}
              className="btn-secondary mt-4 w-full"
            >
              Back to website
            </Link>
          </Card>
        </div>
      </div>
    </>
  );
}

function FindingCard({ finding }: { finding: AiFinding }) {
  const priorityTone: Record<string, string> = {
    critical: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
    high: "bg-orange-500/15 text-orange-300 ring-orange-500/30",
    medium: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
    low: "bg-slate-500/15 text-slate-400 ring-slate-500/30",
  };

  return (
    <article className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <header className="flex flex-wrap items-center gap-2">
        <span className={`chip ${priorityTone[finding.priority] ?? priorityTone.low}`}>
          {finding.priority}
        </span>
        <span className="chip bg-slate-500/15 text-slate-400 ring-slate-500/30">
          {finding.effort} effort
        </span>
        <h4 className="text-sm font-medium text-slate-200">{finding.issue}</h4>
      </header>

      <p className="mt-2 text-sm text-slate-400">{finding.explanation}</p>

      <p className="mt-2 text-sm">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Why it matters:{" "}
        </span>
        <span className="text-slate-300">{finding.why_it_matters}</span>
      </p>

      <p className="mt-2 text-sm">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Fix: </span>
        <span className="text-sky-300">{finding.recommended_fix}</span>
      </p>

      {finding.implementation && (
        <p className="mt-2 rounded bg-slate-900 p-2 text-xs text-slate-400">
          <span className="font-semibold text-slate-300">Implementation: </span>
          {finding.implementation}
        </p>
      )}

      {finding.expected_impact && (
        <p className="mt-2 text-xs text-emerald-300/90">Impact: {finding.expected_impact}</p>
      )}
    </article>
  );
}

function Field({
  label,
  value,
  sub,
}: {
  label: string;
  value: string | null | undefined;
  sub?: string;
}) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 break-words text-sm text-slate-300">
        {value || <span className="text-rose-400">missing</span>}
      </dd>
      {sub && <dd className="text-xs text-slate-600">{sub}</dd>}
    </div>
  );
}

function MetricRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <dt className="text-slate-400">{label}</dt>
      <dd className="tnum text-slate-200">{value}</dd>
    </div>
  );
}
