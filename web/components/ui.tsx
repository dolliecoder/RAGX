"use client";

import type { ReactNode } from "react";

const STATUS_TONE: Record<string, string> = {
  verified: "ok",
  applied: "ok",
  done: "ok",
  ok: "ok",
  active: "ok",
  fixed: "ok",
  promoted: "ok",
  partial: "warn",
  pending_approval: "warn",
  needs_human: "warn",
  canary: "violet",
  running: "info",
  queued: "info",
  evaluating: "info",
  proposed: "info",
  fixing: "info",
  open: "info",
  direct: "accent",
  escalated: "violet",
  failed: "bad",
  error: "bad",
  rejected: "bad",
  rolled_back: "bad",
  quarantined: "bad",
  superseded: "",
  retired: "",
  staged: "violet",
  wont_fix: "",
  low: "ok",
  medium: "warn",
  high: "bad",
};

export function Badge({ value, tone }: { value: string; tone?: string }) {
  return <span className={`badge ${tone ?? STATUS_TONE[value] ?? ""}`}>{value.replace(/_/g, " ")}</span>;
}

export function Spinner() {
  return <span className="spinner" aria-label="loading" />;
}

export function ErrorBox({ error }: { error: string | null | undefined }) {
  if (!error) return null;
  return (
    <div className="error-box" role="alert">
      {error}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function PageHead({ title, sub, children }: { title: string; sub?: ReactNode; children?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      {children && <div className="row">{children}</div>}
    </div>
  );
}

export function pct(v: number | null | undefined, digits = 0): string {
  return v === null || v === undefined ? "–" : `${(v * 100).toFixed(digits)}%`;
}

export function num(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined ? "–" : v.toFixed(digits);
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z");
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 60) return `${Math.max(0, s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function Tile({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: "ok" | "warn" | "bad" }) {
  const color = tone ? `var(--${tone})` : undefined;
  return (
    <div className="card tile">
      <span className="label">{label}</span>
      <span className="value" style={{ color }}>
        {value}
      </span>
      {hint && <span className="hint">{hint}</span>}
    </div>
  );
}

export function Meter({ value, label }: { value: number | null | undefined; label: string }) {
  const v = Math.max(0, Math.min(1, value ?? 0));
  const color = v >= 0.9 ? "var(--ok)" : v >= 0.6 ? "var(--warn)" : "var(--bad)";
  return (
    <div style={{ minWidth: 110 }}>
      <div className="spread small">
        <span className="muted">{label}</span>
        <span style={{ fontVariantNumeric: "tabular-nums" }}>{value === null || value === undefined ? "–" : pct(v)}</span>
      </div>
      <div className="bar">
        <span style={{ width: `${v * 100}%`, background: color }} />
      </div>
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return (
    <pre className="mono small" style={{ background: "var(--panel-2)", padding: 10, borderRadius: 8, maxHeight: 360, overflow: "auto" }}>
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

/** Tiny dependency-free line chart for daily series (0..1 values). */
export function LineChart({ points, second, labels }: { points: (number | null)[]; second?: (number | null)[]; labels: string[] }) {
  const w = 600;
  const h = 140;
  const pad = 24;
  const n = Math.max(points.length, 1);
  const x = (i: number) => pad + (n === 1 ? (w - 2 * pad) / 2 : (i * (w - 2 * pad)) / (n - 1));
  const y = (v: number) => h - pad - v * (h - 2 * pad);
  const path = (pts: (number | null)[]) =>
    pts
      .map((v, i) => (v === null ? null : `${x(i)},${y(v)}`))
      .filter(Boolean)
      .map((p, i) => `${i === 0 ? "M" : "L"}${p}`)
      .join(" ");
  return (
    <svg className="chart" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="trend chart">
      {[0, 0.5, 1].map((g) => (
        <g key={g}>
          <line className="axis" x1={pad} x2={w - pad} y1={y(g)} y2={y(g)} />
          <text x={2} y={y(g) + 3}>
            {g * 100}%
          </text>
        </g>
      ))}
      <path className="line" d={path(points)} />
      {second && <path className="line2" d={path(second)} />}
      {points.map((v, i) => (v !== null ? <circle key={i} cx={x(i)} cy={y(v)} r={2.5} fill="var(--accent)" /> : null))}
      {labels.map((l, i) =>
        i % Math.ceil(n / 7) === 0 ? (
          <text key={l} x={x(i) - 14} y={h - 6}>
            {l.slice(5)}
          </text>
        ) : null,
      )}
    </svg>
  );
}
