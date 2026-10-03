export function fmtDate(iso?: string | null): string {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function fmtRelative(iso?: string | null): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function fmtMs(ms?: number | null): string {
  if (ms === null || ms === undefined) return "–";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
}

export function fmtUsd(v?: number | null, digits = 4): string {
  if (v === null || v === undefined) return "–";
  if (v === 0) return "$0";
  if (Math.abs(v) < 0.0001) return `$${Number(v.toPrecision(2)).toFixed(8).replace(/0+$/, "")}`;
  return `$${v.toFixed(v >= 1 ? 2 : digits)}`;
}

export function fmtNum(v?: number | null, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return v.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function fmtSigned(v?: number | null, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const s = v.toFixed(digits);
  return v > 0 ? `+${s}` : s.replace("-", "−");
}

export function fmtPct(v?: number | null, digits = 0): string {
  if (v === null || v === undefined) return "–";
  return `${(v * 100).toFixed(digits)}%`;
}

export function shortId(id?: string | null): string {
  return id ? id.slice(0, 8) : "–";
}

export function sentence(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/**
 * Report warnings arrive as "JUDGE NOT CALIBRATED (poor): reason" so they read well in plain-text
 * PR comments. In the UI the shouted prefix becomes a sentence-case title.
 */
export function splitWarning(w: string): { title: string | null; body: string; severe: boolean } {
  const m = /^([A-Z][A-Z ]*[A-Z])(?: \(([^)]+)\))?:\s*([\s\S]*)$/.exec(w);
  if (!m) return { title: null, body: sentence(w), severe: false };
  const detail = m[2] && m[2] !== "uncalibrated" ? ` (${m[2].replaceAll("_", " ")})` : "";
  const title = sentence(m[1].toLowerCase()) + detail;
  return { title, body: sentence(m[3]), severe: true };
}

export function truncate(s: string | null | undefined, n: number): string {
  if (!s) return "";
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
