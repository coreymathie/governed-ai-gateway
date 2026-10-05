# Corey Mathie, 2026
"""
Token estimation and tokens-per-minute (TPM) limits per key and per team.

Before a provider call the gateway reserves an *estimate*: prompt tokens plus
the request's `max_tokens` (or `policies.default_output_tokens_estimate` when
the caller didn't set one). After the response, the reservation is corrected to
the provider-reported usage; if every provider fails, it's released.

Estimator:
  * tiktoken's cl100k_base encoding when tiktoken is importable and its
    encoding loads (LiteLLM installs tiktoken; set ROUTER_TOKEN_ESTIMATOR=heuristic
    to skip it).
  * Otherwise a heuristic: ceil(characters / 4) per message + 4 tokens of
    per-message overhead + 3 for the reply primer. The 4-chars-per-token rule
    of thumb is for English text; code and non-Latin scripts tokenize denser,
    which is why the reservation is corrected after the response.

The window is a 60 s sliding window held in process memory (per worker), with
an injectable clock. Pure Python; the browser demo runs it unchanged.
"""

from __future__ import annotations

import math
import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

CHARS_PER_TOKEN = 4
PER_MESSAGE_OVERHEAD = 4
REPLY_PRIMER = 3

_encoding = None
_encoding_tried = False


def _tiktoken_encoding():
    global _encoding, _encoding_tried
    if _encoding_tried:
        return _encoding
    _encoding_tried = True
    if os.environ.get("ROUTER_TOKEN_ESTIMATOR", "").lower() == "heuristic":
        return None
    try:
        import tiktoken

        _encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:  # noqa: BLE001 - missing package, no cached encoding, no network: use the heuristic
        _encoding = None
    return _encoding


def estimator_name() -> str:
    return "tiktoken:cl100k_base" if _tiktoken_encoding() is not None else "heuristic:chars/4"


def count_text_tokens(text: str) -> int:
    enc = _tiktoken_encoding()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_prompt_tokens(messages: Iterable[Mapping]) -> int:
    total = REPLY_PRIMER
    for m in messages:
        total += PER_MESSAGE_OVERHEAD + count_text_tokens(str(m.get("content") or ""))
    return total


def estimate_request_tokens(messages: Iterable[Mapping], max_tokens: int | None, default_output: int) -> int:
    return estimate_prompt_tokens(messages) + int(max_tokens if max_tokens is not None else default_output)


# ---------- TPM limiter ----------


class TokenLimitExceeded(Exception):
    def __init__(self, scope: str, limit: int, used: int, requested: int, retry_after: float):
        self.scope = scope  # e.g. "team:product" or "key:web-app"
        self.limit = limit
        self.used = used
        self.requested = requested
        self.retry_after = retry_after
        kind = scope.split(":", 1)[0]
        super().__init__(f"per-{kind} token rate limit exceeded ({limit} tokens/min)")


@dataclass
class Reservation:
    tokens: int
    _entries: list[list] = field(default_factory=list)  # shared [timestamp, tokens] cells in each window
    settled: bool = False

    def settle(self, actual_tokens: int) -> None:
        """Replace the estimate with the real usage (both directions)."""
        for cell in self._entries:
            cell[1] = max(0, int(actual_tokens))
        self.settled = True

    def release(self) -> None:
        self.settle(0)


class TokenRateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, window_seconds: float = 60.0):
        self.clock = clock
        self.window_seconds = window_seconds
        self._windows: dict[str, deque[list]] = {}

    def _window(self, scope: str) -> deque[list]:
        q = self._windows.setdefault(scope, deque())
        cutoff = self.clock() - self.window_seconds
        while q and q[0][0] <= cutoff:
            q.popleft()
        return q

    def used(self, scope: str) -> int:
        return sum(cell[1] for cell in self._window(scope))

    def reserve(self, limits: Mapping[str, int], tokens: int) -> Reservation:
        """Check every scope first, then reserve in all of them (all or nothing). 0/None = no limit."""
        now = self.clock()
        active = {s: int(lim) for s, lim in limits.items() if lim}
        for scope, limit in active.items():
            q = self._window(scope)
            used = sum(cell[1] for cell in q)
            if used + tokens > limit:
                retry = self.window_seconds if tokens > limit else self._retry_after(q, used + tokens - limit, now)
                raise TokenLimitExceeded(scope, limit, used, tokens, retry)
        res = Reservation(tokens)
        for scope in active:
            cell = [now, tokens]
            self._windows[scope].append(cell)
            res._entries.append(cell)
        return res

    def _retry_after(self, q: deque[list], excess: int, now: float) -> float:
        """Seconds until enough old entries leave the window to fit the request."""
        freed = 0
        for ts, n in q:
            freed += n
            if freed >= excess:
                return max(0.0, ts + self.window_seconds - now)
        return self.window_seconds

    def snapshot(self) -> dict[str, int]:
        return {scope: self.used(scope) for scope in sorted(self._windows)}

    def reset(self) -> None:
        self._windows.clear()
