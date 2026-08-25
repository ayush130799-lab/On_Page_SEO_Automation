"use client";

/** Small presentational primitives shared across every screen. */

import Link from "next/link";
import type { ReactNode } from "react";

import { bandTone, formatScore, scoreTone, severityTone, statusTone } from "@/lib/format";

// ── Layout ─────────────────────────────────────────────────────────────────

export function Card({
  title,
  action,
  children,
  className = "",
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <header className="flex items-center justify-between gap-3 border-b border-slate-800 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-200">{title}</h2>
          {action}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "default" | "good" | "warn" | "bad";
}) {
  const toneClass = {
    default: "text-slate-100",
    good: "text-emerald-400",
    warn: "text-amber-400",
    bad: "text-rose-400",
  }[tone];

  return (
    <div className="card px-4 py-3">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`tnum mt-1 text-2xl font-semibold ${toneClass}`}>{value}</div>
      {hint && <div className="mt-0.5 text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
  breadcrumb,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  breadcrumb?: { href: string; label: string }[];
}) {
  return (
    <header className="mb-6">
      {breadcrumb && breadcrumb.length > 0 && (
        <nav className="mb-2 flex items-center gap-1.5 text-xs text-slate-500">
          {breadcrumb.map((crumb, index) => (
            <span key={crumb.href} className="flex items-center gap-1.5">
              {index > 0 && <span aria-hidden>/</span>}
              <Link href={crumb.href} className="hover:text-slate-300">
                {crumb.label}
              </Link>
            </span>
          ))}
        </nav>
      )}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">{title}</h1>
          {subtitle && <p className="mt-1 text-sm text-slate-400">{subtitle}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </header>
  );
}

// ── Badges ─────────────────────────────────────────────────────────────────

export function ScoreBadge({ score }: { score: number | null | undefined }) {
  const tone = scoreTone(score);
  const classes = {
    good: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
    warn: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
    bad: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
    none: "bg-slate-500/15 text-slate-400 ring-slate-500/30",
  }[tone];

  return <span className={`chip tnum ${classes}`}>{formatScore(score)}</span>;
}

export function SeverityBadge({ severity }: { severity: string | null | undefined }) {
  if (!severity || severity === "NONE") {
    return <span className="chip bg-emerald-500/15 text-emerald-300 ring-emerald-500/30">None</span>;
  }
  return <span className={`chip ${severityTone(severity)}`}>{severity}</span>;
}

export function BandBadge({ band }: { band: string | null | undefined }) {
  if (!band) return <span className="text-slate-600">—</span>;
  return <span className={`chip ${bandTone(band)}`}>{band}</span>;
}

const STATUS_LABELS: Record<string, string> = {
  not_connected: "Not connected",
  connected: "Connected",
  syncing: "Syncing",
  error: "Error",
  expired: "Reauthorise",
};

export function StatusBadge({ status, label }: { status: string | null; label?: string }) {
  const text =
    label ?? STATUS_LABELS[status ?? ""] ?? (status ? status.replace(/_/g, " ") : "unknown");
  return <span className={`chip ${statusTone(status)}`}>{text}</span>;
}

export function AiBadge({ status }: { status: string }) {
  const label: Record<string, string> = {
    completed: "Analysed",
    cached: "Cached",
    skipped: "Skipped",
    failed: "Failed",
    queued: "Queued",
    pending: "Pending",
  };
  const tone: Record<string, string> = {
    completed: "bg-violet-500/15 text-violet-300 ring-violet-500/30",
    cached: "bg-violet-500/10 text-violet-400 ring-violet-500/20",
    failed: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
    queued: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
  };
  return (
    <span className={`chip ${tone[status] ?? "bg-slate-500/15 text-slate-400 ring-slate-500/30"}`}>
      {label[status] ?? status}
    </span>
  );
}

// ── Feedback ───────────────────────────────────────────────────────────────

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-slate-400">
      <span
        className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-600 border-t-sky-400"
        aria-hidden
      />
      {label ?? "Loading…"}
    </div>
  );
}

export function ErrorNote({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
      <div className="flex items-start justify-between gap-3">
        <span>{error}</span>
        {onRetry && (
          <button type="button" onClick={onRetry} className="shrink-0 underline hover:no-underline">
            Retry
          </button>
        )}
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-dashed border-slate-700 px-6 py-10 text-center">
      <p className="text-sm font-medium text-slate-300">{title}</p>
      {description && <p className="mx-auto mt-1 max-w-md text-sm text-slate-500">{description}</p>}
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  );
}

export function ProgressBar({ value, label }: { value: number; label?: string }) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
        <div
          className="h-full rounded-full bg-sky-500 transition-all duration-500"
          style={{ width: `${clamped}%` }}
        />
      </div>
      {label && <p className="mt-1 text-xs text-slate-500">{label}</p>}
    </div>
  );
}

// ── Charts ─────────────────────────────────────────────────────────────────

/**
 * A dependency-free line chart.
 *
 * A charting library would add hundreds of kilobytes for what is, here, a polyline over a
 * normalised series — so the SVG is drawn directly.
 */
export function Sparkline({
  points,
  height = 56,
  color = "rgb(56 189 248)",
  label,
}: {
  points: number[];
  height?: number;
  color?: string;
  label?: string;
}) {
  const usable = points.filter((p) => Number.isFinite(p));
  if (usable.length < 2) {
    return (
      <div className="flex h-14 items-center justify-center text-xs text-slate-600">
        Not enough data yet
      </div>
    );
  }

  const width = 300;
  const max = Math.max(...usable);
  const min = Math.min(...usable);
  const range = max - min || 1;

  const coords = usable.map((value, index) => {
    const x = (index / (usable.length - 1)) * width;
    const y = height - ((value - min) / range) * (height - 6) - 3;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  return (
    <figure>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="h-14 w-full"
        role="img"
        aria-label={label ?? "Trend"}
      >
        <polyline
          points={coords.join(" ")}
          fill="none"
          stroke={color}
          strokeWidth="2"
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      {label && <figcaption className="mt-1 text-xs text-slate-500">{label}</figcaption>}
    </figure>
  );
}

/** Horizontal proportion bar used for score and priority distributions. */
export function DistributionBar({
  segments,
}: {
  segments: { label: string; value: number; className: string }[];
}) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);
  if (total === 0) {
    return <p className="text-xs text-slate-600">No data yet</p>;
  }

  return (
    <div>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-800">
        {segments
          .filter((segment) => segment.value > 0)
          .map((segment) => (
            <div
              key={segment.label}
              className={segment.className}
              style={{ width: `${(segment.value / total) * 100}%` }}
              title={`${segment.label}: ${segment.value}`}
            />
          ))}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
        {segments
          .filter((segment) => segment.value > 0)
          .map((segment) => (
            <li key={segment.label} className="flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-sm ${segment.className}`} aria-hidden />
              {segment.label}
              <span className="tnum text-slate-500">{segment.value}</span>
            </li>
          ))}
      </ul>
    </div>
  );
}
