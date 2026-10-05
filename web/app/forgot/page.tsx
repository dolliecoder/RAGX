"use client";

import Link from "next/link";
import { useState } from "react";
import { ErrorBox, Spinner } from "@/components/ui";
import { post } from "@/lib/api";

export default function ForgotPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await post("/auth/forgot", { email });
      setSent(true);
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card auth-card">
      <Link href="/" className="brand brand-link" title="RAGX home" style={{ padding: 0, marginBottom: 16 }}>
        <span className="brand-mark">RX</span> RAGX
      </Link>
      <h1>Forgot your password?</h1>
      {sent ? (
        <>
          <p className="muted">
            If an account exists for <b>{email}</b>, we&apos;ve sent a link to choose a new password. It works for one hour. Check your spam folder too.
          </p>
          <Link href="/login">Back to sign in</Link>
        </>
      ) : (
        <>
          <p className="muted">Enter your email and we&apos;ll send you a link to choose a new password.</p>
          <form onSubmit={submit}>
            <label className="field">
              Email
              <input type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
            </label>
            <ErrorBox error={err} />
            <button className="primary" disabled={busy}>
              {busy ? <Spinner /> : "Send reset link"}
            </button>
          </form>
          <p className="small muted mt">
            <Link href="/login">Back to sign in</Link>
          </p>
        </>
      )}
    </div>
  );
}
