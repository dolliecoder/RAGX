"""RAGX command line: a thin client of the HTTP API (plus `serve`)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(help="RAGX - self-healing RAG platform", no_args_is_help=True)
kb_app = typer.Typer(help="Knowledge bases", no_args_is_help=True)
source_app = typer.Typer(help="Document sources", no_args_is_help=True)
repair_app = typer.Typer(help="Repair loop: diagnoses and fixes", no_args_is_help=True)
fix_app = typer.Typer(help="Act on a proposed fix", no_args_is_help=True)
golden_app = typer.Typer(help="Golden evaluation set", no_args_is_help=True)
eval_app = typer.Typer(help="Evaluations", no_args_is_help=True)
config_app = typer.Typer(help="Runtime configuration", no_args_is_help=True)
users_app = typer.Typer(help="Accounts (admin)", no_args_is_help=True)
admin_app = typer.Typer(help="Server-side admin tools (run where the database is)", no_args_is_help=True)
for sub, name in (
    (users_app, "users"),
    (admin_app, "admin"),
    (kb_app, "kb"),
    (source_app, "source"),
    (repair_app, "repair"),
    (fix_app, "fix"),
    (golden_app, "golden"),
    (eval_app, "eval"),
    (config_app, "config"),
):
    app.add_typer(sub, name=name)

con = Console()


CRED_FILE = Path(os.environ.get("RAGX_CREDENTIALS", Path.home() / ".ragx" / "credentials.json"))


def _load_token() -> str | None:
    try:
        return json.loads(CRED_FILE.read_text(encoding="utf-8")).get("token")
    except (OSError, ValueError):
        return None


def _save_token(token: str | None, base: str) -> None:
    if token is None:
        CRED_FILE.unlink(missing_ok=True)
        return
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    CRED_FILE.write_text(json.dumps({"token": token, "api": base}), encoding="utf-8")
    try:
        os.chmod(CRED_FILE, 0o600)
    except OSError:
        pass


class Client:
    def __init__(self) -> None:
        self.base = os.environ.get("RAGX_API_URL", "http://localhost:8000").rstrip("/")
        headers = {"x-ragx-csrf": "1"}
        if os.environ.get("RAGX_API_KEY"):
            headers["X-API-Key"] = os.environ["RAGX_API_KEY"]
        elif token := _load_token():
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(base_url=self.base, headers=headers, timeout=600)

    def req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self.http.request(method, path, **kw)
        except httpx.ConnectError:
            con.print(f"[red]Cannot reach the RAGX API at {self.base}. Start it with `ragx serve` or set RAGX_API_URL.[/]")
            raise typer.Exit(2)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text
            con.print(f"[red]Error {r.status_code}:[/] {detail}")
            if r.status_code == 401:
                con.print("Sign in with `ragx login` (or set RAGX_API_KEY).")
            raise typer.Exit(1)
        return r.json()

    def get(self, path: str, **kw: Any) -> Any:
        return self.req("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.req("POST", path, **kw)


def c() -> Client:
    return Client()


def wait_job(cl: Client, job_id: str, label: str = "job") -> dict[str, Any]:
    with con.status(f"waiting for {label} {job_id[:8]}..."):
        while True:
            j = cl.get(f"/api/jobs/{job_id}")
            if j["status"] in ("done", "failed"):
                break
            time.sleep(1.5)
    if j["status"] == "failed":
        con.print(f"[red]{label} failed:[/] {(j.get('error') or '').splitlines()[0] if j.get('error') else ''}")
    return j


def dump(obj: Any) -> None:
    con.print_json(json.dumps(obj, default=str))


# -------------------------------------------------------------------- auth
@app.command()
def login(email: str = typer.Option(..., prompt=True), password: str = typer.Option(..., prompt=True, hide_input=True)) -> None:
    """Sign in; the session is stored in ~/.ragx/credentials.json."""
    cl = c()
    cl.http.headers.pop("Authorization", None)
    r = cl.post("/api/auth/login", json={"email": email, "password": password, "return_token": True})
    _save_token(r["token"], cl.base)
    u = r["user"]
    con.print(f"Signed in as {u['email']} ({u['role']}).")
    if u.get("must_change_password"):
        con.print("[yellow]Please change your temporary password with `ragx password`.[/]")


@app.command()
def logout() -> None:
    """Sign out and forget the stored session."""
    if _load_token():
        try:
            c().post("/api/auth/logout")
        except typer.Exit:
            pass
    _save_token(None, "")
    con.print("Signed out.")


@app.command()
def whoami() -> None:
    """Show the signed-in account and today's usage."""
    dump(c().get("/api/auth/me")["user"])


