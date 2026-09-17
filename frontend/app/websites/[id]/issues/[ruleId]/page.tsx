"use client";

/**
 * Affected-pages drilldown for one SEO issue ("Most common issues" → click a row).
 *
 * Reuses the same ``PriorityPagesTable`` the website dashboard uses for its "Priority pages"
 * table, just scoped to pages carrying this issue's ``rule_id`` — no separate table/UI, no
 * duplicated page data, and every URL still opens the existing page-details screen.
 */

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import { PriorityPagesTable } from "@/components/PriorityPagesTable";
import { ErrorNote, PageHeader, SeverityBadge, Spinner } from "@/components/ui";
import { ApiError, api } from "@/lib/api";

export default function IssuePagesRoute() {
  return (
    <AuthGate>
      <IssuePages />
    </AuthGate>
  );
}

/** "missing_meta_description" -> "Missing Meta Description" — only used until the authoritative
 *  title loads (or as a last resort if the rule no longer exists in stored issue data). */
function titleCaseFromRuleId(ruleId: string): string {
  return ruleId
    .replace(/[_-]+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function IssuePages() {
  const params = useParams<{ id: string; ruleId: string }>();
  const websiteId = Number(params.id);
  const ruleId = decodeURIComponent(params.ruleId);

  const searchParams = useSearchParams();
  const queryTitle = searchParams.get("title");
  const querySeverity = searchParams.get("severity");

  // Seeded from the query string (instant header, no flash) when the row was clicked from the
  // dashboard, then reconciled against the backend's own issue data below — never treated as
  // authoritative on its own, since a direct link/refresh carries no query string at all.
  const [title, setTitle] = useState(queryTitle || titleCaseFromRuleId(ruleId));
  const [severity, setSeverity] = useState<string | null>(querySeverity);
  const [loadingMeta, setLoadingMeta] = useState(!queryTitle);
  const [metaError, setMetaError] = useState("");

  const loadIssueMeta = useCallback(async () => {
    setLoadingMeta(true);
    try {
      // One representative unresolved issue row for this rule — the real title/severity/
      // description exactly as the backend stores them, not a hardcoded label.
      const result = await api.pages.issues(websiteId, { rule_id: ruleId, limit: 1 });
      const first = result.items[0] as
        | { title?: string; severity?: string }
        | undefined;
      if (first?.title) setTitle(first.title);
      if (first?.severity) setSeverity(first.severity);
      setMetaError("");
    } catch (caught) {
      // Non-fatal: the page still works from the rule_id alone (see PriorityPagesTable below),
      // this only affects the header's title/severity chip.
      setMetaError(caught instanceof ApiError ? caught.message : "Could not load issue details.");
    } finally {
      setLoadingMeta(false);
    }
  }, [websiteId, ruleId]);

  useEffect(() => {
    void loadIssueMeta();
  }, [loadIssueMeta]);

  return (
    <>
      <PageHeader
        breadcrumb={[
          { href: "/", label: "Portfolio" },
          { href: `/websites/${websiteId}`, label: "Website" },
        ]}
        title={
          <span className="flex flex-wrap items-center gap-2">
            {severity && <SeverityBadge severity={severity} />}
            <span>{title}</span>
          </span>
        }
        subtitle="Every page currently carrying this issue, using the same priority table as the website dashboard."
        actions={
          <Link href={`/websites/${websiteId}`} className="btn-secondary">
            ← Back to dashboard
          </Link>
        }
      />

      {loadingMeta && !queryTitle && (
        <div className="mb-4">
          <Spinner label="Loading issue details…" />
        </div>
      )}

      {metaError && (
        <div className="mb-4">
          <ErrorNote error={metaError} onRetry={() => void loadIssueMeta()} />
        </div>
      )}

      <PriorityPagesTable websiteId={websiteId} ruleId={ruleId} title="Affected pages" />
    </>
  );
}
