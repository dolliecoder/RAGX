"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AnswerView } from "@/components/answer";
import { Icon } from "@/components/icons";
import { ACCEPT, UploadChips, type Uploads } from "@/components/files";
import { CHAT_EVENT, NeedKB, useAuth } from "@/components/shell";
import { ErrorBox, Spinner } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { Answer, Chat as ChatData, Job, KB } from "@/lib/types";

type Msg =
  | { id: number; role: "user"; text: string }
  | { id: number; role: "assistant"; answer?: Answer; error?: string; pending?: boolean; deep?: { jobId: string; status: string } };

let nextId = 1;

const EXAMPLES = [
  "Give me a short summary of these documents",
  "What are the most important dates and deadlines?",
  "Explain the key terms in simple words",
];

const MODES: { value: string; label: string; hint: string }[] = [
  { value: "auto", label: "Auto", hint: "Picks the right depth for each question" },
  { value: "fast", label: "Fast", hint: "Quick answers to simple questions" },
  { value: "standard", label: "Thorough", hint: "Searches more widely before answering" },
  { value: "deep", label: "Deep research", hint: "A longer report, built in the background" },
];

/** Themed replacement for a native <select>, which can't be styled in dark mode. */
function ModeMenu({ mode, setMode }: { mode: string; setMode: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [focus, setFocus] = useState(0);
  const [down, setDown] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const current = MODES.find((m) => m.value === mode) ?? MODES[0];

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  useEffect(() => {
    if (open) items.current[focus]?.focus();
  }, [open, focus]);

  const show = () => {
    setFocus(Math.max(0, MODES.findIndex((m) => m.value === mode)));
    // open upwards unless there isn't room above (e.g. the centred start screen)
    setDown((wrap.current?.getBoundingClientRect().top ?? 999) < 300);
    setOpen(true);
  };
  const pick = (v: string) => {
    setMode(v);
    setOpen(false);
  };

  return (
    <div
      className="mode-menu"
      ref={wrap}
      onKeyDown={(e) => {
        if (!open) return;
        if (e.key === "Escape") {
          e.preventDefault();
          setOpen(false);
        } else if (e.key === "ArrowDown") {
          e.preventDefault();
          setFocus((f) => (f + 1) % MODES.length);
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          setFocus((f) => (f - 1 + MODES.length) % MODES.length);
        } else if (e.key === "Tab") {
          setOpen(false);
        }
      }}
    >
      <button
        type="button"
        className="mode-pill"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Answer mode: ${current.label}`}
        onClick={() => (open ? setOpen(false) : show())}
        onKeyDown={(e) => {
          if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
            e.preventDefault();
            show();
          }
        }}
      >
        {current.label}
        <Icon name="chevron" size={14} style={{ transform: open ? "rotate(-90deg)" : "rotate(90deg)" }} />
      </button>
      {open && (
        <div className={`mode-list${down ? " down" : ""}`} role="listbox" aria-label="Answer mode">
          {MODES.map((m, i) => (
            <button
              key={m.value}
              type="button"
              role="option"
              aria-selected={m.value === mode}
              ref={(el) => {
                items.current[i] = el;
              }}
              className={`mode-option${m.value === mode ? " selected" : ""}`}
              onClick={() => pick(m.value)}
              onMouseEnter={() => setFocus(i)}
            >
              <span className="grow">
                <span className="mode-label">{m.label}</span>
                <span className="mode-hint">{m.hint}</span>
              </span>
              {m.value === mode && <Icon name="check" size={16} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const newChatId = () => (typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);

function limitMessage(e: unknown): string {
  if (e instanceof ApiError && e.status === 429) {
    const kind = e.data?.limit;
    const wait = Number(e.data?.retry_after ?? 0);
    if (kind === "minute" || kind === "concurrent") return `${e.message} (try again in about ${Math.max(1, wait)} seconds)`;
    return e.message;
  }
  return (e as Error).message;
}

function Composer({
  onSend,
  busy,
  disabled,
  mode,
  setMode,
  autoFocus,
  uploads,
  canUpload,
}: {
  onSend: (q: string) => void;
  busy: boolean;
  disabled: boolean;
  mode: string;
  setMode: (m: string) => void;
  autoFocus?: boolean;
  uploads: Uploads;
  canUpload: boolean;
}) {
  const [text, setText] = useState("");
  const [drag, setDrag] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [text]);

  const submit = () => {
    if (!text.trim() || busy || disabled) return;
    onSend(text);
    setText("");
  };

  return (
    <form
      className={`composer-box${drag ? " drag" : ""}`}
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      onDragOver={(e) => {
        if (!canUpload || !e.dataTransfer.types.includes("Files")) return;
        e.preventDefault();
        setDrag(true);
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        if (!canUpload) return;
        e.preventDefault();
        setDrag(false);
        void uploads.upload([...e.dataTransfer.files]);
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget || (e.target as HTMLElement).classList.contains("composer-row")) ref.current?.focus();
      }}
    >
      <UploadChips uploads={uploads} />
      <textarea
        ref={ref}
        rows={1}
        value={text}
        autoFocus={autoFocus}
        placeholder="Ask anything about your documents"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
        aria-label="Your question"
      />
      <div className="composer-row">
        <div className="row" style={{ gap: 4 }}>
          {canUpload && (
            <>
              <button type="button" className="icon-btn attach-btn" onClick={() => fileInput.current?.click()} title="Add files to this workspace" aria-label="Add files">
                <Icon name="clip" size={18} />
              </button>
              <input
                ref={fileInput}
                type="file"
                multiple
                accept={ACCEPT}
                hidden
                onChange={(e) => {
                  void uploads.upload([...(e.target.files ?? [])]);
                  e.target.value = "";
                }}
              />
            </>
          )}
          <ModeMenu mode={mode} setMode={setMode} />
        </div>
        <button className="send-btn" type="submit" disabled={busy || disabled || !text.trim()} aria-label="Send" title="Send (Enter)">
          {busy ? <Spinner /> : <Icon name="send" size={18} />}
        </button>
      </div>
    </form>
  );
}

function Thinking() {
  const steps = ["Searching your documents", "Grading what was found", "Writing a cited answer", "Fact-checking it"];
  const [i, setI] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setI((x) => Math.min(x + 1, steps.length - 1)), 2600);
    return () => clearInterval(t);
  }, [steps.length]);
  return (
    <div className="thinking" role="status">
      <span className="dots" aria-hidden>
        <i />
        <i />
        <i />
      </span>
      {steps[i]}…
    </div>
  );
}

function ChatView({ kb }: { kb: KB }) {
  const { user, setUsage, reloadChats, uploads, openFiles } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const chatParam = params.get("c");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [mode, setMode] = useState("auto");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const chatId = useRef<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // let the sidebar highlight the open chat
  useEffect(() => {
    window.dispatchEvent(new Event(CHAT_EVENT));
  }, [chatParam]);

  // open the chat named in ?c=, or start a fresh one
  useEffect(() => {
    if (chatParam && chatParam === chatId.current) return; // already showing it (e.g. we just created it)
    setLoadError(null);
    if (!chatParam) {
      chatId.current = newChatId();
      setMsgs([]);
      return;
    }
    chatId.current = chatParam;
    setMsgs([]);
    setLoading(true);
    let cancelled = false;
    api<ChatData>(`/kbs/${kb.id}/chats/${encodeURIComponent(chatParam)}`)
      .then((chat) => {
        if (cancelled) return;
        setMsgs(
          chat.turns.flatMap((t) => [
            { id: nextId++, role: "user" as const, text: t.query },
            { id: nextId++, role: "assistant" as const, answer: t.answer },
          ]),
        );
      })
      .catch((e) => !cancelled && setLoadError((e as Error).message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [chatParam, kb.id]);

  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: msgs.length > 2 ? "smooth" : "auto" });
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
    const sessionId = chatId.current ?? (chatId.current = newChatId());
    const isNew = msgs.length === 0;
    setBusy(true);
    const userMsg = nextId++;
    const id = nextId++;
    setMsgs((m) => [...m, { id: userMsg, role: "user", text: query }, { id, role: "assistant", pending: true }]);
    try {
      const a = await api<Answer>(`/kbs/${kb.id}/query`, { method: "POST", json: { query, mode, session_id: sessionId } });
      if (a.usage) setUsage(a.usage);
      setMsgs((m) => m.map((x) => (x.id === id ? { id, role: "assistant", answer: a, deep: a.job_id ? { jobId: a.job_id, status: "queued" } : undefined } : x)));
      if (a.job_id) pollDeep(id, a.job_id);
      if (isNew && chatId.current === sessionId) router.replace(`/ask?c=${encodeURIComponent(sessionId)}`, { scroll: false });
      void reloadChats();
    } catch (e) {
      setMsgs((m) => m.map((x) => (x.id === id ? { id, role: "assistant", error: limitMessage(e) } : x)));
    } finally {
      setBusy(false);
    }
  };

  const outOfQuestions = (user?.usage?.remaining ?? 1) <= 0;
  const usageNote =
    user?.usage && user.usage.remaining !== null
      ? user.usage.remaining > 0
        ? `${user.usage.remaining} of ${user.usage.daily_limit} questions left today.`
        : `You've used today's ${user.usage.daily_limit} questions. More after ${new Date(user.usage.resets_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`
      : "";
  const composer = (autoFocus: boolean) => (
    <Composer
      onSend={(q) => void send(q)}
      busy={busy}
      disabled={outOfQuestions}
      mode={mode}
      setMode={setMode}
      autoFocus={autoFocus}
      uploads={uploads}
      canUpload={kb.can_edit}
    />
  );
  const firstName = (user?.name || "").trim().split(/\s+/)[0];

  if (msgs.length === 0 && !loading && !loadError) {
    return (
      <div className="hero">
        <div className="hero-inner">
          <h1 className="hero-title">{firstName ? `What can I find for you, ${firstName}?` : "What can I find for you?"}</h1>
          <p className="hero-sub">
            {kb.documents === 0 ? (
              kb.can_edit ? (
                <>
                  <b>{kb.name}</b> has no files yet.{" "}
                  <button className="linklike" onClick={openFiles}>
                    Add your PDFs
                  </button>{" "}
                  or attach them with <Icon name="clip" size={14} style={{ verticalAlign: -2 }} />. You can already ask
                  {kb.general_knowledge ? ": answers will come from general knowledge until then." : "."}
                </>
              ) : (
                <>Nothing has been added to {kb.name} yet.</>
              )
            ) : (
              <>
                Answers come from the {kb.documents} file{kb.documents === 1 ? "" : "s"} in <b>{kb.name}</b>, with sources you can check.
              </>
            )}
          </p>
          {composer(true)}
          <div className="suggestions" hidden={kb.documents === 0}>
            {EXAMPLES.map((e) => (
              <button key={e} className="suggestion" onClick={() => void send(e)} disabled={busy || outOfQuestions}>
                {e}
              </button>
            ))}
          </div>
          <p className="composer-note">{usageNote || "Every answer is checked against your documents by a second AI. It can still make mistakes."}</p>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="chat-scroll" ref={scroller}>
        <div className="thread">
          {loading && (
            <div className="thinking">
              <Spinner /> Opening chat…
            </div>
          )}
          {loadError && <ErrorBox error={loadError} />}
          {msgs.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="turn-user">
                <div className="bubble">{m.text}</div>
              </div>
            ) : (
              <div key={m.id} className="turn-bot">
                <span className="bot-avatar" aria-hidden>
                  RX
                </span>
                <div className="grow">
                  {m.pending && <Thinking />}
                  {m.error && <ErrorBox error={m.error} />}
                  {m.answer && <AnswerView a={m.answer} />}
                  {m.deep && m.deep.status !== "done" && (
                    <div className="deep-note">
                      {m.deep.status === "failed" ? (
                        <span className="error-box">The deep research job failed.</span>
                      ) : (
                        <>
                          <Spinner /> Deep research is running in the background. The full, verified report will replace this answer.
                        </>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ),
          )}
        </div>
      </div>
      <div className="dock">
        {composer(false)}
        <p className="composer-note">{usageNote || "RAGX checks every answer against your documents, but it can still make mistakes."}</p>
      </div>
    </>
  );
}

export default function AskPage() {
  return (
    <Suspense fallback={null}>
      <NeedKB>{(kb) => <ChatView kb={kb} />}</NeedKB>
    </Suspense>
  );
}
