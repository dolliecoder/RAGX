"use client";

import { useEffect, useState } from "react";
import { Badge, ErrorBox, Json, PageHead, Spinner, ago } from "@/components/ui";
import { api, post } from "@/lib/api";
import { useFetch } from "@/lib/hooks";
import type { ProviderStatus } from "@/lib/types";

type ConfigResp = {
  active_version: number;
  active: Record<string, unknown>;
  canary: { version: number; canary_pct: number; note: string; data: Record<string, unknown> } | null;
  versions: { version: number; status: string; parent_version: number | null; canary_pct: number; note: string; created_at: string; activated_at: string | null }[];
};

type AuditRow = { id: string; actor: string; action: string; target: string; details: Record<string, unknown>; created_at: string };

function Providers() {
  const { data: p, error, loading, reload } = useFetch<ProviderStatus>("/providers");
  return (
    <div className="card">
      <div className="spread">
        <h2>Model providers</h2>
        <button className="sm" onClick={() => void reload()}>
          {loading ? <Spinner /> : "Refresh"}
        </button>
      </div>
      <ErrorBox error={error} />
      {p && (
        <>
          {!p.verifier_independent && (
            <div className="note-box small" style={{ marginBottom: 8 }}>
              Generator and verifier are the same model: verification is not independent.
            </div>
          )}
          <table className="t small">
            <tbody>
              {Object.entries(p.roles).map(([role, info]) => (
                <tr key={role}>
                  <td>
                    <b>{role}</b> {info.offline && <Badge value="offline" tone="warn" />}
                  </td>
                  <td>
                    {info.chain.map((c, i) => (
                      <div key={i}>
                        <span className="mono">{c.provider}</span>{" "}
                        <Badge value={c.state} tone={c.state === "closed" ? "ok" : c.state === "open" ? "bad" : "warn"} />
                        {c.last_error && <div className="faint">{c.last_error}</div>}
                      </div>
                    ))}
                    {info.notes.map((n, i) => (
                      <div key={`n${i}`} className="faint">
                        {n}
                      </div>
                    ))}
                  </td>
                </tr>
              ))}
              <tr>
                <td>
                  <b>embedder</b>
                </td>
                <td>
                  <span className="mono">{p.embedder.model}</span> {p.embedder.note && <div className="faint">{p.embedder.note}</div>}
                </td>
              </tr>
              <tr>
                <td>
                  <b>reranker</b>
                </td>
                <td className="mono">{p.reranker}</td>
              </tr>
              <tr>
                <td>
                  <b>web search</b>
                </td>
                <td>
                  {p.web_search.provider} {p.web_search.available ? <Badge value="available" tone="ok" /> : <Badge value="not configured" />}
                </td>
              </tr>
            </tbody>
          </table>
          <p className="faint small mt">Providers and keys are set with RAGX_* environment variables on the API server (see .env.example).</p>
        </>
      )}
    </div>
  );
}

function ConfigEditor() {
  const cfg = useFetch<ConfigResp>("/config");
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [view, setView] = useState<{ version: number; data: unknown } | null>(null);

  useEffect(() => {
    if (cfg.data) setText(JSON.stringify(cfg.data.active, null, 2));
  }, [cfg.data]);

  const save = async () => {
    setErr(null);
    setSaved(null);
    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch (e) {
      setErr(`Invalid JSON: ${(e as Error).message}`);
      return;
    }
    try {
      const r = await api<{ active_version: number }>("/config", { method: "PUT", json: { data, note: note || "dashboard edit" } });
      setSaved(`Saved as v${r.active_version} and activated.`);
      setNote("");
      void cfg.reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const action = async (v: number, a: "activate" | "rollback") => {
    if (!window.confirm(`${a === "activate" ? "Activate" : "Roll back"} config v${v}?`)) return;
    try {
      await post(`/config/${v}/${a}`, { reason: `dashboard ${a}` });
      void cfg.reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const showVersion = async (v: number) => setView(await api<{ version: number; data: unknown }>(`/config/${v}`));

  return (
    <div className="card">
      <div className="spread">
        <h2>Runtime config {cfg.data && <span className="faint">(active v{cfg.data.active_version})</span>}</h2>
        {cfg.data?.canary && <Badge value={`canary v${cfg.data.canary.version} at ${Math.round(cfg.data.canary.canary_pct * 100)}%`} tone="violet" />}
      </div>
      <p className="muted small">
        Everything the Repair loop is allowed to tune: thresholds, budgets, retrieval sizes, twiddler rules, aliases and prompt add-ons. Every save is a new version
        you can roll back.
      </p>
      <textarea className="mono" rows={18} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} aria-label="config JSON" />
      <div className="row mt">
        <input className="grow" placeholder="Change note" value={note} onChange={(e) => setNote(e.target.value)} />
        <button className="primary" onClick={() => void save()}>
          Save new version
        </button>
      </div>
      <ErrorBox error={err ?? cfg.error} />
      {saved && <div className="note-box small mt">{saved}</div>}

      {cfg.data && (
        <>
          <h3 className="mt">Versions</h3>
          <div className="table-wrap">
            <table className="t small">
              <tbody>
                {cfg.data.versions.map((v) => (
                  <tr key={v.version}>
                    <td>
                      <button className="sm ghost" onClick={() => void showVersion(v.version)}>
                        v{v.version}
                      </button>
                    </td>
                    <td>
                      <Badge value={v.status} />
                    </td>
                    <td className="muted">{v.note}</td>
                    <td className="faint">{ago(v.activated_at ?? v.created_at)}</td>
                    <td className="right">
                      {v.status !== "active" && v.status !== "canary" && (
                        <button className="sm" onClick={() => void action(v.version, "activate")}>
                          Activate
                        </button>
                      )}
                      {(v.status === "active" || v.status === "canary") && v.version > 1 && (
                        <button className="sm" onClick={() => void action(v.version, "rollback")}>
                          Roll back
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {view && (
            <details open className="mt">
              <summary>
                v{view.version} contents{" "}
                <button className="sm ghost" onClick={() => setView(null)}>
                  close
                </button>
              </summary>
              <Json value={view.data} />
            </details>
          )}
        </>
      )}
    </div>
  );
}

function Audit() {
  const { data, error } = useFetch<AuditRow[]>("/audit?limit=100");
  return (
    <div className="card">
      <h2>Audit log</h2>
      <ErrorBox error={error} />
      <div className="table-wrap" style={{ maxHeight: 420, overflowY: "auto" }}>
        <table className="t small">
          <tbody>
            {(data ?? []).map((a) => (
              <tr key={a.id}>
                <td className="faint" style={{ whiteSpace: "nowrap" }}>
                  {ago(a.created_at)}
                </td>
                <td>
                  <Badge value={a.actor} tone={a.actor.startsWith("system") ? "info" : "accent"} />
                </td>
                <td className="mono">{a.action}</td>
                <td className="muted truncate" style={{ maxWidth: 380 }} title={JSON.stringify(a.details)}>
                  {a.target} {Object.keys(a.details).length > 0 && JSON.stringify(a.details).slice(0, 120)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <>
      <PageHead title="Settings" sub="Providers, versioned runtime configuration and the audit trail" />
      <div className="grid k2">
        <Providers />
        <Audit />
      </div>
      <div className="mt" />
      <ConfigEditor />
    </>
  );
}
