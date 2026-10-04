"use client";

import Link from "next/link";
import { useState } from "react";
import { NeedKB } from "@/components/shell";
import { Badge, Empty, ErrorBox, Json, PageHead, Spinner, ago, pct } from "@/components/ui";
import { api, post } from "@/lib/api";
import { useFetch, useJob } from "@/lib/hooks";
import type { Diagnosis, Fix, KB } from "@/lib/types";

const CAUSE_HELP: Record<string, string> = {
  missing_content: "No document contains the answer",
  stale_content: "Source changed since indexing",
  bad_chunking: "Answer split across chunks",
  missing_context: "Chunk lacks the words users search with",
  vocabulary_gap: "Users and documents use different words",
  ranking_miss: "Right chunk ranked too low",
  twiddler_error: "A ranking rule removed needed evidence",
  conflicting_sources: "Sources contradict each other",
  generation_error: "Model stated unsupported facts",
  gate_error: "Answered without retrieval",
  injection: "Prompt-injection text in a document",
  unexplained_negative: "User said wrong; cause unknown",
};

const FIX_HELP: Record<string, string> = {
  augment_chunk: "Add users' phrasing to one chunk's index context (blue/green)",
  add_alias: "Expand user terms at query time",
  rechunk_doc: "Re-chunk a document with larger sections",
  recrawl_doc: "Re-ingest a changed document",
  set_tier: "Move a document to the warm tier",
  doc_boost: "Boost a document in ranking",
  gate_threshold: "Change when retrieval is skipped",
  improve_prompt: "Add instructions to the generator prompt",
  quarantine_doc: "Demote a document from retrieval",
  unquarantine_doc: "Restore a quarantined document",
  content_gap: "Add a new source (content owners)",
};

function EvalSummary({ f }: { f: Fix }) {
  const e = f.eval ?? {};
  if (e.error) return <div className="error-box small">{e.error}</div>;
  if (e.passed === undefined && !e.canary) return <span className="faint small">not evaluated</span>;
  return (
    <div className="small stack" style={{ gap: 2 }}>
      {e.passed !== undefined && <div>{e.passed ? <Badge value="eval passed" tone="ok" /> : <Badge value="eval failed" tone="bad" />}</div>}
      {e.targets && e.targets.n > 0 && (
        <div className="muted">
          targets ({e.targets.n}): {e.targets.baseline} → <b>{e.targets.candidate}</b>
        </div>
      )}
      {e.golden && e.golden.baseline.n > 0 && (
        <div className="muted">
          golden pass {pct(e.golden.baseline.pass_rate)} → <b>{pct(e.golden.candidate.pass_rate)}</b>
          {e.golden.comparison.regression && <span style={{ color: "var(--bad)" }}> regression</span>}
        </div>
      )}
      {e.golden_missing && <div className="faint">no golden set yet: only targets checked</div>}
      {e.canary && (
        <div className="muted">
          canary verified {pct(e.canary.canary.verified)} vs active {pct(e.canary.active.verified)} (n={e.canary.canary.n})
        </div>
      )}
    </div>
  );
}

