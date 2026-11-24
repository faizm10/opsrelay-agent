"""Dashboard API: envelope, independent statuses, uptime from stored checks, incidents, demo scenario."""

import asyncio
from datetime import timedelta

import httpx
import pytest

from fastapi.testclient import TestClient

from uptime.api import create_app
from uptime.worker import UptimeMonitor

from .conftest import SERVICES, make_mongo, make_sqlite, signed_in


@pytest.fixture(params=["sqlite", "mongodb"])
def env(request, tmp_path, settings, clock, apps):
    storage = make_sqlite(tmp_path) if request.param == "sqlite" else make_mongo()
    monitor = UptimeMonitor(storage, SERVICES, interval_seconds=30, clock=clock, sleep=clock.sleep,
                            transport=apps.transport(), worker_id="test-worker")
    storage.acquire_lease("test-worker", clock(), 70)

    def tick(n=1):
        for _ in range(n):
            asyncio.run(monitor.run_cycle())
            clock.advance(30)
            storage.acquire_lease("test-worker", clock(), 70)

    def client(email="owner@example.com"):
        return signed_in(create_app(storage, settings, clock=clock), email)

    return {"storage": storage, "tick": tick, "client": client, "apps": apps, "clock": clock}


def test_envelope_and_unknown_before_any_check(env):
    body = env["client"]().get("/api/v1/summary").json()
    assert set(body) == {"data", "meta"} and body["meta"]["storage"]["backend"] in ("sqlite", "mongodb")
    assert body["data"]["overall"]["status"] == "unknown"
    assert all(s["current"]["status"] == "unknown" for s in body["data"]["services"])
    assert body["data"]["monitoring"]["status"] == "not_started"


def test_independent_statuses_and_real_uptime(env):
    env["tick"](2)
    env["apps"].mode["novamart"] = "functional_bug"
    env["tick"](2)
    data = env["client"]().get("/api/v1/summary", params={"window": "24h"}).json()["data"]
    by_id = {s["id"]: s for s in data["services"]}
    assert by_id["novamart"]["current"]["status"] == "degraded"
    assert by_id["novaconnect"]["current"]["status"] == "operational"
    assert data["overall"] == {"status": "degraded", "label": "Partial degradation", "affected_services": ["NovaMart"]}
    up = by_id["novamart"]["uptime"]
    assert up["check_count"] == 4 and up["failed_check_count"] == 2
    assert up["uptime_pct"] == pytest.approx(50.0, abs=0.01), "computed from stored checks, not hardcoded"
    assert up["sufficient"] is False, "2 minutes of a 24 h window is insufficient history"
    assert len(by_id["novamart"]["bars"]) == 96
    assert [i["service_id"] for i in data["active_incidents"]] == ["novamart"]


def test_agent_activity_tracks_only_linked_run_and_hides_private_evidence(env):
    env["apps"].mode["novamart"] = "functional_bug"
    env["tick"]()
    c = env["client"]()
    summary = c.get("/api/v1/summary").json()["data"]
    incident_id = summary["active_incidents"][0]["id"]
    activity = summary["agent_activity"]
    assert len(activity) == 1 and activity[0]["incident_id"] == incident_id
    if not hasattr(env["storage"], "db"):
        assert activity[0]["state"] == "unavailable"
        return

    assert activity[0]["state"] == "waiting"
    db = env["storage"].db
    now = env["clock"]()
    db["incidents"].insert_one({
        "_id": "repo#unrelated", "source": "live", "live": {"uptime_incident_id": "unrelated"},
        "status": "open", "stage": "detected", "created_at": now, "updated_at": now,
    })
    assert c.get("/api/v1/summary").json()["data"]["agent_activity"][0]["state"] == "waiting"

    agent_id = f"repo#{incident_id}"
    db["incidents"].insert_one({
        "_id": agent_id, "source": "live", "live": {"uptime_incident_id": incident_id},
        "status": "open", "stage": "memory_retrieved", "created_at": now,
        "updated_at": now, "lease": {"expires_at": now + timedelta(minutes=2)},
        "last_error": "private diagnosis must stay hidden",
    })
    db["workflow_steps"].insert_one({
        "incident_id": agent_id, "stage": "memory_retrieved", "outcome": "completed",
        "at": now, "evidence": {"secret": "private diagnosis must stay hidden"},
    })
    response = c.get("/api/v1/summary")
    activity = response.json()["data"]["agent_activity"][0]
    assert activity["state"] == "working"
    assert activity["stage"] == "memory_retrieved"
    assert activity["latest_action"] == {"stage": "memory_retrieved", "outcome": "completed", "at": now.isoformat().replace("+00:00", "Z")}
    assert "private diagnosis" not in response.text

    env["clock"].advance(121)
    assert c.get("/api/v1/summary").json()["data"]["agent_activity"][0]["state"] == "waiting"

    db["incidents"].update_one({"_id": agent_id}, {"$set": {
        "status": "awaiting_merge", "pr": {"url": "https://github.com/example/repo/pull/1"},
    }})
    activity = c.get("/api/v1/summary").json()["data"]["agent_activity"][0]
    assert activity["state"] == "awaiting_review"
    assert activity["pr_url"] == "https://github.com/example/repo/pull/1"


