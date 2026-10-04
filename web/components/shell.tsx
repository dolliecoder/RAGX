"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { SOURCE_URL, api, post } from "@/lib/api";
import { storageGet, storageSet } from "@/lib/hooks";
import type { KB, Usage, User } from "@/lib/types";

type Ctx = {
  user: User | null;
  setUsage: (u: Usage) => void;
  refreshUser: () => Promise<void>;
  kbs: KB[];
  kb: KB | null;
  setKb: (id: string) => void;
  refresh: () => Promise<void>;
  apiError: string | null;
};
const AppCtx = createContext<Ctx>({
  user: null,
  setUsage: () => {},
  refreshUser: async () => {},
  kbs: [],
  kb: null,
  setKb: () => {},
  refresh: async () => {},
  apiError: null,
});
export const useKB = () => useContext(AppCtx);
export const useAuth = () => useContext(AppCtx);

const PUBLIC = ["/login", "/signup"];
const ADMIN_ONLY = ["/health", "/repair", "/knowledge", "/evals", "/users", "/settings"];

type NavItem = { href: string; label: string; icon: string; badge?: boolean };
const STUDENT_NAV: NavItem[] = [
  { href: "/", label: "Ask", icon: "◎" },
  { href: "/traces", label: "My questions", icon: "≡" },
];
const ADMIN_NAV: NavItem[] = [
  { href: "/", label: "Ask", icon: "◎" },
  { href: "/health", label: "Health", icon: "♥" },
  { href: "/traces", label: "Traces", icon: "≡" },
  { href: "/repair", label: "Repair", icon: "✚", badge: true },
  { href: "/knowledge", label: "Knowledge", icon: "▤" },
  { href: "/evals", label: "Evals", icon: "✓" },
  { href: "/users", label: "Users", icon: "☺" },
  { href: "/settings", label: "Settings", icon: "⚙" },
];

