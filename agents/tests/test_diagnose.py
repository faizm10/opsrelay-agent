"""Diagnose mode reads a workspace's repository with its own (encrypted) token and respects the daily cap."""
import httpx
import mongomock
from cryptography.fernet import Fernet

from agents import diagnose


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def generate(self, schema, system, user):
        self.prompts.append(user)
        return schema(summary="CI fails", likely_cause="test_cart asserts 2 == 3", suggested_fix="Fix cart.py", confidence="high")


def test_open_incidents_are_diagnosed_once_within_the_workspace_limit(monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv("OPSRELAY_SECRET_KEY", key.decode())
    monkeypatch.setenv("OPSRELAY_DIAGNOSES_PER_DAY", "1")
    db = mongomock.MongoClient(tz_aware=True)["opsrelay_test"]
    db[diagnose.REPOS].insert_one({"_id": "w1:shop", "workspace_id": "w1", "repo": "octo/shop", "base_branch": "main",
                                   "agent": {"enabled": True, "mode": "diagnose"}})
    sealed = diagnose.SEALED + Fernet(key).encrypt(b"ghp_workspace_token").decode()
    db[diagnose.GITHUB].insert_one({"_id": "w1", "token": sealed})
    db[diagnose.UPTIME_INCIDENTS].insert_many([
        {"_id": "ACT-1", "service_id": "w1:shop#actions", "state": "open", "title": "Shop: 1 failing workflow",
         "summary": "CI failed at abc1234", "evidence": {"source": "actions", "runs": [{"id": 9, "sha": "abc1234"}]}},
        {"_id": "INC-2", "service_id": "w1:shop", "state": "open", "title": "Shop down", "summary": "HTTP 502"},
        {"_id": "INC-3", "service_id": "w1:shopping", "state": "open", "title": "another service", "summary": "x"},
    ])
    auth = set()

    def github(request: httpx.Request) -> httpx.Response:
        auth.add(request.headers["authorization"])
        routes = {
            "/repos/octo/shop/actions/runs/9/jobs": {"jobs": [{"id": 5, "name": "test", "conclusion": "failure",
                                                               "steps": [{"name": "pytest", "conclusion": "failure"}]}]},
            "/repos/octo/shop/commits/abc1234": {"commit": {"message": "Change cart"}, "files": [{"filename": "cart.py", "patch": "-2\n+3"}]},
            "/repos/octo/shop/commits": [{"sha": "abc1234def", "commit": {"message": "Change cart", "author": {"date": "2026-11-18"}}}],
        }
        if request.url.path == "/repos/octo/shop/actions/jobs/5/logs":
            return httpx.Response(200, text="FAILED test_cart - assert 2 == 3")
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    llm = FakeLLM()
    assert diagnose.run(db, llm, str, httpx.MockTransport(github)) == 1  # the limit stops the second incident
    assert auth == {"Bearer ghp_workspace_token"}
    assert "assert 2 == 3" in llm.prompts[0] and "<job_log>" in llm.prompts[0]
    saved = db[diagnose.DIAGNOSES].find_one({"_id": "ACT-1"})
    assert saved["workspace_id"] == "w1" and saved["likely_cause"] == "test_cart asserts 2 == 3"

    monkeypatch.setenv("OPSRELAY_DIAGNOSES_PER_DAY", "10")
    assert diagnose.run(db, llm, str, httpx.MockTransport(github)) == 1  # INC-2 now; ACT-1 is unchanged
    assert db[diagnose.DIAGNOSES].count_documents({}) == 2 and db[diagnose.DIAGNOSES].find_one({"_id": "INC-3"}) is None

    # "Run the agent now" diagnoses an unchanged incident once more, then clears the request.
    db[diagnose.REQUESTS].insert_one({"_id": "ACT-1", "workspace_id": "w1"})
    assert diagnose.run(db, llm, str, httpx.MockTransport(github)) == 1
    assert db[diagnose.REQUESTS].count_documents({}) == 0 and diagnose.run(db, llm, str, httpx.MockTransport(github)) == 0