def test_service_detail_checks_latency_and_incidents(env):
    env["apps"].mode["novaconnect"] = "backend_down"
    env["tick"](3)
    c = env["client"]()
    detail = c.get("/api/v1/services/novaconnect").json()["data"]
    assert detail["current"]["status"] == "down" and detail["current"]["failure_type"] == "backend_unreachable"
    assert set(detail["uptime"]) == {"24h", "7d", "30d"}
    assert detail["service"]["functional_path"] == "/api/monitor/probe" and detail["service"]["interval_seconds"] == 30
    assert len(detail["incident_ids"]) == 1
    checks = c.get("/api/v1/services/novaconnect/checks", params={"limit": 2}).json()["data"]
    assert len(checks) == 2 and checks[0]["checked_at"] > checks[1]["checked_at"]
    assert checks[0]["availability"]["error_kind"] == "connection_refused"
    assert len(c.get("/api/v1/services/novamart/latency").json()["data"]) == 120
    assert c.get("/api/v1/services/nope").status_code == 404


def test_service_incident_history_survives_newer_incidents_elsewhere(env):
    env["apps"].mode["novaconnect"] = "backend_down"
    env["tick"]()
    storage = env["storage"]
    outage = storage.get_open_incident("novaconnect")
    for n in range(200):
        newer = outage.model_copy(update={"id": f"incident-{n}", "service_id": "novamart",
                                         "started_at": env["clock"]()})
        assert storage.create_incident(newer)
        storage.save_incident(newer.model_copy(update={"state": "resolved", "resolved_at": env["clock"]()}))

    data = env["client"]().get("/api/v1/incidents", params={"service_id": "novaconnect", "limit": 1}).json()["data"]
    assert [item["id"] for item in data] == [outage.id]


def test_incident_detail_and_agent_handoff_evidence(env):
    env["apps"].mode["novamart"] = "functional_bug"
    env["tick"]()
    c = env["client"]()
    incident = c.get("/api/v1/incidents", params={"state": "open"}).json()["data"][0]
    evidence = c.get(f"/api/v1/incidents/{incident['id']}/evidence").json()["data"]
    assert evidence["schema"] == "opsrelay.uptime_incident.v1"
    assert evidence["investigation_route"] == "investigate" and evidence["code_repair_candidate"] is True
    assert evidence["suspect_sources"] == ["backend/app/pricing.py::compute_discount"]
    assert evidence["live_check"]["healthy"] is True and evidence["live_check"]["app"] == "shopping"
    env["apps"].mode["novamart"] = "backend_down"
    env["tick"]()
    evidence = c.get(f"/api/v1/incidents/{incident['id']}/evidence").json()["data"]
    assert evidence["investigation_route"] == "service_health" and evidence["code_repair_candidate"] is False
    assert evidence["live_check"]["failure_kind"] == "connection_refused"


def test_stale_data_is_reported_as_unknown(env):
    env["tick"]()
    env["clock"].advance(600)  # worker stopped for 10 minutes
    data = env["client"]().get("/api/v1/summary").json()["data"]
    assert all(s["current"]["status"] == "unknown" and s["current"]["stale"] for s in data["services"])
    assert data["monitoring"]["status"] == "stale"


def test_mutations_need_no_token(env):
    c = env["client"]()
    r = c.patch("/api/v1/services/novamart", json={"enabled": False})
    assert r.status_code == 200 and r.json()["data"]["enabled"] is False


def test_can_attach_repair_pr_link_once(env):
    env["apps"].mode["novamart"] = "functional_bug"
    env["tick"]()
    c = env["client"]()
    inc = c.get("/api/v1/incidents").json()["data"][0]["id"]
    payload = {"kind": "repair_pr", "url": "https://github.com/faizm10/agentic-mongodb/pull/99", "label": "Fix discount"}
    for _ in range(2):
        r = c.post(f"/api/v1/incidents/{inc}/links", json=payload)
    assert r.status_code == 200 and len(r.json()["data"]["links"]) == 1


def test_demo_scenario_degraded_then_recovered_while_other_service_stays_operational(env):
    """Spec section 9: bug -> degraded within one cycle, NovaConnect unaffected, reset -> operational,
    incident history records the degraded period. Then NovaConnect's backend stops -> down (not degraded)."""
    c = env["client"]()
    status = lambda: {s["id"]: s["current"]["status"] for s in c.get("/api/v1/summary").json()["data"]["services"]}  # noqa: E731
    env["tick"]()
    assert status() == {"novamart": "operational", "novaconnect": "operational"}
    env["apps"].mode["novamart"] = "functional_bug"          # discount bug injected
    env["tick"]()                                             # the next 30 s cycle
    assert status() == {"novamart": "degraded", "novaconnect": "operational"}
    env["tick"]()
    env["apps"].mode["novamart"] = "healthy"                 # bug reset
    env["tick"]()
    assert status() == {"novamart": "operational", "novaconnect": "operational"}
    history = c.get("/api/v1/incidents", params={"service_id": "novamart"}).json()["data"]
    assert len(history) == 1 and history[0]["state"] == "resolved" and history[0]["peak_severity"] == "degraded"
    assert history[0]["duration_seconds"] == 60 and history[0]["failed_check_count"] == 2
    assert [e["type"] for e in history[0]["events"]] == ["opened", "resolved"]

    env["apps"].mode["novaconnect"] = "backend_down"         # NovaConnect backend stopped
    env["tick"]()
    assert status() == {"novamart": "operational", "novaconnect": "down"}