function UsageMeter({ usage }: { usage: Usage }) {
  if (usage.daily_limit === null) return null;
  const used = usage.questions_today;
  const pct = Math.min(100, (used / Math.max(1, usage.daily_limit)) * 100);
  const color = pct >= 100 ? "var(--bad)" : pct >= 80 ? "var(--warn)" : "var(--ok)";
  return (
    <div className="small" title={`Resets ${new Date(usage.resets_at).toLocaleString()}`}>
      <div className="spread">
        <span className="muted">Questions today</span>
        <span>
          {used}/{usage.daily_limit}
        </span>
      </div>
      <div className="bar">
        <span style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const isPublic = PUBLIC.some((p) => path.startsWith(p));
  const [user, setUser] = useState<User | null>(null);
  const [checked, setChecked] = useState(false);
  const [kbs, setKbs] = useState<KB[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [pending, setPending] = useState(0);

  const refreshUser = useCallback(async () => {
    try {
      const r = await api<{ user: User }>("/auth/me");
      setUser(r.user);
      setApiError(null);
    } catch (e) {
      const status = (e as { status?: number }).status;
      setUser(null);
      if (status === 401) {
        if (!isPublic) router.replace(`/login?next=${encodeURIComponent(path)}`);
      } else {
        setApiError((e as Error).message);
      }
    } finally {
      setChecked(true);
    }
  }, [isPublic, path, router]);

  useEffect(() => {
    void refreshUser();
    // only re-check when entering/leaving the public pages
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPublic]);

  const refresh = useCallback(async () => {
    try {
      const list = await api<KB[]>("/kbs");
      setKbs(list);
      setSelected((cur) => {
        const saved = cur ?? storageGet("ragx.kb");
        return list.find((k) => k.id === saved)?.id ?? list[0]?.id ?? null;
      });
    } catch (e) {
      setApiError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (user) void refresh();
  }, [user?.id, refresh]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (user?.must_change_password && path !== "/account") router.replace("/account");
  }, [user, path, router]);

  const kb = kbs.find((k) => k.id === selected) ?? null;
  const isAdmin = user?.role === "admin";

  useEffect(() => {
    if (!kb || !isAdmin) return;
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
  }, [kb, isAdmin, path]);

  const setKb = (id: string) => {
    setSelected(id);
    storageSet("ragx.kb", id);
  };
  const setUsage = (u: Usage) => setUser((cur) => (cur ? { ...cur, usage: u } : cur));

  const signOut = async () => {
    try {
      await post("/auth/logout");
    } finally {
      setUser(null);
      window.location.assign("/login");
    }
  };

  const ctx: Ctx = { user, setUsage, refreshUser, kbs, kb, setKb, refresh, apiError };

  if (isPublic) {
    return (
      <AppCtx.Provider value={ctx}>
        <main className="auth-page">{children}</main>
      </AppCtx.Provider>
    );
  }

  if (!checked || (!user && !apiError)) {
    return (
      <div className="auth-page">
        <span className="spinner" />
      </div>
    );
  }

  if (!user) {
    return (
      <div className="auth-page">
        <div className="error-box">Cannot reach the RAGX server: {apiError}</div>
      </div>
    );
  }

  const nav = isAdmin ? ADMIN_NAV : STUDENT_NAV;
  const blocked = !isAdmin && ADMIN_ONLY.some((p) => path.startsWith(p));

  return (
    <AppCtx.Provider value={ctx}>
      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <span className="brand-mark">RX</span> RAGX
          </div>
          {kbs.length > 1 && (
            <label className="field" style={{ padding: "0 4px" }}>
              Knowledge base
              <select value={selected ?? ""} onChange={(e) => setKb(e.target.value)} aria-label="Knowledge base">
                {kbs.map((k) => (
                  <option key={k.id} value={k.id}>
                    {k.name}
                    {k.visibility === "admins" ? " (admins)" : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
          <nav className="nav">
            {nav.map((n) => {
              const active = n.href === "/" ? path === "/" : path.startsWith(n.href);
              return (
                <Link key={n.href} href={n.href} className={active ? "active" : ""}>
                  <span aria-hidden>{n.icon}</span> {n.label}
                  {n.badge && pending > 0 && (
                    <span className="count" title="fixes awaiting approval">
                      {pending}
                    </span>
                  )}
                </Link>
              );
            })}
          </nav>
          <div className="user-box">
            {user.usage && <UsageMeter usage={user.usage} />}
            <div className="spread">
              <Link href="/account" className="truncate" title={user.email} style={{ maxWidth: 140 }}>
                {user.name || user.email}
              </Link>
              {isAdmin && <span className="badge accent">admin</span>}
            </div>
            <div className="spread">
              <button className="sm ghost" onClick={() => void signOut()} style={{ padding: 0 }}>
                Sign out
              </button>
              <a href={SOURCE_URL} target="_blank" rel="noreferrer" className="faint small" title="RAGX is free software (AGPL-3.0)">
                source
              </a>
            </div>
          </div>
        </aside>
        <main className="main">
          {apiError && (
            <div className="error-box" style={{ marginBottom: 16 }}>
              {apiError}
            </div>
          )}
          {blocked ? (
            <div className="card empty">
              This page is for administrators. <Link href="/">Go to Ask</Link>
            </div>
          ) : (
            children
          )}
        </main>
      </div>
    </AppCtx.Provider>
  );
}

export function NeedKB({ children }: { children: (kb: KB) => ReactNode }) {
  const { kb, kbs, user } = useKB();
  if (!kb)
    return (
      <div className="card empty">
        {kbs.length === 0 ? (
          user?.role === "admin" ? (
            <>
              No knowledge base yet. <Link href="/knowledge">Create one and add documents</Link>.
            </>
          ) : (
            <>No course material has been published yet. Check back soon.</>
          )
        ) : (
          <span className="spinner" />
        )}
      </div>
    );
  return <>{children(kb)}</>;
}