@app.command()
def password(
    current: str = typer.Option(..., prompt="Current password", hide_input=True),
    new: str = typer.Option(..., prompt="New password", hide_input=True, confirmation_prompt=True),
) -> None:
    """Change your password (signs out other devices)."""
    cl = c()
    cl.post("/api/auth/password", json={"current_password": current, "new_password": new})
    con.print("Password changed. Other sessions were signed out; run `ragx login` again here.")
    _save_token(None, "")


@users_app.command("list")
def users_list(search: str = "") -> None:
    t = Table("id", "email", "name", "role", "plan", "today", "active", "last seen")
    for u in c().get("/api/users", params={"q": search}):
        us = u.get("usage") or {}
        today = f"{us.get('questions_today', 0)}/{us.get('daily_limit') or '∞'}"
        t.add_row(u["id"][:12], u["email"], u["name"], u["role"], u["plan"], today, "yes" if u["active"] else "no", (u["last_seen_at"] or "-")[:16])
    con.print(t)


@users_app.command("create")
def users_create(email: str, name: str = "", admin: bool = False, plan: Optional[str] = None) -> None:
    """Create an account; prints a temporary password to hand over."""
    r = c().post("/api/users", json={"email": email, "name": name, "role": "admin" if admin else "user", "plan": plan})
    con.print(f"Created {r['user']['email']}. Temporary password: [bold]{r['temporary_password']}[/] (must be changed at first sign-in)")


@admin_app.command("create-user")
def admin_create_user(
    email: str,
    admin: bool = typer.Option(False, help="give the account the admin role"),
    password: Optional[str] = typer.Option(None, help="omit to generate a temporary one"),
) -> None:
    """Create an account directly in the database (bootstrap the first admin)."""
    from .auth import create_user
    from .bootstrap import init_app
    from .control import audit
    from .db import session_scope

    init_app()
    with session_scope() as s:
        user, pw = create_user(s, email=email, role="admin" if admin else "user", password=password)
        audit(s, "cli:admin", "user.create", user.id, email=user.email, role=user.role)
    con.print(f"Created {email} ({'admin' if admin else 'user'}). Password: [bold]{pw}[/]")


@admin_app.command("reset-password")
def admin_reset_password(email: str) -> None:
    """Reset a password directly in the database (account recovery)."""
    from sqlalchemy import select

    from .auth import end_all_sessions, hash_password, normalize_email, temp_password
    from .bootstrap import init_app
    from .control import audit
    from .db import session_scope
    from .models import User

    init_app()
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == normalize_email(email)))
        if user is None:
            con.print(f"[red]No account for {email}[/]")
            raise typer.Exit(1)
        pw = temp_password()
        user.password_hash = hash_password(pw)
        user.must_change_password = True
        user.active = True
        end_all_sessions(s, user.id)
        audit(s, "cli:admin", "user.reset_password", user.id, email=user.email)
    con.print(f"New temporary password for {email}: [bold]{pw}[/]")


