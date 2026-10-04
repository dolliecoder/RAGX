"""Source crawler with Googlebot-style adaptive recrawl scheduling.

Each document carries its own recrawl interval: halved when a change is observed,
doubled when nothing changed (bounded). The Repair loop raises priority for
documents diagnosed as stale by setting ``next_crawl_at`` to now.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Document, Source
from .parsers import SUPPORTED
from .pipeline import IngestResult, ingest_bytes

log = logging.getLogger("ragx.crawl")

MIN_INTERVAL = 300
MAX_INTERVAL = 7 * 86400
MAX_FILE_BYTES = 50 * 1024 * 1024


@dataclass
class CrawlReport:
    source_id: str
    results: list[IngestResult] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped: int = 0
    error: str | None = None

    def summary(self) -> dict:
        by: dict[str, int] = {}
        for r in self.results:
            by[r.status] = by.get(r.status, 0) + 1
        return {
            "source_id": self.source_id,
            "counts": by,
            "removed": len(self.removed),
            "skipped_not_due": self.skipped,
            "errors": [{"uri": r.uri, "error": r.error} for r in self.results if r.error],
            "notes": [n for r in self.results for n in r.notes],
            "error": self.error,
        }


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _schedule(doc: Document, changed: bool) -> None:
    interval = doc.crawl_interval_s or 3600
    interval = max(MIN_INTERVAL, interval // 2) if changed else min(MAX_INTERVAL, interval * 2)
    doc.crawl_interval_s = interval
    doc.next_crawl_at = utcnow() + timedelta(seconds=interval)


def _due(doc: Document | None, force: bool) -> bool:
    if force or doc is None or doc.next_crawl_at is None:
        return True
    return _aware(doc.next_crawl_at) <= utcnow()


def _files_for(source: Source) -> list[Path]:
    p = Path(source.uri)
    if source.kind == "file":
        return [p] if p.is_file() else []
    if not p.is_dir():
        return []
    walker = p.rglob("*") if source.recursive else p.glob("*")
    return sorted(f for f in walker if f.is_file() and f.suffix.lower() in SUPPORTED and not f.name.startswith((".", "~$")))


def crawl_source(session: Session, source: Source, *, force: bool = False) -> CrawlReport:
    report = CrawlReport(source.id)
    existing = {d.uri: d for d in session.scalars(select(Document).where(Document.source_id == source.id))}
    try:
        if source.kind in ("file", "directory"):
            root = Path(source.uri)
            if not root.exists():
                raise FileNotFoundError(f"path not found: {source.uri}")
            seen: set[str] = set()
            for f in _files_for(source):
                uri = str(f.resolve())
                seen.add(uri)
                doc = existing.get(uri)
                if not _due(doc, force):
                    report.skipped += 1
                    continue
                if f.stat().st_size > MAX_FILE_BYTES:
                    report.results.append(IngestResult(doc.id if doc else None, uri, "error", error="file too large (>50MB)"))
                    continue
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                res = ingest_bytes(
                    session,
                    source.kb_id,
                    f.read_bytes(),
                    uri=uri,
                    filename=f.name,
                    source_id=source.id,
                    authority=source.authority,
                    modified_at=mtime,
                )
                report.results.append(res)
                d = session.get(Document, res.doc_id) if res.doc_id else None
                if d is not None:
                    _schedule(d, changed=res.status in ("created", "updated"))
            for uri, doc in existing.items():
                if uri not in seen and doc.status not in ("superseded",):
                    doc.status = "superseded"
                    doc.error = "removed from source"
                    report.removed.append(uri)
        elif source.kind == "url":
            doc = existing.get(source.uri)
            if _due(doc, force):
                with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "RAGX-crawler/0.1"}) as http:
                    r = http.get(source.uri)
                    r.raise_for_status()
                ctype = r.headers.get("content-type", "").split(";")[0].strip()
                name = Path(urlparse(source.uri).path).name or "index.html"
                if ctype == "application/pdf" and not name.lower().endswith(".pdf"):
                    name += ".pdf"
                elif ctype in ("text/html", "") and Path(name).suffix.lower() not in SUPPORTED:
                    name += ".html"
                elif ctype == "text/plain" and Path(name).suffix.lower() not in SUPPORTED:
                    name += ".txt"
                lm = r.headers.get("last-modified")
                modified = None
                if lm:
                    try:
                        from email.utils import parsedate_to_datetime

                        modified = parsedate_to_datetime(lm)
                    except (TypeError, ValueError):
                        modified = None
                res = ingest_bytes(
                    session,
                    source.kb_id,
                    r.content,
                    uri=source.uri,
                    filename=name,
                    source_id=source.id,
                    authority=source.authority,
                    modified_at=modified,
                    force=force,
                )
                report.results.append(res)
                d = session.get(Document, res.doc_id) if res.doc_id else None
                if d is not None:
                    _schedule(d, changed=res.status in ("created", "updated"))
            else:
                report.skipped += 1
        else:
            raise ValueError(f"unknown source kind {source.kind}")
        source.status, source.last_error = "ok", None
    except Exception as e:  # noqa: BLE001 - recorded on the source for the dashboard
        log.warning("crawl of source %s failed: %s", source.id, e)
        source.status, source.last_error = "error", f"{type(e).__name__}: {e}"
        report.error = source.last_error
    source.last_crawled_at = utcnow()
    session.flush()
    return report


def reingest_document(session: Session, doc: Document, *, strategy: dict | None = None, stage: bool = False) -> IngestResult:
    """Re-read a document from its source (used by healing actions)."""
    if doc.uri.startswith(("http://", "https://")):
        with httpx.Client(timeout=30, follow_redirects=True) as http:
            r = http.get(doc.uri)
            r.raise_for_status()
        data = r.content
        name = Path(urlparse(doc.uri).path).name or "index.html"
        if Path(name).suffix.lower() not in SUPPORTED:
            name += ".html"
    else:
        path = Path(doc.uri)
        if not path.is_file():
            raise FileNotFoundError(f"source file missing: {doc.uri}")
        data = path.read_bytes()
        name = path.name
    src = session.get(Source, doc.source_id) if doc.source_id else None
    return ingest_bytes(
        session,
        doc.kb_id,
        data,
        uri=doc.uri,
        filename=name,
        source_id=doc.source_id,
        authority=src.authority if src else doc.authority,
        modified_at=doc.modified_at,
        strategy=strategy,
        stage=stage,
        force=True,
    )


def is_safe_local_path(path: str, allowed_roots: list[Path]) -> bool:
    """Directory sources are restricted to configured roots (defense for the API)."""
    try:
        p = Path(os.path.realpath(path))
    except OSError:
        return False
    return any(p == r or r in p.parents for r in (Path(os.path.realpath(x)) for x in allowed_roots))
