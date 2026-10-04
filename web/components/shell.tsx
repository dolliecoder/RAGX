"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { storageGet, storageSet } from "@/lib/hooks";
import type { KB } from "@/lib/types";

type KBCtx = { kbs: KB[]; kb: KB | null; setKb: (id: string) => void; refresh: () => Promise<void>; apiError: string | null };
const Ctx = createContext<KBCtx>({ kbs: [], kb: null, setKb: () => {}, refresh: async () => {}, apiError: null });
export const useKB = () => useContext(Ctx);

const NAV = [
  { href: "/", label: "Ask", icon: "◎" },
  { href: "/health", label: "Health", icon: "♥" },
  { href: "/traces", label: "Traces", icon: "≡" },
  { href: "/repair", label: "Repair", icon: "✚", badge: true },
  { href: "/knowledge", label: "Knowledge", icon: "▤" },
  { href: "/evals", label: "Evals", icon: "✓" },
  { href: "/settings", label: "Settings", icon: "⚙" },
];

export function Shell({ children }: { children: ReactNode }) {
  const [kbs, setKbs] = useState<KB[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [pending, setPending] = useState(0);
  const path = usePathname();

  const refresh = useCallback(async () => {
    try {
      const list = await api<KB[]>("/kbs");
      setKbs(list);
      setApiError(null);
      setSelected((cur) => {
        const saved = cur ?? storageGet("ragx.kb");
        return list.find((k) => k.id === saved)?.id ?? list[0]?.id ?? null;
      });
    } catch (e) {
      setApiError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const kb = kbs.find((k) => k.id === selected) ?? null;

  useEffect(() => {
    if (!kb) return;
    let alive = true;
    const load = () =>
      api<{ id: string }[]>(`/kbs/${kb.id}/fixes?status=pending_approval`)
        .then((f) => alive && setPending(f.length))
        .catch(() => {});
    void load();
    const t = setInterval(load, 30000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [kb, path]);

  const setKb = (id: string) => {
    setSelected(id);
    storageSet("ragx.kb", id);
  };

  return (
    <Ctx.Provider value={{ kbs, kb, setKb, refresh, apiError }}>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <span className="brand-mark">RX</span> RAGX
          </div>
          <label className="field" style={{ padding: "0 4px" }}>
            Knowledge base
            <select value={selected ?? ""} onChange={(e) => setKb(e.target.value)} aria-label="Knowledge base">
              {kbs.length === 0 && <option value="">none yet</option>}
              {kbs.map((k) => (
                <option key={k.id} value={k.id}>
                  {k.name}
                </option>
              ))}
            </select>
          </label>
          <nav className="nav">
            {NAV.map((n) => {
              const active = n.href === "/" ? path === "/" : path.startsWith(n.href);
              return (
                <Link key={n.href} href={n.href} className={active ? "active" : ""}>
                  <span aria-hidden>{n.icon}</span> {n.label}
                  {n.badge && pending > 0 && <span className="count" title="fixes awaiting approval">{pending}</span>}
                </Link>
              );
            })}
          </nav>
          <div className="faint small" style={{ marginTop: "auto", padding: "0 8px" }}>
            self-healing RAG · v0.1
          </div>
        </aside>
        <main className="main">
          {apiError && (
            <div className="error-box" style={{ marginBottom: 16 }}>
              Cannot reach the RAGX API: {apiError}. Start the backend (<code>ragx serve</code>) or check RAGX_API_URL.
            </div>
          )}
          {children}
        </main>
      </div>
    </Ctx.Provider>
  );
}

export function NeedKB({ children }: { children: (kb: KB) => ReactNode }) {
  const { kb, kbs, apiError } = useKB();
  if (apiError) return null;
  if (!kb)
    return (
      <div className="card empty">
        {kbs.length === 0 ? (
          <>
            No knowledge base yet. <Link href="/knowledge">Create one and add documents</Link>.
          </>
        ) : (
          <span className="spinner" />
        )}
      </div>
    );
  return <>{children(kb)}</>;
}
