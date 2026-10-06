"use client";

import { Fragment, type ReactNode } from "react";
import type { Span } from "@/lib/types";

const TEX: Record<string, string> = {
  pi: "π", theta: "θ", alpha: "α", beta: "β", gamma: "γ", delta: "δ", Delta: "Δ", lambda: "λ", mu: "μ", sigma: "σ", Sigma: "Σ",
  omega: "ω", Omega: "Ω", phi: "φ", epsilon: "ε", infty: "∞", le: "≤", leq: "≤", ge: "≥", geq: "≥", ne: "≠", neq: "≠",
  approx: "≈", times: "×", cdot: "·", div: "÷", pm: "±", to: "→", rightarrow: "→", Rightarrow: "⇒", implies: "⇒",
  in: "∈", notin: "∉", subset: "⊂", cup: "∪", cap: "∩", forall: "∀", exists: "∃", int: "∫", sum: "Σ", prod: "∏",
  partial: "∂", nabla: "∇", degree: "°", circ: "°", ldots: "…", dots: "…", prime: "′",
};
const SUP: Record<string, string> = { "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", n: "ⁿ", "-": "⁻", "+": "⁺" };

/** Models sometimes answer in LaTeX despite instructions: turn common notation into readable symbols. */
export function tidyMath(text: string): string {
  if (!/[$\\^]/.test(text)) return text;
  let t = text.replace(/\$\$([^$]+)\$\$/g, "$1").replace(/\$([^$\n]+)\$/g, "$1").replace(/\\\(|\\\)|\\\[|\\\]/g, "");
  const group = (s: string) => (/^[\w.′']+$/.test(s) ? s : `(${s})`);
  t = t.replace(/\\frac\{([^{}]*)\}\{([^{}]*)\}/g, (_m, a: string, b: string) => `${group(a)}/${group(b)}`);
  t = t.replace(/\\sqrt\{([^{}]*)\}/g, (_m, a: string) => `√${group(a)}`).replace(/\\sqrt\s*(\w)/g, "√$1");
  t = t.replace(/\\(?:text|mathrm|mathbf|operatorname)\{([^{}]*)\}/g, "$1").replace(/\\left|\\right/g, "");
  t = t.replace(/\\([A-Za-z]+)/g, (m, name: string) => TEX[name] ?? m);
  t = t.replace(/\^\{([0-9n+-]+)\}|\^([0-9n])/g, (m, a?: string, b?: string) => {
    const s = (a ?? b ?? "").split("").map((c) => SUP[c]);
    return s.every(Boolean) ? s.join("") : m;
  });
  return t.replace(/\\,|\\;|\\!/g, " ").replace(/\\\{|\\\}/g, (m) => m[1]);
}

/** **bold** and `code` inside a line of answer text (no HTML is ever injected). */
export function inline(raw: string): ReactNode[] {
  const text = tidyMath(raw);
  const out: ReactNode[] = [];
  const re = /\*\*(.+?)\*\*|`([^`]+)`/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[1] !== undefined ? <strong key={m.index}>{m[1]}</strong> : <code key={m.index}>{m[2]}</code>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

type Item = { kind: "p" | "ul" | "ol" | "h"; parts: Span[][] };

/** Group answer sentences into headings, paragraphs and lists. */
function layout(spans: Span[]): Item[] {
  const items: Item[] = [];
  const cur = () => items[items.length - 1];
  for (const s of spans) {
    let block = s.block ?? "continue";
    if (s.heading) {
      items.push({ kind: "h", parts: [[{ ...s, text: s.heading, citations: [] }]] });
      if (block === "continue") block = "paragraph";
    }
    const last = cur();
    if (block === "bullet" || block === "numbered") {
      const kind = block === "bullet" ? "ul" : "ol";
      if (last && last.kind === kind) last.parts.push([s]);
      else items.push({ kind, parts: [[s]] });
    } else if (block === "paragraph" || !last || last.kind === "h") {
      items.push({ kind: "p", parts: [[s]] });
    } else {
      last.parts[last.parts.length - 1].push(s); // continue the paragraph or list item
    }
  }
  return items;
}

/**
 * A run of sentences with citation markers. Consecutive sentences citing the same
 * sources share one marker at the end of the run, so the text isn't cluttered.
 */
function Run({ spans, cite }: { spans: Span[]; cite: (n: number) => ReactNode }) {
  return (
    <>
      {spans.map((s, i) => {
        const next = spans[i + 1];
        const same = next && next.citations.join(",") === s.citations.join(",");
        return (
          <Fragment key={i}>
            {inline(s.text)}
            {!same && s.citations.length > 0 && <span className="cites">{s.citations.map((n) => cite(n))}</span>}
            {i < spans.length - 1 && " "}
          </Fragment>
        );
      })}
    </>
  );
}

export function StructuredAnswer({ spans, cite }: { spans: Span[]; cite: (n: number) => ReactNode }) {
  return (
    <>
      {layout(spans).map((it, i) => {
        if (it.kind === "h") return <h3 key={i}>{inline(it.parts[0][0].text)}</h3>;
        if (it.kind === "p")
          return (
            <p key={i}>
              <Run spans={it.parts[0]} cite={cite} />
            </p>
          );
        const List = it.kind === "ul" ? "ul" : "ol";
        return (
          <List key={i}>
            {it.parts.map((run, j) => (
              <li key={j}>
                <Run spans={run} cite={cite} />
              </li>
            ))}
          </List>
        );
      })}
    </>
  );
}

/** Minimal Markdown for replies without citation spans (chat replies, older answers). */
export function Markdown({ text }: { text: string }) {
  const blocks = text.replace(/\r/g, "").split(/\n{2,}/);
  return (
    <>
      {blocks.map((b, i) => {
        const lines = b.split("\n").filter((l) => l.trim());
        if (!lines.length) return null;
        const h = /^#{1,6}\s+(.*)$/.exec(lines[0]);
        if (h && lines.length === 1) return <h3 key={i}>{inline(h[1])}</h3>;
        if (lines.every((l) => /^\s*[-*•]\s+/.test(l)))
          return (
            <ul key={i}>
              {lines.map((l, j) => (
                <li key={j}>{inline(l.replace(/^\s*[-*•]\s+/, ""))}</li>
              ))}
            </ul>
          );
        if (lines.every((l) => /^\s*\d+[.)]\s+/.test(l)))
          return (
            <ol key={i}>
              {lines.map((l, j) => (
                <li key={j}>{inline(l.replace(/^\s*\d+[.)]\s+/, ""))}</li>
              ))}
            </ol>
          );
        if (h)
          return (
            <Fragment key={i}>
              <h3>{inline(h[1])}</h3>
              <Markdown text={lines.slice(1).join("\n")} />
            </Fragment>
          );
        return (
          <p key={i}>
            {lines.map((l, j) => (
              <Fragment key={j}>
                {j > 0 && <br />}
                {inline(l)}
              </Fragment>
            ))}
          </p>
        );
      })}
    </>
  );
}
