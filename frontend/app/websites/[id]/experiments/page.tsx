"use client";

/**
 * Post-deployment experiments — the AI Feedback Loop's UI.
 *
 * Every tracked deployment predicts an impact when it lands; each checkpoint later compares that
 * prediction against what GSC/GA4 actually recorded. The accuracy report is shown first because it
 * is the trust signal: it says whether the AI's predictions are worth acting on at all.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import { Card, EmptyState, ErrorNote, PageHeader, Spinner, Stat, StatusBadge } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { displayPath, formatDate, formatPercent, formatRelative } from "@/lib/format";
import type { ExperimentAccuracyReport, ExperimentDetail, ExperimentListItem } from "@/lib/types";

export default function ExperimentsPage() {
  return (
    <AuthGate>
      <Experiments />
    </AuthGate>
  );
}

function Experiments() {
  const params = useParams<{ id: string }>();
  const websiteId = Number(params.id);

  const [items, setItems] = useState<ExperimentListItem[]>([]);
  const [accuracy, setAccuracy] = useState<ExperimentAccuracyReport | null>(null);
  const [selected, setSelected] = useState<ExperimentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    try {
      const [list, report] = await Promise.all([
        api.experiments.list(websiteId),
        api.experiments.accuracy(websiteId),
      ]);
      setItems(list.items);
      setAccuracy(report);
      setError("");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load experiments.");
    } finally {
      setLoading(false);
    }
  }, [websiteId]);

  useEffect(() => {
    void load();
  }, [load]);

  const openExperiment = async (id: number) => {
    setLoadingDetail(true);
    setError("");
    try {
      const detail = await api.experiments.get(websiteId, id);
      setSelected(detail);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load this experiment.");
    } finally {
      setLoadingDetail(false);
    }
  };

  const runDueCheckpoints = async () => {
    setRunning(true);
    setError("");
    setNotice("");
    try {
      const result = await api.experiments.runDueCheckpoints(websiteId);
      setNotice(
        `Measured ${result.measured} checkpoint(s); ${result.experiments_completed} experiment(s) completed.` +
          (result.errors.length > 0 ? ` ${result.errors.length} error(s) occurred.` : ""),
      );
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not run due checkpoints.");
    } finally {
      setRunning(false);
    }
  };

  if (loading) {
    return (
      <div className="py-16">
        <Spinner label="Loading experiments…" />
      </div>
    );
  }

  return (
    <>
      <PageHeader
        breadcrumb={[
          { href: "/", label: "Portfolio" },
          { href: `/websites/${websiteId}`, label: "Website" },
        ]}
        title="Post-deployment experiments"
        subtitle="Every deployment's predicted impact, checked against what GSC and GA4 actually recorded."
        actions={
          <button type="button" onClick={() => void runDueCheckpoints()} disabled={running} className="btn-primary">
            {running ? "Running…" : "Run due checkpoints"}
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

      {accuracy && (
        <Card title="Prediction accuracy" className="mb-6">
          {!accuracy.sample_size_sufficient ? (
            <p className="text-sm text-slate-400">
              Only {accuracy.total_measured - accuracy.insufficient_data} evaluable checkpoint(s) so
              far — not enough yet for a meaningful accuracy rate.
            </p>
          ) : (
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
              <Stat label="Checkpoints measured" value={accuracy.total_measured} />
              <Stat label="Matched prediction" value={accuracy.matched} />
              <Stat
                label="Accuracy rate"
                value={accuracy.accuracy_rate !== null ? formatPercent(accuracy.accuracy_rate, 0) : "—"}
                tone={
                  accuracy.accuracy_rate === null
                    ? "default"
                    : accuracy.accuracy_rate >= 0.7
                      ? "good"
                      : accuracy.accuracy_rate >= 0.5
                        ? "warn"
                        : "bad"
                }
              />
            </div>
          )}

          {accuracy.weight_adjustment_suggestions.length > 0 && (
            <ul className="mt-4 space-y-2 border-t border-slate-800 pt-4">
              {accuracy.weight_adjustment_suggestions.map((s, i) => (
                <li key={i} className="text-sm text-slate-300">
                  <span className="font-medium text-slate-200">{s.factor}</span> — {s.direction}:{" "}
                  <span className="text-slate-400">{s.reason}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}

      {items.length === 0 ? (
        <EmptyState
          title="No experiments tracked yet"
          description="An experiment starts automatically when a tracked pull request affecting a crawled page is merged and deployed."
        />
      ) : (
        <Card title={`Experiments (${items.length})`}>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>URL</th>
                  <th>PR</th>
                  <th>Predicted impact</th>
                  <th>Risk</th>
                  <th>Status</th>
                  <th>Deployed</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr
                    key={item.id}
                    className="cursor-pointer hover:bg-slate-800/40"
                    onClick={() => void openExperiment(item.id)}
                  >
                    <td className="max-w-xs truncate text-slate-300">
                      {item.affected_url ? displayPath(item.affected_url) : "—"}
                    </td>
                    <td className="tnum text-slate-400">
                      {item.pull_request_id ? `#${item.pull_request_id}` : "—"}
                    </td>
                    <td className="text-slate-300">{item.predicted_impact ?? "—"}</td>
                    <td className="text-slate-400">{item.predicted_risk_level ?? "—"}</td>
                    <td>
                      <StatusBadge status={item.status} />
                    </td>
                    <td className="text-slate-500">{formatRelative(item.deployed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {loadingDetail && (
        <div className="mt-6 py-8">
          <Spinner label="Loading experiment…" />
        </div>
      )}

      {selected && !loadingDetail && (
        <Card
          title={selected.affected_url ? displayPath(selected.affected_url) : `Experiment #${selected.id}`}
          className="mt-6"
          action={
            <button type="button" onClick={() => setSelected(null)} className="text-xs text-sky-400 hover:underline">
              Close
            </button>
          }
        >
          <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Predicted impact" value={selected.predicted_impact ?? "—"} />
            <Stat label="Risk level" value={selected.predicted_risk_level ?? "—"} />
            <Stat
              label="Confidence (positive)"
              value={
                selected.predicted_positive_confidence !== null
                  ? formatPercent(selected.predicted_positive_confidence, 0)
                  : "—"
              }
            />
            <Stat label="Deployed" value={formatDate(selected.deployed_at)} />
          </div>

          {selected.page_id && (
            <p className="mb-4 text-sm">
              <Link href={`/pages/${selected.page_id}`} className="text-sky-400 hover:underline">
                View page details →
              </Link>
            </p>
          )}

          {selected.checkpoints.length === 0 ? (
            <p className="text-sm text-slate-500">No checkpoint has come due yet.</p>
          ) : (
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Day</th>
                    <th>Measured</th>
                    <th>Clicks Δ</th>
                    <th>Impressions Δ</th>
                    <th>Position Δ</th>
                    <th>Conversions Δ</th>
                    <th>Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {selected.checkpoints.map((c) => (
                    <tr key={c.checkpoint_day}>
                      <td className="tnum text-slate-300">Day {c.checkpoint_day}</td>
                      <td className="text-slate-500">
                        {c.measured_at ? formatRelative(c.measured_at) : "Pending"}
                      </td>
                      <td className="tnum text-slate-300">
                        {c.deltas.clicks_pct !== null ? formatPercent(c.deltas.clicks_pct, 1) : "—"}
                      </td>
                      <td className="tnum text-slate-300">
                        {c.deltas.impressions_pct !== null
                          ? formatPercent(c.deltas.impressions_pct, 1)
                          : "—"}
                      </td>
                      <td className="tnum text-slate-300">
                        {c.deltas.position !== null ? c.deltas.position.toFixed(1) : "—"}
                      </td>
                      <td className="tnum text-slate-300">
                        {c.deltas.conversions_pct !== null
                          ? formatPercent(c.deltas.conversions_pct, 1)
                          : "—"}
                      </td>
                      <td>
                        {c.prediction_matched === null ? (
                          <span className="text-slate-500">—</span>
                        ) : c.prediction_matched ? (
                          <span className="text-emerald-400">Matched</span>
                        ) : (
                          <span className="text-rose-400">Mismatched</span>
                        )}
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
