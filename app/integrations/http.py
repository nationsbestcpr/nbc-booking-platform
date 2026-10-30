"""The one place outbound HTTP calls go through.

Retries rate limits (429) and server errors a few times, honouring
Retry-After. Raises IntegrationError with a short, secret-free message.
Tests replace `transport` with a fake.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import requests


class IntegrationError(RuntimeError):
    """A call to another platform failed.

    retry=False means trying again won't help (bad request, not found,
    missing setup); the outbox then stops retrying that job.
    """

    def __init__(self, message: str, *, status: int | None = None, retry: bool = True,
                 body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry = retry
        self.body = body


class NotConfigured(IntegrationError):
    """A secret or setting this call needs is missing. Retried later, quietly."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retry=True)


def _requests_transport(method: str, url: str, **kwargs: Any) -> requests.Response:
    return requests.request(method, url, **kwargs)


# Swapped out by tests: transport(method, url, **kwargs) -> object with
# .status_code, .headers, .json(), .text
transport: Callable[..., Any] = _requests_transport


def request(method: str, url: str, *, service: str, headers: dict | None = None,
            params: Any = None, json: Any = None, data: Any = None,
            timeout: float = 25, attempts: int = 3) -> Any:
    """Make a call and return the decoded JSON body (or {} when empty)."""
    last: IntegrationError | None = None
    for attempt in range(1, attempts + 1):
        try:
            res = transport(method, url, headers=headers or {}, params=params, json=json, data=data,
                            timeout=timeout)
        except requests.RequestException as exc:
            last = IntegrationError(f"{service}: could not connect ({exc.__class__.__name__})")
            time.sleep(min(2 ** attempt, 8))
            continue
        status = res.status_code
        if status == 429 or status >= 500:
            wait = _retry_after(res.headers, attempt)
            last = IntegrationError(f"{service}: {_reason(res)}", status=status)
            if attempt < attempts:
                time.sleep(wait)
                continue
            raise last
        if status >= 400:
            raise IntegrationError(f"{service}: {_reason(res)}", status=status,
                                   retry=status in (401, 403, 408, 409, 423), body=_body(res))
        return _body(res) or {}
    assert last is not None
    raise last


def _retry_after(headers: Any, attempt: int) -> float:
    try:
        return min(float(headers.get("Retry-After", "")), 30.0)
    except (TypeError, ValueError):
        return min(2.0 ** attempt, 10.0)


def _body(res: Any) -> Any:
    try:
        return res.json()
    except ValueError:
        return None


def _reason(res: Any) -> str:
    """'HTTP 422 INVALID_FILTER: …' — the platform's own error text, trimmed."""
    body = _body(res)
    detail = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            detail = " ".join(str(err.get(k, "")) for k in ("type", "code", "message") if err.get(k))
        elif err:
            detail = str(err)
        if body.get("message") and not detail:
            msg = body["message"]
            detail = "; ".join(msg) if isinstance(msg, list) else str(msg)
    return f"HTTP {res.status_code} {detail}".strip()[:400]