def test_learning_summary_is_aggregate_only(env):
    c = env["client"]()
    if not hasattr(env["storage"], "db"):
        assert c.get("/api/v1/learning").json()["data"] == {
            "available": False, "totals": {}, "strategies": [], "training": None,
            "decisions": [], "vector_indexes": []}
        return
    db = env["storage"].db
    assert c.get("/api/v1/learning").json()["data"]["available"] is False  # nothing trained yet

    now = env["clock"]()
    db["rl_experiences"].insert_many([
        {"app": "shopping", "action": "fix_discount_divisor", "reward": 1.0, "source": "mock",
         "state_text": "private log line", "embedding": [0.1] * 4},
        {"app": "shopping", "action": "fix_discount_divisor", "reward": 1.0, "source": "ci", "embedding": [0.1] * 4},
        {"app": "shopping", "action": "revert_last_commit", "reward": 0.0, "source": "mock", "embedding": [0.1] * 4},
    ])
    db["mock_incidents"].insert_one({"_id": "mock:000", "failed_logs": "private log line"})
    db["rl_training_runs"].insert_one({
        "at": now, "accuracy_before": 0.13, "accuracy_after": 0.83, "per_bug": {"discount": 1.0},
        "curve": [{"epoch": 1, "incidents": "1-25", "first_try_rate": 0.24, "avg_attempts": 2.3}]})
    db["incidents"].insert_one({
        "_id": "repo#9", "app": "shopping", "source": "ci", "status": "awaiting_merge", "created_at": now,
        "rl": {"action": "fix_discount_divisor", "expected_success": 0.95, "hinted": True},
        "pr": {"url": "javascript:alert(1)"}, "failed_logs": "private log line"})

    r = c.get("/api/v1/learning")
    data = r.json()["data"]
    assert data["available"] and data["totals"]["experiences"] == 3 and data["totals"]["real_experiences"] == 1
    assert data["totals"]["mock_incidents"] == 1
    top = data["strategies"][0]
    assert (top["action"], top["tries"], top["success_rate"], top["real_tries"]) == ("fix_discount_divisor", 2, 1.0, 1)
    assert data["training"]["accuracy_after"] == 0.83 and data["training"]["curve"][0]["first_try_rate"] == 0.24
    assert data["decisions"][0]["action"] == "fix_discount_divisor" and data["decisions"][0]["pr_url"] is None
    assert "private log line" not in r.text and "\"embedding\":" not in r.text


def test_accounts_only_see_their_own_workspaces(env):
    env["apps"].mode["novamart"] = "functional_bug"
    env["tick"](2)
    owner = env["client"]()  # first sign-up: owns the demo workspace and its services
    incident_id = owner.get("/api/v1/incidents").json()["data"][0]["id"]
    other = env["client"]("other@example.com")

    assert other.get("/api/v1/auth/me").json()["data"]["workspaces"] == []
    assert other.get("/api/v1/summary").json()["error"]["message"] == "Create a workspace first."
    own_ws = other.post("/api/v1/workspaces", json={"name": "Other team"}).json()["data"]["id"]
    data = other.get("/api/v1/summary").json()["data"]
    assert data["services"] == [] and data["active_incidents"] == []
    assert other.get("/api/v1/incidents").json()["data"] == []

    # Guessing the other account's workspace or incident ids finds nothing.
    for path in ("/api/v1/summary", "/api/v1/services", "/api/v1/incidents"):
        assert other.get(path, headers={"x-opsrelay-workspace": "demo"}).status_code == 404
    other.cookies.set("opsrelay_workspace", "demo")
    assert other.get("/api/v1/summary").status_code == 404
    other.cookies.set("opsrelay_workspace", own_ws)
    for path in (f"/api/v1/incidents/{incident_id}", f"/api/v1/incidents/{incident_id}/evidence",
                 f"/api/v1/incidents/{incident_id}/walkthrough", "/api/v1/services/novamart",
                 "/api/v1/services/novamart/checks"):
        assert other.get(path).status_code == 404, path
    assert other.post(f"/api/v1/incidents/{incident_id}/acknowledge", json={"by": "x"}).status_code == 404
    assert other.patch("/api/v1/services/novamart", json={"enabled": False}).status_code == 404
    assert owner.get("/api/v1/summary", headers={"x-opsrelay-workspace": own_ws}).status_code == 404
    assert owner.get(f"/api/v1/incidents/{incident_id}").status_code == 200

    assert TestClient(owner.app).get("/api/v1/summary").status_code == 401
    assert other.post("/api/v1/auth/sign-in", json={"email": "other@example.com", "password": "wrong pass"}).status_code == 401
    other.post("/api/v1/auth/sign-out")
    assert other.get("/api/v1/auth/me").status_code == 401


