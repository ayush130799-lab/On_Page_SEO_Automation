"use client";

/**
 * Integration connection screen.
 *
 * Secrets go in and never come back out: the API returns status and non-sensitive configuration
 * only, so this screen shows connection state rather than the values behind it.
 */

import { useParams, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import { Card, ErrorNote, PageHeader, Spinner, StatusBadge } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { PROVIDER_LABELS, formatRelative } from "@/lib/format";
import type { IntegrationSummary } from "@/lib/types";

export default function IntegrationsPage() {
  return (
    <AuthGate>
      <Suspense fallback={<Spinner />}>
        <Integrations />
      </Suspense>
    </AuthGate>
  );
}

function Integrations() {
  const params = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const websiteId = Number(params.id);

  const [integrations, setIntegrations] = useState<IntegrationSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    try {
      setIntegrations(await api.integrations.list(websiteId));
      setError("");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load integrations.");
    } finally {
      setLoading(false);
    }
  }, [websiteId]);

  useEffect(() => {
    void load();
  }, [load]);

  // The OAuth callback redirects back here with the outcome in the query string.
  useEffect(() => {
    const status = searchParams.get("status");
    const provider = searchParams.get("provider");
    if (status === "connected" && provider) {
      setNotice(
        searchParams.get("selected")
          ? `${PROVIDER_LABELS[provider] ?? provider} connected and configured.`
          : `${PROVIDER_LABELS[provider] ?? provider} connected — choose a property below.`,
      );
    } else if (status === "error") {
      setError(searchParams.get("message") || "Authorisation failed.");
    }
  }, [searchParams]);

  const statusOf = (provider: string) =>
    integrations.find((integration) => integration.provider === provider);

  if (loading) {
    return (
      <div className="py-16">
        <Spinner label="Loading integrations…" />
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
        title="Integrations"
        subtitle="Connect the data sources that turn technical severity into business priority."
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
        <GoogleCard
          websiteId={websiteId}
          provider="gsc"
          integration={statusOf("gsc")}
          onChange={load}
          setError={setError}
        />
        <GoogleCard
          websiteId={websiteId}
          provider="ga4"
          integration={statusOf("ga4")}
          onChange={load}
          setError={setError}
        />
        <SemrushCard
          websiteId={websiteId}
          integration={statusOf("semrush")}
          onChange={load}
          setError={setError}
        />
        <GitHubCard
          websiteId={websiteId}
          integration={statusOf("github")}
          onChange={load}
          setError={setError}
        />
      </div>
    </div>
  );
}

function IntegrationHeader({
  title,
  description,
  integration,
}: {
  title: string;
  description: string;
  integration?: IntegrationSummary;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
        <p className="mt-0.5 text-sm text-slate-400">{description}</p>
        {integration?.account_label && (
          <p className="mt-1 text-xs text-slate-500">{integration.account_label}</p>
        )}
        {integration?.last_sync_at && (
          <p className="text-xs text-slate-500">
            Last synced {formatRelative(integration.last_sync_at)}
          </p>
        )}
        {integration?.last_error && (
          <p className="mt-1 text-xs text-rose-300">{integration.last_error}</p>
        )}
      </div>
      <StatusBadge status={integration?.status ?? "not_connected"} />
    </div>
  );
}

