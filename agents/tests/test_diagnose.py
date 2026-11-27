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

    # A workspace's own OpenAI key is used instead of the shared model and is not held to the shared limit.
    monkeypatch.setenv("OPSRELAY_DIAGNOSES_PER_DAY", "1")
    db[diagnose.OPENAI].insert_one({"_id": "w1", "key": diagnose.SEALED + Fernet(key).encrypt(b"sk-own").decode()})
    db[diagnose.UPTIME_INCIDENTS].insert_one({"_id": "INC-4", "service_id": "w1:shop", "state": "open", "title": "Shop slow", "summary": "p95 9s"})
    own, keys = FakeLLM(), []
    assert diagnose.run(db, llm, str, httpx.MockTransport(github), own_llm=lambda k: keys.append(k) or own) == 1
    assert keys == ["sk-own"] and len(own.prompts) == 1 and len(llm.prompts) == 3


def test_backlog_gets_one_verdict_per_item_from_facts_checks_and_errors(monkeypatch):
    monkeypatch.setenv("OPSRELAY_DIAGNOSES_PER_DAY", "10")
    db = mongomock.MongoClient(tz_aware=True)["opsrelay_test"]
    db[diagnose.REPOS].insert_one({"_id": "w1:shop", "workspace_id": "w1", "repo": "octo/shop", "base_branch": "main",
                                   "agent": {"enabled": True, "mode": "diagnose"}})
    db[diagnose.GITHUB].insert_one({"_id": "w1", "token": "ghp_plain"})
    items = [{"number": 7, "kind": "pull_request"}, {"number": 8, "kind": "pull_request"}, {"number": 9, "kind": "issue"}]
    db[diagnose.UPTIME_INCIDENTS].insert_one({"_id": "BAC-1", "service_id": "w1:shop#backlog", "state": "open", "summary": "3 open",
                                              "evidence": {"source": "backlog", "items": items}})
    pr = {"user": {"login": "dependabot[bot]"}, "created_at": "2026-11-16T00:00:00Z", "mergeable_state": "clean"}

    def github(request: httpx.Request) -> httpx.Response:
        routes = {
            "/repos/octo/shop/git/trees/main": {"tree": [{"path": "web/package.json"}, {"path": "web/package-lock.json"}]},
            "/repos/octo/shop/commits/main/check-runs": {"check_runs": []},
            "/repos/octo/shop/commits/main/status": {"statuses": []},
            "/repos/octo/shop/pulls/7": {**pr, "title": "Bump next from 15.5.0 to 16.1.0 in /web", "body": "Breaking: drops Node 18",
                                         "head": {"sha": "a7"}},
            "/repos/octo/shop/pulls/7/files": [{"filename": "web/package.json", "status": "modified", "patch": '+"next": "16"'},
                                               {"filename": "web/package-lock.json", "status": "modified", "patch": "lock noise"}],
            "/repos/octo/shop/commits/a7/check-runs": {"check_runs": [
                {"id": 42, "name": "build", "conclusion": "failure", "app": {"slug": "github-actions"}},
                {"id": 43, "name": "build", "conclusion": "failure", "app": {"slug": "github-actions"}}]},
            "/repos/octo/shop/commits/a7/status": {"statuses": [{"context": "Vercel", "state": "error"}]},
            "/repos/octo/shop/pulls/8": {**pr, "mergeable_state": "dirty", "title": "Bump flask from 3.0.0 to 3.1.0 in /api",
                                         "head": {"sha": "a8"}},
            "/repos/octo/shop/pulls/8/files": [{"filename": "api/requirements.txt", "status": "modified"}],
            "/repos/octo/shop/commits/a8/check-runs": {"check_runs": []},
            "/repos/octo/shop/commits/a8/status": {"statuses": []},
            "/repos/octo/shop/issues/9": {"title": "Checkout is slow", "created_at": "2026-11-20T00:00:00Z", "body": "p95 is 9s"},
            "/repos/octo/shop/issues/9/timeline": [{"event": "cross-referenced", "source": {"issue": {
                "number": 11, "title": "Cache prices", "state": "closed", "pull_request": {"merged_at": "2026-11-21"}}}}],
            "/repos/octo/shop/commits": [],
        }
        if request.url.path == "/repos/octo/shop/actions/jobs/42/logs":
            return httpx.Response(200, text="2026-11-16T22:21:15.4Z npm run build\n2026-11-16T22:21:15.4Z ##[error]TS2882 './globals.css'\n"
                                            "2026-11-16T22:21:15.5Z Post job cleanup.")
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    class Triage(FakeLLM):
        def generate(self, schema, system, user):
            self.prompts.append(user)
            return schema(summary="1 merge", likely_cause="x", suggested_fix="y", confidence="high", items=[
                {"number": 7, "verdict": "change", "reason": "build fails"}, {"number": 99, "verdict": "close", "reason": "made up"}])

    llm = Triage()
    assert diagnose.run(db, llm, str, httpx.MockTransport(github)) == 1
    text = llm.prompts[0]
    assert "PR #7 facts: ready to merge; open" in text and "major upgrade" in text and "lock noise" not in text
    assert "checks: build=failure, Vercel=failure" in text  # one entry per check, statuses included
    assert "##[error]TS2882" in text and "Post job cleanup" not in text and "22:21:15" not in text
    assert 'PR #8 facts: merge conflict with the base branch' in text and 'no longer exist on the base branch, e.g. "api/requirements.txt"' in text
    assert "mentioned by #11 Cache prices (merged)" in text
    assert [v["number"] for v in db[diagnose.DIAGNOSES].find_one({"_id": "BAC-1"})["items"]] == [7]  # #99 wasn't shown
