"use client";

import Link from "next/link";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ErrorBox, Spinner } from "@/components/ui";
import { post } from "@/lib/api";

function ResetForm() {
  const params = useSearchParams();
  const token = params.get("token") ?? "";
  const invite = params.get("invite") === "1";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (password !== confirm) {
      setErr("The passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      await post("/auth/reset", { token, password });
      window.location.assign("/ask");
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
      <h1>{invite ? "Welcome! Choose a password" : "Choose a new password"}</h1>
      {!token ? (
        <>
          <p className="muted">This link is incomplete. Open the link from your email again, or request a new one.</p>
          <Link href="/forgot">Request a new link</Link>
        </>
      ) : (
        <form onSubmit={submit}>
          <label className="field">
            New password (at least 8 characters, letters and numbers)
            <input type="password" autoComplete="new-password" required value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
          </label>
          <label className="field">
            Repeat it
            <input type="password" autoComplete="new-password" required value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          </label>
          <ErrorBox error={err} />
          {err?.includes("expired") && (
            <p className="small">
              <Link href="/forgot">Request a new link</Link>
            </p>
          )}
          <button className="primary" disabled={busy}>
            {busy ? <Spinner /> : invite ? "Set password and continue" : "Save and sign in"}
          </button>
        </form>
      )}
    </div>
  );
}

export default function ResetPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <ResetForm />
    </Suspense>
  );
}
