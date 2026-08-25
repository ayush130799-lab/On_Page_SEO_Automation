"use client";

/** Platform-wide settings: the rule catalogue, model provider status and global defaults. */

import { useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import { Card, ErrorNote, PageHeader, SeverityBadge, Spinner } from "@/components/ui";
import { ApiError, api } from "@/lib/api";

interface Rule {
  id: string;
  check_type: string;
  category: string;
  title: string;
  weight: number;
  description?: string;
  fix_hint?: string;
  site_wide?: boolean;
}

export default function SettingsPage() {
  return (
    <AuthGate>
      <PlatformSettings />
    </AuthGate>
  );
}

function PlatformSettings() {
  const [rules, setRules] = useState<Rule[]>([]);
  const [providers, setProviders] = useState<{
    enabled: boolean;
    active: string;
    configured: string[];
    max_pages_per_run: number;
    seo_score_threshold: number;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const [ruleList, providerData] = await Promise.all([api.seo.rules(), api.ai.providers()]);
      setRules(ruleList as Rule[]);
      setProviders(providerData);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load settings.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <div className="py-16">
        <Spinner label="Loading settings…" />
      </div>
    );
  }

  const byCategory = rules.reduce<Record<string, Rule[]>>((groups, rule) => {
    (groups[rule.category] ??= []).push(rule);
    return groups;
  }, {});

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader
        title="Platform settings"
        subtitle="How the audit engine and AI stage are configured across every website."
      />

      {error && (
        <div className="mb-4">
          <ErrorNote error={error} onRetry={load} />
        </div>
      )}

      <div className="space-y-4">
        {providers && (
          <Card title="AI provider">
            <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
              <Row label="Status" value={providers.enabled ? "Enabled" : "Disabled"} />
              <Row label="Active provider" value={providers.active} />
              <Row
                label="Configured providers"
                value={providers.configured.join(", ") || "none — set an API key"}
              />
              <Row label="Pages per run" value={String(providers.max_pages_per_run)} />
              <Row
                label="Skip threshold"
                value={`SEO score above ${providers.seo_score_threshold}`}
              />
            </dl>
            <p className="mt-3 border-t border-slate-800 pt-3 text-xs text-slate-500">
              A page is analysed when it ranks inside the top {providers.max_pages_per_run} by
              business priority <em>and</em> scores below {providers.seo_score_threshold} — or
              carries a CRITICAL issue at any score. Unchanged pages reuse their previous result.
            </p>
          </Card>
        )}

        <Card
          title={`SEO rules (${rules.length})`}
          action={<span className="text-xs text-slate-500">Weights feed the health score</span>}
        >
          <div className="space-y-5">
            {Object.entries(byCategory).map(([category, categoryRules]) => (
              <div key={category}>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
                  {category.replace(/_/g, " ")}
                </h3>
                <ul className="space-y-1.5">
                  {categoryRules.map((rule) => (
                    <li
                      key={rule.id}
                      className="flex flex-wrap items-baseline justify-between gap-2 rounded-lg border border-slate-800 px-3 py-2"
                    >
                      <span className="min-w-0">
                        <span className="text-sm text-slate-200">{rule.title}</span>
                        <code className="ml-2 text-xs text-slate-500">{rule.id}</code>
                        {rule.site_wide && (
                          <span className="chip ml-2 bg-sky-500/15 text-sky-300 ring-sky-500/30">
                            site-wide
                          </span>
                        )}
                        {rule.fix_hint && (
                          <span className="mt-0.5 block text-xs text-slate-500">
                            {rule.fix_hint}
                          </span>
                        )}
                      </span>
                      <span className="tnum shrink-0 text-xs text-slate-400">
                        weight {rule.weight}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </Card>

        <Card title="Priority weights">
          <p className="text-sm text-slate-400">
            Weights are configured per website so a brochure site and an ecommerce site can be
            ranked on different signals. Open a website&apos;s settings to adjust them, with a live
            preview of the resulting ranking.
          </p>
          <dl className="mt-4 grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <Row label="SEO severity" value="40% (default)" />
            <Row label="User activity (GA4)" value="30% (default)" />
            <Row label="Search performance (GSC)" value="20% (default)" />
            <Row label="Keyword opportunity (Semrush)" value="10% (default)" />
          </dl>
        </Card>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-slate-300">{value}</dd>
    </div>
  );
}
