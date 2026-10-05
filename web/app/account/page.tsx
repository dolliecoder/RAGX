"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/components/shell";
import { ErrorBox, PageHead, Spinner } from "@/components/ui";
import { api, post } from "@/lib/api";
import type { AuthOptions } from "@/lib/types";

export default function AccountPage() {
  const { user, refreshUser } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [opts, setOpts] = useState<AuthOptions | null>(null);
  useEffect(() => {
    api<AuthOptions>("/auth/options").then(setOpts).catch(() => {});
  }, []);
  if (!user) return null;
  const u = user.usage;

  const change = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setOk(false);
    if (next !== confirm) {
      setErr("The new passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      await post("/auth/password", { current_password: current, new_password: next });
      setOk(true);
      setCurrent("");
      setNext("");
      setConfirm("");
      await refreshUser();
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHead title="Your account" sub={user.email} />
      {user.must_change_password && (
        <div className="note-box" style={{ marginBottom: 16 }}>
          You signed in with a temporary password. Choose your own password to continue.
        </div>
      )}
      <div className="grid k2">
        <div className="card">
          <h2>Profile</h2>
          <dl className="kv">
            <dt>Name</dt>
            <dd>{user.name || "–"}</dd>
            <dt>Email</dt>
            <dd>
              {user.email}{" "}
              {!opts?.email_enabled ? null : user.email_verified ? (
                <span className="badge ok">confirmed</span>
              ) : (
                <>
                  <span className="badge warn">not confirmed</span>{" "}
                  <button
                    className="sm"
                    onClick={async () => {
                      try {
                        await post("/auth/resend-verification");
                        setErr(null);
                        setOk(false);
                        window.alert("Confirmation email sent. Check your inbox and spam folder.");
                      } catch (e) {
                        window.alert((e as Error).message);
                      }
                    }}
                  >
                    Resend email
                  </button>
                </>
              )}
            </dd>
            <dt>Role</dt>
            <dd>{user.role === "admin" ? "Administrator" : "Student"}</dd>
            <dt>Plan</dt>
            <dd>{user.plan}</dd>
          </dl>
          {u && (
            <>
              <h3 className="mt">Today&apos;s usage</h3>
              <dl className="kv">
                <dt>Questions</dt>
                <dd>
                  {u.questions_today}
                  {u.daily_limit !== null ? ` of ${u.daily_limit}` : " (no limit)"}
                </dd>
                <dt>Deep research</dt>
                <dd>
                  {u.deep_today}
                  {u.deep_limit !== null ? ` of ${u.deep_limit}` : ""}
                </dd>
                <dt>Resets</dt>
                <dd>{new Date(u.resets_at).toLocaleString()}</dd>
              </dl>
            </>
          )}
        </div>
        <div className="card">
          <h2>Change password</h2>
          <form className="stack" onSubmit={change}>
            <label className="field">
              Current password
              <input type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
            </label>
            <label className="field">
              New password
              <input type="password" autoComplete="new-password" required value={next} onChange={(e) => setNext(e.target.value)} />
            </label>
            <label className="field">
              Repeat new password
              <input type="password" autoComplete="new-password" required value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            </label>
            <ErrorBox error={err} />
            {ok && <div className="note-box small">Password changed. You were signed out on other devices.</div>}
            <div>
              <button className="primary" disabled={busy}>
                {busy ? <Spinner /> : "Change password"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </>
  );
}
