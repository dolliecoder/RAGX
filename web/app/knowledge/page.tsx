"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { useKB } from "@/components/shell";
import { Badge, Empty, ErrorBox, PageHead, Spinner, ago, num } from "@/components/ui";
import { api, post } from "@/lib/api";
import { useFetch, useJob } from "@/lib/hooks";
import type { Diagnosis, Doc, KB, Source } from "@/lib/types";

function CreateKB() {
  const { refresh, setKb } = useKB();
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  return (
    <form
      className="row"
      onSubmit={async (e) => {
        e.preventDefault();
        setErr(null);
        try {
          const kb = await post<KB>("/kbs", { name: name.trim() });
          setName("");
          await refresh();
          setKb(kb.id);
        } catch (ex) {
          setErr((ex as Error).message);
        }
      }}
    >
      <input placeholder="New knowledge base name" value={name} onChange={(e) => setName(e.target.value)} aria-label="knowledge base name" />
      <button className="primary" disabled={!name.trim()}>
        Create
      </button>
      <ErrorBox error={err} />
    </form>
  );
}

function Sources({ kb, onChange }: { kb: KB; onChange: () => void }) {
  const srcs = useFetch<Source[]>(`/kbs/${kb.id}/sources`);
  const [kind, setKind] = useState<"directory" | "file" | "url">("directory");
  const [uri, setUri] = useState("");
  const [authority, setAuthority] = useState(1);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const { track, running } = useJob((j) => {
    const r = j.result as { counts?: Record<string, number>; errors?: { uri: string; error: string }[]; error?: string };
    setResult(
      j.status === "failed"
        ? `failed: ${j.error?.split("\n")[0]}`
        : r.error
          ? `error: ${r.error}`
          : `${Object.entries(r.counts ?? {}).map(([k, v]) => `${v} ${k}`).join(", ") || "nothing new"}${r.errors?.length ? ` · ${r.errors.length} errors` : ""}`,
    );
    void srcs.reload();
    onChange();
  });

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setResult(null);
    try {
      const r = await post<{ job_id: string }>(`/kbs/${kb.id}/sources`, { kind, uri: uri.trim(), authority });
      setUri("");
      track(r.job_id);
      void srcs.reload();
    } catch (ex) {
      setErr((ex as Error).message);
    }
  };

  const crawl = async (s: Source) => {
    setResult(null);
    const r = await post<{ job_id: string }>(`/sources/${s.id}/crawl?force=true`);
    track(r.job_id);
  };

  const remove = async (s: Source) => {
    if (!window.confirm(`Remove source ${s.uri} and its documents from the index?`)) return;
    await api(`/sources/${s.id}`, { method: "DELETE" });
    void srcs.reload();
    onChange();
  };

  return (
    <div className="card">
      <h2>Sources</h2>
      <form className="stack" onSubmit={add}>
        <div className="row">
          <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)} aria-label="source kind">
            <option value="directory">folder (server path)</option>
            <option value="file">file (server path)</option>
            <option value="url">web page URL</option>
          </select>
          <label className="row small muted">
            authority
            <input type="number" min={0.1} max={2} step={0.1} value={authority} onChange={(e) => setAuthority(Number(e.target.value))} style={{ width: 70 }} />
          </label>
        </div>
        <div className="row" style={{ flexWrap: "nowrap" }}>
          <input className="grow" placeholder={kind === "url" ? "https://…" : "D:\docs or /docs"} value={uri} onChange={(e) => setUri(e.target.value)} aria-label="source location" />
          <button className="primary" disabled={!uri.trim() || running}>
            Add &amp; crawl
          </button>
        </div>
      </form>
      <ErrorBox error={err} />
      {running && (
        <div className="row small muted mt">
          <Spinner /> crawling and indexing (parse → chunk → contextualize → embed)…
        </div>
      )}
      {result && <div className="note-box small mt">{result}</div>}
      {srcs.data && srcs.data.length > 0 && (
        <div className="stack mt">
          {srcs.data.map((s) => (
            <div key={s.id} className="spread" style={{ borderTop: "1px solid var(--border)", paddingTop: 8, flexWrap: "nowrap" }}>
              <div className="grow">
                <div className="row">
                  <Badge value={s.kind} />
                  <Badge value={s.status} />
                  <span className="faint small">crawled {ago(s.last_crawled_at)}</span>
                </div>
                <div className="mono small truncate" title={s.uri}>
                  {s.uri}
                </div>
                {s.last_error && <div className="small" style={{ color: "var(--bad)" }}>{s.last_error}</div>}
              </div>
              <div className="row" style={{ flexWrap: "nowrap" }}>
                <button className="sm" onClick={() => void crawl(s)} disabled={running}>
                  Re-crawl
                </button>
                <button className="sm danger" onClick={() => void remove(s)}>
                  Remove
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Upload({ kb, onChange }: { kb: KB; onChange: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { track, running } = useJob((j) => {
    const r = j.result as { counts?: Record<string, number>; errors?: { uri: string; error: string }[] };
    setMsg(j.status === "done" ? `Indexed: ${Object.entries(r.counts ?? {}).map(([k, v]) => `${v} ${k}`).join(", ")}${r.errors?.length ? ` · errors: ${r.errors.map((e) => e.error).join("; ")}` : ""}` : `Failed: ${j.error}`);
    onChange();
  });

  const send = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setErr(null);
    setMsg(null);
    const fd = new FormData();
    Array.from(files).forEach((f) => fd.append("files", f));
    try {
      const r = await api<{ saved: string[]; rejected: { file: string; reason: string }[]; job_id: string | null }>(`/kbs/${kb.id}/upload`, { method: "POST", body: fd });
      if (r.rejected.length) setErr(r.rejected.map((x) => `${x.file}: ${x.reason}`).join("; "));
      if (r.job_id) track(r.job_id);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <div
      className="card"
      onDragOver={(e) => {
        e.preventDefault();
        setDrag(true);
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDrag(false);
        void send(e.dataTransfer.files);
      }}
      style={{ borderStyle: "dashed", borderColor: drag ? "var(--accent)" : undefined, textAlign: "center" }}
    >
      <p className="muted">Drop PDF, DOCX, HTML, Markdown or text files here</p>
      <input ref={input} type="file" multiple accept=".pdf,.docx,.html,.htm,.md,.markdown,.txt" hidden onChange={(e) => void send(e.target.files)} />
      <button onClick={() => input.current?.click()} disabled={running}>
        {running ? (
          <>
            <Spinner /> indexing…
          </>
        ) : (
          "Choose files"
        )}
      </button>
      <ErrorBox error={err} />
      {msg && <div className="note-box small mt">{msg}</div>}
    </div>
  );
}

function Documents({ kb, version }: { kb: KB; version: number }) {
  const docs = useFetch<Doc[]>(`/kbs/${kb.id}/documents`, [version]);
  const [filter, setFilter] = useState("");
  const act = async (d: Doc, action: string, body?: unknown) => {
    await post(`/documents/${d.id}/${action}`, body);
    void docs.reload();
  };
  const rows = (docs.data ?? []).filter((d) => !filter || d.title.toLowerCase().includes(filter.toLowerCase()) || d.uri.toLowerCase().includes(filter.toLowerCase()));
  return (
    <div className="card">
      <div className="spread">
        <h2>Documents {docs.data && <span className="faint">({docs.data.length})</span>}</h2>
        <input placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="filter documents" />
      </div>
      <ErrorBox error={docs.error} />
      {docs.data && rows.length === 0 && <Empty>No documents yet. Add a source or upload files.</Empty>}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table className="t">
            <thead>
              <tr>
                <th>Title</th>
                <th>Status</th>
                <th>Tier</th>
                <th className="right">Ver</th>
                <th className="right">Chunks</th>
                <th className="right">Quality</th>
                <th>Indexed</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((d) => (
                <tr key={d.id}>
                  <td>
                    <Link href={`/knowledge/${d.id}`}>{d.title || d.uri}</Link>
                    <div className="faint small truncate" style={{ maxWidth: 360 }}>
                      {d.uri}
                    </div>
                    {d.error && <div className="small" style={{ color: "var(--bad)" }}>{d.error}</div>}
                    {(d.quality.injection_chunks ?? 0) > 0 && <Badge value="injection suspect" tone="bad" />}
                  </td>
                  <td>
                    <Badge value={d.status} />
                  </td>
                  <td>
                    <select value={d.tier} onChange={(e) => void act(d, "update", { tier: e.target.value })} aria-label="tier">
                      <option value="hot">hot</option>
                      <option value="warm">warm</option>
                      <option value="cold">cold</option>
                    </select>
                  </td>
                  <td className="right">{d.version}</td>
                  <td className="right">{d.chunks}</td>
                  <td className="right">{num(d.quality.score)}</td>
                  <td className="faint small">{ago(d.last_ingested_at)}</td>
                  <td className="right">
                    <div className="row" style={{ justifyContent: "flex-end" }}>
                      {d.status === "quarantined" ? (
                        <button className="sm" onClick={() => void act(d, "unquarantine")}>
                          Restore
                        </button>
                      ) : (
                        <button className="sm" onClick={() => void act(d, "quarantine")} title="exclude from normal retrieval">
                          Quarantine
                        </button>
                      )}
                      <button className="sm" onClick={() => void act(d, "reingest")}>
                        Re-ingest
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

function ContentGaps({ kb }: { kb: KB }) {
  const gaps = useFetch<Diagnosis[]>(`/kbs/${kb.id}/diagnoses?status=open,fixing,needs_human`);
  const list = (gaps.data ?? []).filter((d) => d.root_cause === "missing_content" || d.root_cause === "stale_content");
  if (list.length === 0) return null;
  return (
    <div className="card">
      <h2>Content gaps &amp; stale sources</h2>
      <p className="muted small">Questions users asked that no document answers. Add a source that covers them, or dismiss.</p>
      <ul>
        {list.map((d) => (
          <li key={d.id}>
            <Badge value={d.root_cause.replace("_content", "")} tone={d.root_cause === "missing_content" ? "warn" : "info"} /> {d.summary}{" "}
            <span className="faint small">(asked {d.trace_ids.length}×)</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function KnowledgePage() {
  const { kb, kbs, refresh } = useKB();
  const [version, setVersion] = useState(0);
  const changed = () => {
    setVersion((v) => v + 1);
    void refresh();
  };
  return (
    <>
      <PageHead title="Knowledge" sub={kb ? `${kb.name}: ${kb.documents} documents · ${kb.tokens.toLocaleString()} tokens` : "Create a knowledge base to begin"}>
        {kb && (
          <label className="row small muted">
            Visible to
            <select
              value={kb.visibility}
              onChange={async (e) => {
                await api(`/kbs/${kb.id}`, { method: "PATCH", json: { visibility: e.target.value } });
                void refresh();
              }}
              aria-label="who can ask this knowledge base"
            >
              <option value="all">everyone signed in</option>
              <option value="admins">admins only (draft)</option>
            </select>
          </label>
        )}
        <CreateKB />
      </PageHead>
      {kb ? (
        <>
          <div className="grid k2">
            <Sources kb={kb} onChange={changed} />
            <Upload kb={kb} onChange={changed} />
          </div>
          <div className="mt" />
          <ContentGaps kb={kb} />
          <Documents kb={kb} version={version} />
        </>
      ) : (
        kbs.length === 0 && <Empty>No knowledge bases yet.</Empty>
      )}
    </>
  );
}
