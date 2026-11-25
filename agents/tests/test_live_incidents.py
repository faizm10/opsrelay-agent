"""Live-check incidents in Atlas (mongomock): open/update/resolve, evidence, investigator hand-off."""
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

import mongomock
import pytest

from agents import live_incidents as li
from agents import memory
from agents import mongodb as m
from agents.contracts import LiveCheckResult, ProbeStep

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db():
    db = mongomock.MongoClient(tz_aware=True)["opsrelay_live_test"]
    m.ensure_setup(db, vector_index=False)
    return db


def check(ok=False, step="send", status=500, kind="http_error", service="messaging"):
    url = "http://127.0.0.1:8002"
    steps = [ProbeStep(name="health", method="GET", url=f"{url}/health", status_code=200, latency_ms=3.0)]
    if not ok:
        steps.append(ProbeStep(name=step, method="POST", url=f"{url}/probe/messages",
                               status_code=status, failure_kind=kind, error="Internal Server Error"))
    return LiveCheckResult(
        service=service, app="messaging", checked_at=datetime.now(UTC), base_url=url, target="fixture",
        probe_id=f"opsrelay-{uuid.uuid4().hex}", mode="send_readback", reachable=True, healthy=True,
        delivery="delivered" if ok else "not_delivered", failure_kind=None if ok else kind,
        error=None if ok else "Internal Server Error", http_status=None if ok else status, steps=steps,
        delivery_latency_ms=12.0 if ok else None,
    )


def test_repeated_failures_update_one_incident_then_resolve(db):
    inc, ev = li.record_check(check(), db=db)
    assert ev == "opened" and inc["failed_step"]["name"] == "send"
    assert inc["failed_step"]["status_code"] == 500
    for _ in range(3):
        inc2, ev2 = li.record_check(check(), db=db)
        assert ev2 == "updated" and inc2["_id"] == inc["_id"]
    assert m.live_incidents(db).count_documents({}) == 1
    assert m.live_incidents(db).find_one()["failure_count"] == 4

    good = check(ok=True)
    res, ev3 = li.record_check(good, db=db)
    assert ev3 == "resolved" and res["status"] == li.LIVE_RESOLVED
    assert res["recovery"]["probe_id"] == good.probe_id and "active_service" not in res
    assert li.record_check(check(ok=True), db=db) == (None, "healthy")

    # A new failure after recovery is a new incident
    inc4, ev4 = li.record_check(check(), db=db)
    assert ev4 == "opened" and inc4["_id"] != inc["_id"]
    steps = [s["stage"] for s in m.incident_timeline(inc["_id"], db=db)]
    assert steps.count("live_check_failed") == 4 and "live_resolved" in steps


def test_services_are_tracked_separately(db):
    li.record_check(check(service="messaging"), db=db)
    li.record_check(check(service="shopping"), db=db)
    assert m.live_incidents(db).count_documents({"status": "open"}) == 2


def test_evidence_has_step_commit_changes_logs_ci_and_similar(db, messaging, tmp_path, monkeypatch):
    memory.seed_memory("messaging",
                       "live check failed for messaging: step send POST /probe/messages http_error HTTP 500",
                       "Probe send 500: idempotency guard inverted in messages.py", db=db)
    memory.seed_memory("shopping", "step send http_error HTTP 500", "shopping memory must not leak", db=db)
    log = tmp_path / "svc.log"
    c = check()
    log.write_text(f"INFO ok\nERROR messaging_probe_failed probe={c.probe_id}\nINFO tail line\n")
    monkeypatch.setenv("OPSRELAY_MESSAGING_LOG_FILE", str(log))
    monkeypatch.delenv("OPSRELAY_MESSAGING_DEPLOYED_SHA", raising=False)
    github = NS(list_workflow_runs=lambda **kw: [
        {"path": ".github/workflows/shopping-ci.yml", "status": "completed", "conclusion": "failure"},
        {"path": ".github/workflows/messaging-ci.yml", "status": "completed", "conclusion": "success"}])

    inc, _ = li.record_check(c, db=db)
    ev = li.collect_evidence(inc, c, str(messaging.repo), github=github, db=db)

    assert ev.deployed_sha and len(ev.deployed_sha) == 40
    assert ev.ci_status == "passing"
    assert c.probe_id in ev.service_logs
    assert any(p.startswith("message-platform/src/") for p in ev.source_files)
    assert ev.similar_incidents and "idempotency" in ev.similar_incidents[0].summary
    assert all("leak" not in s.summary for s in ev.similar_incidents)
    stored = m.live_incidents(db).find_one({"_id": inc["_id"]})["evidence"]
    assert stored["failed_step"]["name"] == "send" and stored["deployed_sha"] == ev.deployed_sha
    assert stored["ci_status"] == "passing" and stored["similar"]
    assert "evidence_collected" in [s["stage"] for s in m.incident_timeline(inc["_id"], db=db)]


