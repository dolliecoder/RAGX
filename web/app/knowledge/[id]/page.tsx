"use client";

import Link from "next/link";
import { use, useState } from "react";
import { Badge, Empty, ErrorBox, Json, PageHead, Spinner, ago, num } from "@/components/ui";
import { useFetch } from "@/lib/hooks";
import type { ChunkRow, Doc } from "@/lib/types";

export default function DocPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data: d, error, loading } = useFetch<Doc & { chunk_list: ChunkRow[] }>(`/documents/${id}`);
  const [show, setShow] = useState<"active" | "all">("active");
  if (error) return <ErrorBox error={error} />;
  if (!d) return loading ? <Spinner /> : <Empty>Not found</Empty>;
  const chunks = d.chunk_list.filter((c) => show === "all" || c.status === "active");
  return (
    <>
      <PageHead title={d.title || "Document"} sub={<span className="mono small">{d.uri}</span>}>
        <Link href="/knowledge">← knowledge</Link>
      </PageHead>
      <div className="grid k2">
        <div className="card">
          <dl className="kv">
            <dt>Status</dt>
            <dd>
              <Badge value={d.status} />
            </dd>
            <dt>Version</dt>
            <dd>v{d.version}</dd>
            <dt>Tier</dt>
            <dd>{d.tier}</dd>
            <dt>Authority</dt>
            <dd>{d.authority}</dd>
            <dt>Type</dt>
            <dd>{d.mime}</dd>
            <dt>Tokens</dt>
            <dd>{d.token_count.toLocaleString()}</dd>
            <dt>Modified</dt>
            <dd>{ago(d.modified_at)}</dd>
            <dt>Indexed</dt>
            <dd>{ago(d.last_ingested_at)}</dd>
            <dt>Next crawl</dt>
            <dd>{d.next_crawl_at ? new Date(d.next_crawl_at).toLocaleString() : "–"}</dd>
          </dl>
        </div>
        <div className="card">
          <h3>Quality signals</h3>
          <dl className="kv">
            <dt>Score</dt>
            <dd>{num(d.quality.score)}</dd>
            <dt>Freshness</dt>
            <dd>{num(d.quality.freshness)}</dd>
            <dt>Parse quality</dt>
            <dd>{num(d.quality.parse_quality)}</dd>
            <dt>Injection chunks</dt>
            <dd>{d.quality.injection_chunks ?? 0}</dd>
          </dl>
          <h3 className="mt">Chunking strategy</h3>
          <Json value={d.chunk_strategy} />
        </div>
      </div>
      <div className="card mt">
        <div className="spread">
          <h2>Chunks ({chunks.length})</h2>
          <select value={show} onChange={(e) => setShow(e.target.value as "active" | "all")} aria-label="chunk filter">
            <option value="active">live version</option>
            <option value="all">all versions (staged / retired)</option>
          </select>
        </div>
        <div className="stack">
          {chunks.map((c) => (
            <div key={c.id} className="card" style={{ background: "var(--panel-2)" }}>
              <div className="spread small">
                <span>
                  <b>#{c.ord}</b> <span className="faint">{c.section}</span> {c.page && <span className="faint">· p.{c.page}</span>}
                </span>
                <span className="row">
                  {c.flags.map((f) => (
                    <Badge key={f} value={f} tone="bad" />
                  ))}
                  <Badge value={c.status} /> <span className="faint">v{c.version} · {c.tokens} tok</span>
                </span>
              </div>
              <div className="small" style={{ color: "var(--info)", margin: "6px 0" }}>
                <b>context:</b> {c.context}
                {c.aliases.length > 0 && <div><b>aliases:</b> {c.aliases.join(", ")}</div>}
              </div>
              <pre className="small">{c.text}</pre>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}
