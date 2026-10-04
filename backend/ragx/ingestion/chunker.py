"""Structure-aware chunking with a versioned, per-document strategy."""

from __future__ import annotations

from dataclasses import dataclass

from ..text import estimate_tokens, sentences
from .parsers import Block, ParsedDoc

DEFAULT_STRATEGY = {"name": "structured", "target_tokens": 500, "max_tokens": 800, "min_tokens": 120, "overlap_tokens": 60}
# Used by the Repair loop when a diagnosis says answers were split across chunks.
LARGE_STRATEGY = {"name": "structured-large", "target_tokens": 900, "max_tokens": 1400, "min_tokens": 250, "overlap_tokens": 120}


@dataclass
class ChunkDraft:
    ord: int
    text: str
    section_path: str
    page: int | None
    token_count: int


def _split_oversized(block: Block, max_tokens: int) -> list[Block]:
    if estimate_tokens(block.text) <= max_tokens:
        return [block]
    if block.kind in ("table", "code", "list"):
        pieces, cur = [], []
        header = block.text.split("\n", 1)[0] if block.kind == "table" else ""
        for line in block.text.split("\n"):
            if cur and estimate_tokens("\n".join(cur + [line])) > max_tokens:
                pieces.append("\n".join(cur))
                cur = [header] if header and line != header else []
            cur.append(line)
        if cur:
            pieces.append("\n".join(cur))
        return [Block(block.kind, p, block.level, block.page) for p in pieces if p.strip()]
    out, cur = [], []
    for s in sentences(block.text) or [block.text]:
        if cur and estimate_tokens(" ".join(cur + [s])) > max_tokens:
            out.append(" ".join(cur))
            cur = []
        if estimate_tokens(s) > max_tokens:  # pathological sentence: hard wrap
            step = max_tokens * 4
            out += [s[i : i + step] for i in range(0, len(s), step)]
            continue
        cur.append(s)
    if cur:
        out.append(" ".join(cur))
    return [Block(block.kind, p, block.level, block.page) for p in out]


def _tail_overlap(text: str, tokens: int) -> str:
    if tokens <= 0:
        return ""
    sents = sentences(text)
    out: list[str] = []
    for s in reversed(sents):
        if estimate_tokens(" ".join([s] + out)) > tokens:
            break
        out.insert(0, s)
    return " ".join(out)


def chunk_document(doc: ParsedDoc, strategy: dict | None = None) -> list[ChunkDraft]:
    st = {**DEFAULT_STRATEGY, **(strategy or {})}
    target, max_t, min_t, ov = st["target_tokens"], st["max_tokens"], st["min_tokens"], st["overlap_tokens"]

    chunks: list[ChunkDraft] = []
    stack: list[tuple[int, str]] = []  # heading stack (level, text)
    cur: list[str] = []
    cur_tokens = 0
    cur_page: int | None = None
    cur_section = ""

    def section_path() -> str:
        return " > ".join(t for _, t in stack)

    def emit(carry_overlap: bool) -> None:
        nonlocal cur, cur_tokens, cur_page
        body = "\n\n".join(cur).strip()
        if body:
            chunks.append(ChunkDraft(len(chunks), body, cur_section, cur_page, estimate_tokens(body)))
        tail = _tail_overlap(cur[-1], ov) if (carry_overlap and cur and not cur[-1].startswith(("|", "```"))) else ""
        cur = [tail] if tail else []
        cur_tokens = estimate_tokens(tail) if tail else 0
        cur_page = None

    for raw in doc.blocks:
        if raw.kind == "heading":
            # A new section starts a new chunk once the current one is big enough.
            if cur_tokens >= min_t:
                emit(carry_overlap=False)
            elif cur and all(c.startswith("#") for c in cur):
                pass  # consecutive headings stay together
            while stack and stack[-1][0] >= raw.level:
                stack.pop()
            stack.append((raw.level, raw.text))
            if not cur or cur_tokens == 0:
                cur_section = section_path()
            cur.append("#" * max(1, min(raw.level, 6)) + " " + raw.text)
            cur_tokens += estimate_tokens(raw.text)
            if cur_page is None:
                cur_page = raw.page
            continue
        for block in _split_oversized(raw, max_t):
            bt = estimate_tokens(block.text)
            if cur_tokens + bt > max_t or (cur_tokens >= target and cur_tokens + bt > target):
                emit(carry_overlap=block.kind == "para")
            if not cur or cur_tokens == 0:
                cur_section = section_path()
            if cur_page is None:
                cur_page = block.page
            cur.append(block.text)
            cur_tokens += bt
    emit(carry_overlap=False)

    # Merge a tiny trailing chunk into its predecessor when they share a section.
    if len(chunks) >= 2 and chunks[-1].token_count < min_t // 2 and chunks[-1].section_path == chunks[-2].section_path:
        last = chunks.pop()
        prev = chunks[-1]
        merged = prev.text + "\n\n" + last.text
        chunks[-1] = ChunkDraft(prev.ord, merged, prev.section_path, prev.page, estimate_tokens(merged))
    return chunks