def test_deployed_sha_env_override(monkeypatch):
    monkeypatch.setenv("OPSRELAY_MESSAGING_DEPLOYED_SHA", "abc1234")
    assert li.deployed_commit("messaging", str(REPO_ROOT)) == ("abc1234", "env")


def test_investigator_runs_once_and_result_is_stored(db):
    calls = []

    def diagnose(evidence):
        calls.append(evidence)
        return NS(outcome="needs_review", route="investigate", reason="delivery failed",
                  likely_cause="idempotency guard inverted", recommended_actions=["roll back"],
                  next_diagnostic=None, recovery="not_applicable")

    runs = iter([check(), check(), check()])
    watcher = li.LiveWatcher(["messaging"], str(REPO_ROOT), db=db, run_check=lambda s: next(runs),
                             diagnose=diagnose)
    for _ in range(3):
        watcher.tick()
    inc = m.live_incidents(db).find_one()
    assert len(calls) == 1 and inc["failure_count"] == 3
    assert inc["status"] == li.LIVE_DIAGNOSED
    assert inc["investigation"]["likely_cause"] == "idempotency guard inverted"
    assert calls[0].check.service == "messaging" and calls[0].deployed_sha


def test_investigator_error_leaves_incident_open_for_retry(db):
    c = check()
    inc, _ = li.record_check(c, db=db)

    def boom(_):
        raise TimeoutError("LLM timeout")

    assert li.investigate(inc, NS(), boom, db=db) is None
    doc = m.live_incidents(db).find_one({"_id": inc["_id"]})
    assert doc["status"] == li.LIVE_OPEN and "LLM timeout" in doc["last_error"]


def test_live_services_env(monkeypatch):
    monkeypatch.setenv("OPSRELAY_LIVE_SERVICES", "messaging, shopping")
    assert li.live_services() == ["messaging", "shopping"]
    monkeypatch.setenv("OPSRELAY_LIVE_SERVICES", "none")
    assert li.live_services() == []
    monkeypatch.delenv("OPSRELAY_LIVE_SERVICES")
    assert li.live_services() == []


def test_investigation_retries_after_error_up_to_limit(db):
    attempts = []

    def flaky(evidence):
        attempts.append(1)
        if len(attempts) < 2:
            raise TimeoutError("LLM timeout")
        return NS(outcome="needs_review", route="investigate", reason="r", likely_cause="c",
                  recommended_actions=[], next_diagnostic=None, recovery="not_applicable")

    w = li.LiveWatcher(["messaging"], str(REPO_ROOT), db=db, run_check=lambda s: check(), diagnose=flaky)
    w.tick()
    assert m.live_incidents(db).find_one()["status"] == li.LIVE_OPEN
    w.tick()
    assert m.live_incidents(db).find_one()["status"] == li.LIVE_DIAGNOSED and len(attempts) == 2
    w.tick()
    assert len(attempts) == 2  # diagnosed: no more calls

    def always_fails(_):
        attempts.append(1)
        raise TimeoutError("down")

    m.live_incidents(db).delete_many({})
    attempts.clear()
    w2 = li.LiveWatcher(["messaging"], str(REPO_ROOT), db=db, run_check=lambda s: check(),
                        diagnose=always_fails)
    for _ in range(5):
        w2.tick()
    assert len(attempts) == li.MAX_INVESTIGATIONS


def test_system_check_against_fake_app(monkeypatch):
    """run_system_check classifies healthy / 503 functional failure / unreachable."""
    import httpx

    responses = {}

    def fake_get(url, timeout):
        r = responses[url]
        if isinstance(r, Exception):
            raise r
        return httpx.Response(r[0], json=r[1], request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv("OPSRELAY_SHOPPING_URL", "http://shop.test")
    responses["http://shop.test/api/health"] = (200, {"status": "ok"})
    responses["http://shop.test/api/health/checkout"] = (200, {"status": "healthy"})
    assert li.run_system_check("shopping").ok

    responses["http://shop.test/api/health/checkout"] = (
        503, {"status": "unhealthy", "failed_checks": ["promotion_discounts"]})
    c = li.run_system_check("shopping")
    assert not c.ok and c.reachable and c.healthy and c.delivery == "not_delivered"
    step = li.failed_step(c)
    assert step["name"] == "synthetic" and step["status_code"] == 503
    assert "promotion_discounts" in step["error"]

    responses["http://shop.test/api/health"] = httpx.ConnectError("Connection refused")
    c = li.run_system_check("shopping")
    assert not c.reachable and c.failure_kind == "connection_refused"
