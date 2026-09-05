"use client";

/**
 * Integration connection screen.
 *
 * Secrets go in and never come back out: the API returns status and non-sensitive configuration
 * only, so this screen shows connection state rather than the values behind it.
 */

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import { Card, ErrorNote, PageHeader, Spinner, StatusBadge } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { PROVIDER_LABELS, formatDate, formatRelative } from "@/lib/format";
import type { GitHubEventItem, IntegrationSummary } from "@/lib/types";

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
  const [showServiceAccount, setShowServiceAccount] = useState(false);
  const [serviceAccountKey, setServiceAccountKey] = useState("");

  const connected = integration?.status === "connected" || integration?.status === "syncing";
  const usingServiceAccount = integration?.account_label?.endsWith(
    ".iam.gserviceaccount.com",
  );

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
          <button
            type="button"
            disabled={busy}
            onClick={() => void connect()}
            className="btn-primary"
          >
            Connect with Google OAuth
          </button>
        )}
      </div>

      {connected && usingServiceAccount && (
        <p className="mt-3 text-xs text-slate-500">
          Authenticated with a service account - no token expiry, no periodic reconsent.
        </p>
      )}

      {!connected && (
        <div className="mt-4 border-t border-slate-800/80 pt-4">
          <label className="label font-medium text-slate-200" htmlFor={`${provider}-sa`}>
            🔑 Or Connect via Service Account JSON key (Recommended - No Expiry)
          </label>
          <textarea
            id={`${provider}-sa`}
            rows={4}
            className="input font-mono text-xs mt-1.5 w-full"
            placeholder='{"type": "service_account", "project_id": "...", ...}'
            value={serviceAccountKey}
            onChange={(event) => setServiceAccountKey(event.target.value)}
          />
          <p className="mt-1.5 text-xs text-slate-400">
            Paste the whole key file from Google Cloud. Grants 24/7 background access with no 7-day token expiry.
          </p>
          <button
            type="button"
            disabled={busy || serviceAccountKey.trim().length < 50}
            onClick={() =>
              void run("Connect", async () => {
                await api.integrations.connectServiceAccount(websiteId, provider, {
                  key: serviceAccountKey,
                });
                setServiceAccountKey("");
              })
            }
            className="btn-secondary mt-2.5"
          >
            Connect Service Account
          </button>
        </div>
      )}
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
  const [editing, setEditing] = useState(false);
  const [copied, setCopied] = useState(false);
  const [events, setEvents] = useState<GitHubEventItem[]>([]);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [simulation, setSimulation] = useState<string[]>([]);
  const [simulationResult, setSimulationResult] = useState<{
    requires_full_recrawl: boolean;
    reason: string;
    affected_paths: string[];
  } | null>(null);

  const connected = integration?.status === "connected";
  const configuredRepo =
    (integration?.config?.repo as string) || integration?.account_label || "";
  const configuredBranch = (integration?.config?.branch as string) || "main";

  useEffect(() => {
    if (configuredRepo) {
      setRepo(configuredRepo);
    }
    if (configuredBranch) {
      setBranch(configuredBranch);
    }
  }, [configuredRepo, configuredBranch]);

  const loadEvents = useCallback(async () => {
    setLoadingEvents(true);
    try {
      const res = await api.github.events(websiteId, 25);
      setEvents(res.items ?? []);
    } catch {
      // Non-critical background fetch
    } finally {
      setLoadingEvents(false);
    }
  }, [websiteId]);

  useEffect(() => {
    void loadEvents();
  }, [loadEvents]);

  const webhookUrl = `${process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000"}/api/webhooks/github`;

  const copyWebhookUrl = () => {
    navigator.clipboard.writeText(webhookUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 2500);
  };

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      setSecret("");
      setEditing(false);
      await onChange();
      await loadEvents();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : `${label} failed.`);
    } finally {
      setBusy(false);
    }
  };

  const latestPush = events.find((e) => e.event_type === "push");
  const latestEvent = events[0];

  return (
    <Card>
      <IntegrationHeader
        title="GitHub"
        description="Re-audit automatically whenever the website's code changes via push or PR."
        integration={integration}
      />

      {/* Connected State Overview */}
      {connected && (
        <div className="mt-4 rounded-xl border border-emerald-500/30 bg-emerald-950/20 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2.5">
              <span className="relative flex h-3 w-3">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
              </span>
              <span className="font-semibold text-sm text-emerald-300">
                Connected & Actively Listening
              </span>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setEditing(!editing)}
                className="btn-secondary text-xs"
              >
                {editing ? "Cancel Editing" : "Edit Configuration"}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  void run("Disconnect", () =>
                    api.integrations.disconnect(websiteId, "github"),
                  )
                }
                className="btn-ghost text-xs text-rose-400 hover:text-rose-300 hover:bg-rose-500/10"
              >
                Disconnect
              </button>
            </div>
          </div>

          <div className="mt-3.5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 border-t border-emerald-500/20 pt-3 text-xs">
            <div>
              <span className="text-slate-400 block font-medium">Repository</span>
              {configuredRepo ? (
                <a
                  href={`https://github.com/${configuredRepo}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="font-mono text-sky-400 hover:underline font-semibold mt-0.5 block truncate"
                >
                  {configuredRepo} ↗
                </a>
              ) : (
                <span className="text-slate-300 font-mono">Not set</span>
              )}
            </div>

            <div>
              <span className="text-slate-400 block font-medium">Monitored Branch</span>
              <span className="mt-0.5 inline-block font-mono text-slate-200 bg-slate-900 px-2 py-0.5 rounded border border-slate-800">
                {configuredBranch}
              </span>
            </div>

            <div>
              <span className="text-slate-400 block font-medium">Webhook Secret</span>
              <span className="mt-0.5 text-emerald-400 font-semibold block">
                ●●●●●●●● (HMAC-SHA256 active)
              </span>
            </div>

            <div>
              <span className="text-slate-400 block font-medium">Connected Since</span>
              <span className="mt-0.5 text-slate-300 block">
                {integration?.connected_at
                  ? formatDate(integration.connected_at)
                  : "Active"}
              </span>
            </div>
          </div>
        </div>
      )}

      {/* Setup / Configuration Form (shown when not connected or editing) */}
      {(!connected || editing) && (
        <div className="mt-4 rounded-lg border border-slate-800 bg-slate-900/30 p-4">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-3">
            {connected ? "Update GitHub Webhook Settings" : "Configure GitHub Repository"}
          </h3>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="gh-repo">
                Repository (owner/repo)
              </label>
              <input
                id="gh-repo"
                className="input"
                placeholder="keshukumar-sys/namandarshan"
                value={repo}
                onChange={(event) => setRepo(event.target.value)}
              />
            </div>
            <div>
              <label className="label" htmlFor="gh-branch">
                Tracked Branch
              </label>
              <input
                id="gh-branch"
                className="input"
                placeholder="main"
                value={branch}
                onChange={(event) => setBranch(event.target.value)}
              />
            </div>
            <div>
              <label className="label" htmlFor="gh-secret">
                Webhook Secret
              </label>
              <input
                id="gh-secret"
                type="password"
                className="input"
                placeholder={
                  connected
                    ? "Leave blank to keep existing secret, or enter new secret"
                    : "Shared secret from the GitHub webhook"
                }
                value={secret}
                onChange={(event) => setSecret(event.target.value)}
              />
            </div>
            <div>
              <label className="label" htmlFor="gh-framework">
                Framework Mapping
              </label>
              <select
                id="gh-framework"
                className="input"
                value={framework}
                onChange={(event) => setFramework(event.target.value)}
              >
                <option value="">Detect automatically</option>
                {[
                  "next",
                  "nuxt",
                  "astro",
                  "sveltekit",
                  "remix",
                  "gatsby",
                  "hugo",
                  "jekyll",
                  "static",
                ].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="mt-4 flex items-center gap-2">
            <button
              type="button"
              disabled={busy || repo.length < 3 || (!connected && secret.length < 4)}
              onClick={() =>
                void run("Connect", () =>
                  api.integrations.connectGithub(websiteId, {
                    repo,
                    branch,
                    webhook_secret: secret || undefined,
                    framework: framework || null,
                  }),
                )
              }
              className="btn-primary"
            >
              {busy
                ? "Saving…"
                : connected
                ? "Update Connection"
                : "Save & Connect Repository"}
            </button>
            {editing && (
              <button
                type="button"
                onClick={() => setEditing(false)}
                className="btn-secondary"
              >
                Cancel
              </button>
            )}
          </div>
        </div>
      )}

      {/* Webhook Payload URL Box with 1-Click Copy */}
      <div className="mt-4 rounded-lg border border-slate-800 bg-slate-950/60 p-3.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-400">
            GitHub Webhook Payload URL
          </p>
          <button
            type="button"
            onClick={copyWebhookUrl}
            className="chip bg-sky-500/15 text-sky-300 ring-sky-500/30 hover:bg-sky-500/25 text-xs transition-colors cursor-pointer"
          >
            {copied ? "✓ Copied to clipboard" : "📋 Copy Payload URL"}
          </button>
        </div>
        <code className="mt-1.5 block break-all text-sm font-mono text-sky-300 bg-slate-900/80 px-3 py-2 rounded border border-slate-800">
          {webhookUrl}
        </code>
        <p className="mt-2 text-xs text-slate-400 leading-relaxed">
          In GitHub repository <strong>Settings → Webhooks</strong>: paste this Payload URL, select content type{" "}
          <code className="text-slate-200">application/json</code>, enter your Webhook Secret, and choose the{" "}
          <code className="text-slate-200">Just the push event</code> (or pull requests) trigger.
        </p>
        <p className="mt-1 text-xs text-slate-500">
          💡 <em>Testing on localhost?</em> If you are using ngrok, replace <code className="text-slate-400">127.0.0.1:8000</code> with your public ngrok URL (e.g. <code className="text-slate-400">https://conjoined-mothproof-secret.ngrok-free.dev/api/webhooks/github</code>).
        </p>
      </div>

      {/* Section: Webhook Activity & Latest Push Details */}
      <div className="mt-6 border-t border-slate-800 pt-5">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
          <div>
            <h3 className="text-sm font-semibold text-slate-200">
              Webhook Activity & Code Push History
            </h3>
            <p className="text-xs text-slate-400">
              Real-time audit log of inbound GitHub deliveries and automated SEO re-audits.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void loadEvents()}
            disabled={loadingEvents}
            className="btn-secondary text-xs"
          >
            {loadingEvents ? "Refreshing…" : "🔄 Refresh Activity"}
          </button>
        </div>

        {events.length === 0 ? (
          <div className="rounded-lg border border-dashed border-slate-800 p-6 text-center">
            <p className="text-sm text-slate-300 font-medium">No webhook deliveries received yet</p>
            <p className="mt-1 text-xs text-slate-500">
              Once you configure the webhook in GitHub, GitHub will send a test ping followed by push events whenever code is committed.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {/* Spotlight on Latest Push Event */}
            {latestPush ? (
              <div className="rounded-xl border border-sky-500/30 bg-sky-950/20 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-sky-500/20 pb-2.5">
                  <div className="flex items-center gap-2">
                    <span className="chip bg-sky-500/20 text-sky-300 ring-sky-500/40 text-xs font-bold uppercase">
                      Latest Push
                    </span>
                    <span className="text-xs font-semibold text-slate-200 font-mono">
                      {latestPush.branch || "main"}
                    </span>
                    {latestPush.after_sha && (
                      <span className="text-xs font-mono text-slate-400 bg-slate-900 px-2 py-0.5 rounded border border-slate-800">
                        SHA: {latestPush.after_sha}
                      </span>
                    )}
                  </div>
                  <span className="text-xs text-slate-400">
                    Pushed {formatRelative(latestPush.created_at)}
                    {latestPush.pusher && ` by ${latestPush.pusher}`}
                  </span>
                </div>

                <div className="mt-3 space-y-2 text-xs">
                  {latestPush.commit_messages && latestPush.commit_messages.length > 0 && (
                    <div>
                      <span className="text-slate-400 font-medium block">Commit Message:</span>
                      <div className="mt-1 space-y-1">
                        {latestPush.commit_messages.map((msg, idx) => (
                          <p
                            key={idx}
                            className="rounded bg-slate-900/80 p-2 font-mono text-slate-200 border border-slate-800"
                          >
                            {msg}
                          </p>
                        ))}
                      </div>
                    </div>
                  )}

                  <div className="grid gap-2 sm:grid-cols-2 pt-1">
                    <div>
                      <span className="text-slate-400 font-medium block">
                        Changed Files ({latestPush.changed_file_count}):
                      </span>
                      {latestPush.changed_files && latestPush.changed_files.length > 0 ? (
                        <div className="mt-1.5 flex flex-wrap gap-1.5">
                          {latestPush.changed_files.map((file, idx) => (
                            <span
                              key={idx}
                              className="chip bg-slate-800 text-slate-300 font-mono text-xs truncate max-w-xs"
                            >
                              {file}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <span className="text-slate-500 italic">No files recorded</span>
                      )}
                    </div>

                    <div>
                      <span className="text-slate-400 font-medium block">
                        SEO Affected Pages ({latestPush.affected_urls?.length ?? 0}):
                      </span>
                      {latestPush.affected_urls && latestPush.affected_urls.length > 0 ? (
                        <div className="mt-1.5 flex flex-wrap gap-1.5">
                          {latestPush.affected_urls.map((url, idx) => (
                            <span
                              key={idx}
                              className="chip bg-indigo-500/15 text-indigo-300 ring-indigo-500/30 font-mono text-xs"
                            >
                              {url}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <span className="text-slate-500 italic">None or full re-crawl triggered</span>
                      )}
                    </div>
                  </div>

                  <div className="mt-2.5 rounded bg-slate-900/60 p-2.5 border border-slate-800 flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <span className="text-slate-400">Action Triggered:</span>
                      <span
                        className={`chip text-xs font-semibold ${
                          latestPush.action_taken === "incremental_crawl"
                            ? "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30"
                            : latestPush.action_taken === "full_crawl"
                            ? "bg-amber-500/15 text-amber-300 ring-amber-500/30"
                            : "bg-slate-500/15 text-slate-300 ring-slate-500/30"
                        }`}
                      >
                        {latestPush.action_taken ?? "Processed"}
                      </span>
                    </div>
                    {latestPush.action_reason && (
                      <span className="text-slate-300 text-xs">
                        {latestPush.action_reason}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            ) : latestEvent?.event_type === "ping" ? (
              /* Ping Handshake Spotlight */
              <div className="rounded-xl border border-emerald-500/30 bg-emerald-950/20 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-emerald-500/20 pb-2.5">
                  <div className="flex items-center gap-2">
                    <span className="chip bg-emerald-500/20 text-emerald-300 ring-emerald-500/40 text-xs font-bold uppercase">
                      ✓ Ping Handshake Verified
                    </span>
                    <span className="text-xs font-semibold text-slate-200 font-mono">
                      {latestEvent.repository}
                    </span>
                  </div>
                  <span className="text-xs text-slate-400">
                    Received {formatRelative(latestEvent.created_at)}
                  </span>
                </div>
                <div className="mt-3 text-xs space-y-1.5">
                  <p className="text-slate-300">
                    <strong className="text-emerald-300">Delivery ID:</strong>{" "}
                    <code className="font-mono text-slate-200">{latestEvent.delivery_id}</code>
                  </p>
                  <p className="text-slate-300">
                    <strong className="text-emerald-300">Status:</strong>{" "}
                    <span>Accepted & Validated (HTTP 202)</span>
                  </p>
                  <div className="mt-2.5 rounded-lg border border-emerald-500/20 bg-emerald-900/10 p-3 text-slate-300 leading-relaxed">
                    🎉 <strong>Webhook handshake verified successfully!</strong> The server is actively connected to your GitHub repository.
                    Whenever you run <code className="bg-slate-900 px-1.5 py-0.5 rounded text-sky-300">git push</code> on GitHub, the commit diff, changed files, affected SEO URLs, and automatic re-audit execution will appear right here!
                  </div>
                </div>
              </div>
            ) : null}

            {/* Full Deliveries Table/List */}
            <div>
              <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400 mb-2">
                All Inbound Deliveries ({events.length})
              </h4>
              <div className="divide-y divide-slate-800 rounded-lg border border-slate-800 bg-slate-950/40">
                {events.map((event) => (
                  <div key={event.id} className="p-3 text-xs flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className={`chip text-xs font-semibold uppercase ${
                          event.event_type === "push"
                            ? "bg-sky-500/15 text-sky-300 ring-sky-500/30"
                            : event.event_type === "ping"
                            ? "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30"
                            : "bg-slate-500/15 text-slate-300 ring-slate-500/30"
                        }`}
                      >
                        {event.event_type}
                      </span>
                      <span className="font-mono text-slate-300 truncate">
                        {event.delivery_id}
                      </span>
                      {event.pusher && (
                        <span className="text-slate-400">by @{event.pusher}</span>
                      )}
                    </div>

                    <div className="flex items-center gap-3 text-slate-400 shrink-0">
                      {event.action_taken && (
                        <span className="chip bg-slate-900 text-slate-300 text-xs border border-slate-800">
                          {event.action_taken}
                        </span>
                      )}
                      <span>{formatRelative(event.created_at)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* File Mapping Simulator */}
      {connected && (
        <div className="mt-6 border-t border-slate-800 pt-5">
          <label className="label" htmlFor="gh-simulate">
            Test the file-to-page mapping simulator
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
                  setSimulationResult(
                    await api.github.simulate(websiteId, simulation),
                  );
                } catch (caught) {
                  setError(
                    caught instanceof ApiError
                      ? caught.message
                      : "Simulation failed.",
                  );
                } finally {
                  setBusy(false);
                }
              })()
            }
            className="btn-secondary mt-2"
          >
            Simulate Mapping
          </button>

          {simulationResult && (
            <div className="mt-3 rounded-lg border border-slate-800 p-3 text-sm">
              <p className="text-slate-300">{simulationResult.reason}</p>
              {simulationResult.requires_full_recrawl ? (
                <p className="mt-1 text-amber-300">→ A full re-crawl would run.</p>
              ) : (
                <p className="mt-1 text-emerald-300">
                  → Incremental re-audit of:{" "}
                  {simulationResult.affected_paths.join(", ")}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
