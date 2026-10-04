"use client";

import Link from "next/link";
import { useState } from "react";
import { NeedKB } from "@/components/shell";
import { ErrorBox, LineChart, PageHead, Spinner, Tile, num, pct } from "@/components/ui";
import { useFetch } from "@/lib/hooks";
import type { HealthMetrics, KB } from "@/lib/types";

function tone(v: number | null, good: number, ok: number, higherBetter = true): "ok" | "warn" | "bad" | undefined {
  if (v === null || v === undefined) return undefined;
  if (higherBetter) return v >= good ? "ok" : v >= ok ? "warn" : "bad";
  return v <= good ? "ok" : v <= ok ? "warn" : "bad";
}

function dur(s: number | null) {
  if (s === null) return "–";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}

function Health({ kb }: { kb: KB }) {
  const [days, setDays] = useState(7);
  const { data: m, error, loading, reload } = useFetch<HealthMetrics>(`/kbs/${kb.id}/metrics?days=${days}`);
  return (
    <>
      <PageHead title="Health" sub="Service-level indicators the platform optimizes">
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="window">
          {[1, 7, 30, 90].map((d) => (
            <option key={d} value={d}>
              last {d} day{d > 1 ? "s" : ""}
            </option>
          ))}
        </select>
        <button onClick={() => void reload()}>{loading ? <Spinner /> : "Refresh"}</button>
      </PageHead>
      <ErrorBox error={error} />
      {m && (
        <>
          <div className="grid k4">
            <Tile label="Queries" value={m.queries} hint={Object.entries(m.by_status).map(([k, v]) => `${k} ${v}`).join(" · ") || "no traffic yet"} />
            <Tile label="Verified answers" value={pct(m.verified_rate)} tone={tone(m.verified_rate, 0.85, 0.6)} hint="passed independent verification" />
            <Tile label="Groundedness" value={pct(m.groundedness)} tone={tone(m.groundedness, 0.9, 0.75)} hint="claims fully entailed by sources" />
            <Tile label="Contradiction rate" value={pct(m.contradiction_rate, 1)} tone={tone(m.contradiction_rate, 0.02, 0.1, false)} />
            <Tile label="Heal rate" value={pct(m.heal_rate)} hint="queries that needed a heal step" />
            <Tile label="Heal success" value={pct(m.heal_success_rate)} tone={tone(m.heal_success_rate, 0.7, 0.4)} hint="healed queries ending verified" />
            <Tile label="Repeat failures" value={pct(m.repeat_failure_rate)} tone={tone(m.repeat_failure_rate, 0.1, 0.3, false)} hint="same question failing again" />
            <Tile label="Negative signals" value={pct(m.negative_signal_rate)} tone={tone(m.negative_signal_rate, 0.05, 0.15, false)} hint="thumbs-down or re-asked" />
            <Tile label="Abstention" value={pct(m.abstention_rate)} hint="honest 'not found' answers" />
            <Tile label="Latency p50 / p95" value={`${(m.latency_p50_ms / 1000).toFixed(1)}s`} hint={`p95 ${(m.latency_p95_ms / 1000).toFixed(1)}s`} />
            <Tile label="Model calls / query" value={num(m.avg_llm_calls, 1)} hint={`${(m.tokens.in / 1000).toFixed(0)}k in · ${(m.tokens.out / 1000).toFixed(0)}k out tokens`} />
            <Tile label="Mean time to heal" value={dur(m.mean_time_to_heal_s)} hint="diagnosis → fix applied" />
          </div>

          <div className="grid k2 mt">
            <div className="card">
              <div className="spread">
                <h2>Daily quality</h2>
                <span className="small">
                  <span style={{ color: "var(--accent)" }}>■</span> verified rate <span style={{ color: "var(--ok)" }}>■</span> groundedness
                </span>
              </div>
              {m.series.length ? (
                <LineChart points={m.series.map((s) => s.verified_rate)} second={m.series.map((s) => s.groundedness)} labels={m.series.map((s) => s.day)} />
              ) : (
                <div className="empty">No answered queries in this window.</div>
              )}
            </div>
            <div className="card">
              <h2>Self-healing</h2>
              <dl className="kv">
                <dt>Open diagnoses</dt>
                <dd>{m.repair.open_diagnoses}</dd>
                <dt>Needs a human</dt>
                <dd>{m.repair.needs_human}</dd>
                <dt>Fixes awaiting approval</dt>
                <dd>{m.repair.pending_approval > 0 ? <Link href="/repair">{m.repair.pending_approval} — review</Link> : 0}</dd>
                <dt>Fixes applied</dt>
                <dd>{m.repair.applied_fixes}</dd>
                <dt>Golden tests</dt>
                <dd>{m.repair.golden_items}</dd>
                <dt>Config</dt>
                <dd>
                  v{m.config.active}
                  {m.config.canary && (
                    <span className="badge violet" style={{ marginLeft: 6 }}>
                      canary v{m.config.canary} at {pct(m.config.canary_pct)}
                    </span>
                  )}
                </dd>
              </dl>
              <div className="hr" />
              <h3>Corpus</h3>
              <dl className="kv">
                <dt>Documents</dt>
                <dd>{Object.entries(m.corpus.documents).map(([k, v]) => `${v} ${k}`).join(" · ") || "none"}</dd>
                <dt>Chunks</dt>
                <dd>{m.corpus.chunks}</dd>
                <dt>Tokens</dt>
                <dd>{m.corpus.tokens.toLocaleString()}</dd>
              </dl>
            </div>
          </div>
        </>
      )}
    </>
  );
}

export default function HealthPage() {
  return <NeedKB>{(kb) => <Health kb={kb} />}</NeedKB>;
}
