"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { NeedKB } from "@/components/shell";
import { Badge, Empty, ErrorBox, PageHead, Spinner, ago, num } from "@/components/ui";
import { useFetch } from "@/lib/hooks";
import type { KB, TraceBrief } from "@/lib/types";

const PAGE = 50;

function Traces({ kb }: { kb: KB }) {
  const [status, setStatus] = useState("");
  const [healed, setHealed] = useState("");
  const [evalToo, setEvalToo] = useState(false);
  const [page, setPage] = useState(0);
  const router = useRouter();
  const qs = new URLSearchParams({ limit: String(PAGE), offset: String(page * PAGE) });
  if (status) qs.set("status", status);
  if (healed) qs.set("healed", healed);
  if (evalToo) qs.set("include_eval", "true");
  const { data, error, loading, reload } = useFetch<{ total: number; items: TraceBrief[] }>(`/kbs/${kb.id}/traces?${qs}`);

  return (
    <>
      <PageHead title="Traces" sub="Every query, every stage — the record the Repair loop learns from">
        <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(0); }} aria-label="status filter">
          <option value="">all statuses</option>
          <option value="verified">verified</option>
          <option value="partial,failed">partial / failed</option>
          <option value="error">error</option>
          <option value="direct">direct</option>
          <option value="escalated">escalated</option>
        </select>
        <select value={healed} onChange={(e) => { setHealed(e.target.value); setPage(0); }} aria-label="healed filter">
          <option value="">healed or not</option>
          <option value="true">healed</option>
          <option value="false">first-pass</option>
        </select>
        <label className="row small">
          <input type="checkbox" checked={evalToo} onChange={(e) => setEvalToo(e.target.checked)} /> eval runs
        </label>
        <button onClick={() => void reload()}>{loading ? <Spinner /> : "Refresh"}</button>
      </PageHead>
      <ErrorBox error={error} />
      <div className="card table-wrap">
        {data && data.items.length === 0 && <Empty>No traces match.</Empty>}
        {data && data.items.length > 0 && (
          <table className="t">
            <thead>
              <tr>
                <th>When</th>
                <th>Query</th>
                <th>Route</th>
                <th>Status</th>
                <th className="right">Grounded</th>
                <th className="right">Coverage</th>
                <th className="right">Calls</th>
                <th className="right">Latency</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((t) => (
                <tr key={t.id} className="click" onClick={() => router.push(`/traces/${t.id}`)}>
                  <td className="faint small" style={{ whiteSpace: "nowrap" }}>{ago(t.created_at)}</td>
                  <td>
                    <div className="truncate" style={{ maxWidth: 420 }}>{t.query}</div>
                    <div className="row">
                      {t.healed && <Badge value="healed" tone="info" />}
                      {t.negative_signal && <Badge value="negative" tone="bad" />}
                      {t.is_eval && <Badge value="eval" />}
                      <span className="faint small">cfg v{t.config_version}</span>
                    </div>
                  </td>
                  <td>{t.route}</td>
                  <td><Badge value={t.status} /></td>
                  <td className="right">{num(t.groundedness)}</td>
                  <td className="right">{num(t.coverage)}</td>
                  <td className="right">{t.llm_calls}</td>
                  <td className="right">{(t.latency_ms / 1000).toFixed(1)}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {data && data.total > PAGE && (
          <div className="spread mt small">
            <span className="faint">
              {page * PAGE + 1}–{Math.min((page + 1) * PAGE, data.total)} of {data.total}
            </span>
            <div className="row">
              <button className="sm" disabled={page === 0} onClick={() => setPage(page - 1)}>← newer</button>
              <button className="sm" disabled={(page + 1) * PAGE >= data.total} onClick={() => setPage(page + 1)}>older →</button>
            </div>
          </div>
        )}
      </div>
    </>
  );
}

export default function TracesPage() {
  return <NeedKB>{(kb) => <Traces kb={kb} />}</NeedKB>;
}