# ------------------------------------------------------------------ server
@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Run the RAGX API server."""
    import uvicorn

    uvicorn.run("ragx.api:app", host=host, port=port, reload=reload)


@app.command()
def providers() -> None:
    """Show model providers, fallbacks and circuit-breaker state."""
    st = c().get("/api/providers")
    t = Table("role", "chain", "notes")
    for role, info in st["roles"].items():
        chain = ", ".join(f"{p['provider']} ({p['state']})" for p in info["chain"])
        t.add_row(role + (" [yellow](offline)[/]" if info["offline"] else ""), chain, "\n".join(info["notes"]))
    con.print(t)
    con.print(f"embedder: {st['embedder']['model']} {st['embedder']['note']}")
    con.print(f"reranker: {st['reranker']}   web search: {st['web_search']}")
    if not st["verifier_independent"]:
        con.print("[yellow]warning: generator and verifier are the same model; verification is not independent[/]")


@app.command()
def models(provider: str = typer.Argument(..., help="ollama | gemini | groq | openrouter"), search: str = "") -> None:
    """List models you can use with a provider (all listed here are free)."""
    t = Table("model", "kind", "note")
    for m in c().get("/api/providers/models", params={"provider": provider}):
        if search.lower() in m["id"].lower():
            t.add_row(m["id"], m.get("kind", "chat"), m.get("note", ""))
    con.print(t)
    con.print(f"Use one as e.g. RAGX_GENERATOR={provider}:<model> in .env, then restart.")


# --------------------------------------------------------------------- kbs
@kb_app.command("list")
def kb_list() -> None:
    t = Table("id", "name", "documents", "tokens")
    for kb in c().get("/api/kbs"):
        t.add_row(kb["id"], kb["name"], str(kb["documents"]), str(kb["tokens"]))
    con.print(t)


@kb_app.command("create")
def kb_create(name: str, description: str = "") -> None:
    dump(c().post("/api/kbs", json={"name": name, "description": description}))


@kb_app.command("delete")
def kb_delete(kb: str, yes: bool = typer.Option(False, "--yes", help="skip confirmation")) -> None:
    if not yes and not typer.confirm(f"Delete knowledge base {kb} and all its documents?"):
        raise typer.Exit()
    dump(c().req("DELETE", f"/api/kbs/{kb}"))


# ----------------------------------------------------------------- sources
@source_app.command("add")
def source_add(
    kb: str,
    location: str,
    kind: Optional[str] = typer.Option(None, help="file | directory | url (auto-detected)"),
    no_recursive: bool = False,
    authority: float = 1.0,
    wait: bool = True,
) -> None:
    """Add a directory, file or URL as a source and crawl it."""
    if kind is None:
        kind = "url" if location.startswith(("http://", "https://")) else ("directory" if Path(location).is_dir() else "file")
    uri = location if kind == "url" else str(Path(location).resolve())
    cl = c()
    res = cl.post(f"/api/kbs/{kb}/sources", json={"kind": kind, "uri": uri, "recursive": not no_recursive, "authority": authority})
    con.print(f"source {res['id']} added")
    if wait:
        dump(wait_job(cl, res["job_id"], "crawl").get("result"))


@source_app.command("list")
def source_list(kb: str) -> None:
    t = Table("id", "kind", "uri", "status", "last crawled")
    for s in c().get(f"/api/kbs/{kb}/sources"):
        t.add_row(s["id"], s["kind"], s["uri"], s["status"] + (f" ({s['last_error']})" if s["last_error"] else ""), s["last_crawled_at"] or "-")
    con.print(t)


@source_app.command("crawl")
def source_crawl(source_id: str, force: bool = False, wait: bool = True) -> None:
    cl = c()
    res = cl.post(f"/api/sources/{source_id}/crawl", params={"force": force})
    if wait:
        dump(wait_job(cl, res["job_id"], "crawl").get("result"))


@app.command()
def upload(kb: str, files: list[Path], wait: bool = True) -> None:
    """Upload PDF/DOCX/HTML/MD/TXT files into a knowledge base."""
    cl = c()
    handles = [("files", (f.name, f.read_bytes())) for f in files]
    res = cl.post(f"/api/kbs/{kb}/upload", files=handles)
    for r in res["rejected"]:
        con.print(f"[yellow]rejected {r['file']}: {r['reason']}[/]")
    if res.get("job_id") and wait:
        dump(wait_job(cl, res["job_id"], "ingest").get("result"))


@app.command()
def docs(kb: str) -> None:
    """List documents with status, tier and quality."""
    t = Table("id", "title", "status", "tier", "ver", "chunks", "quality")
    for d in c().get(f"/api/kbs/{kb}/documents"):
        t.add_row(d["id"][:12], d["title"][:60], d["status"], d["tier"], str(d["version"]), str(d["chunks"]), str((d["quality"] or {}).get("score", "-")))
    con.print(t)


# ------------------------------------------------------------------- query
STATUS_COLOR = {"verified": "green", "partial": "yellow", "failed": "red", "error": "red", "direct": "cyan", "escalated": "magenta"}


@app.command()
def ask(
    kb: str,
    question: str,
    mode: str = typer.Option("auto", help="auto | fast | standard | deep"),
    session: Optional[str] = None,
    show_trace: bool = False,
    wait_deep: bool = True,
) -> None:
    """Ask a question; prints a verified, cited answer."""
    cl = c()
    r = cl.post(f"/api/kbs/{kb}/query", json={"query": question, "mode": mode, "session_id": session})
    if r["status"] == "escalated" and r.get("job_id") and wait_deep:
        con.print("[magenta]deep research started...[/]")
        j = wait_job(cl, r["job_id"], "deep research")
        if j["status"] == "done":
            r = {**r, **j["result"], "route": "deep"}
    color = STATUS_COLOR.get(r["status"], "white")
    m = r.get("metrics") or {}
    sub = f"route={r['route']}  status=[{color}]{r['status']}[/]"
    if m.get("groundedness") is not None:
        sub += f"  grounded={m['groundedness']:.2f} coverage={m.get('coverage', 0):.2f} confidence={m.get('confidence', 0):.2f}"
    if r.get("healed"):
        sub += "  [cyan]healed[/]"
    if r.get("offline"):
        sub += "  [yellow]OFFLINE provider[/]"
    con.print(Panel(Markdown(r["answer"]), title="answer", subtitle=sub))
    for ci in r.get("citations", []):
        loc = ci.get("url") or ci.get("title")
        con.print(f"  [bold][{ci['n']}][/] {loc} [dim]{ci.get('section') or ''}[/]\n      [dim]{ci['cited_text'][:160]}[/]")
    if r.get("job_id") and r["status"] != "escalated":
        con.print(f"[magenta]deep research job started for the unverified parts: {r['job_id']}[/]")
    if r.get("trace_id"):
        con.print(f"[dim]trace {r['trace_id']}  ({r['llm']['calls']} model calls, {r['latency_ms']} ms)[/]")
        if show_trace:
            trace(r["trace_id"])


@app.command()
def feedback(
    trace_id: str,
    rating: int = typer.Option(..., min=-1, max=1),
    comment: str = "",
    correction: str = "",
) -> None:
    """Give feedback on an answer (-1 / 0 / +1). A correction becomes a golden test."""
    dump(c().post(f"/api/traces/{trace_id}/feedback", json={"rating": rating, "comment": comment, "correction": correction}))


@app.command()
def traces(kb: str, status: Optional[str] = None, limit: int = 20) -> None:
    """List recent traces."""
    data = c().get(f"/api/kbs/{kb}/traces", params={"status": status, "limit": limit} if status else {"limit": limit})
    t = Table("id", "when", "route", "status", "grounded", "healed", "calls", "query")
    for x in data["items"]:
        col = STATUS_COLOR.get(x["status"], "white")
        g = "-" if x["groundedness"] is None else f"{x['groundedness']:.2f}"
        t.add_row(x["id"][:12], x["created_at"][:19], x["route"], f"[{col}]{x['status']}[/]", g, "yes" if x["healed"] else "", str(x["llm_calls"]), x["query"][:60])
    con.print(t)


@app.command()
def trace(trace_id: str) -> None:
    """Show every stage of one trace."""
    t = c().get(f"/api/traces/{trace_id}")
    con.print(Panel(t["answer"], title=f"{t['query']}", subtitle=f"{t['route']} / {t['status']}"))
    for st in t["data"].get("steps", []):
        stage = st.pop("stage")
        ms = st.pop("t_ms")
        brief = {k: v for k, v in st.items() if k not in ("final", "verdicts", "twiddles")}
        con.print(f"[bold]{ms:>6} ms  {stage}[/] {json.dumps(brief, default=str)[:300]}")
    diag = t["data"].get("diag", {})
    if diag.get("heals"):
        con.print("[cyan]heals:[/]")
        for h in diag["heals"]:
            con.print(f"  {json.dumps(h, default=str)[:300]}")


@app.command()
def metrics(kb: str, days: int = 7) -> None:
    """Health metrics (SLOs) for a knowledge base."""
    m = c().get(f"/api/kbs/{kb}/metrics", params={"days": days})
    m.pop("series", None)
    dump(m)


# ------------------------------------------------------------------ repair
@repair_app.command("run")
def repair_run(kb: str, wait: bool = True) -> None:
    """Run one repair cycle now: diagnose failures, propose, evaluate and apply fixes."""
    cl = c()
    res = cl.post(f"/api/kbs/{kb}/repair")
    if wait:
        dump(wait_job(cl, res["job_id"], "repair").get("result"))
    else:
        con.print(f"repair job {res['job_id']}")


@repair_app.command("list")
def repair_list(kb: str) -> None:
    """Show diagnoses and fixes."""
    cl = c()
    t = Table("diagnosis", "root cause", "impact", "status", "summary")
    for d in cl.get(f"/api/kbs/{kb}/diagnoses"):
        t.add_row(d["id"][:12], d["root_cause"], str(d["impact"]), d["status"], d["summary"][:80])
    con.print(t)
    t = Table("fix", "kind", "risk", "status", "eval", "rationale")
    for f in cl.get(f"/api/kbs/{kb}/fixes"):
        ev = f.get("eval") or {}
        evs = "passed" if ev.get("passed") else ("failed" if "passed" in ev else "-")
        t.add_row(f["id"][:12], f["kind"], f["risk"], f["status"], evs, f["rationale"][:70])
    con.print(t)


def _fix(action: str, fix_id: str, reason: str = "") -> None:
    dump(c().post(f"/api/fixes/{fix_id}/{action}", json={"reason": reason}))


@fix_app.command("approve")
def fix_approve(fix_id: str) -> None:
    _fix("approve", fix_id)


@fix_app.command("reject")
def fix_reject(fix_id: str, reason: str = "") -> None:
    _fix("reject", fix_id, reason)


@fix_app.command("rollback")
def fix_rollback(fix_id: str, reason: str = "") -> None:
    _fix("rollback", fix_id, reason)


# ------------------------------------------------------------------ golden
@golden_app.command("add")
def golden_add(kb: str, question: str, expected: str = "") -> None:
    dump(c().post(f"/api/kbs/{kb}/golden", json={"question": question, "expected_answer": expected}))


@golden_app.command("list")
def golden_list(kb: str) -> None:
    t = Table("id", "origin", "active", "question", "expected")
    for g in c().get(f"/api/kbs/{kb}/golden"):
        t.add_row(g["id"][:12], g["origin"], str(g["active"]), g["question"][:60], g["expected_answer"][:60])
    con.print(t)


@golden_app.command("import")
def golden_import(kb: str, path: Path) -> None:
    """Import a JSONL file of {"question": ..., "expected_answer": ...} lines."""
    cl = c()
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            cl.post(f"/api/kbs/{kb}/golden", json={"question": item["question"], "expected_answer": item.get("expected_answer", "")})
            n += 1
    con.print(f"imported {n} golden items")


@eval_app.command("run")
def eval_run(kb: str, min_pass: float = typer.Option(0.0, help="exit 1 if pass rate is below this (CI gate)")) -> None:
    """Run the golden set against the active config."""
    cl = c()
    res = cl.post(f"/api/kbs/{kb}/evals")
    j = wait_job(cl, res["job_id"], "eval")
    if j["status"] != "done":
        raise typer.Exit(1)
    summary = j["result"]["summary"]
    dump(summary)
    if summary.get("n", 0) and summary.get("pass_rate", 0) < min_pass:
        con.print(f"[red]pass rate {summary['pass_rate']} < required {min_pass}[/]")
        raise typer.Exit(1)


# ------------------------------------------------------------------ config
@config_app.command("show")
def config_show() -> None:
    cfg = c().get("/api/config")
    con.print(f"active version: v{cfg['active_version']}" + (f"  canary: v{cfg['canary']['version']} ({cfg['canary']['canary_pct']:.0%})" if cfg["canary"] else ""))
    dump(cfg["active"])


@config_app.command("set")
def config_set(key: str, value: str, note: str = "cli edit") -> None:
    """Set one config field (value parsed as JSON when possible)."""
    cl = c()
    cfg = cl.get("/api/config")["active"]
    if key not in cfg:
        con.print(f"[red]unknown key {key}[/]")
        raise typer.Exit(1)
    try:
        cfg[key] = json.loads(value)
    except json.JSONDecodeError:
        cfg[key] = value
    dump(cl.req("PUT", "/api/config", json={"data": cfg, "note": note}))


@config_app.command("rollback")
def config_rollback(version: int, reason: str = "cli rollback") -> None:
    dump(c().post(f"/api/config/{version}/rollback", json={"reason": reason}))


@app.command()
def job(job_id: str, wait: bool = False) -> None:
    cl = c()
    dump(wait_job(cl, job_id) if wait else cl.get(f"/api/jobs/{job_id}"))


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
