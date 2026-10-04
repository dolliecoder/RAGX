"""Layout-aware parsers: PDF, DOCX, HTML, Markdown, plain text -> structured blocks."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}


@dataclass
class Block:
    kind: str  # heading | para | list | table | code
    text: str
    level: int = 0  # heading level
    page: int | None = None


@dataclass
class ParsedDoc:
    title: str
    mime: str
    blocks: list[Block] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)


class ParseError(ValueError):
    pass


def mime_for(name: str) -> str | None:
    return SUPPORTED.get(Path(name).suffix.lower())


def parse_bytes(data: bytes, filename: str, mime: str | None = None) -> ParsedDoc:
    mime = mime or mime_for(filename)
    if mime is None:
        raise ParseError(f"unsupported file type: {filename}")
    stem = Path(filename).stem.replace("_", " ").replace("-", " ").strip() or filename
    try:
        if mime == "application/pdf":
            doc = _parse_pdf(data, stem)
        elif mime.endswith("wordprocessingml.document"):
            doc = _parse_docx(data, stem)
        elif mime == "text/html":
            doc = _parse_html(_decode(data), stem)
        elif mime == "text/markdown":
            doc = _parse_markdown(_decode(data), stem)
        else:
            doc = _parse_text(_decode(data), stem)
    except ParseError:
        raise
    except Exception as e:  # noqa: BLE001 - library-specific errors
        raise ParseError(f"failed to parse {filename}: {type(e).__name__}: {e}") from e
    doc.blocks = [b for b in doc.blocks if b.text.strip()]
    if not doc.blocks:
        raise ParseError(f"no extractable text in {filename}")
    return doc


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            if enc == "utf-16" and "\x00" in text:
                continue
            return text
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


_NUMBERED_HEADING = re.compile(r"^(\d+(\.\d+)*\.?|[IVX]+\.|Chapter \d+|Section \d+)\s+\S")


def _looks_like_heading(line: str) -> int:
    s = line.strip()
    if not s or len(s) > 90 or s.endswith((".", ",", ";", ":")) and not _NUMBERED_HEADING.match(s):
        return 0
    words = s.split()
    if len(words) > 12:
        return 0
    if _NUMBERED_HEADING.match(s):
        return min(1 + s.split()[0].count("."), 4)
    if s.isupper() and len(s) > 3 and any(c.isalpha() for c in s):
        return 1
    caps = sum(1 for w in words if w[:1].isupper())
    if len(words) <= 8 and caps >= max(1, int(len(words) * 0.7)) and not s.endswith("?"):
        return 2
    return 0


def _paragraph_blocks(text: str, page: int | None = None, detect_headings: bool = True) -> list[Block]:
    blocks: list[Block] = []
    for para in re.split(r"\n\s*\n", text):
        lines = [ln.rstrip() for ln in para.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        if detect_headings and len(lines) >= 1 and (lvl := _looks_like_heading(lines[0])):
            blocks.append(Block("heading", lines[0].strip(), level=lvl, page=page))
            lines = lines[1:]
            if not lines:
                continue
        if all(re.match(r"^\s*([-*•]|\d+[.)])\s+", ln) for ln in lines):
            blocks.append(Block("list", "\n".join(ln.strip() for ln in lines), page=page))
        else:
            # PDFs hard-wrap lines; re-join with spaces, fix hyphenation.
            joined = " ".join(ln.strip() for ln in lines)
            joined = re.sub(r"(\w)- (\w)", r"\1\2", joined)
            blocks.append(Block("para", joined, page=page))
    return blocks


def _parse_pdf(data: bytes, stem: str) -> ParsedDoc:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as e:  # noqa: BLE001
            raise ParseError("encrypted PDF") from e
    title = ""
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title).strip()
    except Exception:  # noqa: BLE001
        pass
    blocks: list[Block] = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        blocks += _paragraph_blocks(text, page=i)
    if not title:
        first_heading = next((b.text for b in blocks if b.kind == "heading"), "")
        title = first_heading or stem
    return ParsedDoc(title=title, mime="application/pdf", blocks=blocks)


def _parse_docx(data: bytes, stem: str) -> ParsedDoc:
    import docx  # python-docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(data))
    title = (d.core_properties.title or "").strip()
    blocks: list[Block] = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            if style.lower().startswith("heading"):
                m = re.search(r"(\d+)", style)
                blocks.append(Block("heading", text, level=int(m.group(1)) if m else 1))
            elif style.lower() == "title":
                title = title or text
                blocks.append(Block("heading", text, level=1))
            elif "list" in style.lower():
                blocks.append(Block("list", f"- {text}"))
            else:
                blocks.append(Block("para", text))
        elif tag == "tbl":
            t = Table(child, d)
            rows = []
            for row in t.rows:
                cells = []
                for c in row.cells:
                    ct = " ".join(c.text.split())
                    if not cells or cells[-1] != ct:  # merged cells repeat
                        cells.append(ct)
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                blocks.append(Block("table", "\n".join(rows)))
    # merge consecutive list items
    merged: list[Block] = []
    for b in blocks:
        if b.kind == "list" and merged and merged[-1].kind == "list":
            merged[-1].text += "\n" + b.text
        else:
            merged.append(b)
    if not title:
        title = next((b.text for b in merged if b.kind == "heading"), stem)
    return ParsedDoc(title=title, mime=SUPPORTED[".docx"], blocks=merged)


def _parse_html(html: str, stem: str) -> ParsedDoc:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "iframe"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else "") or ""
    root = soup.find("main") or soup.find("article") or soup.body or soup
    blocks: list[Block] = []
    seen: set[int] = set()
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "pre", "table", "blockquote", "dl"]):
        if any(id(parent) in seen for parent in el.parents):
            continue
        name = el.name
        if name in ("ul", "ol", "pre", "table", "blockquote", "dl"):
            seen.add(id(el))
        if name and name[0] == "h" and len(name) == 2:
            text = " ".join(el.get_text(" ").split())
            if text:
                blocks.append(Block("heading", text, level=int(name[1])))
        elif name == "p" or name == "blockquote":
            text = " ".join(el.get_text(" ").split())
            if text:
                blocks.append(Block("para", text))
        elif name in ("ul", "ol"):
            items = [" ".join(li.get_text(" ").split()) for li in el.find_all("li")]
            items = [i for i in items if i]
            if items:
                blocks.append(Block("list", "\n".join(f"- {i}" for i in items)))
        elif name == "pre":
            blocks.append(Block("code", el.get_text()))
        elif name == "table":
            rows = []
            for tr in el.find_all("tr"):
                cells = [" ".join(td.get_text(" ").split()) for td in tr.find_all(["td", "th"])]
                if any(cells):
                    rows.append("| " + " | ".join(cells) + " |")
            if rows:
                blocks.append(Block("table", "\n".join(rows)))
        elif name == "dl":
            text = "\n".join(" ".join(x.get_text(" ").split()) for x in el.find_all(["dt", "dd"]))
            if text:
                blocks.append(Block("list", text))
    if not blocks:  # unstructured HTML: fall back to text
        blocks = _paragraph_blocks(root.get_text("\n"), detect_headings=False)
    if not title:
        title = next((b.text for b in blocks if b.kind == "heading"), stem)
    return ParsedDoc(title=title, mime="text/html", blocks=blocks)


def _parse_markdown(text: str, stem: str) -> ParsedDoc:
    blocks: list[Block] = []
    lines = text.replace("\r\n", "\n").split("\n")
    # strip YAML front matter
    title = ""
    if lines and lines[0].strip() == "---":
        for j in range(1, min(len(lines), 60)):
            if lines[j].strip() == "---":
                for fm in lines[1:j]:
                    if fm.lower().startswith("title:"):
                        title = fm.split(":", 1)[1].strip().strip("\"'")
                lines = lines[j + 1 :]
                break
    buf: list[str] = []
    kind = "para"

    def flush():
        nonlocal buf, kind
        if buf:
            body = "\n".join(buf).strip()
            if body:
                if kind == "para":
                    body = " ".join(body.split("\n"))
                blocks.append(Block(kind, body))
        buf, kind = [], "para"

    i = 0
    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush()
            fence = stripped[:3]
            code = [ln]
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            if i < len(lines):
                code.append(lines[i])
            blocks.append(Block("code", "\n".join(code)))
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", stripped)
        if m:
            flush()
            blocks.append(Block("heading", m.group(2), level=len(m.group(1))))
        elif i + 1 < len(lines) and stripped and re.match(r"^(=+|-+)\s*$", lines[i + 1].strip()) and not buf:
            flush()
            blocks.append(Block("heading", stripped, level=1 if lines[i + 1].strip()[0] == "=" else 2))
            i += 1
        elif not stripped:
            flush()
        elif stripped.startswith("|"):
            if kind != "table":
                flush()
                kind = "table"
            if not re.match(r"^\|?\s*:?-{2,}", stripped):
                buf.append(stripped)
        elif re.match(r"^([-*+]|\d+[.)])\s+", stripped):
            if kind != "list":
                flush()
                kind = "list"
            buf.append(stripped)
        else:
            if kind == "list" and ln.startswith((" ", "\t")):
                buf[-1] += " " + stripped
            else:
                if kind != "para":
                    flush()
                buf.append(stripped)
        i += 1
    flush()
    if not title:
        title = next((b.text for b in blocks if b.kind == "heading" and b.level == 1), "") or next(
            (b.text for b in blocks if b.kind == "heading"), stem
        )
    return ParsedDoc(title=title, mime="text/markdown", blocks=blocks)


def _parse_text(text: str, stem: str) -> ParsedDoc:
    blocks = _paragraph_blocks(text.replace("\r\n", "\n"))
    title = next((b.text for b in blocks if b.kind == "heading"), stem)
    return ParsedDoc(title=title, mime="text/plain", blocks=blocks)
