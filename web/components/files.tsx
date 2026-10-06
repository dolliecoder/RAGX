"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { Doc, Job, KB } from "@/lib/types";
import { Icon } from "./icons";

export type UploadItem = { id: number; name: string; state: "uploading" | "reading" | "ready" | "rejected" | "error"; reason?: string };
export type Uploads = { items: UploadItem[]; busy: boolean; upload: (files: File[]) => Promise<void>; clear: () => void; version: number };

export const ACCEPT = ".pdf,.docx,.md,.markdown,.txt,.html,.htm";
let seq = 1;

/** Upload files into a workspace and follow the indexing job until they can be asked about. */
export function useUploads(kb: KB | null, onChange: () => void): Uploads {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [version, setVersion] = useState(0); // bumps when the file list changed
  const kbId = kb?.id;
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => setItems([]), [kbId]);

  const patch = (ids: Set<number>, fn: (i: UploadItem) => UploadItem) => setItems((cur) => cur.map((i) => (ids.has(i.id) ? fn(i) : i)));

  const upload = useCallback(
    async (files: File[]) => {
      if (!kbId || !files.length) return;
      const batch: UploadItem[] = files.map((f) => ({ id: seq++, name: f.name, state: "uploading" }));
      const ids = new Set(batch.map((b) => b.id));
      setItems((cur) => [...cur.filter((i) => i.state === "uploading" || i.state === "reading"), ...batch]);
      const form = new FormData();
      files.forEach((f) => form.append("files", f));
      try {
        const r = await api<{ saved: string[]; rejected: { file: string; reason: string }[]; job_id: string | null }>(`/kbs/${kbId}/upload`, {
          method: "POST",
          body: form,
        });
        const rejected = new Map(r.rejected.map((x) => [x.file, x.reason]));
        patch(ids, (i) => (rejected.has(i.name) ? { ...i, state: "rejected", reason: rejected.get(i.name) } : { ...i, state: r.job_id ? "reading" : "ready" }));
        if (!r.job_id) return;
        // indexing runs in the background: wait until the new files can be searched
        for (let n = 0; n < 400 && alive.current; n++) {
          await new Promise((res) => setTimeout(res, n < 10 ? 1000 : 2500));
          const j = await api<Job>(`/jobs/${r.job_id}`).catch(() => null);
          if (!j || (j.status !== "done" && j.status !== "failed")) continue;
          patch(ids, (i) =>
            i.state !== "reading" ? i : j.status === "done" ? { ...i, state: "ready" } : { ...i, state: "error", reason: j.error ?? "Couldn't read this file." },
          );
          break;
        }
      } catch (e) {
        patch(ids, (i) => ({ ...i, state: "error", reason: (e as Error).message }));
      } finally {
        if (alive.current) {
          setVersion((v) => v + 1);
          onChange();
        }
      }
    },
    [kbId, onChange],
  );

  const clear = () => setItems((cur) => cur.filter((i) => i.state === "uploading" || i.state === "reading"));
  return { items, busy: items.some((i) => i.state === "uploading" || i.state === "reading"), upload, clear, version };
}

const STATE_LABEL: Record<UploadItem["state"], string> = {
  uploading: "Uploading…",
  reading: "Reading…",
  ready: "Added",
  rejected: "Not added",
  error: "Failed",
};

export function UploadChips({ uploads }: { uploads: Uploads }) {
  if (!uploads.items.length) return null;
  return (
    <div className="upload-chips">
      {uploads.items.map((i) => (
        <span key={i.id} className={`upload-chip ${i.state}`} title={i.reason ?? STATE_LABEL[i.state]}>
          {i.state === "uploading" || i.state === "reading" ? <span className="spinner" /> : <Icon name={i.state === "ready" ? "check" : "close"} size={14} />}
          <span className="truncate">{i.name}</span>
          <span className="faint">{i.reason ?? STATE_LABEL[i.state]}</span>
        </span>
      ))}
      {!uploads.busy && (
        <button type="button" className="icon-btn" onClick={uploads.clear} aria-label="Dismiss" title="Dismiss">
          <Icon name="close" size={14} />
        </button>
      )}
    </div>
  );
}

function pages(tokens: number) {
  const n = Math.round(tokens / 500);
  return n < 1 ? "under 1 page" : `~${n} page${n === 1 ? "" : "s"}`;
}

