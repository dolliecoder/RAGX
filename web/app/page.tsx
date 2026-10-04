"use client";

import { useEffect, useRef, useState } from "react";
import { AnswerView } from "@/components/answer";
import { NeedKB } from "@/components/shell";
import { ErrorBox, PageHead, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import type { Answer, Job, KB } from "@/lib/types";

type Msg =
  | { id: number; role: "user"; text: string }
  | { id: number; role: "assistant"; answer?: Answer; error?: string; pending?: boolean; deep?: { jobId: string; status: string } };

let nextId = 1;

const EXAMPLES = ["What is our refund policy?", "Compare the Pro and Enterprise plans", "What does error ERR-4029 mean?"];

function Chat({ kb }: { kb: KB }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [mode, setMode] = useState("auto");
  const [busy, setBusy] = useState(false);
  const [sessionId] = useState(() => (typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : String(Date.now())));
  const end = useRef<HTMLDivElement>(null);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    setMsgs([]);
  }, [kb.id]);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [msgs]);

  const updateAssistant = (id: number, patch: (x: Extract<Msg, { role: "assistant" }>) => Msg) =>
    setMsgs((m) => m.map((x) => (x.id === id && x.role === "assistant" ? patch(x) : x)));

  /** Deep research runs as a background job; its verified report replaces the interim answer. */
  const pollDeep = (id: number, jobId: string) => {
    const tick = async () => {
      if (!alive.current) return;
      try {
        const j = await api<Job>(`/jobs/${jobId}`);
        if (j.status === "done") {
          const report = j.result as unknown as Partial<Answer>;
          updateAssistant(id, (x) => ({
            ...x,
            deep: { jobId, status: "done" },
            answer: { ...(x.answer as Answer), ...report, route: "deep", job_id: jobId },
          }));
          return;
        }
        updateAssistant(id, (x) => ({ ...x, deep: { jobId, status: j.status } }));
        if (j.status === "failed") return;
      } catch {
        /* transient: retry */
      }
      setTimeout(tick, 2500);
    };
    void tick();
  };

  const send = async (q: string) => {
    const query = q.trim();
    if (!query || busy) return;
    setText("");
    setBusy(true);
    const userId = nextId++;
    const id = nextId++;
    setMsgs((m) => [...m, { id: userId, role: "user", text: query }, { id, role: "assistant", pending: true }]);
    try {
      const a = await api<Answer>(`/kbs/${kb.id}/query`, { method: "POST", json: { query, mode, session_id: sessionId } });
      setMsgs((m) => m.map((x) => (x.id === id ? { id, role: "assistant", answer: a, deep: a.job_id ? { jobId: a.job_id, status: "queued" } : undefined } : x)));
      if (a.job_id) pollDeep(id, a.job_id);
    } catch (e) {
      setMsgs((m) => m.map((x) => (x.id === id ? { id, role: "assistant", error: (e as Error).message } : x)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHead title="Ask" sub={`Verified, cited answers from “${kb.name}” (${kb.documents} documents)`} />
      <div className="chat">
        {msgs.length === 0 && (
          <div className="card empty">
            <p>Every answer is retrieved, graded, generated with citations and checked by an independent verifier.</p>
            <div className="row" style={{ justifyContent: "center" }}>
              {EXAMPLES.map((e) => (
                <button key={e} className="sm" onClick={() => void send(e)}>
                  {e}
                </button>
              ))}
            </div>
          </div>
        )}
        {msgs.map((m) =>
          m.role === "user" ? (
            <div key={m.id} className="msg-user">
              {m.text}
            </div>
          ) : (
            <div key={m.id}>
              {m.pending && (
                <div className="card row muted">
                  <Spinner /> retrieving, grading, generating and verifying…
                </div>
              )}
              {m.error && <ErrorBox error={m.error} />}
              {m.answer && <AnswerView a={m.answer} />}
              {m.deep && m.deep.status !== "done" && (
                <div className="card row small" style={{ marginTop: 8 }}>
                  {m.deep.status === "failed" ? (
                    <span className="error-box">Deep research job failed.</span>
                  ) : (
                    <>
                      <Spinner /> Deep research running in the background (job {m.deep.jobId.slice(0, 8)}). The verified report will appear here.
                    </>
                  )}
                </div>
              )}
            </div>
          ),
        )}
        <div ref={end} />
      </div>
      <div className="composer">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void send(text);
          }}
        >
          <textarea
            value={text}
            placeholder="Ask a question about your documents…"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send(text);
              }
            }}
            aria-label="Question"
          />
          <select value={mode} onChange={(e) => setMode(e.target.value)} aria-label="Mode" title="auto routes by complexity">
            <option value="auto">auto</option>
            <option value="fast">fast</option>
            <option value="standard">standard</option>
            <option value="deep">deep research</option>
          </select>
          <button className="primary" type="submit" disabled={busy || !text.trim()}>
            {busy ? <Spinner /> : "Ask"}
          </button>
        </form>
      </div>
    </>
  );
}

export default function AskPage() {
  return <NeedKB>{(kb) => <Chat kb={kb} />}</NeedKB>;
}
