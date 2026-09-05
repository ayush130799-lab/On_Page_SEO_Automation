"use client";

/**
 * Per-website settings, principally the priority weights.
 *
 * The sliders show the live effect on the ranking before anything is saved, which is the only way
 * to tune a weighted model with any confidence.
 */

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import { Card, ErrorNote, PageHeader, Spinner } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { COMPONENT_LABELS, displayPath, formatNumber } from "@/lib/format";
import type { Website, WeightResponse } from "@/lib/types";

const COMPONENTS = ["seo_severity", "ga4_activity", "gsc_search", "semrush_opportunity"] as const;

const COMPONENT_HELP: Record<string, string> = {
  seo_severity: "How badly the page is broken, from its worst issue and overall score.",
  ga4_activity: "Real users, sessions, conversions and revenue flowing through the page.",
  gsc_search: "Search demand already captured, and the demand being left behind.",
  semrush_opportunity: "Unclaimed organic upside — striking-distance keywords and volume.",
};

export default function WebsiteSettingsPage() {
  return (
    <AuthGate>
      <WebsiteSettings />
    </AuthGate>
  );
}

function WebsiteSettings() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const websiteId = Number(params.id);

  const [website, setWebsite] = useState<Website | null>(null);
  const [weights, setWeights] = useState<WeightResponse | null>(null);
  const [draft, setDraft] = useState<Record<string, number>>({});
  const [crawlSettings, setCrawlSettings] = useState({
    render_mode: "auto",
    max_pages: "",
    respect_robots_txt: true,
  });
  const [preview, setPreview] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    try {
      const [site, weightData] = await Promise.all([
        api.websites.get(websiteId),
        api.priority.weights(websiteId),
      ]);
      setWebsite(site);
      setWeights(weightData);
      setDraft(weightData.weights);
      setCrawlSettings({
        render_mode: site.render_mode || "auto",
        max_pages: site.max_pages ? String(site.max_pages) : "",
        respect_robots_txt: site.respect_robots_txt ?? true,
      });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load settings.");
    } finally {
      setLoading(false);
    }
  }, [websiteId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Preview the ranking these weights would produce, without saving them.
  useEffect(() => {
    if (Object.keys(draft).length === 0) return;
    const timer = setTimeout(async () => {
      try {
        const result = await api.priority.preview(websiteId, draft);
        setPreview(result.top_pages.slice(0, 10));
      } catch {
        setPreview([]);
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [draft, websiteId]);

  const total = Object.values(draft).reduce((sum, value) => sum + value, 0);

  const save = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const saved = await api.priority.setWeights(websiteId, draft);
      setWeights(saved);
      setDraft(saved.weights);
      await api.priority.rescore(websiteId);
      setNotice("Weights saved and every page rescored.");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not save the weights.");
    } finally {
      setBusy(false);
    }
  };

  const saveCrawlSettings = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const updated = await api.websites.update(websiteId, {
        render_mode: crawlSettings.render_mode,
        max_pages: crawlSettings.max_pages ? Number(crawlSettings.max_pages) : null,
        respect_robots_txt: crawlSettings.respect_robots_txt,
      });
      setWebsite(updated);
      setNotice("Crawl settings updated. Run 'Crawl now' to recrawl with these settings.");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not save crawl settings.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`Delete ${website?.name}? Every crawl, metric and score is removed.`)) {
      return;
    }
    setBusy(true);
    try {
      await api.websites.remove(websiteId);
      router.push("/");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not delete the website.");
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="py-16">
        <Spinner label="Loading settings…" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader
        breadcrumb={[
          { href: "/", label: "Portfolio" },
          { href: `/websites/${websiteId}`, label: "Website" },
        ]}
        title="Settings"
        subtitle={website?.name}
        actions={
          <div className="flex items-center gap-2">
            <Link href={`/websites/${websiteId}/roadmap`} className="btn-secondary">
              Roadmap
            </Link>
            <Link href={`/websites/${websiteId}/recommendations`} className="btn-secondary">
              AI recommendations
            </Link>
          </div>
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

      <div className="space-y-4">
        <Card
          title="Priority weights"
          action={
            <span className="text-xs text-slate-500">
              Values are normalised, so they need not sum to 100
            </span>
          }
        >
          <p className="mb-4 text-sm text-slate-400">
            These decide how business signals are balanced against technical severity when ranking
            work. A page with more users and conversions can outrank a technically worse page —
            that trade-off is exactly what these control.
          </p>

          <div className="space-y-5">
            {COMPONENTS.map((component) => {
              const value = draft[component] ?? 0;
              const share = total > 0 ? value / total : 0;
              const effective = weights?.effective_weights?.[component] ?? 0;
              const inactive = effective === 0 && value > 0;

              return (
                <div key={component}>
                  <div className="mb-1.5 flex items-baseline justify-between gap-3">
                    <label className="text-sm font-medium text-slate-200" htmlFor={component}>
                      {COMPONENT_LABELS[component]}
                    </label>
                    <span className="tnum text-sm text-slate-400">
                      {(share * 100).toFixed(0)}%
                    </span>
                  </div>
                  <input
                    id={component}
                    type="range"
                    min={0}
                    max={100}
                    value={Math.round(value * 100)}
                    onChange={(event) =>
                      setDraft({ ...draft, [component]: Number(event.target.value) / 100 })
                    }
                    className="w-full accent-sky-500"
                  />
                  <p className="mt-1 text-xs text-slate-500">{COMPONENT_HELP[component]}</p>
                  {inactive && (
                    <p className="mt-1 text-xs text-amber-400">
                      No data for this signal yet, so its weight is redistributed across the others
                      rather than penalising every page equally.
                    </p>
                  )}
                </div>
              );
            })}
          </div>

          <div className="mt-5 flex flex-wrap gap-2">
            <button type="button" onClick={() => void save()} disabled={busy} className="btn-primary">
              {busy ? "Saving…" : "Save and rescore"}
            </button>
            <button
              type="button"
              onClick={() => setDraft({
                seo_severity: 0.4,
                ga4_activity: 0.3,
                gsc_search: 0.2,
                semrush_opportunity: 0.1,
              })}
              className="btn-secondary"
            >
              Reset to defaults
            </button>
          </div>

          <p className="mt-3 text-xs text-slate-500">
            Signals with data: {weights?.data_sources.join(", ") || "none yet"}.
          </p>
        </Card>

        {preview.length > 0 && (
          <Card
            title="Preview"
            action={<span className="text-xs text-slate-500">Not saved until you apply</span>}
          >
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>URL</th>
                    <th className="text-right">Priority</th>
                    <th>Band</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.map((row, index) => (
                    <tr key={String(row.page_id)}>
                      <td className="tnum text-slate-500">{index + 1}</td>
                      <td className="max-w-md truncate text-slate-300">
                        {displayPath(String(row.url))}
                      </td>
                      <td className="tnum text-right text-slate-100">
                        {Number(row.priority_score).toFixed(1)}
                      </td>
                      <td className="text-slate-400">{String(row.band ?? "—")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}

        {website && (
          <Card title="Crawl & Rendering settings">
            <div className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="sm:col-span-2">
                  <label className="label" htmlFor="render-mode">
                    JavaScript rendering
                  </label>
                  <select
                    id="render-mode"
                    className="input"
                    value={crawlSettings.render_mode}
                    onChange={(e) =>
                      setCrawlSettings({ ...crawlSettings, render_mode: e.target.value })
                    }
                  >
                    <option value="auto">Auto — render only thin pages / SPAs</option>
                    <option value="always">Always render (Full Chromium — best accuracy)</option>
                    <option value="never">Never render (fastest, static HTML only)</option>
                  </select>
                  <p className="mt-1 text-xs text-slate-500">
                    Use &quot;Always render&quot; for React / Next.js / Vue SPAs so every route
                    gets unique rendered content instead of the static shell.
                  </p>
                </div>

                <div className="sm:col-span-2">
                  <label className="flex items-center gap-2 text-sm text-slate-300 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={!!crawlSettings.max_pages}
                      onChange={(e) =>
                        setCrawlSettings({ ...crawlSettings, max_pages: e.target.checked ? "500" : "" })
                      }
                      className="rounded border-slate-700 bg-slate-900 text-sky-500"
                    />
                    Limit number of pages crawled
                  </label>
                  {crawlSettings.max_pages && (
                    <div className="mt-2">
                      <input
                        id="max-pages"
                        type="number"
                        min={1}
                        className="input"
                        placeholder="e.g. 500"
                        value={crawlSettings.max_pages}
                        onChange={(e) =>
                          setCrawlSettings({ ...crawlSettings, max_pages: e.target.value })
                        }
                      />
                      <p className="mt-1 text-xs text-slate-500">
                        Leave unchecked to crawl every page the site has (recommended).
                      </p>
                    </div>
                  )}
                </div>

                <div className="sm:col-span-2">
                  <label className="flex items-center gap-2 text-sm text-slate-300">
                    <input
                      type="checkbox"
                      checked={crawlSettings.respect_robots_txt}
                      onChange={(e) =>
                        setCrawlSettings({ ...crawlSettings, respect_robots_txt: e.target.checked })
                      }
                      className="rounded border-slate-700 bg-slate-900 text-sky-500"
                    />
                    Respect robots.txt rules during crawl
                  </label>
                </div>
              </div>

              <div className="pt-2">
                <button
                  type="button"
                  onClick={() => void saveCrawlSettings()}
                  disabled={busy}
                  className="btn-primary"
                >
                  {busy ? "Saving…" : "Save crawl settings"}
                </button>
              </div>

              <div className="mt-4 border-t border-slate-800 pt-4">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-500 mb-2">
                  Site details
                </p>
                <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
                  <Row label="URL" value={website.url} />
                  <Row label="Domain" value={website.domain} />
                  <Row label="Repository" value={website.github_repo ?? "not connected"} />
                  <Row label="Branch" value={website.github_branch ?? "—"} />
                  <Row label="Pages tracked" value={formatNumber(website.total_pages)} />
                </dl>
              </div>
            </div>
          </Card>
        )}

        <Card title="Danger zone">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-slate-400">
              Deleting removes every page, crawl, metric, score and recommendation for this website.
            </p>
            <button
              type="button"
              onClick={() => void remove()}
              disabled={busy}
              className="btn border border-rose-500/40 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20"
            >
              Delete website
            </button>
          </div>
        </Card>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 break-words text-slate-300">{value}</dd>
    </div>
  );
}