function GoogleCard({
  websiteId,
  provider,
  integration,
  onChange,
  setError,
}: {
  websiteId: number;
  provider: "gsc" | "ga4";
  integration?: IntegrationSummary;
  onChange: () => Promise<void>;
  setError: (value: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [properties, setProperties] = useState<{ id: string; label: string }[]>([]);
  const [selected, setSelected] = useState("");

  const connected = integration?.status === "connected" || integration?.status === "syncing";

  const loadProperties = useCallback(async () => {
    if (!connected) return;
    try {
      if (provider === "gsc") {
        const data = await api.integrations.gscProperties(websiteId);
        setProperties(data.properties.map((p) => ({ id: p.site_url, label: p.site_url })));
        setSelected(data.selected ?? "");
      } else {
        const data = await api.integrations.ga4Properties(websiteId);
        setProperties(
          data.properties.map((p) => ({
            id: p.property_id,
            label: `${p.display_name} (${p.account})`,
          })),
        );
        setSelected(data.selected ?? "");
      }
    } catch {
      // Listing properties needs a live token; failing here is not worth an error banner —
      // the connection state above already tells the story.
    }
  }, [connected, provider, websiteId]);

  useEffect(() => {
    void loadProperties();
  }, [loadProperties]);

  const connect = async () => {
    setBusy(true);
    setError("");
    try {
      const { authorization_url } = await api.integrations.authorizeGoogle(websiteId, provider);
      window.location.href = authorization_url;
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "Could not start the Google authorisation flow.",
      );
      setBusy(false);
    }
  };

  const saveProperty = async (value: string) => {
    setBusy(true);
    try {
      if (provider === "gsc") {
        await api.integrations.selectGscProperty(websiteId, value);
      } else {
        await api.integrations.selectGa4Property(websiteId, value);
      }
      setSelected(value);
      await onChange();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not save the property.");
    } finally {
      setBusy(false);
    }
  };

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      await onChange();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : `${label} failed.`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <IntegrationHeader
        title={provider === "gsc" ? "Google Search Console" : "Google Analytics 4"}
        description={
          provider === "gsc"
            ? "Clicks, impressions, CTR, average position and top queries per page."
            : "Users, sessions, engagement, conversions and revenue per page."
        }
        integration={integration}
      />

      {connected && (
        <div className="mt-4">
          <label className="label" htmlFor={`${provider}-property`}>
            {provider === "gsc" ? "Search Console property" : "GA4 property"}
          </label>
          <select
            id={`${provider}-property`}
            className="input max-w-md"
            value={selected}
            disabled={busy || properties.length === 0}
            onChange={(event) => void saveProperty(event.target.value)}
          >
            <option value="">
              {properties.length === 0 ? "No properties available" : "Select a property…"}
            </option>
            {properties.map((property) => (
              <option key={property.id} value={property.id}>
                {property.label}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        {connected ? (
          <>
            <button
              type="button"
              disabled={busy || !selected}
              onClick={() => void run("Sync", () => api.integrations.sync(websiteId, provider))}
              className="btn-secondary"
            >
              Sync now
            </button>
            <button
              type="button"
              disabled={busy || !selected}
              onClick={() =>
                void run("Backfill", () => api.integrations.sync(websiteId, provider, true))
              }
              className="btn-secondary"
            >
              Backfill 90 days
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void run("Disconnect", () => api.integrations.disconnect(websiteId, provider))
              }
              className="btn-ghost"
            >
              Disconnect
            </button>
          </>
        ) : (
          <button type="button" disabled={busy} onClick={() => void connect()} className="btn-primary">
            Connect with Google
          </button>
        )}
      </div>
    </Card>
  );
}

function SemrushCard({
  websiteId,
  integration,
  onChange,
  setError,
}: {
  websiteId: number;
  integration?: IntegrationSummary;
  onChange: () => Promise<void>;
  setError: (value: string) => void;
}) {
  const [apiKey, setApiKey] = useState("");
  const [database, setDatabase] = useState("us");
  const [busy, setBusy] = useState(false);

  const connected = integration?.status === "connected" || integration?.status === "syncing";

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      setApiKey("");
      await onChange();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : `${label} failed.`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <IntegrationHeader
        title="Semrush"
        description="Ranking keywords, striking-distance opportunities and backlinks per page."
        integration={integration}
      />

      {!connected && (
        <div className="mt-4 grid gap-3 sm:grid-cols-[1fr_auto_auto] sm:items-end">
          <div>
            <label className="label" htmlFor="semrush-key">
              API key
            </label>
            <input
              id="semrush-key"
              type="password"
              className="input"
              placeholder="Your Semrush API key"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
            />
          </div>
          <div>
            <label className="label" htmlFor="semrush-db">
              Database
            </label>
            <select
              id="semrush-db"
              className="input"
              value={database}
              onChange={(event) => setDatabase(event.target.value)}
            >
              {["us", "uk", "ca", "au", "de", "fr", "es", "it", "in", "br"].map((code) => (
                <option key={code} value={code}>
                  {code.toUpperCase()}
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            disabled={busy || apiKey.length < 8}
            onClick={() =>
              void run("Connect", () =>
                api.integrations.connectSemrush(websiteId, {
                  api_key: apiKey,
                  database,
                  max_pages: 250,
                }),
              )
            }
            className="btn-primary"
          >
            Connect
          </button>
        </div>
      )}

      {connected && (
        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => void run("Sync", () => api.integrations.sync(websiteId, "semrush"))}
            className="btn-secondary"
          >
            Sync now
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void run("Disconnect", () => api.integrations.disconnect(websiteId, "semrush"))
            }
            className="btn-ghost"
          >
            Disconnect
          </button>
        </div>
      )}

      <p className="mt-3 text-xs text-slate-500">
        The key is verified against Semrush before it is stored, then encrypted at rest. It is never
        returned by the API.
      </p>
    </Card>
  );
}

function GitHubCard({
  websiteId,
  integration,
  onChange,
  setError,
}: {
  websiteId: number;
  integration?: IntegrationSummary;
  onChange: () => Promise<void>;
  setError: (value: string) => void;
}) {
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("main");
  const [secret, setSecret] = useState("");
  const [framework, setFramework] = useState("");
  const [busy, setBusy] = useState(false);
  const [simulation, setSimulation] = useState<string[]>([]);
  const [simulationResult, setSimulationResult] = useState<{
    requires_full_recrawl: boolean;
    reason: string;
    affected_paths: string[];
  } | null>(null);

  const connected = integration?.status === "connected";
  const webhookUrl = `${process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000"}/api/webhooks/github`;

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      setSecret("");
      await onChange();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : `${label} failed.`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <IntegrationHeader
        title="GitHub"
        description="Re-audit automatically whenever the website's code changes."
        integration={integration}
      />

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div>
          <label className="label" htmlFor="gh-repo">
            Repository
          </label>
          <input
            id="gh-repo"
            className="input"
            placeholder="acme/website"
            value={repo}
            onChange={(event) => setRepo(event.target.value)}
          />
        </div>
        <div>
          <label className="label" htmlFor="gh-branch">
            Branch
          </label>
          <input
            id="gh-branch"
            className="input"
            value={branch}
            onChange={(event) => setBranch(event.target.value)}
          />
        </div>
        <div>
          <label className="label" htmlFor="gh-secret">
            Webhook secret
          </label>
          <input
            id="gh-secret"
            type="password"
            className="input"
            placeholder="Shared secret from the GitHub webhook"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
          />
        </div>
        <div>
          <label className="label" htmlFor="gh-framework">
            Framework
          </label>
          <select
            id="gh-framework"
            className="input"
            value={framework}
            onChange={(event) => setFramework(event.target.value)}
          >
            <option value="">Detect automatically</option>
            {["next", "nuxt", "astro", "sveltekit", "remix", "gatsby", "hugo", "jekyll", "static"].map(
              (value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ),
            )}
          </select>
        </div>
      </div>

      <button
        type="button"
        disabled={busy || repo.length < 3 || secret.length < 8}
        onClick={() =>
          void run("Connect", () =>
            api.integrations.connectGithub(websiteId, {
              repo,
              branch,
              webhook_secret: secret,
              framework: framework || null,
            }),
          )
        }
        className="btn-primary mt-4"
      >
        {connected ? "Update connection" : "Connect repository"}
      </button>

      <div className="mt-4 rounded-lg border border-slate-800 bg-slate-950/60 p-3">
        <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
          Webhook payload URL
        </p>
        <code className="mt-1 block break-all text-sm text-sky-300">{webhookUrl}</code>
        <p className="mt-2 text-xs text-slate-500">
          Add this in GitHub under Settings → Webhooks with content type{" "}
          <code>application/json</code>, the same secret, and the <code>push</code> event.
        </p>
      </div>

      {connected && (
        <div className="mt-4 border-t border-slate-800 pt-4">
          <label className="label" htmlFor="gh-simulate">
            Test the file-to-page mapping
          </label>
          <textarea
            id="gh-simulate"
            rows={3}
            className="input font-mono text-xs"
            placeholder={"pages/about.tsx\napp/layout.tsx"}
            onChange={(event) =>
              setSimulation(
                event.target.value
                  .split("\n")
                  .map((line) => line.trim())
                  .filter(Boolean),
              )
            }
          />
          <button
            type="button"
            disabled={busy || simulation.length === 0}
            onClick={() =>
              void (async () => {
                setBusy(true);
                try {
                  setSimulationResult(await api.github.simulate(websiteId, simulation));
                } catch (caught) {
                  setError(
                    caught instanceof ApiError ? caught.message : "Simulation failed.",
                  );
                } finally {
                  setBusy(false);
                }
              })()
            }
            className="btn-secondary mt-2"
          >
            Simulate
          </button>

          {simulationResult && (
            <div className="mt-3 rounded-lg border border-slate-800 p-3 text-sm">
              <p className="text-slate-300">{simulationResult.reason}</p>
              {simulationResult.requires_full_recrawl ? (
                <p className="mt-1 text-amber-300">→ A full re-crawl would run.</p>
              ) : (
                <p className="mt-1 text-emerald-300">
                  → Incremental re-audit of: {simulationResult.affected_paths.join(", ")}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
