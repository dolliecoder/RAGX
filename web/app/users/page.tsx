"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/components/shell";
import { Badge, Empty, ErrorBox, PageHead, Spinner, Tile, ago } from "@/components/ui";
import { api, post } from "@/lib/api";
import { useFetch } from "@/lib/hooks";
import type { AccessPolicy, AuthOptions, PlanLimits, User, UsageStats } from "@/lib/types";

function randomCode() {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

function Bars({ days }: { days: UsageStats["days"] }) {
  if (days.length === 0) return <Empty>No questions asked yet.</Empty>;
  const max = Math.max(1, ...days.map((d) => d.questions));
  const w = 600;
  const h = 120;
  const bw = Math.min(40, (w - 20) / days.length - 4);
  return (
    <svg className="chart" viewBox={`0 0 ${w} ${h + 18}`} role="img" aria-label="questions per day">
      {days.map((d, i) => {
        const x = 10 + i * ((w - 20) / days.length);
        const bh = (d.questions / max) * h;
        return (
          <g key={d.day}>
            <rect x={x} y={h - bh} width={bw} height={Math.max(1, bh)} rx={3} fill="var(--accent)" opacity={0.85}>
              <title>{`${d.day}: ${d.questions} questions, ${d.active_users} people`}</title>
            </rect>
            {(i % Math.ceil(days.length / 7) === 0 || i === days.length - 1) && (
              <text x={x} y={h + 14}>
                {d.day.slice(5)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

function Overview() {
  const { data, error } = useFetch<UsageStats>("/usage?days=14");
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;
  const cap = data.today.global_limit;
  return (
    <>
      <div className="grid k4">
        <Tile label="Accounts" value={data.users.total} />
        <Tile label="Active today" value={data.users.active_today} hint="people who asked something" />
        <Tile
          label="Questions today"
          value={cap ? `${data.today.questions}/${cap}` : data.today.questions}
          hint={cap ? "site-wide daily cap" : "no site-wide cap"}
          tone={cap && data.today.questions >= cap ? "bad" : cap && data.today.questions >= cap * 0.8 ? "warn" : undefined}
        />
        <Tile label="Questions (14 days)" value={data.days.reduce((a, d) => a + d.questions, 0)} />
      </div>
      <div className="grid k2 mt">
        <div className="card">
          <h2>Questions per day</h2>
          <Bars days={data.days} />
        </div>
        <div className="card">
          <h2>Most active today</h2>
          {data.top_users_today.length === 0 ? (
            <Empty>Nobody has asked anything today.</Empty>
          ) : (
            <table className="t small">
              <tbody>
                {data.top_users_today.map((u) => (
                  <tr key={u.email}>
                    <td>{u.email}</td>
                    <td className="right">{u.questions}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </>
  );
}

function AccessForm() {
  const pol = useFetch<AccessPolicy>("/access");
  const opts = useFetch<AuthOptions>("/auth/options");
  const emailOn = !!opts.data?.email_enabled;
  const [p, setP] = useState<AccessPolicy | null>(null);
  const [domains, setDomains] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (pol.data) {
      setP(pol.data);
      setDomains(pol.data.allowed_email_domains.join(", "));
    }
  }, [pol.data]);

  if (!p) return <div className="card">{pol.error ? <ErrorBox error={pol.error} /> : <Spinner />}</div>;

  const setPlan = (name: string, field: keyof PlanLimits, value: number) =>
    setP({ ...p, plans: { ...p.plans, [name]: { ...p.plans[name], [field]: Math.max(0, Math.floor(value || 0)) } } });

  const save = async () => {
    setErr(null);
    setSaved(false);
    try {
      const body = { ...p, allowed_email_domains: domains.split(/[,\s]+/).filter(Boolean) };
      const r = await api<AccessPolicy>("/access", { method: "PUT", json: body });
      setP(r);
      setDomains(r.allowed_email_domains.join(", "));
      setSaved(true);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const invite = p.join_code ? `${window.location.origin}/signup?code=${encodeURIComponent(p.join_code)}` : "";

  return (
    <div className="card">
      <h2>Access &amp; limits</h2>
      <div className="grid k2">
        <div className="stack">
          <label className="row">
            <input type="checkbox" checked={p.signup_enabled} onChange={(e) => setP({ ...p, signup_enabled: e.target.checked })} />
            Anyone can create an account (sign-up open)
          </label>
          <label className="field">
            Allowed email domains (empty = any email)
            <input value={domains} onChange={(e) => setDomains(e.target.value)} placeholder="optional, e.g. example.org" />
          </label>
          <label className="field">
            Join code (empty = not required)
            <div className="row" style={{ flexWrap: "nowrap" }}>
              <input className="grow" value={p.join_code} onChange={(e) => setP({ ...p, join_code: e.target.value })} placeholder="e.g. CS101-FALL" />
              <button type="button" className="sm" onClick={() => setP({ ...p, join_code: randomCode() })}>
                Generate
              </button>
            </div>
          </label>
          {invite && (
            <div className="small">
              Invite link (save first):{" "}
              <button
                type="button"
                className="sm"
                onClick={() => {
                  void navigator.clipboard.writeText(invite).then(() => {
                    setCopied(true);
                    setTimeout(() => setCopied(false), 1500);
                  });
                }}
              >
                {copied ? "copied" : "copy link"}
              </button>
              <div className="faint truncate">{invite}</div>
            </div>
          )}
          <label className="row">
            <input
              type="checkbox"
              checked={p.require_email_verification}
              disabled={!emailOn}
              onChange={(e) => setP({ ...p, require_email_verification: e.target.checked })}
            />
            Members must confirm their email before asking
          </label>
          <p className="faint small">
            {emailOn
              ? "Email is set up: people can reset their own passwords and confirm their address, which makes the domain rule trustworthy."
              : "Email is not set up (see SMTP settings in .env), so addresses are not verified: the join code is what keeps outsiders out. Change it every term."}
          </p>
        </div>
        <div className="stack">
          <label className="field">
            Site-wide questions per day (0 = no cap; protects a free AI quota)
            <input type="number" min={0} value={p.global_daily_questions} onChange={(e) => setP({ ...p, global_daily_questions: Math.max(0, Number(e.target.value) || 0) })} />
          </label>
          <label className="field">
            Plan for new accounts
            <select value={p.default_plan} onChange={(e) => setP({ ...p, default_plan: e.target.value })}>
              {Object.keys(p.plans).map((n) => (
                <option key={n}>{n}</option>
              ))}
            </select>
          </label>
          <div className="table-wrap">
          <table className="t small">
            <thead>
              <tr>
                <th>Plan</th>
                <th>Per day</th>
                <th>Per minute</th>
                <th>Deep / day</th>
                <th>At once</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(p.plans).map(([name, lim]) => (
                <tr key={name}>
                  <td>
                    <b>{name}</b>
                  </td>
                  {(["daily_questions", "per_minute", "deep_per_day", "max_concurrent"] as const).map((f) => (
                    <td key={f}>
                      <input type="number" min={0} style={{ width: 70 }} value={lim[f]} onChange={(e) => setPlan(name, f, Number(e.target.value))} aria-label={`${name} ${f}`} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          <p className="faint small">0 questions per day = unlimited. Administrators are never limited.</p>
          <div className="table-wrap mt">
          <table className="t small">
            <thead>
              <tr>
                <th>Plan</th>
                <th>Workspaces</th>
                <th>Files each</th>
                <th>Pages each</th>
                <th>Max file (MB)</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(p.plans).map(([name, lim]) => (
                <tr key={name}>
                  <td>
                    <b>{name}</b>
                  </td>
                  {(["workspaces", "files_per_workspace", "pages_per_workspace", "max_file_mb"] as const).map((f) => (
                    <td key={f}>
                      <input type="number" min={0} style={{ width: 70 }} value={lim[f]} onChange={(e) => setPlan(name, f, Number(e.target.value))} aria-label={`${name} ${f}`} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          <p className="faint small">
            Limits for the private workspaces people create (0 = unlimited). Everyone&apos;s uploads use your AI quota, so keep these modest on a free key.
          </p>
        </div>
      </div>
      <div className="row mt">
        <button className="primary" onClick={() => void save()}>
          Save access settings
        </button>
        {saved && <span className="small" style={{ color: "var(--ok)" }}>Saved.</span>}
      </div>
      <ErrorBox error={err} />
    </div>
  );
}

function UserRow({ u, plans, me, onChange, onSecret }: { u: User; plans: string[]; me: string | null; onChange: () => void; onSecret: (msg: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [override, setOverride] = useState(u.daily_limit_override?.toString() ?? "");
  const patch = async (body: Record<string, unknown>) => {
    setBusy(true);
    try {
      await api(`/users/${u.id}`, { method: "PATCH", json: body });
      onChange();
    } catch (e) {
      window.alert((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const reset = async () => {
    if (!window.confirm(`Reset the password for ${u.email}?`)) return;
    const r = await post<{ temporary_password?: string; email_sent?: boolean }>(`/users/${u.id}/reset-password`);
    onSecret(r.email_sent ? `A password reset link was emailed to ${u.email}.` : `Temporary password for ${u.email}: ${r.temporary_password}`);
  };
  const remove = async () => {
    if (!window.confirm(`Delete ${u.email}? Their questions are kept anonymously.`)) return;
    try {
      await api(`/users/${u.id}`, { method: "DELETE" });
      onChange();
    } catch (e) {
      window.alert((e as Error).message);
    }
  };
  const us = u.usage;
  return (
    <tr style={{ opacity: u.active ? 1 : 0.55 }}>
      <td>
        <div>{u.name || "–"}</div>
        <div className="faint small">{u.email}</div>
        {u.must_change_password && <Badge value="temporary password" tone="warn" />}
        {!u.email_verified && <Badge value="email not confirmed" />}
      </td>
      <td>
        <select value={u.role} disabled={busy || u.id === me} onChange={(e) => void patch({ role: e.target.value })} aria-label="role">
          <option value="user">member</option>
          <option value="admin">admin</option>
        </select>
      </td>
      <td>
        <select value={u.plan} disabled={busy} onChange={(e) => void patch({ plan: e.target.value })} aria-label="plan">
          {plans.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
      </td>
      <td className="small">
        {u.role === "admin" ? (
          <span className="faint">unlimited</span>
        ) : (
          <>
            {us?.questions_today ?? 0}/{us?.daily_limit ?? "∞"}
            <div className="row" style={{ flexWrap: "nowrap", marginTop: 4 }}>
              <input
                type="number"
                min={0}
                placeholder="plan"
                title="Personal daily limit (empty = use plan)"
                style={{ width: 70 }}
                value={override}
                onChange={(e) => setOverride(e.target.value)}
                onBlur={() => {
                  const cur = u.daily_limit_override?.toString() ?? "";
                  if (override === cur) return;
                  void patch(override === "" ? { clear_daily_limit_override: true } : { daily_limit_override: Number(override) });
                }}
                aria-label="personal daily limit"
              />
            </div>
          </>
        )}
      </td>
      <td className="faint small">{ago(u.last_seen_at ?? u.last_login_at)}</td>
      <td className="right">
        <div className="row" style={{ justifyContent: "flex-end" }}>
          {u.id !== me && (
            <button className="sm" disabled={busy} onClick={() => void patch({ active: !u.active })}>
              {u.active ? "Disable" : "Enable"}
            </button>
          )}
          <button className="sm" onClick={() => void reset()}>
            Reset password
          </button>
          {u.id !== me && (
            <button className="sm danger" onClick={() => void remove()}>
              Delete
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

function UserList() {
  const { user } = useAuth();
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const users = useFetch<User[]>(`/users?q=${encodeURIComponent(query)}`);
  const pol = useFetch<AccessPolicy>("/access");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState("user");
  const [secret, setSecret] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const t = setTimeout(() => setQuery(q), 300);
    return () => clearTimeout(t);
  }, [q]);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    try {
      const r = await post<{ user: User; temporary_password?: string; invite_sent: boolean }>("/users", { email, name, role });
      setSecret(
        r.invite_sent
          ? `Invitation emailed to ${r.user.email}. They choose their own password from the link.`
          : `Account created for ${r.user.email}. Temporary password: ${r.temporary_password}`,
      );
      setEmail("");
      setName("");
      void users.reload();
    } catch (ex) {
      setErr((ex as Error).message);
    }
  };

  const plans = Object.keys(pol.data?.plans ?? { free: 0, pro: 0 });
  return (
    <div className="card">
      <div className="spread">
        <h2>People {users.data && <span className="faint">({users.data.length})</span>}</h2>
        <input placeholder="Search name or email" value={q} onChange={(e) => setQ(e.target.value)} aria-label="search users" />
      </div>
      <form className="row" onSubmit={create} style={{ marginBottom: 12 }}>
        <input placeholder="name@example.com" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} aria-label="new user email" />
        <input placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} aria-label="new user name" />
        <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="new user role">
          <option value="user">member</option>
          <option value="admin">admin</option>
        </select>
        <button className="primary">Add account</button>
      </form>
      <ErrorBox error={err ?? users.error} />
      {secret && (
        <div className="note-box small" style={{ marginBottom: 12 }}>
          <span className="secret">{secret}</span>
          {secret.startsWith("Temporary") || secret.includes("Temporary password") ? " — share it privately; it is shown only once and must be changed at first sign-in. " : " "}
          <button className="sm ghost" onClick={() => setSecret(null)}>
            dismiss
          </button>
        </div>
      )}
      {users.data && users.data.length === 0 && <Empty>No accounts match.</Empty>}
      {users.data && users.data.length > 0 && (
        <div className="table-wrap">
          <table className="t">
            <thead>
              <tr>
                <th>Person</th>
                <th>Role</th>
                <th>Plan</th>
                <th>Today / limit</th>
                <th>Last seen</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {users.data.map((u) => (
                <UserRow key={u.id} u={u} plans={plans} me={user?.id ?? null} onChange={() => void users.reload()} onSecret={setSecret} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function UsersPage() {
  return (
    <>
      <PageHead title="Users" sub="Accounts, sign-up rules and usage limits" />
      <Overview />
      <div className="mt" />
      <AccessForm />
      <UserList />
    </>
  );
}
