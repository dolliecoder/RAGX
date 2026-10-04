"use client";

import Link from "next/link";
import { use } from "react";
import { Badge, Empty, ErrorBox, Json, Meter, PageHead, Spinner, ago } from "@/components/ui";
import { useFetch } from "@/lib/hooks";
import type { Step, Trace } from "@/lib/types";

const HEAL_STAGES = new Set(["heal_retrieval", "heal_answer", "verify_after_repair", "escalate"]);

function describe(s: Step): string {
  const pick = (k: string) => (s[k] === undefined ? "" : `${k}=${JSON.stringify(s[k])}`);
  switch (s.stage) {
    case "gate":
      return [pick("route"), pick("retrieval_need"), pick("complexity"), pick("freshness_need")].filter(Boolean).join("  ");
    case "plan":
      return (s.subquestions as string[]).join(" | ");
    case "retrieve":
      return `lists ${JSON.stringify(s.lists)} · counts ${JSON.stringify(s.counts)}`;
    case "grade":
      return `${s.correct} correct · ${s.ambiguous} ambiguous · ${s.incorrect} incorrect`;
    case "heal_retrieval":
      return `round ${s.round}: missing ${JSON.stringify(s.missing)} → ${(s.actions as string[]).join(", ")} · new ${s.new} · fixed ${JSON.stringify(s.fixed)}`;
    case "generate":
      return `${s.sentences} sentences · ${s.unanswered} unanswered · ${s.conflicts} conflicts (${s.model})`;
    case "verify":
    case "verify_after_repair":
      return `grounded ${s.groundedness} · contradiction ${s.contradiction} · coverage ${s.coverage} · claims ${s.claims}`;
    case "heal_answer":
      return `${s.step}: dropped ${s.dropped} · fixed citations ${s.fixed_citations}`;
    default:
      return Object.entries(s)
        .filter(([k]) => k !== "stage" && k !== "t_ms")
        .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
        .join("  ")
        .slice(0, 240);
  }
}