def test_workspace_connects_github_and_maps_repositories(tmp_path, settings, clock):
    def github(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] != "Bearer ghp_valid_token_for_tests_000":
            return httpx.Response(401)
        routes = {
            "/user": {"login": "octo"},
            "/user/repos": [{"full_name": "octo/shop", "default_branch": "main", "private": True},
                            {"full_name": "octo/chat", "default_branch": "trunk", "private": False}],
            "/repos/octo/shop": {"full_name": "octo/shop"}, "/repos/octo/chat": {"full_name": "octo/chat"},
            "/repos/octo/shop/branches/main": {"name": "main"}, "/repos/octo/chat/branches/trunk": {"name": "trunk"},
            "/repos/octo/shop/actions/workflows/ci.yml": {"path": ".github/workflows/ci.yml"},
        }
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    app = create_app(make_sqlite(tmp_path), settings, clock=clock, github_transport=httpx.MockTransport(github))
    owner = signed_in(app)
    assert owner.put("/api/v1/github", json={"token": "ghp_wrong_token_for_tests_0000"}).status_code == 422
    assert owner.post("/api/v1/repositories", json={"repo": "octo/shop", "service_name": "Shop", "base_branch": "main",
                                                    "deployed_url": "https://shop.example"}).status_code == 409
    connected = owner.put("/api/v1/github", json={"token": "ghp_valid_token_for_tests_000"}).json()["data"]
    assert connected["login"] == "octo" and "token" not in connected
    assert [r["full_name"] for r in owner.get("/api/v1/github/repos").json()["data"]] == ["octo/shop", "octo/chat"]

    shop = {"repo": "octo/shop", "service_name": "Shop", "base_branch": "main", "deployed_url": "https://shop.example", "workflow": "ci.yml"}
    chat = {"repo": "octo/chat", "service_name": "Chat API", "base_branch": "trunk", "deployed_url": "https://chat.example"}
    assert owner.post("/api/v1/repositories", json={**chat, "base_branch": "nope"}).json()["error"]["message"] == \
        "Branch 'nope' doesn't exist in octo/chat."
    assert owner.post("/api/v1/repositories", json={**chat, "workflow": "missing.yml"}).status_code == 422
    assert owner.post("/api/v1/repositories", json=shop).status_code == 200
    assert owner.post("/api/v1/repositories", json=chat).status_code == 200
    assert owner.post("/api/v1/repositories", json=shop).status_code == 409
    listed = owner.get("/api/v1/repositories").json()["data"]
    assert {(m["service_id"], m["repo"], m["base_branch"], m["workflow"]) for m in listed} == {
        ("shop", "octo/shop", "main", "ci.yml"), ("chat-api", "octo/chat", "trunk", None)}

    other = signed_in(app, "other@example.com")
    other.post("/api/v1/workspaces", json={"name": "Other"})
    assert other.get("/api/v1/repositories").json()["data"] == []
    assert other.get("/api/v1/github").json()["data"] == {"connected": False}
    assert other.delete("/api/v1/repositories/shop").status_code == 404
    assert owner.delete("/api/v1/repositories/shop").status_code == 200
    assert [m["service_id"] for m in owner.get("/api/v1/repositories").json()["data"]] == ["chat-api"]


