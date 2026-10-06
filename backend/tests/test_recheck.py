"""A thumbs-down re-checks the answer against the documents instead of blindly changing anything."""

from __future__ import annotations

from test_accounts import admin, ask, student  # noqa: F401  (fixture)

Q = "How long do I have to request a refund on the Pro plan?"


def _signal(admin, trace_id):  # noqa: F811
    from ragx.db import session_scope
    from ragx.models import ChunkSignal, Trace

    with session_scope() as s:
        t = s.get(Trace, trace_id)
        ids = [c["chunk_id"] for c in t.data["citations"] if c.get("chunk_id")]
        bad = sum((s.get(ChunkSignal, i).bad if s.get(ChunkSignal, i) else 0.0) for i in ids)
        return bad, t.negative_signal


def _golden(admin):  # noqa: F811
    return [g for g in admin.get(f"/api/kbs/{admin.kb}/golden").json() if g["origin"] == "feedback"]


def test_false_correction_does_not_change_the_answer(admin):  # noqa: F811
    a = student("alice")
    r = ask(a, admin.kb, Q, session_id="c1").json()
    assert r["status"] == "verified"
    out = a.post(f"/api/traces/{r['trace_id']}/feedback", json={"rating": -1, "correction": "You have 90 days to request a refund."}).json()
    rc = out["recheck"]
    assert rc["verdict"] == "stands_correction_unsupported", rc
    assert rc["original"]["supported"] and not rc["correction"]["supported"]
    assert rc["updated_trace_id"] is None
    # the click's ranking penalty is undone and nothing is sent to repair
    bad, negative = _signal(admin, r["trace_id"])
    assert bad == 0 and negative is False
    # the false correction is kept for review but never becomes an active test
    g = _golden(admin)
    assert len(g) == 1 and g[0]["active"] is False and "Not supported" in g[0]["note"]
    # the verdict shows up when the chat is reopened
    turn = a.get(f"/api/kbs/{admin.kb}/chats/c1").json()["turns"][0]
    assert turn["answer"]["recheck"]["verdict"] == "stands_correction_unsupported"


def test_recheck_runs_once_per_answer(admin):  # noqa: F811
    a = student("alice")
    r = ask(a, admin.kb, Q).json()
    first = a.post(f"/api/traces/{r['trace_id']}/feedback", json={"rating": -1}).json()["recheck"]
    assert first["verdict"] == "stands"
    again = a.post(f"/api/traces/{r['trace_id']}/feedback", json={"rating": -1}).json()["recheck"]
    assert again == first


def test_supported_admin_correction_becomes_a_test(admin):  # noqa: F811
    r = ask(admin, admin.kb, Q).json()
    corr = r["sentences"][0]["text"]  # a correction the documents back
    rc = admin.post(f"/api/traces/{r['trace_id']}/feedback", json={"rating": -1, "correction": corr}).json()["recheck"]
    assert rc["correction"]["supported"]
    assert rc["verdict"] in ("both", "you_were_right")
    g = _golden(admin)
    assert len(g) == 1 and g[0]["active"] is True and g[0]["note"] == ""


def test_wrong_answer_is_replaced(admin, monkeypatch):  # noqa: F811
    from ragx.db import session_scope
    from ragx.models import Trace

    a = student("alice")
    r = ask(a, admin.kb, Q, session_id="c1").json()
    with session_scope() as s:  # pretend the stored answer had said something the documents don't
        t = s.get(Trace, r["trace_id"])
        t.data = {**t.data, "sentences": [{"text": "Refunds on the Pro plan are available for 365 days.", "citations": [1]}]}
    rc = a.post(f"/api/traces/{r['trace_id']}/feedback", json={"rating": -1}).json()["recheck"]
    assert rc["verdict"] == "fixed" and rc["updated_trace_id"]
    turn = a.get(f"/api/kbs/{admin.kb}/chats/c1").json()["turns"]
    assert len(turn) == 1  # the corrected answer is attached, not a new chat turn
    assert turn[0]["answer"]["recheck"]["updated"]["status"] == "verified"
    _, negative = _signal(admin, r["trace_id"])
    assert negative is True  # a real failure still goes to the repair loop