export default function TracePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data: t, error, loading } = useFetch<Trace>(`/traces/${id}`);
  if (error) return <ErrorBox error={error} />;
  if (!t) return loading ? <Spinner /> : <Empty>Not found</Empty>;
  const steps = t.data.steps ?? [];
  const total = Math.max(t.latency_ms, ...steps.map((s) => s.t_ms), 1);
  const diag = t.data.diag ?? { heals: [], rescues: [], unsupported: [], degraded: [], errors: [] };

  return (
    <>
      <PageHead title="Trace" sub={<span className="mono small">{t.id}</span>}>
        <Link href="/traces">← all traces</Link>
      </PageHead>

      <div className="card stack">
        <div className="spread">
          <h2 style={{ margin: 0 }}>{t.query}</h2>
          <div className="row">
            <Badge value={t.status} />
            <span className="badge">{t.route}</span>
            {t.healed && <Badge value="healed" tone="info" />}
            {t.negative_signal && <Badge value="negative signal" tone="bad" />}
            {t.is_eval && <Badge value="eval" />}
          </div>
        </div>
        <div className="row" style={{ gap: 20 }}>
          <Meter label="grounded" value={t.groundedness} />
          <Meter label="coverage" value={t.coverage} />
          <Meter label="confidence" value={t.confidence} />
          <span className="small muted">
            {t.llm_calls} model calls · {t.tokens_in.toLocaleString()} in / {t.tokens_out.toLocaleString()} out tokens · {(t.latency_ms / 1000).toFixed(2)}s · config v{t.config_version} ·{" "}
            {ago(t.created_at)}
          </span>
        </div>
        <div className="answer-text" style={{ whiteSpace: "pre-wrap" }}>{t.answer}</div>
        {t.data.job_id && (
          <div className="small">
            Deep research job: <span className="mono">{t.data.job_id}</span>
          </div>
        )}
      </div>

      <div className="card">
        <h2>Pipeline</h2>
        <div className="wf">
          {steps.map((s, i) => {
            const start = i === 0 ? 0 : steps[i - 1].t_ms;
            const cls = HEAL_STAGES.has(s.stage) ? "heal" : s.stage.startsWith("verify") ? "verify" : s.stage.includes("error") || s.stage === "budget_exhausted" ? "bad" : "";
            return (
              <div key={i}>
                <div className="wf-row">
                  <span className="mono small">{s.stage}</span>
                  <div className="wf-track">
                    <div className={`wf-bar ${cls}`} style={{ left: `${(start / total) * 100}%`, width: `${Math.max(0.5, ((s.t_ms - start) / total) * 100)}%` }} />
                  </div>
                  <span className="faint small right">{s.t_ms - start} ms</span>
                </div>
                <div className="faint small wf-desc">{describe(s)}</div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="grid k2 mt">
        <div className="card">
          <h2>Healing</h2>
          {diag.heals.length === 0 && diag.rescues.length === 0 ? (
            <Empty>No heal step was needed.</Empty>
          ) : (
            <div className="stack small">
              {diag.heals.map((h, i) => (
                <div key={i} className="cite">
                  <b>{String(h.step)}</b> <span className="muted">{JSON.stringify({ ...h, step: undefined })}</span>
                </div>
              ))}
              {diag.rescues.map((r, i) => (
                <div key={`r${i}`}>
                  Sub-question {String(r.subq)} rescued <Badge value={`via ${String(r.origin)}`} tone="info" />{" "}
                  {r.doc_id ? <Link href={`/knowledge/${String(r.doc_id)}`}>document</Link> : r.url ? <a href={String(r.url)}>{String(r.url)}</a> : null}
                </div>
              ))}
              {(diag.rewrites ?? []).map((r, i) => (
                <div key={`w${i}`} className="muted">
                  rewrite [{r.subq}]: {r.queries.join(" | ")}
                </div>
              ))}
              {diag.unsupported.length > 0 && (
                <div>
                  <b>Removed as unsupported:</b>
                  <ul>
                    {diag.unsupported.map((u, i) => (
                      <li key={i} className="muted">{String(u.sentence)}</li>
                    ))}
                  </ul>
                </div>
              )}
              {diag.degraded.map((d, i) => (
                <div key={`d${i}`} className="note-box">{d}</div>
              ))}
              {diag.errors.map((d, i) => (
                <div key={`e${i}`} className="error-box">{d}</div>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <h2>Verification</h2>
          {t.data.claims && t.data.claims.length > 0 ? (
            <table className="t small">
              <thead>
                <tr>
                  <th>Claim</th>
                  <th>Support</th>
                  <th>Sources</th>
                </tr>
              </thead>
              <tbody>
                {t.data.claims.map((c, i) => (
                  <tr key={i}>
                    <td>{c.claim}</td>
                    <td>
                      <Badge value={c.support} tone={c.support === "full" ? "ok" : c.support === "partial" ? "warn" : "bad"} />
                    </td>
                    <td>
                      {c.supporting.map((n) => `[${n}]`).join(" ")}
                      {c.contradicting.length > 0 && <span style={{ color: "var(--bad)" }}> contradicts {c.contradicting.map((n) => `[${n}]`).join(" ")}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No claims were verified for this answer.</Empty>
          )}
        </div>
      </div>

      <div className="grid k2 mt">
        <div className="card">
          <h2>Sub-questions</h2>
          <ol className="small">
            {t.data.subquestions.map((s, i) => (
              <li key={i}>
                {s.question}
                {s.variants.length > 0 && <div className="faint">rewrites: {s.variants.join(" | ")}</div>}
              </li>
            ))}
          </ol>
          {t.data.unanswered.length > 0 && <div className="note-box small">Unanswered: {t.data.unanswered.join("; ")}</div>}
        </div>
        <div className="card table-wrap">
          <h2>Evidence graded ({t.data.evidence.length})</h2>
          <table className="t small">
            <thead>
              <tr>
                <th>Chunk</th>
                <th>Verdict</th>
                <th>Origin</th>
                <th>Sub-q</th>
              </tr>
            </thead>
            <tbody>
              {t.data.evidence.slice(0, 40).map((e) => (
                <tr key={e.id}>
                  <td className="mono">{e.doc_id ? <Link href={`/knowledge/${e.doc_id}`}>{e.id.slice(0, 10)}</Link> : e.url ? <a href={e.url}>web</a> : e.id.slice(0, 10)}</td>
                  <td>{e.origin === "long_context" ? <Badge value="in context" /> : <Badge value={e.verdict} tone={e.verdict === "correct" ? "ok" : e.verdict === "ambiguous" ? "warn" : ""} />}</td>
                  <td>{e.origin}</td>
                  <td>{e.subqs.join(",")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {t.feedback.length > 0 && (
        <div className="card mt">
          <h2>Feedback</h2>
          {t.feedback.map((f, i) => (
            <div key={i} className="small">
              <Badge value={f.kind} /> rating {f.rating} {f.comment && `· “${f.comment}”`} {f.correction && <div className="muted">correction: {f.correction}</div>}
            </div>
          ))}
        </div>
      )}

      <details className="card mt">
        <summary>Raw trace data</summary>
        <Json value={t.data} />
      </details>
    </>
  );
}
