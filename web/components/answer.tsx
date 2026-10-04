"use client";

import Link from "next/link";
import { useState } from "react";
import { post } from "@/lib/api";
import type { Answer } from "@/lib/types";
import { Badge, Meter } from "./ui";

export function AnswerView({ a }: { a: Answer }) {
  const [hl, setHl] = useState<number | null>(null);
  const [fb, setFb] = useState<"none" | "up" | "down-open" | "down-sent">("none");
  const [correction, setCorrection] = useState("");
  const [comment, setComment] = useState("");
  const [copied, setCopied] = useState(false);

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
      await navigator.clipboard.writeText(a.answer);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
      void sendFeedback(0, "copy");
    } catch {
      /* clipboard unavailable */
    }
  };

  const m = a.metrics ?? {};
  const hasSpans = a.sentences && a.sentences.length > 0;

  return (
    <div className="card stack" style={{ gap: 12 }}>
      <div className="spread">
        <div className="row">
          <Badge value={a.status} />
          <span className="badge">{a.route}</span>
          {a.healed && <Badge value="healed" tone="info" />}
          {a.offline && <Badge value="offline provider" tone="warn" />}
        </div>
        {m.groundedness !== undefined && m.groundedness !== null && (
          <div className="row" style={{ gap: 16 }}>
            <Meter label="grounded" value={m.groundedness} />
            <Meter label="coverage" value={m.coverage ?? null} />
            <Meter label="confidence" value={m.confidence ?? null} />
          </div>
        )}
      </div>

      <div className="answer-text">
        {hasSpans
          ? a.sentences.map((s, i) => (
              <span key={i}>
                {s.text}
                {s.citations.map((n) => (
                  <sup key={n}>
                    <a
                      href={`#cite-${a.trace_id}-${n}`}
                      onMouseEnter={() => setHl(n)}
                      onMouseLeave={() => setHl(null)}
                      aria-label={`source ${n}`}
                    >
                      {n}
                    </a>
                  </sup>
                ))}{" "}
              </span>
            ))
          : a.answer}
      </div>

      {hasSpans && ((a.unanswered.length > 0 && a.status !== "verified") || a.conflicts.length > 0) && (
        <div className="note-box small">
          {a.unanswered.length > 0 && a.status !== "verified" && <div>Not found in the knowledge base: {a.unanswered.join("; ")}</div>}
          {a.conflicts.length > 0 && <div>Sources disagree: {a.conflicts.join("; ")}</div>}
        </div>
      )}

      {a.citations.length > 0 && (
        <div className="stack">
          {a.citations.map((c) => (
            <div key={c.n} id={`cite-${a.trace_id}-${c.n}`} className={`cite ${hl === c.n ? "hl" : ""}`}>
              <div className="spread small">
                <span>
                  <b>[{c.n}]</b>{" "}
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
                  {c.section && c.section !== c.title && <span className="faint"> · {c.section}</span>}
                  {c.page && <span className="faint"> · p.{c.page}</span>}
                </span>
                {c.origin !== "initial" && c.origin !== "long_context" && <Badge value={`via ${c.origin}`} tone="info" />}
              </div>
              <div className="muted small">“{c.cited_text}”</div>
            </div>
          ))}
        </div>
      )}

      <div className="spread small">
        <div className="row">
          {a.trace_id && (
            <>
              <button
                className={`sm ${fb === "up" ? "primary" : ""}`}
                onClick={() => {
                  setFb("up");
                  void sendFeedback(1);
                }}
              >
                ▲ helpful
              </button>
              <button className="sm" onClick={() => setFb(fb === "down-open" ? "none" : "down-open")} disabled={fb === "down-sent"}>
                ▼ {fb === "down-sent" ? "reported" : "wrong"}
              </button>
              <button className="sm ghost" onClick={copy}>
                {copied ? "copied" : "copy"}
              </button>
            </>
          )}
        </div>
        <div className="row faint">
          {a.llm && <span>{a.llm.calls} model calls</span>}
          <span>{(a.latency_ms / 1000).toFixed(1)}s</span>
          {a.trace_id && <Link href={`/traces/${a.trace_id}`}>trace →</Link>}
        </div>
      </div>

      {fb === "down-open" && (
        <div className="stack">
          <textarea
            placeholder="What is the correct answer? (it becomes a regression test)"
            value={correction}
            onChange={(e) => setCorrection(e.target.value)}
            rows={2}
          />
          <input placeholder="Optional comment" value={comment} onChange={(e) => setComment(e.target.value)} />
          <div className="row">
            <button
              className="primary sm"
              onClick={() => {
                void sendFeedback(-1);
                setFb("down-sent");
              }}
            >
              Send feedback
            </button>
            <span className="faint small">The Repair loop will diagnose this answer.</span>
          </div>
        </div>
      )}
    </div>
  );
}