def test_betterstack_alerts_route_to_their_workspace_and_replays_update_one_incident(tmp_path, settings, clock):
    storage = make_sqlite(tmp_path)
    app = create_app(storage, settings, clock=clock)
    owner = signed_in(app)
    storage.insert_doc("repos", "demo:shop", {"id": "demo:shop", "workspace_id": "demo", "service_id": "shop", "service_name": "Shop",
                                              "repo": "octo/shop", "base_branch": "main", "deployed_url": "https://shop.example",
                                              "workflow": None, "created_at": "2026-03-02T12:00:00Z"})
    hook = owner.post("/api/v1/betterstack").json()["data"]
    assert owner.put("/api/v1/repositories/shop/betterstack", json={"monitor_id": "42"}).status_code == 200

    def alert(path=hook["webhook_path"], secret=hook["secret"], monitor="42", **attrs):
        body = {"data": {"id": "9001", "type": "incident",
                         "attributes": {"cause": "Status 500", "url": "https://shop.example", "started_at": "2026-03-02T11:59:00Z",
                                        "acknowledged_at": None, "resolved_at": None, **attrs},
                         "relationships": {"monitor": {"data": {"id": monitor, "type": "monitor"}}}}}
        return TestClient(app).post(path, json=body, headers={"X-OpsRelay-Secret": secret} if secret else {})

    assert alert(secret=None).status_code == 401 and alert(secret="wrong").status_code == 401
    created = alert().json()["data"]
    assert created["result"] == "created"
    incident_id = created["incident_id"]
    events = len(owner.get(f"/api/v1/incidents/{incident_id}").json()["data"]["events"])
    assert alert().json()["data"] == {"result": "duplicate", "incident_id": incident_id}, "a redelivery changes nothing"
    listed = owner.get("/api/v1/incidents").json()["data"]
    assert [(i["id"], i["service_name"]) for i in listed if i["id"].startswith("BS-")] == [(incident_id, "Shop")]
    assert len(owner.get(f"/api/v1/incidents/{incident_id}").json()["data"]["events"]) == events

    assert alert(acknowledged_at="2026-03-02T12:01:00Z").json()["data"] == {"result": "updated", "incident_id": incident_id}
    resolved = {"acknowledged_at": "2026-03-02T12:01:00Z", "resolved_at": "2026-03-02T12:05:00Z"}
    assert alert(**resolved).json()["data"]["result"] == "updated"
    assert alert(**resolved).json()["data"]["result"] == "duplicate"
    assert owner.get(f"/api/v1/incidents/{incident_id}").json()["data"]["state"] == "resolved"
    assert alert(cause="Timeout").json()["data"] == {"result": "updated", "incident_id": incident_id}, "reopened"
    reopened = owner.get(f"/api/v1/incidents/{incident_id}").json()["data"]
    assert reopened["state"] == "open" and reopened["events"][-1]["type"] == "relapsed"
    assert alert(monitor="7").json()["data"]["result"] == "ignored"
    assert owner.get("/api/v1/betterstack").json()["data"]["last_delivery"]["result"] == "ignored"

    other = signed_in(app, "other@example.com")
    other.post("/api/v1/workspaces", json={"name": "Other"})
    other_hook = other.post("/api/v1/betterstack").json()["data"]
    assert alert(path=other_hook["webhook_path"], secret=other_hook["secret"]).json()["data"]["result"] == "ignored", \
        "monitor 42 is only mapped in the owner's workspace"
    assert alert(path=hook["webhook_path"], secret=other_hook["secret"]).status_code == 401
    assert TestClient(app).post(hook["webhook_path"], content=b"").status_code == 401  # empty body, no secret
    assert other.get(f"/api/v1/incidents/{incident_id}").status_code == 404
    assert other.put("/api/v1/repositories/shop/betterstack", json={"monitor_id": "42"}).status_code == 404


def test_repo_agent_is_off_by_default_and_only_enables_for_the_workers_repo(tmp_path, settings, clock, monkeypatch):
    monkeypatch.setenv("GITHUB_OWNER", "octo")
    monkeypatch.setenv("GITHUB_REPO", "shop")
    storage = make_sqlite(tmp_path)
    owner = signed_in(create_app(storage, settings, clock=clock))
    for sid, repo in (("shop", "octo/shop"), ("chat", "octo/chat")):
        storage.insert_doc("repos", f"demo:{sid}", {"id": f"demo:{sid}", "workspace_id": "demo", "service_id": sid, "service_name": sid,
                                                    "repo": repo, "base_branch": "main", "deployed_url": "https://x.example",
                                                    "workflow": None, "created_at": "2026-03-02T12:00:00Z"})
    repos = {r["service_id"]: r for r in owner.get("/api/v1/summary").json()["data"]["repositories"]}
    assert repos["shop"]["agent"] == {"enabled": False, "mode": "report_only", "app": None}
    assert repos["shop"]["health"] == "unknown", "no Better Stack monitor mapped yet"

    on = {"enabled": True, "mode": "report_only", "app": "shopping"}
    assert owner.put("/api/v1/repositories/chat/agent", json=on).status_code == 409, "the shared worker only serves octo/shop"
    assert owner.put("/api/v1/repositories/shop/agent", json={**on, "app": "nope"}).status_code == 422
    assert owner.put("/api/v1/repositories/shop/agent", json=on).json()["data"]["agent"] == on
    assert owner.put("/api/v1/repositories/chat/agent", json={"enabled": False}).status_code == 200, "switching off always works"
    diagnose = owner.put("/api/v1/repositories/chat/agent", json={"enabled": True, "mode": "diagnose", "app": "shopping"})
    assert diagnose.json()["data"]["agent"] == {"enabled": True, "mode": "diagnose", "app": None}, "any repository can be diagnosed"
    owner.put("/api/v1/repositories/chat/agent", json={"enabled": False}).raise_for_status()
    assert {r["service_id"]: r["agent"]["enabled"] for r in owner.get("/api/v1/summary").json()["data"]["repositories"]} == \
        {"shop": True, "chat": False}

    other = signed_in(create_app(storage, settings, clock=clock), "other@example.com")
    other.post("/api/v1/workspaces", json={"name": "Other"})
    assert other.put("/api/v1/repositories/shop/agent", json={"enabled": False}).status_code == 404


