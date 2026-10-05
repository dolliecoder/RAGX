"use client";

import Link from "next/link";
import { useState } from "react";
import { useAuth } from "@/components/shell";
import { SOURCE_URL } from "@/lib/api";

const STEPS = [
  {
    n: "1",
    title: "Find",
    body: "Searches your documents three ways at once: by keyword, by meaning, and by exact codes or names. Then it ranks the passages that actually answer the question.",
  },
  {
    n: "2",
    title: "Check",
    body: "A second, independent AI checks every claim against the source. Anything it can't confirm is removed. If the answer isn't in your documents, it says so.",
  },
  {
    n: "3",
    title: "Heal",
    body: "If the first search misses, it rephrases, looks further, and tries again, while you wait. Later, it works out why it missed, and fixes the cause.",
  },
];

const LOOP = [
  { t: "A question fails", d: "A student asks how to get their money back; the first search misses." },
  { t: "Diagnosed", d: "Students say “money back”, the policy says “refund”: a vocabulary gap." },
  { t: "Fix proposed", d: "Teach the search that this passage answers “money back” questions." },
  { t: "Tested first", d: "Re-run the failing questions and every saved test, before and after." },
  { t: "Applied or undone", d: "Better and nothing broke? Applied. Worse? Rolled back automatically." },
  { t: "Locked in", d: "The fixed question becomes a permanent regression test." },
];

const MODELS = [
  {
    name: "Gemini free tier",
    tag: "Default",
    points: ["Free key from Google AI Studio", "Fast: a few seconds per answer", "Daily limits per model"],
  },
  {
    name: "Local open models",
    tag: "Most private",
    points: ["Ollama on your own machine", "No keys, no limits, nothing leaves", "Slower without a graphics card"],
  },
  {
    name: "Free cloud mix",
    tag: "Most resilient",
    points: ["Groq + OpenRouter free models", "Falls over to the next when one is busy", "More free quota overall"],
  },
];

const FAQ = [
  {
    q: "Is it really free?",
    a: "Yes. The software is free and open source, and it runs on free AI: Google's Gemini free tier, Groq and OpenRouter free models, or open-source models on your own computer through Ollama. Paid models are optional.",
  },
  {
    q: "How do I know an answer is right?",
    a: "Every sentence links to the passage it came from, and an independent model checks each claim before you see it. Unsupported claims are removed, and the answer shows how well it is supported. When the documents don't contain the answer, RAGX says so instead of guessing.",
  },
  {
    q: "Where does my data go?",
    a: "Your documents and questions stay in your own installation. They are only sent to the AI provider you choose. With local Ollama models, nothing leaves your machine at all. Note that Google may use free-tier Gemini requests to improve its products.",
  },
  {
    q: "Can students see each other's questions?",
    a: "No. Each student sees only their own history. Administrators can see questions so they can improve the material and approve fixes.",
  },
  {
    q: "What does the AGPL license mean for me?",
    a: "You can use, change and share RAGX freely, including in a school or business. If you run a modified version as a website for other people, you must share your changes with them. That keeps improvements open for everyone.",
  },
  {
    q: "Is it finished?",
    a: "It's an early release (v0.1). The core works and is covered by automated tests, but it hasn't been used at scale yet. Feedback and bug reports on GitHub are very welcome.",
  },
];

function Receipt() {
  return (
    <div className="lp-receipt" aria-label="Example of a verified answer with its source">
      <div className="lp-receipt-q">How long do Pro customers have to request a refund?</div>
      <p className="lp-receipt-a">
        <mark>Pro customers can request a full refund within 30 days of purchase</mark>
        <sup>1</sup>, through <mark>Settings › Billing › Request Refund</mark>
        <sup>1</sup>. Usage-based charges like GPU hours aren&apos;t refundable<sup>1</sup>.
      </p>
      <div className="lp-stamp">
        <span>✓ Verified</span> 3 of 3 claims supported
      </div>
      <div className="lp-source">
        <div className="lp-source-head">
          <b>1</b> Refund Policy · Eligibility
        </div>
        “Customers on the Pro plan can request a full refund within 30 days of purchase.”
      </div>
      <div className="lp-margin-note">
        <b>Healed</b> First search missed: the question said “money back”, the policy says “refund”. Rephrased and found it. Tonight the
        repair loop teaches the search this phrasing.
      </div>
    </div>
  );
}

function CopyBlock({ lines }: { lines: string[] }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="lp-terminal">
      <button
        className="sm"
        onClick={() => {
          void navigator.clipboard?.writeText(lines.join("\n")).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          });
        }}
      >
        {copied ? "copied" : "copy"}
      </button>
      {lines.map((l) => (
        <div key={l}>
          <span className="lp-prompt">$</span> {l}
        </div>
      ))}
    </div>
  );
}

