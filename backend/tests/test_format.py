"""Structured (ChatGPT-style) answers: layout survives repair and renders as Markdown."""

from __future__ import annotations

from ragx.reflex.engine import QueryEngine, sentences_to_markdown


def S(text, block="continue", heading="", cites=("C1",)):
    return {"text": text, "citations": list(cites), "block": block, "heading": heading}


def test_markdown_rendering():
    md = sentences_to_markdown(
        [
            S("Three people shipped fixes this week."),
            S("It was a busy week.", "continue"),
            S("Devin caught up all blog PRs.", "bullet", heading="Highlights"),
            S("Krishna reviewed **3 PRs**.", "bullet"),
            S("Install the app.", "numbered", heading="Next steps"),
            S("Run it.", "numbered"),
            S("That is all.", "paragraph"),
        ]
    )
    assert md == (
        "Three people shipped fixes this week. It was a busy week.\n\n"
        "### Highlights\n- Devin caught up all blog PRs.\n- Krishna reviewed **3 PRs**.\n\n"
        "### Next steps\n1. Install the app.\n2. Run it.\n\nThat is all."
    )


def test_repair_keeps_layout_of_dropped_sentences():
    sents = [
        S("Intro.", "paragraph"),
        S("Unsupported claim.", "bullet", heading="People"),  # dropped by the verifier
        S("Supported claim.", "continue"),
    ]
    ver = {
        "claims": [
            {"sentence": 0, "claim": "a", "support": "full", "supporting": ["C1"], "contradicting": []},
            {"sentence": 1, "claim": "b", "support": "none", "supporting": [], "contradicting": []},
            {"sentence": 2, "claim": "c", "support": "full", "supporting": ["C2"], "contradicting": []},
        ]
    }

    class Rec:
        def add(self, *a, **k):
            pass

    diag = {"unsupported": [], "heals": []}
    kept, claims = QueryEngine._repair(None, sents, ver, Rec(), diag)
    assert [k["text"] for k in kept] == ["Intro.", "Supported claim."]
    assert kept[1]["block"] == "bullet" and kept[1]["heading"] == "People" and kept[1]["citations"] == ["C2"]
    assert claims[1]["sentence"] == 1


def test_query_returns_layout_fields(env, monkeypatch, docs_dir):
    from conftest import make_client, set_config

    c = make_client(monkeypatch)
    kb = c.post("/api/kbs", json={"name": "k"}).json()["id"]
    c.post(f"/api/kbs/{kb}/sources", json={"kind": "directory", "uri": str(docs_dir)})
    set_config(long_context_max_tokens=0)
    a = c.post(f"/api/kbs/{kb}/query", json={"query": "How long do I have to request a refund on the Pro plan?"}).json()
    assert a["status"] == "verified"
    assert all(s["block"] in ("continue", "paragraph", "bullet", "numbered") and "heading" in s for s in a["sentences"])
    c.__exit__(None, None, None)
