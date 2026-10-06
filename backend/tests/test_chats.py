"""Chat history: a person's own questions grouped by session, ChatGPT-style."""

from __future__ import annotations

from test_accounts import admin, ask, student  # noqa: F401  (fixture)


def test_chats_are_listed_loaded_and_private(admin):  # noqa: F811
    a, b = student("alice"), student("bob")
    ask(a, admin.kb, session_id="chat-1")
    ask(a, admin.kb, "What does the Enterprise plan include?", session_id="chat-1")
    ask(a, admin.kb, "Is there a free trial?", session_id="chat-2")
    ask(a, admin.kb, "No session id")  # not part of any chat

    chats = a.get(f"/api/kbs/{admin.kb}/chats").json()
    assert [c["id"] for c in chats] == ["chat-2", "chat-1"]  # newest first
    first = chats[1]
    assert first["title"] == "How long do I have to request a refund on the Pro plan?" and first["messages"] == 2

    chat = a.get(f"/api/kbs/{admin.kb}/chats/chat-1").json()
    assert [t["query"] for t in chat["turns"]] == [
        "How long do I have to request a refund on the Pro plan?",
        "What does the Enterprise plan include?",
    ]
    ans = chat["turns"][0]["answer"]
    assert ans["status"] == "verified" and ans["trace_id"] and ans["citations"] and ans["sentences"]

    # other people (admins included) never see someone else's chats here
    assert b.get(f"/api/kbs/{admin.kb}/chats").json() == []
    assert b.get(f"/api/kbs/{admin.kb}/chats/chat-1").status_code == 404
    assert admin.get(f"/api/kbs/{admin.kb}/chats/chat-1").status_code == 404
    assert b.delete(f"/api/kbs/{admin.kb}/chats/chat-1").status_code == 404


def test_deleting_a_chat_hides_it_but_keeps_traces(admin):  # noqa: F811
    a = student("alice")
    tid = ask(a, admin.kb, session_id="chat-1").json()["trace_id"]
    assert a.delete(f"/api/kbs/{admin.kb}/chats/chat-1").json()["removed"] == 1
    assert a.get(f"/api/kbs/{admin.kb}/chats").json() == []
    assert a.get(f"/api/kbs/{admin.kb}/chats/chat-1").status_code == 404
    # still available to the repair loop and the admin trace view
    assert admin.get(f"/api/traces/{tid}").status_code == 200
