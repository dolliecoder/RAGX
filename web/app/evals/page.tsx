"use client";

import Link from "next/link";
import { useState } from "react";
import { NeedKB } from "@/components/shell";
import { Badge, Empty, ErrorBox, PageHead, Spinner, ago, pct } from "@/components/ui";
import { api, post } from "@/lib/api";
import { useFetch, useJob } from "@/lib/hooks";
import type { EvalRun, Golden, KB } from "@/lib/types";

type RunDetail = EvalRun & {
  results: { question: string; status: string; passed: boolean; trace_id: string | null; judge: { overall: number; rationale: string }; kind: string }[];
};

function GoldenSet({ kb }: { kb: KB }) {
  const golden = useFetch<Golden[]>(`/kbs/${kb.id}/golden`);
  const [q, setQ] = useState("");
  const [exp, setExp] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    try {
      await post(`/kbs/${kb.id}/golden`, { question: q.trim(), expected_answer: exp.trim() });
      setQ("");
      setExp("");
      void golden.reload();
    } catch (ex) {
      setErr((ex as Error).message);
    }
  };
  const toggle = async (g: Golden) => {
    await api(`/golden/${g.id}`, { method: "PATCH", json: { active: !g.active } });
    void golden.reload();
  };
  const remove = async (g: Golden) => {
    if (!window.confirm("Delete this golden test?")) return;
    await api(`/golden/${g.id}`, { method: "DELETE" });
    void golden.reload();
  };

  return (
    <div className="card">
      <h2>Golden set {golden.data && <span className="faint">({golden.data.filter((g) => g.active).length} active)</span>}</h2>
      <p className="muted small">
        Regression tests every fix must pass. They grow automatically: healed failures and user corrections are added here.
      </p>
      <form className="stack" onSubmit={add}>
        <input placeholder="Question" value={q} onChange={(e) => setQ(e.target.value)} aria-label="question" />
        <textarea placeholder="Expected answer (or 'not found in the knowledge base')" rows={2} value={exp} onChange={(e) => setExp(e.target.value)} aria-label="expected answer" />
        <div>
          <button className="primary" disabled={!q.trim()}>
            Add test
          </button>
        </div>
      </form>
      <ErrorBox error={err ?? golden.error} />
      {golden.data && golden.data.length === 0 && <Empty>No golden tests yet.</Empty>}
      {golden.data && golden.data.length > 0 && (
        <div className="table-wrap mt">
          <table className="t">
            <thead>
              <tr>
                <th>Question</th>
                <th>Expected</th>
                <th>Origin</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {golden.data.map((g) => (
                <tr key={g.id} style={{ opacity: g.active ? 1 : 0.5 }}>
                  <td>{g.question}</td>
                  <td className="small muted">{g.expected_answer.slice(0, 200) || "–"}</td>
                  <td>
                    <Badge value={g.origin} tone={g.origin === "healed" ? "info" : g.origin === "feedback" ? "warn" : ""} />
                    {g.origin === "feedback" && !g.active && (
                      <div className="small" style={{ color: "var(--warn)" }}>
                        {g.note || "user correction: check it, then Enable"}
                      </div>
                    )}
                  </td>
                  <td className="right">
                    <div className="row" style={{ justifyContent: "flex-end" }}>
                      <button className="sm" onClick={() => void toggle(g)}>
                        {g.active ? "Disable" : "Enable"}
                      </button>
                      <button className="sm danger" onClick={() => void remove(g)}>
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Runs({ kb }: { kb: KB }) {
  const runs = useFetch<EvalRun[]>(`/kbs/${kb.id}/evals`);
  const [open, setOpen] = useState<RunDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { track, running } = useJob(() => void runs.reload());

  const start = async () => {
    setErr(null);
    try {
      track((await post<{ job_id: string }>(`/kbs/${kb.id}/evals`)).job_id);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const show = async (r: EvalRun) => setOpen(await api<RunDetail>(`/evals/${r.id}`));
  const s = (r: EvalRun, k: string) => r.summary[k] as number | undefined;

  return (
    <div className="card">
      <div className="spread">
        <h2>Evaluation runs</h2>
        <button className="primary" onClick={() => void start()} disabled={running}>
          {running ? (
            <>
              <Spinner /> running…
            </>
          ) : (
            "Run golden set"
          )}
        </button>
      </div>
      <ErrorBox error={err ?? runs.error} />
      {runs.data && runs.data.length === 0 && <Empty>No runs yet.</Empty>}
      {runs.data && runs.data.length > 0 && (
        <div className="table-wrap">
          <table className="t">
            <thead>
              <tr>
                <th>When</th>
                <th>Purpose</th>
                <th className="right">Items</th>
                <th className="right">Pass</th>
                <th className="right">Judge</th>
                <th className="right">Verified</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.id} className="click" onClick={() => void show(r)}>
                  <td className="faint small">{ago(r.created_at)}</td>
                  <td>
                    {r.purpose.startsWith("fix:") ? <Badge value="fix gate" tone="info" /> : r.purpose} <span className="faint small">cfg v{r.config_version}</span>
                  </td>
                  <td className="right">{s(r, "n") ?? (r.summary.golden as { n?: number } | undefined)?.n ?? "–"}</td>
                  <td className="right">{r.summary.passed !== undefined ? (r.summary.passed ? "✓" : "✗") : pct(s(r, "pass_rate"))}</td>
                  <td className="right">{pct(s(r, "judge_overall"))}</td>
                  <td className="right">{pct(s(r, "verified_rate"))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && (
        <div className="mt">
          <div className="spread">
            <h3>Run {open.id.slice(0, 8)}</h3>
            <button className="sm ghost" onClick={() => setOpen(null)}>
              close
            </button>
          </div>
          <table className="t small">
            <tbody>
              {open.results.map((r, i) => (
                <tr key={i}>
                  <td>{r.passed ? <Badge value="pass" tone="ok" /> : <Badge value="fail" tone="bad" />}</td>
                  <td>
                    {r.question}
                    <div className="faint">{r.judge?.rationale}</div>
                  </td>
                  <td>
                    <Badge value={r.status} />
                  </td>
                  <td className="right">{pct(r.judge?.overall)}</td>
                  <td>{r.trace_id && <Link href={`/traces/${r.trace_id}`}>trace</Link>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function EvalsPage() {
  return (
    <NeedKB>
      {(kb) => (
        <>
          <PageHead title="Evals" sub="Golden regression tests, LLM-judge rubric and the history of every eval gate" />
          <div className="grid k2">
            <GoldenSet kb={kb} />
            <Runs kb={kb} />
          </div>
        </>
      )}
    </NeedKB>
  );
}
