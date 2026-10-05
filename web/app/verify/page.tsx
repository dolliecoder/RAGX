"use client";

import Link from "next/link";
import { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Spinner } from "@/components/ui";
import { post } from "@/lib/api";

function Verify() {
  const token = useSearchParams().get("token") ?? "";
  const [state, setState] = useState<"working" | "ok" | "error">("working");
  const [msg, setMsg] = useState("");
  const once = useRef(false);

  useEffect(() => {
    if (once.current) return; // tokens are single-use; strict mode runs effects twice
    once.current = true;
    if (!token) {
      setState("error");
      setMsg("This link is incomplete.");
      return;
    }
    post<{ email: string }>("/auth/verify", { token })
      .then((r) => {
        setState("ok");
        setMsg(r.email);
      })
      .catch((e) => {
        setState("error");
        setMsg((e as Error).message);
      });
  }, [token]);

  return (
    <div className="card auth-card">
      <Link href="/" className="brand brand-link" title="RAGX home" style={{ padding: 0, marginBottom: 16 }}>
        <span className="brand-mark">RX</span> RAGX
      </Link>
      {state === "working" && (
        <p className="row muted">
          <Spinner /> Confirming your email…
        </p>
      )}
      {state === "ok" && (
        <>
          <h1>Email confirmed</h1>
          <p className="muted">Thanks, {msg} is confirmed. You can ask questions now.</p>
          <Link href="/ask" className="btn primary">
            Start asking
          </Link>
        </>
      )}
      {state === "error" && (
        <>
          <h1>Link not valid</h1>
          <p className="muted">{msg}</p>
          <p className="small muted">Sign in and use “Resend email” on your account page to get a new link.</p>
          <Link href="/login">Sign in</Link>
        </>
      )}
    </div>
  );
}

export default function VerifyPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <Verify />
    </Suspense>
  );
}
