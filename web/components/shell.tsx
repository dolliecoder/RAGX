"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { SOURCE_URL, api, post } from "@/lib/api";
import { storageGet, storageSet } from "@/lib/hooks";
import type { AuthOptions, ChatBrief, KB, Usage, User } from "@/lib/types";
import { FilesPanel, useUploads, type Uploads } from "./files";
import { Icon, type IconName } from "./icons";

type Ctx = {
  user: User | null;
  setUsage: (u: Usage) => void;
  refreshUser: () => Promise<void>;
  kbs: KB[];
  kb: KB | null;
  setKb: (id: string) => void;
  refresh: () => Promise<void>;
  apiError: string | null;
  chats: ChatBrief[];
  reloadChats: () => Promise<void>;
  uploads: Uploads;
  openFiles: () => void;
  createWorkspace: (name: string) => Promise<void>;
  kbsLoaded: boolean;
};
const NO_UPLOADS: Uploads = { items: [], busy: false, upload: async () => {}, clear: () => {}, version: 0 };
const AppCtx = createContext<Ctx>({
  user: null,
  setUsage: () => {},
  refreshUser: async () => {},
  kbs: [],
  kb: null,
  setKb: () => {},
  refresh: async () => {},
  apiError: null,
  chats: [],
  reloadChats: async () => {},
  uploads: NO_UPLOADS,
  openFiles: () => {},
  createWorkspace: async () => {},
  kbsLoaded: false,
});
export const useKB = () => useContext(AppCtx);
export const useAuth = () => useContext(AppCtx);

const PUBLIC = ["/login", "/signup", "/forgot", "/reset", "/verify"];
const ADMIN_ONLY = ["/health", "/repair", "/evals", "/users", "/settings"];
// /knowledge/<doc> (opened from citations) is for everyone who can see the file; the list page is admin-only
const isAdminOnly = (path: string) => path === "/knowledge" || ADMIN_ONLY.some((p) => path.startsWith(p));

type NavItem = { href: string; label: string; icon: IconName; badge?: boolean };
const ADMIN_NAV: NavItem[] = [
  { href: "/health", label: "Health", icon: "health" },
  { href: "/traces", label: "Traces", icon: "list" },
  { href: "/repair", label: "Repair", icon: "wrench", badge: true },
  { href: "/evals", label: "Evals", icon: "check" },
  { href: "/users", label: "Users", icon: "users" },
  { href: "/settings", label: "Settings", icon: "settings" },
];

/** Opening a chat updates ?c= without a route change; this tells the sidebar. */
export const CHAT_EVENT = "ragx:chat";

function parseDate(iso: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z");
}

/** Today / Yesterday / Previous 7 days / Previous 30 days / Older. */
function groupChats(chats: ChatBrief[]): [string, ChatBrief[]][] {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const start = today.getTime();
  const day = 86400000;
  const label = (c: ChatBrief) => {
    const t = parseDate(c.updated_at).getTime();
    if (t >= start) return "Today";
    if (t >= start - day) return "Yesterday";
    if (t >= start - 7 * day) return "Previous 7 days";
    if (t >= start - 30 * day) return "Previous 30 days";
    return "Older";
  };
  const groups = new Map<string, ChatBrief[]>();
  for (const c of chats) {
    const k = label(c);
    groups.set(k, [...(groups.get(k) ?? []), c]);
  }
  return [...groups.entries()];
}

