"""Deterministic fixtures: fake clock, fake apps over httpx.MockTransport, both storage backends."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import mongomock
import pytest

from uptime.config import MonitorSettings
from uptime.models import ServiceConfig
from uptime.storage.mongo import MongoStorage
from uptime.storage.sqlite import SqliteStorage
from uptime.worker import UptimeMonitor

T0 = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)  # aligned to a 30 s slot


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        """Fake asyncio.sleep: advances the clock instead of waiting."""
        self.advance(seconds)
        await asyncio.sleep(0)


NOVAMART = ServiceConfig(id="novamart", name="NovaMart", app="shopping", frontend_url="http://shop-web.test",
                         backend_url="http://shop-api.test", availability_path="/api/health",
                         functional_path="/api/health/checkout")
NOVACONNECT = ServiceConfig(id="novaconnect", name="NovaConnect", app="messaging", frontend_url="http://msg-web.test",
                            backend_url="http://msg-api.test", availability_path="/health",
                            functional_path="/api/monitor/probe")
SERVICES = [NOVAMART, NOVACONNECT]

SHOP_PROBE_FAIL = {
    "probe": "checkout", "status": "unhealthy", "summary": "5/7 checks passed",
    "failed_checks": ["promotion_discounts", "checkout_quote_totals"],
    "checks": [
        {"name": "promotion_discounts", "component": "pricing", "source": "backend/app/pricing.py::compute_discount",
         "status": "fail", "message": "Promotion discounts do not match the advertised percentage/amount.",
         "expected": {"WELCOME10@20000": 2000}, "actual": {"WELCOME10@20000": 20000}},
        {"name": "catalog_integrity", "status": "pass", "message": "ok"},
    ],
}
MSG_PROBE_FAIL = {
    "service": "messaging", "probe": "functional", "status": "unhealthy", "summary": "5/6 checks passed",
    "failed_checks": ["message_ordering"],
    "checks": [{"service": "messaging", "check": "message_ordering", "component": "messages", "status": "fail",
                "failure_reason": "Rule 3 violated: history is not in chronological (oldest -> newest) order.",
                "details": {"expected": ["one", "two"], "actual": ["two", "one"],
                            "source": "backend/app/messages.py::sort_timeline"}}],
}


class FakeApps:
    """Per-service behaviour: healthy | functional_bug | backend_down | frontend_down | probe_timeout |
    availability_timeout | probe_garbage | slow:<seconds>."""

    HOSTS = {"shop-web.test": ("novamart", "frontend"), "shop-api.test": ("novamart", "backend"),
             "msg-web.test": ("novaconnect", "frontend"), "msg-api.test": ("novaconnect", "backend")}

    def __init__(self) -> None:
        self.mode = {"novamart": "healthy", "novaconnect": "healthy"}
        self.requests: list[str] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        service, part = self.HOSTS[request.url.host]
        mode = self.mode[service]
        self.requests.append(f"{service}:{request.url.path}")
        if mode.startswith("slow:"):
            await asyncio.sleep(float(mode.split(":")[1]))
            mode = "healthy"
        if mode == "backend_down" and part == "backend":
            raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
        if mode == "frontend_down" and part == "frontend":
            raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
        if part == "frontend":
            return httpx.Response(200, text="<html>store</html>")
        path = request.url.path
        if path in ("/api/health", "/health"):
            if mode == "availability_timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(200, json={"status": "ok", "service": service})
        if mode == "probe_timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if mode == "probe_garbage":
            return httpx.Response(200, text="<html>oops</html>")
        if mode == "functional_bug":
            return httpx.Response(503, json=SHOP_PROBE_FAIL if service == "novamart" else MSG_PROBE_FAIL)
        return httpx.Response(200, json={"status": "healthy", "summary": "all checks passed", "failed_checks": [], "checks": []})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def apps() -> FakeApps:
    return FakeApps()


def make_sqlite(tmp_path: Path) -> SqliteStorage:
    s = SqliteStorage(tmp_path / "uptime.db")
    s.init()
    return s


def make_mongo() -> MongoStorage:
    # mongomock applies TTL indexes against the *real* wall clock, while these tests run on a fixed fake
    # clock in the past, so TTL would delete fresh test data. TTL index creation is tested separately.
    s = MongoStorage(mongomock.MongoClient(tz_aware=True)["opsrelay"], ttl_indexes=False)
    s.init()
    return s


@pytest.fixture(params=["sqlite", "mongodb"])
def storage(request, tmp_path):
    s = make_sqlite(tmp_path) if request.param == "sqlite" else make_mongo()
    yield s


@pytest.fixture
def monitor_factory(clock, apps):
    def build(storage, *, worker_id="w1", services=SERVICES, interval=30, **kw):
        return UptimeMonitor(storage, services, interval_seconds=interval, clock=clock, sleep=clock.sleep,
                             transport=apps.transport(), worker_id=worker_id, **kw)
    return build


@pytest.fixture
def settings() -> MonitorSettings:
    return MonitorSettings(services=SERVICES, interval_seconds=30)


def dump(obj) -> str:
    return json.dumps(obj, default=str, indent=2)[:2000]


def signed_in(app, email: str = "owner@example.com"):
    """A TestClient signed in as `email`; the first account to sign up owns the demo workspace."""
    from fastapi.testclient import TestClient

    client = TestClient(app)
    creds = {"email": email, "password": "correct horse"}
    if client.post("/api/v1/auth/sign-up", json=creds).status_code == 409:
        client.post("/api/v1/auth/sign-in", json=creds).raise_for_status()
    return client
