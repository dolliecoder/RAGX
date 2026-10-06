"""Private workspaces: everyone can make their own, upload files from the chat, and
get answers beyond their files (clearly labelled) - within per-plan limits."""

from __future__ import annotations

from conftest import FIXTURES
from test_accounts import _set_plan, admin, ask, student  # noqa: F401  (fixture)

REFUND = (FIXTURES / "refund_policy.md").read_bytes()
SLA = (FIXTURES / "sla.txt").read_bytes()


def _upload(c, kb, *files):
    return c.post(f"/api/kbs/{kb}/upload", files=[("files", (name, data, "text/plain")) for name, data in files])


def _workspace(c, name="Math"):
    r = c.post("/api/kbs", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def test_workspaces_are_private(admin):  # noqa: F811
    a, b = student("alice"), student("bob")
    ws = _workspace(a, "Math")
    assert ws["name"] == "Math" and not ws["shared"] and ws["can_edit"]
    assert ws["limits"] == {"files": 20, "pages": 300, "max_file_mb": 25}
    # the same name is fine for someone else, but not twice for the same person
    assert _workspace(b, "Math")["id"] != ws["id"]
    assert a.post("/api/kbs", json={"name": "math"}).status_code == 409
    # nobody else sees it: not other members, not even admins
    for other in (b, admin):
        assert ws["id"] not in [k["id"] for k in other.get("/api/kbs").json()]
        assert other.get(f"/api/kbs/{ws['id']}").status_code == 404
        assert ask(other, ws["id"]).status_code == 404
        assert _upload(other, ws["id"], ("x.md", b"# x")).status_code == 404
    # members still see the shared knowledge base, read-only
    shared = next(k for k in a.get("/api/kbs").json() if k["id"] == admin.kb)
    assert shared["shared"] and not shared["can_edit"]
    assert _upload(a, admin.kb, ("x.md", b"# x")).status_code == 403


def test_upload_ask_and_remove_files(admin):  # noqa: F811
    a = student("alice")
    ws = _workspace(a)
    r = _upload(a, ws["id"], ("refund_policy.md", REFUND)).json()
    assert r["saved"] == ["refund_policy.md"] and r["job_id"]
    assert a.get(f"/api/jobs/{r['job_id']}").json()["status"] == "done"  # the uploader can follow its job
    docs = a.get(f"/api/kbs/{ws['id']}/documents").json()
    assert [d["title"] for d in docs] and docs[0]["chunks"] > 0
    ans = ask(a, ws["id"], "How long do I have to request a refund on the Pro plan?").json()
    assert ans["status"] == "verified" and ans["citations"][0]["doc_id"] == docs[0]["id"]
    assert a.get(f"/api/documents/{docs[0]['id']}").status_code == 200  # citations open for the owner
    # removing the file removes its knowledge (and a re-crawl doesn't bring it back)
    assert a.delete(f"/api/documents/{docs[0]['id']}").json()["deleted"]
    _upload(a, ws["id"], ("sla.txt", SLA))
    assert [d["title"].lower() for d in a.get(f"/api/kbs/{ws['id']}/documents").json()] != [docs[0]["title"].lower()]
    assert all("refund" not in d["uri"] for d in a.get(f"/api/kbs/{ws['id']}/documents").json())
    # deleting the workspace
    assert a.delete(f"/api/kbs/{ws['id']}").json()["deleted"]
    assert a.get(f"/api/kbs/{ws['id']}").status_code == 404


def test_workspace_limits(admin):  # noqa: F811
    _set_plan(admin, workspaces=1, files_per_workspace=1, pages_per_workspace=1, max_file_mb=1)
    a = student("alice")
    ws = _workspace(a, "One")
    r = a.post("/api/kbs", json={"name": "Two"})
    assert r.status_code == 403 and "up to 1 workspaces" in r.json()["detail"]
    # too many pages
    big = b"# Notes\n\n" + b"Lots of words about many things. " * 400
    r = _upload(a, ws["id"], ("big.md", big)).json()
    assert r["saved"] == [] and "Too big" in r["rejected"][0]["reason"]
    # too many files
    _set_plan(admin, workspaces=1, files_per_workspace=1, pages_per_workspace=300, max_file_mb=1)
    assert _upload(a, ws["id"], ("refund_policy.md", REFUND)).json()["saved"] == ["refund_policy.md"]
    r = _upload(a, ws["id"], ("sla.txt", SLA)).json()
    assert r["saved"] == [] and "full" in r["rejected"][0]["reason"]
    # replacing a file with a new version is fine
    assert _upload(a, ws["id"], ("refund_policy.md", REFUND)).json()["saved"] == ["refund_policy.md"]
    # file size
    r = _upload(a, ws["id"], ("huge.txt", b"x" * (1024 * 1024 + 1))).json()
    assert "Larger than 1 MB" in r["rejected"][0]["reason"]
    # admins are not limited
    for i in range(3):
        _workspace(admin, f"admin-{i}")


def test_answers_beyond_the_files_are_labelled(admin):  # noqa: F811
    a = student("alice")
    ws = _workspace(a)
    _upload(a, ws["id"], ("refund_policy.md", REFUND))
    r = ask(a, ws["id"], "What is the Pythagorean theorem?", session_id="c1").json()
    assert r["status"] == "failed"  # nothing in the files, and the verified part says so
    assert r["general"].startswith("From general knowledge")
    assert a.get(f"/api/kbs/{ws['id']}/chats/c1").json()["turns"][0]["answer"]["general"] == r["general"]
    # answers from the files don't get a general-knowledge add-on
    assert ask(a, ws["id"], "How long do I have to request a refund on the Pro plan?").json()["general"] is None
    # the owner can switch it off
    assert a.patch(f"/api/kbs/{ws['id']}", json={"general_knowledge": False}).json()["general_knowledge"] is False
    assert ask(a, ws["id"], "What is the Pythagorean theorem?").json()["general"] is None


def test_rename_workspace(admin):  # noqa: F811
    a = student("alice")
    ws = _workspace(a, "Mths")
    _workspace(a, "Science")
    assert a.patch(f"/api/kbs/{ws['id']}", json={"name": "Math"}).json()["name"] == "Math"
    assert a.patch(f"/api/kbs/{ws['id']}", json={"name": "science"}).status_code == 409
