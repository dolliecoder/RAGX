"""Small shared helpers."""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def run_parallel(fn: Callable[[T], R], items: Iterable[T], max_workers: int = 8) -> list[R]:
    """Map ``fn`` over ``items`` in threads, preserving order and the caller's
    context variables (so the per-query LLM meter keeps counting)."""
    items = list(items)
    if len(items) <= 1 or max_workers <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, fn, i) for i in items]
        return [f.result() for f in futures]
