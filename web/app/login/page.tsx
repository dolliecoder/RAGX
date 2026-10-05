"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ErrorBox, Spinner } from "@/components/ui";
import { SOURCE_URL, api, post } from "@/lib/api";
import type { AuthOptions, User } from "@/lib/types";

function safeNext(next: string | null): string {
  // only allow same-site paths (no protocol-relative or absolute URLs)
  return next && next.startsWith("/") && !next.startsWith("//") && next !== "/" ? next : "/ask";
}

function LoginForm() {
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [opts, setOpts] = useState<AuthOptions | null>(null);

  useEffect(() => {
    api<{ user: User }>("/auth/me")
      .then(() => window.location.replace(next))
      .catch(() => {});
    api<AuthOptions>("/auth/options").then(setOpts).catch(() => {});
  }, [next]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const r = await post<{ user: User }>("/auth/login", { email, password });
      window.location.assign(r.user.must_change_password ? "/account" : next);
    } catch (ex) {
      setErr((ex as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="card auth-card">
      <Link href="/" className="brand brand-link" title="RAGX home" style={{ padding: 0, marginBottom: 16 }}>
        <span className="brand-mark">RX</span> RAGX
      </Link>
      <h1>Sign in</h1>
      <p className="muted">Ask questions about your course material and get answers with sources.</p>
      <form onSubmit={submit}>
        <label className="field">
          Email
          <input type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
        </label>
        <label className="field">
          Password
          <input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        <ErrorBox error={err} />
        <button className="primary" disabled={busy}>
          {busy ? <Spinner /> : "Sign in"}
        </button>
      </form>
      {opts?.signup_enabled !== false && (
        <p className="small muted mt">
          New here? <Link href={`/signup${next !== "/ask" ? `?next=${encodeURIComponent(next)}` : ""}`}>Create an account</Link>
        </p>
      )}
      {opts?.email_enabled ? (
        <p className="small">
          <Link href="/forgot">Forgot your password?</Link>
        </p>
      ) : (
        <p className="small faint">Forgot your password? Ask your administrator to reset it.</p>
      )}
      <p className="small faint">
        RAGX is free, open-source software (AGPL-3.0).{" "}
        <a href={SOURCE_URL} target="_blank" rel="noreferrer">
          Source code
        </a>
      </p>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <LoginForm />
    </Suspense>
  );
}