def test_sites_belong_to_one_workspace_and_scheduled_checks_need_the_secret(env, settings, clock, apps, monkeypatch):
    app = create_app(env["storage"], settings, clock=clock, monitor_kwargs={"transport": apps.transport()})
    owner, other = signed_in(app), signed_in(app, "other@example.com")
    other.post("/api/v1/workspaces", json={"name": "Other team"}).raise_for_status()

    added = other.post("/api/v1/sites", json={"name": "Shop", "url": "http://shop-web.test"})
    assert added.status_code == 200, added.text
    site_id = added.json()["data"]["id"]
    service = other.get("/api/v1/summary").json()["data"]["services"][-1]
    assert service["id"] == site_id and service["current"]["status"] == "operational"  # checked on add
    assert other.post("/api/v1/sites", json={"name": "x", "url": "ftp://shop"}).status_code == 422
    assert site_id not in {s["id"] for s in owner.get("/api/v1/sites").json()["data"]}
    assert owner.delete(f"/api/v1/sites/{site_id}").status_code == 404

    cron = TestClient(app)
    monkeypatch.delenv("OPSRELAY_CRON_SECRET", raising=False)
    assert cron.post("/api/v1/cron/check", headers={"Authorization": "Bearer "}).status_code == 401
    monkeypatch.setenv("OPSRELAY_CRON_SECRET", "s3cret")
    assert cron.post("/api/v1/cron/check", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert cron.post("/api/v1/cron/check", headers={"Authorization": "Bearer s3cret"}).status_code == 409  # worker's lease
    clock.advance(71)
    ran = cron.post("/api/v1/cron/check", headers={"Authorization": "Bearer s3cret"})
    assert ran.status_code == 200 and ran.json()["data"]["checked"] == len(SERVICES) + 1

    assert other.delete(f"/api/v1/sites/{site_id}").status_code == 200
    assert other.get("/api/v1/sites").json()["data"] == []


def test_private_site_urls_are_refused_when_blocking_is_on(env, settings, clock, monkeypatch):
    from uptime import netguard

    app = create_app(env["storage"], settings, clock=clock)
    owner = signed_in(app)
    monkeypatch.setenv("OPSRELAY_BLOCK_PRIVATE_URLS", "1")
    for body in ({"name": "a", "url": "http://127.0.0.1:8300"},
                 {"name": "b", "url": "https://example.com", "health_url": "http://169.254.169.254/latest"}):
        response = owner.post("/api/v1/sites", json=body)
        assert response.status_code == 422 and "public internet" in response.json()["error"]["message"]
    with pytest.raises(httpx.ConnectError):  # also refused per request, which covers redirects
        asyncio.run(netguard.guard_request(httpx.Request("GET", "http://localhost/")))
    monkeypatch.delenv("OPSRELAY_BLOCK_PRIVATE_URLS")
    asyncio.run(netguard.guard_request(httpx.Request("GET", "http://localhost/")))  # local dev allows it


def test_repeated_sign_in_attempts_are_rate_limited(env, settings, clock):
    client = TestClient(create_app(env["storage"], settings, clock=clock))
    creds = {"email": "someone@example.com", "password": "wrong password"}
    assert all(client.post("/api/v1/auth/sign-in", json=creds).status_code == 401 for _ in range(10))
    blocked = client.post("/api/v1/auth/sign-in", json=creds)
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0


def test_dependabot_alerts_become_one_incident_per_repository(tmp_path, settings, clock, monkeypatch):
    alerts = [{"number": 3, "html_url": "https://github.com/octo/shop/security/dependabot/3",
               "dependency": {"package": {"name": "next", "ecosystem": "npm"}, "manifest_path": "web/package-lock.json"},
               "security_advisory": {"severity": "high", "summary": "Cache poisoning"},
               "security_vulnerability": {"first_patched_version": {"identifier": "15.5.30"}}}]

    def github(request: httpx.Request) -> httpx.Response:
        routes = {"/user": {"login": "octo"}, "/repos/octo/shop": {"full_name": "octo/shop"},
                  "/repos/octo/shop/branches/main": {"name": "main"}, "/repos/octo/shop/dependabot/alerts": alerts}
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    monkeypatch.setenv("OPSRELAY_CRON_SECRET", "s3cret")
    app = create_app(make_sqlite(tmp_path), settings, clock=clock, github_transport=httpx.MockTransport(github))
    owner, other = signed_in(app), signed_in(app, "other@example.com")
    other.post("/api/v1/workspaces", json={"name": "Other"})
    owner.put("/api/v1/github", json={"token": "ghp_valid_token_for_tests_000"}).raise_for_status()
    owner.post("/api/v1/repositories", json={"repo": "octo/shop", "service_name": "Shop", "base_branch": "main",
                                             "deployed_url": "https://shop.example"}).raise_for_status()

    def scheduled():
        return TestClient(app).post("/api/v1/cron/check", headers={"Authorization": "Bearer s3cret"}).json()["data"]

    assert scheduled()["dependabot"] == {"created": 1}
    def dependabot_incidents(client):
        return [i for i in client.get("/api/v1/incidents").json()["data"] if i["evidence"].get("source") == "dependabot"]

    incident = dependabot_incidents(owner)[0]
    assert incident["severity"] == "down" and incident["title"] == "Shop: 1 open Dependabot alert"
    assert incident["evidence"]["alerts"][0]["patched_version"] == "15.5.30"
    assert dependabot_incidents(other) == []
    assert scheduled()["dependabot"] == {"updated": 1}
    assert len(dependabot_incidents(owner)) == 1  # still one incident

    alerts.clear()
    assert scheduled()["dependabot"] == {"resolved": 1}
    assert owner.get(f"/api/v1/incidents/{incident['id']}").json()["data"]["state"] == "resolved"
    assert owner.get("/api/v1/repositories").json()["data"][0]["dependabot"]["result"] == "resolved"


def test_notifications_post_each_event_once_and_retry_failed_sends(tmp_path, settings, clock, monkeypatch):
    posted, up = [], [False]

    def slack(request: httpx.Request) -> httpx.Response:
        posted.append(request.read().decode())
        return httpx.Response(200 if up[0] else 500)

    monkeypatch.setenv("OPSRELAY_CRON_SECRET", "s3cret")
    app = create_app(make_sqlite(tmp_path), settings, clock=clock, notify_transport=httpx.MockTransport(slack))
    owner = signed_in(app)
    assert owner.put("/api/v1/notifications/slack", json={"url": "https://example.com/x"}).status_code == 422
    owner.put("/api/v1/notifications/slack", json={"url": "https://hooks.slack.com/services/T0/B0/abcdef"}).raise_for_status()
    assert "hooks.slack.com" not in str(owner.get("/api/v1/notifications").json())  # the URL is a secret

    def scheduled():
        return TestClient(app).post("/api/v1/cron/check", headers={"Authorization": "Bearer s3cret"}).json()["data"]["notifications"]

    failed = scheduled()
    assert failed["sent"] == 0 and failed["failed"] >= 1  # Slack down: nothing is marked as sent
    up[0] = True
    assert scheduled() == {"sent": failed["failed"], "failed": 0}
    assert scheduled() == {"sent": 0, "failed": 0}  # already delivered
    assert "Incident opened" in posted[-1] and "http://testserver/incidents/" in posted[-1]


def test_webhook_connector_signs_the_exact_body_with_a_secret_shown_once(tmp_path, settings, clock):
    import hashlib
    import hmac

    received = []
    app = create_app(make_sqlite(tmp_path), settings, clock=clock,
                     notify_transport=httpx.MockTransport(lambda r: received.append(r) or httpx.Response(204)))
    owner = signed_in(app)
    saved = owner.put("/api/v1/notifications/webhook", json={"url": "https://hooks.example.com/opsrelay"}).json()["data"]
    assert "secret" not in str(owner.get("/api/v1/notifications").json())
    owner.post("/api/v1/notifications/webhook/test").raise_for_status()

    [request] = received
    body = request.read()
    expected = hmac.new(saved["secret"].encode(), body, hashlib.sha256).hexdigest()
    assert request.headers["x-opsrelay-signature"] == f"sha256={expected}"
    assert request.headers["x-opsrelay-event"] == "test"
    assert owner.put("/api/v1/notifications/pagerduty", json={"routing_key": "short"}).status_code == 422


def test_sentry_issue_webhooks_are_signed_and_roll_up_into_one_incident(tmp_path, settings, clock):
    import hashlib
    import hmac
    import json

    storage = make_sqlite(tmp_path)
    app = create_app(storage, settings, clock=clock)
    owner = signed_in(app)
    storage.insert_doc("repos", "demo:shop", {"id": "demo:shop", "workspace_id": "demo", "service_id": "shop", "service_name": "Shop",
                                              "repo": "octo/shop", "base_branch": "main", "deployed_url": "https://shop.example",
                                              "workflow": None, "created_at": "2026-03-02T12:00:00Z"})
    path = owner.post("/api/v1/sentry").json()["data"]["webhook_path"]
    secret = "s" * 64
    owner.put("/api/v1/sentry/secret", json={"client_secret": secret}).raise_for_status()
    owner.put("/api/v1/repositories/shop/sentry", json={"project": "shop-web"}).raise_for_status()

    def deliver(action, issue_id="11", signed_with=secret):
        body = json.dumps({"action": action, "data": {"issue": {"id": issue_id, "shortId": f"SHOP-{issue_id}", "title": "TypeError",
                                                                "level": "error", "project": {"slug": "shop-web"}}}}).encode()
        sig = hmac.new(signed_with.encode(), body, hashlib.sha256).hexdigest()
        return TestClient(app).post(path, content=body, headers={"Sentry-Hook-Signature": sig, "Sentry-Hook-Resource": "issue"})

    assert deliver("created", signed_with="wrong").status_code == 401
    created = deliver("created").json()["data"]
    assert created["result"] == "created"
    assert deliver("created").json()["data"]["open"] == 1  # a redelivery doesn't add the issue twice
    assert deliver("created", issue_id="12").json()["data"] == {"result": "updated", "open": 2, "incident_id": created["incident_id"]}
    deliver("resolved")
    assert deliver("ignored", issue_id="12").json()["data"]["result"] == "resolved"
    incident = owner.get(f"/api/v1/incidents/{created['incident_id']}").json()["data"]
    assert incident["state"] == "resolved" and incident["service_name"] == "Shop"


def test_failed_vercel_production_deploy_opens_an_incident_until_the_next_ready_one(tmp_path, settings, clock, monkeypatch):
    latest = {"uid": "dpl_1", "state": "ERROR", "created": 1_780_000_000_000, "url": "shop-abc.vercel.app",
              "meta": {"githubCommitSha": "abc1234def"}, "inspectorUrl": "https://vercel.com/octo/shop/dpl_1"}

    def api(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] != "Bearer vercel_token_for_tests_0001":
            return httpx.Response(403)
        if request.url.path == "/v2/user":
            return httpx.Response(200, json={"user": {"username": "octo"}})
        assert request.url.params["projectId"] == "shop" and request.url.params["target"] == "production"
        return httpx.Response(200, json={"deployments": [latest]})

    monkeypatch.setenv("OPSRELAY_CRON_SECRET", "s3cret")
    storage = make_sqlite(tmp_path)
    app = create_app(storage, settings, clock=clock, vercel_transport=httpx.MockTransport(api))
    owner = signed_in(app)
    storage.insert_doc("repos", "demo:shop", {"id": "demo:shop", "workspace_id": "demo", "service_id": "shop", "service_name": "Shop",
                                              "repo": "octo/shop", "base_branch": "main", "deployed_url": "https://shop.example",
                                              "workflow": None, "created_at": "2026-03-02T12:00:00Z"})
    assert owner.put("/api/v1/vercel", json={"token": "wrong_token_for_tests_000000"}).status_code == 422
    assert owner.put("/api/v1/vercel", json={"token": "vercel_token_for_tests_0001"}).json()["data"]["login"] == "octo"
    owner.put("/api/v1/repositories/shop/vercel", json={"project": "shop"}).raise_for_status()

    def scheduled():
        return TestClient(app).post("/api/v1/cron/check", headers={"Authorization": "Bearer s3cret"}).json()["data"]["vercel"]

    assert scheduled() == {"created": 1}
    [incident] = [i for i in owner.get("/api/v1/incidents").json()["data"] if i["evidence"].get("source") == "vercel"]
    assert incident["severity"] == "degraded" and incident["service_health"] == "degraded"
    latest.update(uid="dpl_2", state="BUILDING")
    assert scheduled() == {"building": 1}  # still open while the next deploy builds
    latest.update(state="READY")
    assert scheduled() == {"resolved": 1}
    assert owner.get("/api/v1/repositories").json()["data"][0]["vercel"]["state"] == "READY"


def test_failing_workflow_on_the_base_branch_opens_an_incident_until_it_passes(tmp_path, settings, clock, monkeypatch):
    def run(run_id, workflow_id, conclusion, name="CI"):
        return {"id": run_id, "workflow_id": workflow_id, "name": name, "head_sha": f"{run_id:07d}", "run_attempt": 1,
                "conclusion": conclusion, "html_url": f"https://github.com/octo/shop/actions/runs/{run_id}"}
    # Newest first: CI's latest run failed; Lint failed earlier but its latest run passed.
    runs = [run(9, 1, "failure"), run(8, 2, "success", "Lint"), run(7, 1, "success"), run(6, 2, "failure", "Lint")]
    seen = []

    def github(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        routes = {"/user": {"login": "octo"}, "/repos/octo/shop": {"full_name": "octo/shop"},
                  "/repos/octo/shop/branches/main": {"name": "main"}, "/repos/octo/shop/actions/runs": {"workflow_runs": runs}}
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    monkeypatch.setenv("OPSRELAY_CRON_SECRET", "s3cret")
    app = create_app(make_sqlite(tmp_path), settings, clock=clock, github_transport=httpx.MockTransport(github))
    owner = signed_in(app)
    owner.put("/api/v1/github", json={"token": "ghp_valid_token_for_tests_000"}).raise_for_status()
    owner.post("/api/v1/repositories", json={"repo": "octo/shop", "service_name": "Shop", "base_branch": "main",
                                             "deployed_url": "https://shop.example"}).raise_for_status()

    def scheduled():
        return TestClient(app).post("/api/v1/cron/check", headers={"Authorization": "Bearer s3cret"}).json()["data"]

    assert scheduled()["actions"] == {"created": 1}
    assert {"branch": "main", "status": "completed"}.items() <= seen[-1].items()
    incident = next(i for i in owner.get("/api/v1/incidents").json()["data"] if i["evidence"].get("source") == "actions")
    assert incident["title"] == "Shop: 1 failing workflow" and [r["id"] for r in incident["evidence"]["runs"]] == [9]

    runs.insert(0, run(10, 1, "success"))
    assert scheduled()["actions"] == {"resolved": 1}
    assert owner.get(f"/api/v1/incidents/{incident['id']}").json()["data"]["state"] == "resolved"
