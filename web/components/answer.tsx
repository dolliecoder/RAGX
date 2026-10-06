"use client";

import Link from "next/link";
import { useState } from "react";
import { api, post } from "@/lib/api";
import type { Answer, Recheck } from "@/lib/types";
import { Icon } from "./icons";
import { Markdown, StructuredAnswer } from "./rich";
import { Badge, pct } from "./ui";

const STATUS_LABEL: Record<string, [string, string]> = {
  verified: ["Verified against sources", "ok"],
  partial: ["Partly supported", "warn"],
  failed: ["Not supported by sources", "bad"],
  error: ["Something went wrong", "bad"],
  direct: ["Chat reply", ""],
  escalated: ["Deep research", "violet"],
};

const VERDICT_TONE: Record<Recheck["verdict"], string> = {
  stands: "ok",
  stands_correction_unsupported: "ok",
  you_were_right: "info",
  fixed: "info",
  both: "info",
  unclear: "warn",
  not_checkable: "warn",
  error: "bad",
};

const VERDICT_TITLE: Record<Recheck["verdict"], string> = {
  stands: "Re-checked: the answer stands",
  stands_correction_unsupported: "Re-checked: the answer stands",
  you_were_right: "Re-checked: you were right",
  fixed: "Re-checked: answer corrected",
  both: "Re-checked: both are supported",
  unclear: "Re-checked: not settled by the documents",
  not_checkable: "Flagged for review",
  error: "Couldn't re-check right now",
};

function traceToAnswer(t: Record<string, unknown>): Answer {
  const d = (t.data ?? {}) as Record<string, unknown>;
  return {
    status: t.status,
    answer: t.answer,
    sentences: d.sentences ?? [],
    citations: d.citations ?? [],
    unanswered: d.unanswered ?? [],
    conflicts: d.conflicts ?? [],
    route: t.route,
    metrics: d.metrics ?? {},
    job_id: null,
    trace_id: t.id,
    config_version: t.config_version,
    llm: d.llm ?? { calls: t.llm_calls, tokens_in: 0, tokens_out: 0, by_task: {} },
    latency_ms: t.latency_ms,
    healed: t.healed,
    offline: false,
  } as Answer;
}

function RecheckResult({ rc }: { rc: Recheck }) {
  const tone = VERDICT_TONE[rc.verdict] ?? "";
  return (
    <div className={`recheck ${tone}`}>
      <div className="recheck-head">
        <Icon name={tone === "ok" ? "check" : tone === "bad" ? "close" : "doc"} size={16} />
        <b>{VERDICT_TITLE[rc.verdict] ?? "Re-checked"}</b>
      </div>
      <div>{rc.message}</div>
      {(rc.original || rc.correction) && (
        <div className="recheck-facts">
          {rc.original && (
            <span>
              Original answer: <b>{rc.original.supported ? "supported" : "not supported"}</b> ({pct(rc.original.grounded)} of claims found)
            </span>
          )}
          {rc.correction && (
            <span>
              Your correction: <b>{rc.correction.supported ? "supported" : "not supported"}</b> ({pct(rc.correction.grounded)} of claims found)
            </span>
          )}
        </div>
      )}
      {rc.updated && (
        <div className="recheck-updated">
          <div className="sources-label">Corrected answer</div>
          <AnswerView a={rc.updated} />
        </div>
      )}
    </div>
  );
}

