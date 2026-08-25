/** Display formatting helpers. */

export function formatNumber(value: number | null | undefined, fractionDigits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: 0,
  }).format(value);
}

export function formatCurrency(value: number | null | undefined): string {
  if (!value) return "—";
  // Compact notation keeps a revenue column readable next to click counts.
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    notation: value >= 100000 ? "compact" : "standard",
    maximumFractionDigits: value >= 100000 ? 1 : 0,
  }).format(value);
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(1);
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "Never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "Never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";

  const seconds = Math.round((Date.now() - parsed.getTime()) / 1000);
  if (seconds < 60) return "just now";

  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["minute", 60],
    ["hour", 3600],
    ["day", 86400],
    ["week", 604800],
    ["month", 2592000],
    ["year", 31536000],
  ];

  let chosen: [Intl.RelativeTimeFormatUnit, number] = units[0];
  for (const unit of units) {
    if (seconds >= unit[1]) chosen = unit;
  }
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  return formatter.format(-Math.round(seconds / chosen[1]), chosen[0]);
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return minutes < 60 ? `${minutes}m ${remainder}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function truncate(value: string | null | undefined, length = 60): string {
  if (!value) return "—";
  return value.length <= length ? value : `${value.slice(0, length - 1)}…`;
}

/** Strip the origin so a table shows `/blog/post` rather than the full absolute URL. */
export function displayPath(url: string): string {
  try {
    const parsed = new URL(url);
    return `${parsed.pathname}${parsed.search}` || "/";
  } catch {
    return url;
  }
}

export function scoreTone(score: number | null | undefined): "good" | "warn" | "bad" | "none" {
  if (score === null || score === undefined) return "none";
  if (score > 90) return "good";
  if (score >= 75) return "warn";
  return "bad";
}

export function severityTone(severity: string | null | undefined): string {
  switch ((severity ?? "").toUpperCase()) {
    case "CRITICAL":
      return "bg-rose-500/15 text-rose-300 ring-rose-500/30";
    case "HIGH":
      return "bg-orange-500/15 text-orange-300 ring-orange-500/30";
    case "MEDIUM":
      return "bg-amber-500/15 text-amber-300 ring-amber-500/30";
    case "LOW":
      return "bg-sky-500/15 text-sky-300 ring-sky-500/30";
    default:
      return "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30";
  }
}

export function bandTone(band: string | null | undefined): string {
  switch (band) {
    case "P0":
      return "bg-rose-500/15 text-rose-300 ring-rose-500/30";
    case "P1":
      return "bg-orange-500/15 text-orange-300 ring-orange-500/30";
    case "P2":
      return "bg-amber-500/15 text-amber-300 ring-amber-500/30";
    default:
      return "bg-slate-500/15 text-slate-300 ring-slate-500/30";
  }
}

export function statusTone(status: string | null | undefined): string {
  switch ((status ?? "").toLowerCase()) {
    case "connected":
    case "completed":
    case "success":
      return "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30";
    case "syncing":
    case "running":
    case "queued":
      return "bg-sky-500/15 text-sky-300 ring-sky-500/30";
    case "error":
    case "failed":
    case "expired":
      return "bg-rose-500/15 text-rose-300 ring-rose-500/30";
    default:
      return "bg-slate-500/15 text-slate-400 ring-slate-500/30";
  }
}

export const PROVIDER_LABELS: Record<string, string> = {
  gsc: "Search Console",
  ga4: "Analytics 4",
  semrush: "Semrush",
  github: "GitHub",
};

export const COMPONENT_LABELS: Record<string, string> = {
  seo_severity: "SEO severity",
  ga4_activity: "User activity",
  gsc_search: "Search performance",
  semrush_opportunity: "Keyword opportunity",
};