function Repair({ kb }: { kb: KB }) {
  const [dStatus, setDStatus] = useState("open,fixing,needs_human");
  const diags = useFetch<Diagnosis[]>(`/kbs/${kb.id}/diagnoses${dStatus ? `?status=${dStatus}` : ""}`);
  const fixes = useFetch<Fix[]>(`/kbs/${kb.id}/fixes`);
  const [err, setErr] = useState<string | null>(null);
  const [busyFix, setBusyFix] = useState<string | null>(null);
  const [canary, setCanary] = useState<Record<string, unknown> | null>(null);
  const [lastRun, setLastRun] = useState<Record<string, unknown> | null>(null);
  const { job, track, running } = useJob((j) => {
    setLastRun(j.status === "done" ? j.result : { error: j.error });
    void diags.reload();
    void fixes.reload();
  });

  const run = async () => {
    setErr(null);
    try {
      const r = await post<{ job_id: string }>(`/kbs/${kb.id}/repair`);
      track(r.job_id);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const act = async (f: Fix, action: string) => {
    let reason = "";
    if (action === "reject" || action === "rollback") {
      reason = window.prompt(`Reason to ${action} this fix?`) ?? "";
    }
    setBusyFix(f.id);
    setErr(null);
    try {
      await post(`/fixes/${f.id}/${action}`, { reason });
      await Promise.all([fixes.reload(), diags.reload()]);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusyFix(null);
    }
  };

  const evalCanary = async () => {
    try {
      setCanary(await post("/canary/evaluate"));
      void fixes.reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const setDiag = async (d: Diagnosis, status: string) => {
    await api(`/diagnoses/${d.id}`, { method: "PATCH", json: { status } });
    void diags.reload();
  };

  const pending = (fixes.data ?? []).filter((f) => f.status === "pending_approval");
  const others = (fixes.data ?? []).filter((f) => f.status !== "pending_approval");

  const FixRow = ({ f }: { f: Fix }) => (
    <tr>
      <td>
        <div>
          <b>{f.kind.replace(/_/g, " ")}</b> <Badge value={f.risk} />
        </div>
        <div className="faint small">{FIX_HELP[f.kind]}</div>
        <div className="small">{f.rationale}</div>
        <details className="small">
          <summary>parameters</summary>
          <Json value={f.params} />
        </details>
      </td>
      <td>
        <Badge value={f.status} />
        <div className="faint small">{ago(f.applied_at ?? f.created_at)}</div>
        {f.config_version && <div className="faint small">config v{f.config_version}</div>}
      </td>
      <td>
        <EvalSummary f={f} />
      </td>
      <td className="right">
        <div className="row" style={{ justifyContent: "flex-end" }}>
          {busyFix === f.id && <Spinner />}
          {(f.status === "pending_approval" || f.status === "canary") && (
            <button className="sm primary" onClick={() => void act(f, "approve")} disabled={!!busyFix}>
              Approve
            </button>
          )}
          {["pending_approval", "canary", "proposed"].includes(f.status) && (
            <button className="sm danger" onClick={() => void act(f, "reject")} disabled={!!busyFix}>
              Reject
            </button>
          )}
          {(f.status === "applied" || f.status === "canary") && (
            <button className="sm" onClick={() => void act(f, "rollback")} disabled={!!busyFix}>
              Roll back
            </button>
          )}
          {["rejected", "failed", "proposed"].includes(f.status) && (
            <button className="sm" onClick={() => void act(f, "evaluate")} disabled={!!busyFix} title="re-run the eval gate">
              Re-evaluate
            </button>
          )}
        </div>
      </td>
    </tr>
  );

  return (
    <>
      <PageHead title="Repair" sub="Diagnose failures → propose fixes → eval gate → apply by risk → canary → promote or roll back">
        <button onClick={() => void evalCanary()}>Judge canary</button>
        <button className="primary" onClick={() => void run()} disabled={running}>
          {running ? (
            <>
              <Spinner /> repairing…
            </>
          ) : (
            "Run repair cycle"
          )}
        </button>
      </PageHead>
      <ErrorBox error={err ?? diags.error ?? fixes.error} />
      {job && job.status === "failed" && <ErrorBox error={job.error} />}
      {lastRun && !("error" in lastRun) && (
        <div className="note-box small" style={{ marginBottom: 16 }}>
          Last cycle: {String(lastRun.traces)} traces mined · {String(lastRun.findings)} findings · {(lastRun.fixes as unknown[]).length} fixes processed
        </div>
      )}
      {canary && (
        <div className="note-box small" style={{ marginBottom: 16 }}>
          Canary: {canary.canary ? `v${String(canary.canary)} → ${String(canary.decision)}` : "none running"}
        </div>
      )}

      {pending.length > 0 && (
        <div className="card">
          <h2>Awaiting your approval ({pending.length})</h2>
          <div className="table-wrap">
            <table className="t">
              <tbody>
                {pending.map((f) => (
                  <FixRow key={f.id} f={f} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="card">
        <div className="spread">
          <h2>Diagnoses</h2>
          <select value={dStatus} onChange={(e) => setDStatus(e.target.value)} aria-label="diagnosis filter">
            <option value="open,fixing,needs_human">open</option>
            <option value="fixed">fixed</option>
            <option value="wont_fix">won&apos;t fix</option>
            <option value="">all</option>
          </select>
        </div>
        {diags.data && diags.data.length === 0 && <Empty>Nothing to diagnose. Failures and healed answers appear here after a repair cycle.</Empty>}
        {diags.data && diags.data.length > 0 && (
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr>
                  <th>Root cause</th>
                  <th>Summary</th>
                  <th className="right">Impact</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {diags.data.map((d) => (
                  <tr key={d.id}>
                    <td>
                      <b>{d.root_cause.replace(/_/g, " ")}</b>
                      <div className="faint small">{CAUSE_HELP[d.root_cause]}</div>
                    </td>
                    <td>
                      <div>{d.summary}</div>
                      <div className="row small">
                        {d.trace_ids.slice(-3).map((t) => (
                          <Link key={t} href={`/traces/${t}`} className="mono">
                            {t.slice(0, 8)}
                          </Link>
                        ))}
                        {d.trace_ids.length > 3 && <span className="faint">+{d.trace_ids.length - 3}</span>}
                      </div>
                    </td>
                    <td className="right">{d.impact}</td>
                    <td>
                      <Badge value={d.status} />
                    </td>
                    <td className="right">
                      {d.status !== "wont_fix" && d.status !== "fixed" && (
                        <button className="sm ghost" onClick={() => void setDiag(d, "wont_fix")}>
                          dismiss
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card">
        <h2>Fixes</h2>
        {others.length === 0 ? (
          <Empty>No fixes yet.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr>
                  <th>Fix</th>
                  <th>Status</th>
                  <th>Evaluation</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {others.map((f) => (
                  <FixRow key={f.id} f={f} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

export default function RepairPage() {
  return <NeedKB>{(kb) => <Repair kb={kb} />}</NeedKB>;
}
