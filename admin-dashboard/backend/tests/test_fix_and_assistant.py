"""After the agent's PR merges, a person confirms the fix; the assistant answers from a safe snapshot."""

import asyncio
import json

import httpx
import pytest

from uptime.api import create_app
from uptime.worker import UptimeMonitor

from .conftest import SERVICES, make_mongo, signed_in


@pytest.fixture
def env(tmp_path, settings, clock, apps, monkeypatch):
    storage = make_mongo()
    monitor = UptimeMonitor(storage, SERVICES, interval_seconds=30, clock=clock, sleep=clock.sleep,
                            transport=apps.transport(), worker_id="test-worker")
    storage.acquire_lease("test-worker", clock(), 70)
    apps.mode["novamart"] = "functional_bug"
    asyncio.run(monitor.run_cycle())
    requests: list[httpx.Request] = []

    def openai(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "NovaMart is overcharging. "}}]})

    client = signed_in(create_app(storage, settings, clock=clock, assistant_transport=httpx.MockTransport(openai)))
    incident_id = storage.list_incidents(state="open")[0].id
    storage.db["incidents"].insert_one({
        "_id": f"repo#{incident_id}", "source": "live", "live": {"uptime_incident_id": incident_id},
        "status": "awaiting_merge", "stage": "memory_saved", "created_at": clock(), "updated_at": clock(),
        "pr": {"url": "https://github.com/example/repo/pull/7", "number": 7},
        "patch": {"root_cause": "Discounts were 10x too large.", "diff": "private diff"},
    })
    return {"client": client, "storage": storage, "incident_id": incident_id, "requests": requests}


def activity(client):
    return client.get("/api/v1/summary").json()["data"]["agent_activity"][0]


def test_fix_can_be_confirmed_only_after_the_pr_merges(env):
    c, iid, db = env["client"], env["incident_id"], env["storage"].db
    url = f"/api/v1/incidents/{iid}/fix-confirmation"
    assert c.post(url, json={"resolved": True}).status_code == 409  # PR still awaiting review

    db["incidents"].update_one({"_id": f"repo#{iid}"}, {"$set": {"status": "resolved", "review": {"pr_state": "merged"}}})
    first = activity(c)
    assert first["state"] == "finished" and first["fix_confirmation"] is None

    response = c.post(url, json={"resolved": False})
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "open"
    assert activity(c)["fix_confirmation"] == "disputed"

    assert c.post(url, json={"resolved": True}).status_code == 200
    assert activity(c)["fix_confirmation"] == "confirmed"
    assert [e["type"] for e in c.get(f"/api/v1/incidents/{iid}").json()["data"]["events"]][-2:] == [
        "fix_disputed", "fix_confirmed"]


def test_assistant_answers_from_status_without_source_locations(env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPSRELAY_ASSISTANT_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    c = env["client"]
    response = c.post("/api/v1/assistant/chat", json={
        "incident_id": env["incident_id"], "messages": [{"role": "user", "content": "What is going on?"}]})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["reply"] == "NovaMart is overcharging."

    sent = json.loads(env["requests"][0].content)
    assert sent["model"] == "gpt-test"
    assert env["requests"][0].headers["authorization"] == "Bearer sk-test"
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    system = sent["messages"][0]["content"]
    assert "Discounts were 10x too large." in system and env["incident_id"] in system
    assert "pull/7" in system
    assert "promotion_discounts" in system  # the failed check is described, without its source location
    assert "backend/app" not in system and "private diff" not in system


def test_workspace_openai_key_is_sealed_and_used_instead_of_the_shared_one(env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")
    c, own = env["client"], "sk-own-workspace-key-0001"
    assert c.put("/api/v1/openai", json={"key": own}).json()["data"] == {
        "configured": True, "hint": "0001", "saved_at": c.get("/api/v1/openai").json()["data"]["saved_at"]}
    assert own not in str(list(env["storage"].db["opsrelay_openai"].find()))  # sealed at rest
    assert c.post("/api/v1/assistant/chat", json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 200
    assert [r.headers["authorization"] for r in env["requests"]] == [f"Bearer {own}"] * 2  # key check, then chat

    assert c.delete("/api/v1/openai").json()["data"] == {"configured": False}
    c.post("/api/v1/assistant/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert env["requests"][-1].headers["authorization"] == "Bearer sk-shared"


def test_assistant_needs_an_api_key_and_valid_messages(env, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    c = env["client"]
    no_key = c.post("/api/v1/assistant/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert no_key.status_code == 503 and "OPENAI_API_KEY" in no_key.json()["error"]["message"]
    assert c.post("/api/v1/assistant/chat", json={"messages": [{"role": "system", "content": "x"}]}).status_code == 422
    assert c.post("/api/v1/assistant/chat", json={"messages": []}).status_code == 422
    assert not env["requests"]
