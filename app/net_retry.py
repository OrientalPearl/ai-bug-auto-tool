"""Transient-failure retry shared by both Zentao transports.

An unattended run must survive a rate limit or a gateway hiccup: wait and retry
instead of letting the whole loop abort. Every wait is printed to stderr so the
log shows the run is parked on purpose, not hung.
"""

from __future__ import annotations

import sys
import time
from typing import Callable

import requests

from .config import get_settings

# Rate limits and upstream failures are worth retrying; 401/403/404 are not
# (those need a credential or path fix, not patience).
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

# Some gateways answer 200 with a throttling body, so the text is checked too.
LIMIT_HINTS = ("rate limit", "too many requests", "overloaded", "quota",
               "out of capacity", "try again later", "限流", "繁忙", "稍后再试")


def is_transient(status_code: int, body: str = "") -> bool:
    """True when this response is worth retrying rather than reporting."""
    if status_code in TRANSIENT_STATUS:
        return True
    if 200 <= status_code < 300:
        lowered = (body or "").lower()
        return any(hint in lowered for hint in LIMIT_HINTS)
    return False


def note(message: str) -> None:
    """Show the wait in the log without polluting the JSON on stdout."""
    print(f"[retry] {message}", file=sys.stderr, flush=True)


def _planned_wait(response: requests.Response, fallback: int) -> int:
    """Honour a numeric `Retry-After`, but never wait longer than the fallback."""
    raw = (response.headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return max(1, min(int(raw), fallback))
    return fallback


def call_with_retry(send: Callable[[], requests.Response], *, label: str,
                    wait: int | None = None,
                    attempts: int | None = None) -> requests.Response:
    """Run `send()` until the answer stops looking transient.

    Connection errors are retried as well. Once the budget is used up the last
    response (or the original exception) is handed back, so the caller's existing
    error handling still decides what to report.
    """
    settings = get_settings()
    wait_for = settings.retry_wait if wait is None else max(1, wait)
    total = settings.retry_max if attempts is None else max(1, attempts)
    last_exc: Exception | None = None

    for index in range(total):
        final = index == total - 1
        try:
            response = send()
        except requests.RequestException as exc:
            last_exc = exc
            if final:
                break
            note(f"{label} 连不上（{type(exc).__name__}），等 {wait_for} 秒后重试 "
                 f"{index + 1}/{total - 1}")
            time.sleep(wait_for)
            continue

        if not is_transient(response.status_code, (response.text or "")[:600]):
            return response
        if final:
            return response
        sleep_for = _planned_wait(response, wait_for)
        note(f"{label} 被限流或上游暂时失败（HTTP {response.status_code}），"
             f"等 {sleep_for} 秒后重试 {index + 1}/{total - 1}")
        time.sleep(sleep_for)

    assert last_exc is not None
    raise last_exc
