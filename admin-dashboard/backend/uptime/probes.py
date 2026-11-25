"""HTTP probes. Every probe returns a ProbeResult and never raises."""

from __future__ import annotations

import errno
import time
from typing import Any

import httpx

from .models import ProbeResult, ServiceConfig

MAX_EVIDENCE_CHARS = 600


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        # httpx wraps the OS error ("All connection attempts failed"); look down the cause chain.
        seen, cur = set(), exc
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            if isinstance(cur, ConnectionRefusedError) or getattr(cur, "errno", None) in (errno.ECONNREFUSED,):
                return "connection_refused"
            text = str(cur).lower()
            if "refused" in text or "errno 61" in text or "errno 111" in text:
                return "connection_refused"
            cur = cur.__cause__ or cur.__context__
        return "connection_error"
    return "connection_error"


def _short(text: str) -> str:
    return text if len(text) <= MAX_EVIDENCE_CHARS else text[: MAX_EVIDENCE_CHARS - 1] + "…"


def _timeout(seconds: float) -> httpx.Timeout:
    return httpx.Timeout(seconds, connect=min(3.0, seconds))


async def _request(client: httpx.AsyncClient, method: str, url: str, timeout: float) -> tuple[httpx.Response | None, float, Exception | None]:
    started = time.perf_counter()
    try:
        response = await client.request(method, url, timeout=_timeout(timeout))
        return response, round((time.perf_counter() - started) * 1000, 2), None
    except Exception as exc:  # noqa: BLE001 - classified below, never propagated
        return None, round((time.perf_counter() - started) * 1000, 2), exc


async def probe_frontend(client: httpx.AsyncClient, service: ServiceConfig) -> ProbeResult:
    """The website itself: any 2xx/3xx page counts as reachable."""
    response, latency, exc = await _request(client, "GET", service.frontend_url, service.timeout_seconds)
    if exc is not None:
        return ProbeResult(kind="frontend", url=service.frontend_url, ok=False, latency_ms=latency,
                           error_kind=_error_kind(exc), error=_short(f"{type(exc).__name__}: {exc}"))
    ok = response.status_code < 400
    return ProbeResult(
        kind="frontend", url=service.frontend_url, ok=ok, http_status=response.status_code, latency_ms=latency,
        error_kind=None if ok else "http_error", error=None if ok else f"HTTP {response.status_code}",
    )


async def probe_availability(client: httpx.AsyncClient, service: ServiceConfig) -> ProbeResult:
    """Backend availability endpoint: must answer 2xx (and say 'ok' if it reports a status)."""
    url = service.availability_url
    response, latency, exc = await _request(client, "GET", url, service.timeout_seconds)
    if exc is not None:
        return ProbeResult(kind="availability", url=url, ok=False, latency_ms=latency,
                           error_kind=_error_kind(exc), error=_short(f"{type(exc).__name__}: {exc}"))
    details: dict[str, Any] = {}
    try:
        body = response.json()
        if isinstance(body, dict):
            details = {k: body[k] for k in ("status", "service", "version") if k in body}
    except ValueError:
        body = None
    if not response.is_success:
        return ProbeResult(kind="availability", url=url, ok=False, http_status=response.status_code, latency_ms=latency,
                           error_kind="http_error", error=f"HTTP {response.status_code}", details=details)
    reported = details.get("status")
    if reported is not None and str(reported).lower() not in {"ok", "healthy", "pass", "up"}:
        return ProbeResult(kind="availability", url=url, ok=False, http_status=response.status_code, latency_ms=latency,
                           error_kind="invalid_response", error=f"health endpoint reported status={reported!r}", details=details)
    return ProbeResult(kind="availability", url=url, ok=True, http_status=response.status_code, latency_ms=latency, details=details)


def _probe_failures(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise the apps' probe reports (NovaMart uses name/message, NovaConnect check/failure_reason)."""
    failures = []
    for check in body.get("checks", []) or []:
        if not isinstance(check, dict) or check.get("status") != "fail":
            continue
        details = check.get("details") if isinstance(check.get("details"), dict) else {}
        failures.append({
            "check": check.get("check") or check.get("name"),
            "component": check.get("component"),
            "reason": _short(str(check.get("failure_reason") or check.get("message") or "")),
            "source": check.get("source") or details.get("source"),
            "expected": check.get("expected", details.get("expected")),
            "actual": check.get("actual", details.get("actual")),
        })
    return failures


async def probe_functional(client: httpx.AsyncClient, service: ServiceConfig) -> ProbeResult:
    """Business-logic probe: healthy only if it answers with JSON status == 'healthy'."""
    url = service.functional_url
    response, latency, exc = await _request(client, service.functional_method, url, service.functional_timeout_seconds)
    if exc is not None:
        return ProbeResult(kind="functional", url=url, ok=False, latency_ms=latency,
                           error_kind=_error_kind(exc), error=_short(f"{type(exc).__name__}: {exc}"))
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or "status" not in body:
        return ProbeResult(kind="functional", url=url, ok=False, http_status=response.status_code, latency_ms=latency,
                           error_kind="invalid_response" if response.is_success else "http_error",
                           error=_short(f"HTTP {response.status_code}: response is not a probe report: {response.text[:200]!r}"))
    details = {
        "status": body.get("status"),
        "demo_outage": body.get("demo_outage") is True,
        "summary": body.get("summary"),
        "failed_checks": body.get("failed_checks", []),
        "failures": _probe_failures(body),
    }
    healthy = str(body.get("status")).lower() == "healthy"
    if healthy and response.is_success:
        return ProbeResult(kind="functional", url=url, ok=True, http_status=response.status_code, latency_ms=latency, details=details)
    if not healthy and response.status_code in (200, 503):
        reasons = "; ".join(f"{f['check']}: {f['reason']}" for f in details["failures"]) or str(body.get("summary"))
        return ProbeResult(kind="functional", url=url, ok=False, http_status=response.status_code, latency_ms=latency,
                           error_kind="probe_failed", error=_short(reasons), details=details)
    return ProbeResult(kind="functional", url=url, ok=False, http_status=response.status_code, latency_ms=latency,
                       error_kind="http_error", error=f"HTTP {response.status_code}", details=details)