export function FilesPanel({
  kb,
  uploads,
  onClose,
  onChanged,
  onDeleted,
  isAdmin,
}: {
  kb: KB;
  uploads: Uploads;
  onClose: () => void;
  onChanged: () => Promise<void>;
  onDeleted: () => void;
  isAdmin: boolean;
}) {
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [name, setName] = useState(kb.name);
  const [renaming, setRenaming] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);

  const load = useCallback(async () => {
    try {
      setDocs(await api<Doc[]>(`/kbs/${kb.id}/documents`));
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [kb.id]);

  useEffect(() => {
    void load();
  }, [load, uploads.version]);
  useEffect(() => setName(kb.name), [kb.name]);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose]);

  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const removeDoc = (d: Doc) =>
    act(async () => {
      if (!window.confirm(`Remove “${d.title}” from this workspace?`)) return;
      await api(`/documents/${d.id}`, { method: "DELETE" });
      await load();
      await onChanged();
    });

  const rename = () =>
    act(async () => {
      const n = name.trim();
      if (n && n !== kb.name) {
        await api(`/kbs/${kb.id}`, { method: "PATCH", json: { name: n } });
        await onChanged();
      }
      setRenaming(false);
    });

  const toggleGeneral = () =>
    act(async () => {
      await api(`/kbs/${kb.id}`, { method: "PATCH", json: { general_knowledge: !kb.general_knowledge } });
      await onChanged();
    });

  const removeWorkspace = () =>
    act(async () => {
      if (!window.confirm(`Delete the workspace “${kb.name}” with all its files and chats? This can't be undone.`)) return;
      await api(`/kbs/${kb.id}`, { method: "DELETE" });
      onDeleted();
    });

  const live = (docs ?? []).filter((d) => d.status !== "superseded");
  const lim = kb.limits;

  return (
    <aside className="files-panel" aria-label="Files in this workspace">
      <div className="files-head">
        {renaming ? (
          <form
            className="row grow"
            onSubmit={(e) => {
              e.preventDefault();
              void rename();
            }}
          >
            <input className="grow" value={name} onChange={(e) => setName(e.target.value)} autoFocus aria-label="Workspace name" maxLength={200} />
            <button className="sm primary" type="submit">
              Save
            </button>
          </form>
        ) : (
          <div className="grow" style={{ minWidth: 0 }}>
            <div className="files-title truncate">{kb.name}</div>
            <div className="faint small">{kb.shared ? "Shared with everyone" : "Private workspace"}</div>
          </div>
        )}
        {kb.can_edit && !renaming && (
          <button className="icon-btn" onClick={() => setRenaming(true)} title="Rename" aria-label="Rename workspace">
            <Icon name="edit" size={16} />
          </button>
        )}
        <button className="icon-btn" onClick={onClose} aria-label="Close files">
          <Icon name="close" />
        </button>
      </div>

      <div className="files-body">
        {lim && (
          <div className="usage files-usage">
            <div className="spread">
              <span>
                {live.length} of {lim.files || "∞"} files
              </span>
              <span>
                {kb.documents > 0 && kb.pages < 1 ? "<1" : `~${kb.pages}`} of {lim.pages || "∞"} pages
              </span>
            </div>
            {lim.pages > 0 && (
              <div className="bar">
                <span style={{ width: `${Math.min(100, (kb.pages / lim.pages) * 100)}%` }} />
              </div>
            )}
          </div>
        )}

        {kb.can_edit && (
          <div
            className={`dropzone${drag ? " over" : ""}`}
            onClick={() => input.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              setDrag(true);
            }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDrag(false);
              void uploads.upload([...e.dataTransfer.files]);
            }}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") input.current?.click();
            }}
          >
            <Icon name="plus" />
            <div>
              <b>Add files</b>
              <div className="faint small">PDF, Word, Markdown, HTML or text{lim?.max_file_mb ? `, up to ${lim.max_file_mb} MB each` : ""}</div>
            </div>
            <input
              ref={input}
              type="file"
              multiple
              accept={ACCEPT}
              hidden
              onChange={(e) => {
                void uploads.upload([...(e.target.files ?? [])]);
                e.target.value = "";
              }}
            />
          </div>
        )}
        <UploadChips uploads={uploads} />
        {err && <div className="error-box small">{err}</div>}

        <div className="file-list">
          {docs === null ? (
            <span className="spinner" />
          ) : live.length === 0 ? (
            <div className="side-empty">{kb.can_edit ? "No files yet. Add your notes, slides or books." : "No files yet."}</div>
          ) : (
            live.map((d) => (
              <div key={d.id} className="file-row">
                <Icon name="doc" size={18} />
                <div className="grow" style={{ minWidth: 0 }}>
                  <div className="truncate" title={d.title}>
                    {d.title}
                  </div>
                  <div className="faint small">
                    {d.status === "active" ? pages(d.token_count) : d.status === "error" ? d.error || "Couldn't read this file" : d.status}
                  </div>
                </div>
                {kb.can_edit && (
                  <button className="icon-btn" onClick={() => void removeDoc(d)} title="Remove file" aria-label={`Remove ${d.title}`}>
                    <Icon name="trash" size={15} />
                  </button>
                )}
              </div>
            ))
          )}
        </div>

        {kb.can_edit && (
          <label className="switch-row">
            <input type="checkbox" checked={kb.general_knowledge} onChange={() => void toggleGeneral()} />
            <span>
              <b>Answer beyond these files</b>
              <span className="faint small">
                When your files don&apos;t cover a question, answer from general knowledge, clearly marked as not from your files.
              </span>
            </span>
          </label>
        )}

        {(kb.can_edit || isAdmin) && (
          <div className="files-foot">
            {isAdmin && kb.shared && (
              <Link href="/knowledge" className="small">
                Advanced: web sources, folders and quality →
              </Link>
            )}
            {kb.can_edit && (
              <button className="sm danger ghost" onClick={() => void removeWorkspace()}>
                <Icon name="trash" size={14} /> Delete workspace
              </button>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
