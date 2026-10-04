"""Text utilities shared by indexing, retrieval and the offline provider."""

from __future__ import annotations

import hashlib
import re

STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because been before being below
    between both but by can could did do does doing down during each few for from further had has have
    having he her here hers herself him himself his how i if in into is it its itself just me more most my
    myself no nor not now of off on once only or other our ours ourselves out over own same she should so
    some such than that the their theirs them themselves then there these they this those through to too
    under until up very was we were what when where which while who whom why will with would you your
    yours yourself yourselves tell show give explain describe please list does do much many""".split()
)

_TOKEN = re.compile(r"[a-z0-9]+(?:[._\-'][a-z0-9]+)*", re.IGNORECASE)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_IDENT = re.compile(
    r"""("[^"]{2,80}")|              # quoted phrase
        (\b[A-Z]{2,}[-_]?\d+\b)|      # ERR42, ABC-123
        (\b\w*\d\w*[-_.]\w+\b)|       # v1.2.3, x_86-64
        (\b[a-z]+_[a-z0-9_]+\b)|      # snake_case
        (\b[a-z]+[A-Z][A-Za-z0-9]+\b)| # camelCase
        (\b\d{3,}\b)                  # long numbers
    """,
    re.VERBOSE,
)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def stem(w: str) -> str:
    for suf in ("ingly", "edly", "ing", "ies", "ied", "ed", "es", "s", "ly"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            base = w[: -len(suf)]
            return base + ("y" if suf in ("ies", "ied") else "")
    return w


def tokenize(text: str, *, keep_stopwords: bool = False) -> list[str]:
    toks = [t.lower() for t in _TOKEN.findall(text)]
    if not keep_stopwords:
        toks = [t for t in toks if t not in STOPWORDS]
    return [stem(t) for t in toks]


def content_terms(text: str) -> set[str]:
    return {t for t in tokenize(text) if len(t) > 1}


def sentences(text: str) -> list[str]:
    parts = []
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if para:
            parts += [s.strip() for s in _SENT.split(para) if s.strip()]
    return parts


def identifiers(text: str) -> list[str]:
    out: list[str] = []
    for m in _IDENT.finditer(text):
        tok = next(g for g in m.groups() if g)
        tok = tok.strip('"')
        if tok and tok not in out:
            out.append(tok)
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap(query_terms: set[str], text_terms: set[str]) -> float:
    """Fraction of query terms present in text."""
    if not query_terms:
        return 0.0
    return len(query_terms & text_terms) / len(query_terms)


def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def shingles(text: str, k: int = 5) -> set[str]:
    toks = tokenize(text, keep_stopwords=True)
    if len(toks) < k:
        return {" ".join(toks)} if toks else set()
    return {" ".join(toks[i : i + k]) for i in range(len(toks) - k + 1)}


INJECTION_PATTERNS = re.compile(
    r"(ignore (all |any )?(previous|prior|above) (instructions|prompts)|disregard (the )?(system|previous)|"
    r"you are now (a|an|in)|new instructions:|system prompt|reveal your (instructions|prompt)|"
    r"<\s*/?\s*(system|assistant)\s*>|act as (an? )?(unrestricted|jailbroken))",
    re.IGNORECASE,
)


def looks_like_injection(text: str) -> bool:
    return bool(INJECTION_PATTERNS.search(text))
