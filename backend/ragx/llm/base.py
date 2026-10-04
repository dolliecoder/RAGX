"""Provider-neutral LLM types, JSON extraction and per-request metering."""

from __future__ import annotations

import contextvars
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class LLMRequest:
    """One model call.

    ``task`` names the pipeline step (gate, plan, grade, generate, verify ...).
    ``payload`` carries the same information as the prompt in structured form so
    the offline provider can act on it without parsing prose.
    """

    task: str
    system: str
    user: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema: dict[str, Any] | None = None  # JSON schema for the expected output
    max_tokens: int = 4096
    effort: str | None = None  # low | medium | high (honoured where supported)
    cache_system: bool = False


@dataclass
class LLMResponse:
    text: str
    data: Any = None
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, req: LLMRequest) -> LLMResponse: ...


class ProviderError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class BudgetExceeded(RuntimeError):
    pass


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating code fences and surrounding prose."""
    text = text.strip()
    if not text:
        raise ValueError("empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Outermost balanced object/array.
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(text)):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            break
            start = text.find(open_ch, start + 1)
    raise ValueError("no JSON object found in response")


class Meter:
    """Counts LLM calls and tokens for one query/job and enforces a call budget."""

    def __init__(self, max_calls: int | None = None):
        self.max_calls = max_calls
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.by_task: dict[str, int] = {}
        self._lock = threading.Lock()

    def reserve(self, task: str) -> None:
        with self._lock:
            if self.max_calls is not None and self.calls >= self.max_calls:
                raise BudgetExceeded(f"LLM call budget of {self.max_calls} exhausted (task={task})")
            self.calls += 1
            self.by_task[task] = self.by_task.get(task, 0) + 1

    def record(self, resp: LLMResponse) -> None:
        with self._lock:
            self.tokens_in += resp.tokens_in
            self.tokens_out += resp.tokens_out

    def remaining(self) -> int | None:
        return None if self.max_calls is None else self.max_calls - self.calls

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "by_task": dict(self.by_task),
        }


current_meter: contextvars.ContextVar[Meter | None] = contextvars.ContextVar("ragx_meter", default=None)
