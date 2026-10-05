"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { GoogleButton, OrDivider } from "@/components/google";
import { ErrorBox, Spinner } from "@/components/ui";
import { api, post } from "@/lib/api";
import type { AuthOptions, User } from "@/lib/types";

function SignupForm() {
  const params = useSearchParams();
  const raw = params.get("next");
  const next = raw && raw.startsWith("/") && !raw.startsWith("//") && raw !== "/" ? raw : "/ask";
  const [opts, setOpts] = useState<AuthOptions | null>(null);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState(params.get("code") ?? "");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<AuthOptions>("/auth/options").then(setOpts).catch((e) => setErr((e as Error).message));
  }, []);

  const domains = opts?.allowed_email_domains ?? [];
  const domainHint = domains.length ? domains.map((d) => `@${d}`).join(" or ") : "";

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (password.length < 8) {
      setErr("Password must be at least 8 characters.");
      return;
    }
    setBusy(true);
    try {
      await post<{ user: User }>("/auth/signup", { name, email, password, join_code: code });
      window.location.assign(next);
    } catch (ex) {
      setErr((ex as Error).message);
      setBusy(false);
    }
  };

  if (opts && !opts.signup_enabled) {
    return (
      <div className="card auth-card">
        <h1>Sign-up is closed</h1>
        <p className="muted">Ask your administrator to create an account for you.</p>
        <Link href="/login">Back to sign in</Link>
      </div>
    );
  }

  return (
    <div className="card auth-card">
      <Link href="/" className="brand brand-link" title="RAGX home" style={{ padding: 0, marginBottom: 16 }}>
        <span className="brand-mark">RX</span> RAGX
      </Link>
      <h1>Create your account</h1>
      <p className="muted">{domainHint ? `Sign up with an email address ending in ${domainHint}.` : "Free. All you need is an email address."}</p>
      {opts?.google_enabled && (
        <>
          {opts.requires_join_code && (
            <label className="field" style={{ marginTop: 16 }}>
              Join code (needed for Google sign-up too)
              <input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="off" />
            </label>
          )}
          <div style={{ marginTop: 16 }}>
            <GoogleButton next={next} code={code} />
          </div>
          <OrDivider />
        </>
      )}
      <form onSubmit={submit}>
        <label className="field">
          Full name
          <input autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        </label>
        <label className="field">
          Email
          <input type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder={domains[0] ? `you@${domains[0]}` : "you@example.com"} />
        </label>
        <label className="field">
          Password (at least 8 characters, letters and numbers)
          <input type="password" autoComplete="new-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {opts?.requires_join_code && !opts.google_enabled && (
          <label className="field">
            Join code (from whoever invited you)
            <input required value={code} onChange={(e) => setCode(e.target.value)} autoComplete="off" />
          </label>
        )}
        <ErrorBox error={err} />
        <button className="primary" disabled={busy || !opts}>
          {busy ? <Spinner /> : "Create account"}
        </button>
      </form>
      <p className="small muted mt">
        Already have an account? <Link href="/login">Sign in</Link>
      </p>
    </div>
  );
}

export default function SignupPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <SignupForm />
    </Suspense>
  );
}