export function AnswerView({ a }: { a: Answer }) {
  const [hl, setHl] = useState<number | null>(null);
  const [fb, setFb] = useState<"none" | "up" | "down-open" | "checking" | "down-sent">(a.recheck ? "down-sent" : "none");
  const [correction, setCorrection] = useState("");
  const [comment, setComment] = useState("");
  const [copied, setCopied] = useState(false);
  const [recheck, setRecheck] = useState<Recheck | null>(a.recheck ?? null);

  /** Thumbs-down: the server re-checks the answer (and any correction) against the documents. */
  const report = async () => {
    if (!a.trace_id) return;
    setFb("checking");
    try {
      const r = await post<{ recheck?: Recheck }>(`/traces/${a.trace_id}/feedback`, { rating: -1, kind: "explicit", correction, comment });
      let rc = r.recheck ?? null;
      if (rc?.updated_trace_id && !rc.updated) {
        const t = await api<{ data?: Record<string, unknown> } & Record<string, unknown>>(`/traces/${rc.updated_trace_id}`).catch(() => null);
        if (t) rc = { ...rc, updated: traceToAnswer(t) };
      }
      setRecheck(rc);
    } catch (e) {
      setRecheck({ verdict: "error", message: (e as Error).message, original: null, correction: null, updated_trace_id: null });
    }
    setFb("down-sent");
  };

  const sendFeedback = async (rating: number, kind = "explicit") => {
    if (!a.trace_id) return;
    try {
      await post(`/traces/${a.trace_id}/feedback`, { rating, kind, correction, comment });
    } catch {
      /* feedback is best effort */
    }
  };

  const copy = async () => {
    try {
      const outsideText = a.general ? `\n\n---\nNot from your files (general knowledge):\n\n${a.general}` : "";
      await navigator.clipboard.writeText((a.general && !(a.sentences?.length > 0) ? "" : a.answer) + outsideText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
      void sendFeedback(0, "copy");
    } catch {
      /* clipboard unavailable */
    }
  };

  const m = a.metrics ?? {};
  const hasSpans = a.sentences && a.sentences.length > 0;
  // nothing usable in the files, but a general-knowledge answer was given
  const outside = !!a.general && !hasSpans;
  const [statusText, tone] = outside ? ["Not in your files", "warn"] : (STATUS_LABEL[a.status] ?? [a.status, ""]);
  const checked = !outside && m.groundedness !== undefined && m.groundedness !== null;

  return (
    <div className="answer">
      {!outside && (
      <div className="answer-text">
        {hasSpans ? (
          <StructuredAnswer
            spans={a.sentences}
            cite={(n) => (
              <a
                key={n}
                className="cite-mark"
                href={`#cite-${a.trace_id}-${n}`}
                onMouseEnter={() => setHl(n)}
                onMouseLeave={() => setHl(null)}
                onFocus={() => setHl(n)}
                onBlur={() => setHl(null)}
                aria-label={`source ${n}`}
                title={a.citations.find((c) => c.n === n)?.title}
              >
                {n}
              </a>
            )}
          />
        ) : (
          <Markdown text={a.answer} />
        )}
      </div>
      )}

      {hasSpans && ((a.unanswered.length > 0 && a.status !== "verified" && !a.general) || a.conflicts.length > 0) && (
        <div className="note-box small">
          {a.unanswered.length > 0 && a.status !== "verified" && !a.general && <div>Not found in the documents: {a.unanswered.join("; ")}</div>}
          {a.conflicts.length > 0 && <div>Sources disagree: {a.conflicts.join("; ")}</div>}
        </div>
      )}

      {a.general && (
        <div className="general">
          <div className="general-head">
            <Icon name="globe" size={16} />
            <span>
              <b>{outside ? "Not from your files." : "Not from your files: the rest of the answer."}</b> This part comes from general knowledge, so it
              isn&apos;t checked against your documents. Double-check anything important.
            </span>
          </div>
          <div className="answer-text">
            <Markdown text={a.general} />
          </div>
        </div>
      )}

      {a.citations.length > 0 && (
        <div className="sources">
          <div className="sources-label">Sources</div>
          <div className="sources-grid">
            {a.citations.map((c) => (
              <div key={c.n} id={`cite-${a.trace_id}-${c.n}`} className={`source${hl === c.n ? " hl" : ""}`}>
                <div className="source-head">
                  <span className="source-n">{c.n}</span>
                  <span className="grow truncate" title={c.title || c.url || ""}>
                    {c.url ? (
                      <a href={c.url} target="_blank" rel="noreferrer" onClick={() => sendFeedback(0, "citation_click")}>
                        {c.title || c.url}
                      </a>
                    ) : c.doc_id ? (
                      <Link href={`/knowledge/${c.doc_id}`} onClick={() => sendFeedback(0, "citation_click")}>
                        {c.title}
                      </Link>
                    ) : (
                      c.title
                    )}
                  </span>
                  {c.page && <span className="faint">p.{c.page}</span>}
                </div>
                {c.section && c.section !== c.title && <div className="source-section truncate">{c.section}</div>}
                <div className="source-quote">“{c.cited_text}”</div>
                {c.origin !== "initial" && c.origin !== "long_context" && <Badge value={`via ${c.origin}`} tone="info" />}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="answer-foot">
        {a.trace_id && (
          <div className="row" style={{ gap: 2 }}>
            <button className="icon-btn" onClick={copy} title={copied ? "Copied" : "Copy"} aria-label="Copy answer">
              <Icon name={copied ? "check" : "copy"} size={16} />
            </button>
            <button
              className={`icon-btn${fb === "up" ? " on" : ""}`}
              onClick={() => {
                setFb("up");
                void sendFeedback(1);
              }}
              title="Good answer"
              aria-label="Good answer"
            >
              <Icon name="up" size={16} />
            </button>
            <button
              className={`icon-btn${fb === "down-open" || fb === "down-sent" || fb === "checking" ? " on" : ""}`}
              onClick={() => setFb(fb === "down-open" ? "none" : "down-open")}
              disabled={fb === "down-sent" || fb === "checking"}
              title={fb === "down-sent" ? "Reported" : "Wrong answer"}
              aria-label="Report a wrong answer"
            >
              <Icon name="down" size={16} />
            </button>
          </div>
        )}
        <span className={`status-chip ${tone}`}>
          {a.status === "verified" && <Icon name="check" size={13} />}
          {statusText}
        </span>
        {a.healed && <Badge value="self-healed" tone="info" />}
        {a.offline && <Badge value="offline provider" tone="warn" />}
        <span className="answer-meta">
          {checked && (
            <span title="How much of the answer the verifier found in the sources, and how much of the question it covers">
              {pct(m.groundedness)} grounded · {pct(m.coverage)} covered ·{" "}
            </span>
          )}
          {(a.latency_ms / 1000).toFixed(1)}s
          {a.trace_id && (
            <>
              {" · "}
              <Link href={`/traces/${a.trace_id}`}>details</Link>
            </>
          )}
        </span>
      </div>

      {fb === "checking" && (
        <div className="recheck info" role="status">
          <span className="spinner" /> Re-reading your question and checking the documents again…
        </div>
      )}
      {fb === "down-sent" && recheck && <RecheckResult rc={recheck} />}
      {fb === "down-open" && (
        <div className="feedback-box">
          <div className="small muted">What's wrong? RAGX will re-read the documents and check its answer, and your correction, again.</div>
          <textarea
            placeholder="What is the correct answer? (optional)"
            value={correction}
            onChange={(e) => setCorrection(e.target.value)}
            rows={2}
          />
          <input placeholder="Anything else? (optional)" value={comment} onChange={(e) => setComment(e.target.value)} />
          <div className="row">
            <button className="primary sm" onClick={() => void report()}>
              Re-check this answer
            </button>
            <button className="sm ghost" onClick={() => setFb("none")}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