function UsageMeter({ usage }: { usage: Usage }) {
  if (usage.daily_limit === null) return null;
  const used = usage.questions_today;
  const pct = Math.min(100, (used / Math.max(1, usage.daily_limit)) * 100);
  const color = pct >= 100 ? "var(--bad)" : pct >= 80 ? "var(--warn)" : "var(--ok)";
  return (
    <div className="usage" title={`Resets ${new Date(usage.resets_at).toLocaleString()}`}>
      <div className="spread">
        <span>Questions today</span>
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

function WorkspaceMenu({ kbs, kb, onPick, onCreate }: { kbs: KB[]; kb: KB | null; onPick: (id: string) => void; onCreate: (name: string) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  useEffect(() => {
    if (!open) {
      setCreating(false);
      setName("");
      setErr(null);
    }
  }, [open]);

  const mine = kbs.filter((k) => !k.shared);
  const shared = kbs.filter((k) => k.shared);
  const create = async () => {
    if (!name.trim()) return;
    try {
      await onCreate(name.trim());
      setOpen(false);
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const item = (k: KB) => (
    <button
      key={k.id}
      role="menuitemradio"
      aria-checked={k.id === kb?.id}
      className={k.id === kb?.id ? "selected" : ""}
      onClick={() => {
        onPick(k.id);
        setOpen(false);
      }}
    >
      <span className="ws-tile">{k.name.trim()[0]?.toUpperCase() ?? "?"}</span>
      <span className="grow truncate">{k.name}</span>
      {k.visibility === "admins" && <span className="faint small">admins</span>}
      {k.id === kb?.id && <Icon name="check" size={15} />}
    </button>
  );

  return (
    <div className="ws-menu" ref={ref}>
      <button className="ws-btn" onClick={() => setOpen((o) => !o)} aria-haspopup="menu" aria-expanded={open}>
        <span className="ws-tile">{kb?.name.trim()[0]?.toUpperCase() ?? "+"}</span>
        <span className="grow" style={{ minWidth: 0 }}>
          <span className="ws-name truncate">{kb ? kb.name : "Choose a workspace"}</span>
          <span className="ws-sub">{kb ? (kb.shared ? "Shared" : "Private") + ` · ${kb.documents} file${kb.documents === 1 ? "" : "s"}` : "Workspaces"}</span>
        </span>
        <Icon name="chevron" size={14} style={{ transform: open ? "rotate(-90deg)" : "rotate(90deg)" }} />
      </button>
      {open && (
        <div className="menu ws-list" role="menu">
          {mine.length > 0 && <div className="menu-head">My workspaces</div>}
          {mine.map(item)}
          {shared.length > 0 && <div className="menu-head">Shared with everyone</div>}
          {shared.map(item)}
          <div className="menu-sep" />
          {creating ? (
            <form
              className="ws-create"
              onSubmit={(e) => {
                e.preventDefault();
                void create();
              }}
            >
              <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Math, Physics, Biology" maxLength={200} aria-label="Workspace name" />
              <button className="sm primary" type="submit" disabled={!name.trim()}>
                Create
              </button>
            </form>
          ) : (
            <button role="menuitem" onClick={() => setCreating(true)}>
              <Icon name="plus" size={16} /> New workspace
            </button>
          )}
          {err && <div className="error-box small" style={{ margin: 6 }}>{err}</div>}
        </div>
      )}
    </div>
  );
}

function UserMenu({ user, onSignOut }: { user: User; onSignOut: () => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  const initial = (user.name || user.email).trim()[0]?.toUpperCase() ?? "?";
  return (
    <div className="user-menu" ref={ref}>
      {open && (
        <div className="menu" role="menu">
          <div className="menu-head truncate" title={user.email}>
            {user.email}
          </div>
          <Link href="/account" role="menuitem" onClick={() => setOpen(false)}>
            <Icon name="user" size={16} /> Account
          </Link>
          <a href={SOURCE_URL} target="_blank" rel="noreferrer" role="menuitem" title="RAGX is free software (AGPL-3.0)">
            <Icon name="code" size={16} /> Source code
          </a>
          <button role="menuitem" onClick={onSignOut}>
            <Icon name="logout" size={16} /> Sign out
          </button>
        </div>
      )}
      <button className="user-btn" onClick={() => setOpen((o) => !o)} aria-haspopup="menu" aria-expanded={open}>
        <span className="avatar">{initial}</span>
        <span className="grow truncate">{user.name || user.email}</span>
        {user.role === "admin" && <span className="badge accent">admin</span>}
      </button>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const isLanding = path === "/";
  const isPublic = isLanding || PUBLIC.some((p) => path.startsWith(p));
  const [user, setUser] = useState<User | null>(null);
  const [checked, setChecked] = useState(false);
  const [kbs, setKbs] = useState<KB[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [pending, setPending] = useState(0);
  const [opts, setOpts] = useState<AuthOptions | null>(null);
  const [resent, setResent] = useState<string | null>(null);
  const [chats, setChats] = useState<ChatBrief[]>([]);
  const [drawer, setDrawer] = useState(false); // phones: sidebar slid open
  const [collapsed, setCollapsed] = useState(false); // desktop: sidebar hidden
  const [activeChat, setActiveChat] = useState<string | null>(null);
  const [filesOpen, setFilesOpen] = useState(false);
  const [kbsLoaded, setKbsLoaded] = useState(false);

  useEffect(() => {
    api<AuthOptions>("/auth/options")
      .then(setOpts)
      .catch(() => {});
    setCollapsed(storageGet("ragx.sidebar") === "closed");
  }, []);

  // the open chat is ?c= on /ask (read directly, so no Suspense boundary is needed)
  useEffect(() => {
    const read = () => setActiveChat(path === "/ask" ? new URLSearchParams(window.location.search).get("c") : null);
    read();
    window.addEventListener(CHAT_EVENT, read);
    window.addEventListener("popstate", read);
    return () => {
      window.removeEventListener(CHAT_EVENT, read);
      window.removeEventListener("popstate", read);
    };
  }, [path]);

  useEffect(() => setDrawer(false), [path, activeChat]);

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
      setKbsLoaded(true);
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
  const kbId = kb?.id;

  const reloadChats = useCallback(async () => {
    if (!kbId) {
      setChats([]);
      return;
    }
    try {
      setChats(await api<ChatBrief[]>(`/kbs/${kbId}/chats`));
    } catch {
      /* the list is a convenience; asking still works without it */
    }
  }, [kbId]);

  useEffect(() => {
    if (user) void reloadChats();
  }, [user?.id, reloadChats]); // eslint-disable-line react-hooks/exhaustive-deps

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
    if (path === "/ask") router.push("/ask");
  };
  const uploads = useUploads(kb, () => void refresh());
  const createWorkspace = async (name: string) => {
    const created = await api<KB>("/kbs", { method: "POST", json: { name, shared: false } });
    await refresh();
    setKb(created.id);
    if (path !== "/ask") router.push("/ask");
  };
  const workspaceDeleted = async () => {
    setFilesOpen(false);
    setSelected(null);
    await refresh();
    router.push("/ask");
  };
  const setUsage = (u: Usage) => setUser((cur) => (cur ? { ...cur, usage: u } : cur));

  const signOut = async () => {
    try {
      await post("/auth/logout");
    } finally {
      setUser(null);
      window.location.assign("/");
    }
  };

  const toggleSidebar = () => {
    setCollapsed((c) => {
      storageSet("ragx.sidebar", c ? "open" : "closed");
      return !c;
    });
  };

  const removeChat = async (c: ChatBrief) => {
    if (!kb || !window.confirm(`Delete “${c.title}”? It will disappear from your chats.`)) return;
    try {
      await api(`/kbs/${kb.id}/chats/${encodeURIComponent(c.id)}`, { method: "DELETE" });
      setChats((list) => list.filter((x) => x.id !== c.id));
      if (activeChat === c.id) router.push("/ask");
    } catch (e) {
      window.alert((e as Error).message);
    }
  };

  const ctx: Ctx = {
    user,
    setUsage,
    refreshUser,
    kbs,
    kb,
    setKb,
    refresh,
    apiError,
    chats,
    reloadChats,
    uploads,
    openFiles: () => setFilesOpen(true),
    createWorkspace,
    kbsLoaded,
  };

  if (isLanding) {
    return <AppCtx.Provider value={ctx}>{children}</AppCtx.Provider>;
  }

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

  const blocked = !isAdmin && isAdminOnly(path);
  const isChat = path === "/ask";
  const unverified = !!opts?.require_email_verification && !user.email_verified && !isAdmin;

  return (
    <AppCtx.Provider value={ctx}>
      <div className={`app${collapsed ? " collapsed" : ""}${drawer ? " drawer-open" : ""}`}>
        <aside className="side" aria-label="Chats and navigation">
          <div className="side-top">
            <Link href="/" className="brand brand-link" title="RAGX home">
              <span className="brand-mark">RX</span> RAGX
            </Link>
            <button className="icon-btn hide-phone" onClick={toggleSidebar} title="Close sidebar" aria-label="Close sidebar">
              <Icon name="sidebar" />
            </button>
            <button className="icon-btn show-phone" onClick={() => setDrawer(false)} aria-label="Close menu">
              <Icon name="close" />
            </button>
          </div>

          <Link href="/ask" className={`new-chat${isChat && !activeChat ? " active" : ""}`}>
            <Icon name="edit" size={17} /> New chat
          </Link>

          <WorkspaceMenu kbs={kbs} kb={kb} onPick={setKb} onCreate={createWorkspace} />

          <nav className="side-scroll">
            {isAdmin && (
              <div className="side-group">
                <div className="side-label">Admin</div>
                {ADMIN_NAV.map((n) => (
                  <Link key={n.href} href={n.href} className={`side-item${path.startsWith(n.href) ? " active" : ""}`}>
                    <Icon name={n.icon} size={16} />
                    <span className="grow truncate">{n.label}</span>
                    {n.badge && pending > 0 && (
                      <span className="count" title="fixes awaiting approval">
                        {pending}
                      </span>
                    )}
                  </Link>
                ))}
              </div>
            )}
            {chats.length === 0 ? (
              <div className="side-group">
                <div className="side-label">Chats</div>
                <div className="side-empty">Your conversations will show up here.</div>
              </div>
            ) : (
              groupChats(chats).map(([label, list]) => (
                <div className="side-group" key={label}>
                  <div className="side-label">{label}</div>
                  {list.map((c) => (
                    <div key={c.id} className={`side-item chat-item${activeChat === c.id ? " active" : ""}`}>
                      <Link href={`/ask?c=${encodeURIComponent(c.id)}`} className="grow truncate" title={c.title}>
                        {c.title}
                      </Link>
                      <button className="icon-btn chat-del" onClick={() => void removeChat(c)} title="Delete chat" aria-label={`Delete chat: ${c.title}`}>
                        <Icon name="trash" size={15} />
                      </button>
                    </div>
                  ))}
                </div>
              ))
            )}
          </nav>

          <div className="side-bottom">
            {user.usage && <UsageMeter usage={user.usage} />}
            <UserMenu user={user} onSignOut={() => void signOut()} />
          </div>
        </aside>
        <div className="scrim" onClick={() => setDrawer(false)} aria-hidden />

        <div className="app-main">
          <header className="topbar">
            <button className="icon-btn show-phone" onClick={() => setDrawer(true)} aria-label="Open menu">
              <Icon name="menu" />
            </button>
            {collapsed && (
              <button className="icon-btn hide-phone" onClick={toggleSidebar} title="Open sidebar" aria-label="Open sidebar">
                <Icon name="sidebar" />
              </button>
            )}
            <span className="topbar-title truncate">{kb ? kb.name : "RAGX"}</span>
            {kb && isChat && (
              <button className={`files-btn${filesOpen ? " on" : ""}`} onClick={() => setFilesOpen((o) => !o)} aria-expanded={filesOpen} title="Files in this workspace">
                <Icon name="doc" size={16} />
                <span className="hide-phone">Files</span>
                <span className="files-count">{kb.documents}</span>
              </button>
            )}
            <Link href="/ask" className="icon-btn topbar-new" title="New chat" aria-label="New chat">
              <Icon name="edit" />
            </Link>
          </header>
          {(apiError || unverified) && (
            <div className="banners">
              {apiError && <div className="error-box">{apiError}</div>}
              {unverified && (
                <div className="note-box">
                  Please confirm your email address: open the link we sent to <b>{user.email}</b>.{" "}
                  <button
                    className="sm"
                    onClick={async () => {
                      try {
                        await post("/auth/resend-verification");
                        setResent("Sent. Check your inbox and spam folder.");
                      } catch (e) {
                        setResent((e as Error).message);
                      }
                    }}
                  >
                    Resend email
                  </button>{" "}
                  {resent && <span className="small">{resent}</span>}
                </div>
              )}
            </div>
          )}
          {filesOpen && kb && isChat && (
            <FilesPanel
              key={kb.id}
              kb={kb}
              uploads={uploads}
              isAdmin={isAdmin}
              onClose={() => setFilesOpen(false)}
              onChanged={refresh}
              onDeleted={() => void workspaceDeleted()}
            />
          )}
          <main className={isChat ? "chat-main" : "main"}>
            {blocked ? (
              <div className="card empty">
                This page is for administrators. <Link href="/ask">Go to chat</Link>
              </div>
            ) : (
              children
            )}
          </main>
        </div>
      </div>
    </AppCtx.Provider>
  );
}

function FirstWorkspace() {
  const { createWorkspace } = useKB();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const create = async (n: string) => {
    if (!n.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await createWorkspace(n.trim());
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="hero">
      <div className="hero-inner">
        <h1 className="hero-title">Make your first workspace</h1>
        <p className="hero-sub">A workspace is a folder for one subject: add its PDFs and notes, then ask questions about them.</p>
        <form
          className="composer-box ws-first"
          onSubmit={(e) => {
            e.preventDefault();
            void create(name);
          }}
        >
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name it, e.g. Math" autoFocus maxLength={200} aria-label="Workspace name" />
          <button className="primary" type="submit" disabled={busy || !name.trim()}>
            {busy ? <span className="spinner" /> : "Create"}
          </button>
        </form>
        <div className="suggestions">
          {["Math", "Physics", "Biology", "History"].map((n) => (
            <button key={n} className="suggestion" onClick={() => void create(n)} disabled={busy}>
              {n}
            </button>
          ))}
        </div>
        {err && <div className="error-box mt">{err}</div>}
      </div>
    </div>
  );
}

export function NeedKB({ children }: { children: (kb: KB) => ReactNode }) {
  const { kb, kbs, kbsLoaded } = useKB();
  if (!kb) {
    if (kbs.length === 0 && kbsLoaded) return <FirstWorkspace />;
    return (
      <div className="card empty">
        <span className="spinner" />
      </div>
    );
  }
  return <>{children(kb)}</>;
}