export default function Landing() {
  const { user } = useAuth();
  const cta = user ? { href: "/ask", label: "Open RAGX" } : { href: "/signup", label: "Get started free" };

  return (
    <div className="lp">
      <header className="lp-nav">
        <Link href="/" className="brand lp-brand">
          <span className="brand-mark">RX</span> RAGX
        </Link>
        <nav className="lp-links" aria-label="Sections">
          <a href="#how">How it works</a>
          <a href="#models">Free models</a>
          <a href="#classrooms">For classrooms</a>
          <a href="#open-source">Open source</a>
        </nav>
        <div className="lp-nav-cta">
          {!user && (
            <Link href="/login" className="lp-signin">
              Sign in
            </Link>
          )}
          <Link href={cta.href} className="btn primary">
            {cta.label}
          </Link>
        </div>
      </header>

      <main>
        <section className="lp-hero">
          <div className="lp-hero-text">
            <div className="lp-eyebrow">Free &amp; open source · runs on free AI models</div>
            <h1 className="lp-title">
              Answers you can check.
              <br />
              <em>From a system that fixes itself.</em>
            </h1>
            <p className="lp-lede">
              RAGX answers questions from your documents. Every sentence shows its source. A second AI fact-checks every claim. When something goes wrong, RAGX works out why and repairs it, tests the repair first, and undoes it if it makes things worse.
            </p>
            <div className="lp-actions">
              <Link href={cta.href} className="btn primary lp-big">
                {cta.label}
              </Link>
              <a href={SOURCE_URL} className="btn lp-big" target="_blank" rel="noreferrer">
                Read the code
              </a>
            </div>
            <p className="lp-fine">PDF, Word, HTML, Markdown and text. No credit card. Self-host in five minutes.</p>
          </div>
          <Receipt />
        </section>

        <section className="lp-band">
          <h2 className="lp-h2">Most document chatbots guess.</h2>
          <div className="lp-grid3">
            <div>
              <h3>They make things up</h3>
              <p>A confident answer that isn&apos;t in your documents looks exactly like a right one.</p>
              <p className="lp-fix">RAGX removes every claim it can&apos;t back with a source, and says “not found” when it should.</p>
            </div>
            <div>
              <h3>You can&apos;t check them</h3>
              <p>No sources, or a vague list of files, means you have to trust it blindly.</p>
              <p className="lp-fix">RAGX links every sentence to the exact passage, quoted.</p>
            </div>
            <div>
              <h3>Mistakes repeat forever</h3>
              <p>The same question fails the same way every day until someone notices.</p>
              <p className="lp-fix">RAGX diagnoses failures, tests a fix and keeps it as a regression test.</p>
            </div>
          </div>
        </section>

        <section id="how" className="lp-section">
          <h2 className="lp-h2">How it works</h2>
          <div className="lp-steps">
            {STEPS.map((s) => (
              <div key={s.n} className="lp-step">
                <div className="lp-step-n">{s.n}</div>
                <h3>{s.title}</h3>
                <p>{s.body}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="lp-section">
          <h2 className="lp-h2">A failure today becomes a test tomorrow.</h2>
          <p className="lp-sub">The repair loop runs in the background. Safe fixes apply on their own; risky ones wait for a person to approve.</p>
          <ol className="lp-loop">
            {LOOP.map((l, i) => (
              <li key={l.t}>
                <span className="lp-loop-i">{String(i + 1).padStart(2, "0")}</span>
                <b>{l.t}</b>
                <span>{l.d}</span>
              </li>
            ))}
          </ol>
        </section>

        <section id="models" className="lp-section">
          <h2 className="lp-h2">Runs on free AI. Your choice which.</h2>
          <div className="lp-grid3">
            {MODELS.map((m) => (
              <div key={m.name} className="lp-card">
                <div className="lp-card-tag">{m.tag}</div>
                <h3>{m.name}</h3>
                <ul>
                  {m.points.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
          <p className="lp-sub">Paid models such as Claude and GPT work too, but you never need them.</p>
        </section>

        <section id="classrooms" className="lp-section lp-split">
          <div>
            <h2 className="lp-h2">Ready for a classroom.</h2>
            <ul className="lp-checks">
              <li>Students join with your college email domain and a join code you share as a link.</li>
              <li>Daily question limits per student, so a free AI quota lasts all term.</li>
              <li>Each student sees only their own questions.</li>
              <li>Corrections from students are reviewed before they count.</li>
              <li>Every admin action is in an audit log.</li>
            </ul>
          </div>
          <div className="lp-join">
            <div className="lp-join-label">Join code</div>
            <div className="lp-join-code">CS101-K7Q2</div>
            <div className="lp-join-meter">
              <span>Questions today</span>
              <span>12 / 30</span>
            </div>
            <div className="bar">
              <span style={{ width: "40%" }} />
            </div>
          </div>
        </section>

        <section id="open-source" className="lp-section lp-split">
          <div>
            <h2 className="lp-h2">Open source, AGPL-3.0.</h2>
            <p>
              Read every line, run it on your own computer or server, and improve it. If you host a modified version for others, share your changes. That&apos;s
              the whole deal.
            </p>
            <a href={SOURCE_URL} target="_blank" rel="noreferrer">
              View on GitHub →
            </a>
          </div>
          <CopyBlock lines={["git clone https://github.com/dolliecoder/RAGX.git", "cd RAGX && cp .env.example .env", "docker compose up -d --build"]} />
        </section>

        <section className="lp-section">
          <h2 className="lp-h2">Questions</h2>
          <div className="lp-faq">
            {FAQ.map((f) => (
              <details key={f.q}>
                <summary>{f.q}</summary>
                <p>{f.a}</p>
              </details>
            ))}
          </div>
        </section>

        <section className="lp-final">
          <h2 className="lp-title lp-final-title">
            Stop guessing. <em>Start checking.</em>
          </h2>
          <Link href={cta.href} className="btn primary lp-big">
            {cta.label}
          </Link>
        </section>
      </main>

      <footer className="lp-footer">
        <span>
          <span className="brand-mark" style={{ display: "inline-grid", width: 20, height: 20, fontSize: 11, verticalAlign: "-4px" }}>
            RX
          </span>{" "}
          RAGX · free software under AGPL-3.0
        </span>
        <span className="lp-footer-links">
          <a href={SOURCE_URL} target="_blank" rel="noreferrer">
            GitHub
          </a>
          <a href={`${SOURCE_URL}/blob/main/SECURITY.md`} target="_blank" rel="noreferrer">
            Security
          </a>
          <Link href="/login">Sign in</Link>
        </span>
      </footer>
    </div>
  );
}
