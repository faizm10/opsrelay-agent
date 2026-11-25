"""The walkthrough merges the monitor's and the agent's records into ordered steps."""

import asyncio
import json
from datetime import timedelta


from uptime.api import create_app
from uptime.worker import UptimeMonitor

from .conftest import SERVICES, make_mongo, signed_in


def test_walkthrough_merges_recorded_steps_without_private_fields(settings, clock, apps):
    storage = make_mongo()
    monitor = UptimeMonitor(storage, SERVICES, interval_seconds=30, clock=clock, sleep=clock.sleep,
                            transport=apps.transport(), worker_id="test-worker")
    storage.acquire_lease("test-worker", clock(), 70)
    apps.mode["novamart"] = "functional_bug"
    asyncio.run(monitor.run_cycle())
    uid = storage.list_incidents(state="open")[0].id
    aid = f"repo#{uid}"
    t = clock()
    storage.db["incidents"].insert_one({
        "_id": aid, "source": "live", "live": {"uptime_incident_id": uid}, "status": "awaiting_merge",
        "stage": "memory_saved", "pr": {"url": "https://github.com/x/y/pull/7", "number": 7},
        "patch": {"summary": "Return the saved order", "diff": "private diff",
                  "recommendation": {"action": "propose_code_fix", "cause": "checkout reports failure"}},
        "verification": {"tests_passed": True, "test_output": "100 passed"},
    })
    storage.db["workflow_steps"].insert_many([
        {"incident_id": aid, "stage": "detected", "outcome": "completed", "at": t, "evidence": {}},
        {"incident_id": aid, "stage": "memory_retrieved", "outcome": "completed", "at": t + timedelta(seconds=5),
         "evidence": {"matches": [{"memory_id": "m1", "score": 0.9, "summary": "secret past fix"}]}},
        {"incident_id": aid, "stage": "patch_verified", "outcome": "completed", "at": t + timedelta(seconds=65),
         "evidence": {"tests_passed": True, "changed_files": ["shopping/backend/app/checkout.py"],
                      "summary": "Return the saved order"}},
        {"incident_id": aid, "stage": "pr_confirmed", "outcome": "completed", "at": t + timedelta(seconds=70),
         "evidence": {"pr_number": 7, "url": "https://github.com/x/y/pull/7"}},
    ])
    clock.advance(120)

    body = signed_in(create_app(storage, settings, clock=clock)).get(f"/api/v1/incidents/{uid}/walkthrough").json()
    steps = {s["key"]: s for s in body["data"]["steps"]}

    assert [s["key"] for s in body["data"]["steps"]] == [
        "detected", "similar", "investigated", "pr", "merge", "fresh", "resolved"]
    assert [steps[k]["state"] for k in ("similar", "investigated", "pr", "merge", "fresh")] == [
        "done", "done", "done", "running", "waiting"]
    assert steps["investigated"]["duration_seconds"] == 60
    assert steps["investigated"]["details"]["recommendation"]["action"] == "propose_code_fix"
    assert steps["similar"]["details"]["matches"] == [{"memory_id": "m1", "score": 0.9}]
    assert body["data"]["health"]["state"] == "attention" and "merge" in body["data"]["health"]["reason"]
    text = json.dumps(body)
    assert "secret past fix" not in text and "private diff" not in text
