from datetime import timedelta

import mongomock
import pytest

from agents import memory
from agents import mongodb as m
from agents import orchestrator as o
from agents.fakes import SAMPLE_RUN, FakeGitHub, FakeRepairer


class Crash(BaseException):
    """Simulates the process dying (not caught by the orchestrator's `except Exception`)."""


@pytest.fixture
def db():
    db = mongomock.MongoClient(tz_aware=True)["opsrelay_test"]
    m.ensure_setup(db, vector_index=False)
    return db


def seed(db):
    memory.seed_memory(
        "shopping",
        "FAILED test_cart.py::test_discount_applied AssertionError assert 100.0 == 90.0 Cart.total",
        "Cart.total ignored discount; multiply by (1 - discount)", db=db)
    memory.seed_memory(
        "shopping", "FAILED test_inventory.py::test_stock KeyError sku",
        "Inventory lookup used wrong key", db=db)
    memory.seed_memory(
        "messaging", "FAILED test_send.py AssertionError assert 100.0 == 90.0 Cart.total",
        "messaging memory must never leak into shopping", db=db)


def test_dedup(db):
    iid, created = o.ingest_failed_run(SAMPLE_RUN, db=db)
    iid2, created2 = o.ingest_failed_run(SAMPLE_RUN, db=db)
    assert created and not created2 and iid == iid2
    assert m.incidents(db).count_documents({}) == 1


def test_ci_run_covered_by_live_incident_at_same_commit(db):
    assert o.live_incident_covering(SAMPLE_RUN, db=db) is None
    live = {"uptime_incident_id": "UPT-novamart-1", "app": SAMPLE_RUN["app"]}
    iid, _ = o.ingest_live_incident(SAMPLE_RUN["repo"], live, SAMPLE_RUN["commit_sha"], db=db)
    assert o.live_incident_covering(SAMPLE_RUN, db=db) == iid
    assert o.live_incident_covering({**SAMPLE_RUN, "commit_sha": "f" * 40}, db=db) is None
    # A live incident handed to a human no longer covers the run, so CI can try its own repair.
    m.incidents(db).update_one({"_id": iid}, {"$set": {"status": m.STATUS_NEEDS_REVIEW}})
    assert o.live_incident_covering(SAMPLE_RUN, db=db) is None
    # A re-run of the same workflow is a different attempt -> new incident
    _, created3 = o.ingest_failed_run({**SAMPLE_RUN, "run_attempt": 2}, db=db)
    assert created3


def test_similar_scoped_to_app(db):
    seed(db)
    sims = memory.find_similar(SAMPLE_RUN, k=3, db=db)
    assert sims and all(s["app"] == "shopping" for s in sims)
    assert "discount" in sims[0]["summary"]


def test_forget_memory(db):
    seed(db)
    top = memory.find_similar(SAMPLE_RUN, db=db)[0]
    assert memory.forget(top["memory_id"], "wrong fix", db=db)
    assert all(s["memory_id"] != top["memory_id"] for s in memory.find_similar(SAMPLE_RUN, db=db))


def test_happy_path(db):
    seed(db)
    gh, rep = FakeGitHub(), FakeRepairer()
    iid, _ = o.ingest_failed_run(SAMPLE_RUN, db=db)
    inc = o.Orchestrator(gh, rep, db=db, worker_id="w1").run_incident(iid)
    assert inc["status"] == m.STATUS_AWAITING_MERGE
    assert inc["stage"] == "memory_saved"
    assert inc["pr"]["number"] and gh.create_calls == 1
    assert rep.seen_similar and "discount" in rep.seen_similar[0]["summary"]
    stages = [s["stage"] for s in m.incident_timeline(iid, db=db) if s["outcome"] == "completed"]
    assert stages == m.STAGES
    # The fix is now a memory that future incidents will find
    assert m.memories(db).count_documents({"source_incident_id": iid}) == 1


def test_rejected_pr_marks_incident_rejected_and_forgets_the_fix(db):
    gh = FakeGitHub()
    iid, _ = o.ingest_failed_run(SAMPLE_RUN, db=db)
    orch = o.Orchestrator(gh, FakeRepairer(), db=db, worker_id="w1")
    inc = orch.run_incident(iid)

    gh.set_state(SAMPLE_RUN["repo"], inc["pr_intent"]["branch"], "closed")

    assert orch.check_reviews() == [(iid, m.STATUS_REJECTED)]
    assert m.memories(db).find_one({"source_incident_id": iid})["active"] is False
    assert orch.check_reviews() == []  # recorded once


def test_dependabot_failure_gets_a_fix_pr_on_its_branch_and_resolves_on_merge(db):
    class WebRepairer(FakeRepairer):
        def repair(self, failed_run, similar):
            return {**super().repair(failed_run, similar), "files": {"shopping/frontend/tsconfig.json": "{}\n"},
                    "changed_files": ["shopping/frontend/tsconfig.json"]}

    branch = "dependabot/npm_and_yarn/shopping/frontend/typescript-7.0.2"
    gh = FakeGitHub()
    iid, _ = o.ingest_failed_run({**SAMPLE_RUN, "app": "shopping-web", "head_branch": branch}, db=db)
    orch = o.Orchestrator(gh, WebRepairer(), db=db, worker_id="w1", deployments=object())
    inc = orch.run_incident(iid)
    assert inc["status"] == m.STATUS_AWAITING_MERGE
    assert gh.find_pr(SAMPLE_RUN["repo"], inc["pr_intent"]["branch"])["base"] == branch
    # Merged into the Dependabot branch: nothing deploys on its own, so the incident is resolved.
    gh.set_state(SAMPLE_RUN["repo"], inc["pr_intent"]["branch"], "merged", merge_sha="f" * 40)
    assert orch.check_reviews() == [(iid, m.STATUS_RESOLVED)]


def test_failed_verification_stops_before_pr(db):
    gh = FakeGitHub()
    iid, _ = o.ingest_failed_run(SAMPLE_RUN, db=db)
    inc = o.Orchestrator(gh, FakeRepairer(passes=False), db=db).run_incident(iid)
    assert inc["status"] == m.STATUS_NEEDS_REVIEW
    assert gh.create_calls == 0 and "pr" not in inc


def test_report_only_records_the_verified_patch_without_touching_github(db):
    gh = FakeGitHub()
    iid, _ = o.ingest_failed_run(SAMPLE_RUN, db=db)
    inc = o.Orchestrator(gh, FakeRepairer(), db=db, publish_repos=set()).run_incident(iid)
    assert inc["status"] == m.STATUS_REPORTED and inc["stage"] == "patch_verified"
    assert inc["recommendation"]["kind"] == "verified_patch" and inc["patch"]["diff"]
    assert gh.create_calls == 0 and "pr_intent" not in inc


def test_patch_outside_the_repair_scope_is_not_published(db):
    class OutOfScope(FakeRepairer):
        def repair(self, failed_run, similar):
            return {**super().repair(failed_run, similar), "files": {".github/workflows/ci.yml": "x"},
                    "changed_files": [".github/workflows/ci.yml"]}

    gh = FakeGitHub()
    iid, _ = o.ingest_failed_run(SAMPLE_RUN, db=db)
    inc = o.Orchestrator(gh, OutOfScope(), db=db).run_incident(iid)
    assert inc["status"] == m.STATUS_NEEDS_REVIEW and "CI configuration" in inc["verification"]["scope_violation"]
    assert inc["recommendation"]["kind"] == "needs_review" and gh.create_calls == 0


def test_crash_after_pr_created_resumes_without_duplicate(db):
    gh = FakeGitHub()

    class CrashingGitHub(FakeGitHub):
        def open_draft_pr(self, *a, **kw):
            gh.open_draft_pr(*a, **kw)  # GitHub really creates it…
            raise Crash()               # …then the worker dies before saving the number

        def find_pr(self, repo, branch):
            return gh.find_pr(repo, branch)

    iid, _ = o.ingest_failed_run(SAMPLE_RUN, db=db)
    with pytest.raises(Crash):
        o.Orchestrator(CrashingGitHub(), FakeRepairer(), db=db, worker_id="w1").run_incident(iid)

    inc = m.incidents(db).find_one({"_id": iid})
    assert inc["stage"] == "pr_intent_saved" and "pr" not in inc
    assert gh.create_calls == 1

    # New worker can't steal a live lease…
    w2 = o.Orchestrator(gh, FakeRepairer(), db=db, worker_id="w2")
    assert w2.run_incident(iid) is None
    # …but once it expires, it resumes from saved state and adopts the existing PR.
    m.incidents(db).update_one({"_id": iid}, {"$set": {
        "lease.expires_at": m.utcnow() - timedelta(seconds=1)}})
    assert w2.resumable_ids() == [iid]
    inc = w2.run_incident(iid)
    assert inc["status"] == m.STATUS_AWAITING_MERGE
    assert gh.create_calls == 1  # no duplicate PR
    recovered = [s for s in m.incident_timeline(iid, db=db) if s["outcome"] == "recovered"]
    assert recovered and recovered[0]["worker_id"] == "w2"
    confirm = [s for s in m.incident_timeline(iid, db=db)
               if s["stage"] == "pr_confirmed" and s["outcome"] == "completed"][0]
    assert confirm["evidence"]["recovered_existing"] is True


def test_stale_worker_is_fenced(db):
    iid, _ = o.ingest_failed_run(SAMPLE_RUN, db=db)
    w1 = o.Orchestrator(FakeGitHub(), FakeRepairer(), db=db, worker_id="w1")
    inc = w1.claim(iid)
    # w2 takes over after w1's lease "expires"
    m.incidents(db).update_one({"_id": iid}, {"$set": {"lease.owner": "w2"}})
    with pytest.raises(o.LeaseLost):
        w1._advance(inc, "memory_retrieved", {"similar": []})


def test_stop_requested(db):
    iid, _ = o.ingest_failed_run(SAMPLE_RUN, db=db)
    o.request_stop(iid, "looks wrong", by="reviewer", db=db)
    gh = FakeGitHub()
    inc = o.Orchestrator(gh, FakeRepairer(), db=db).run_incident(iid)
    assert inc["status"] == m.STATUS_NEEDS_REVIEW and gh.create_calls == 0


def test_transient_error_retries(db):
    class Flaky(FakeRepairer):
        calls = 0

        def repair(self, run, similar):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise TimeoutError("LLM timeout")
            return super().repair(run, similar)

    iid, _ = o.ingest_failed_run(SAMPLE_RUN, db=db)
    orch = o.Orchestrator(FakeGitHub(), Flaky(), db=db)
    inc = orch.run_incident(iid)
    assert inc["status"] == m.STATUS_OPEN and inc["attempts"] == 1
    assert inc["stage"] == "memory_retrieved"  # didn't redo or lose earlier work
    assert orch.run_incident(iid)["status"] == m.STATUS_AWAITING_MERGE


def test_monitor_adapter_maps_hamza_events(db):
    from types import SimpleNamespace as NS

    from agents.github_types import FailureEvent

    ev = FailureEvent(incident_id="77-1", application="message-platform", workflow_run_id=77,
                      run_attempt=1, commit_sha="deadbeefcafe", workflow_name="CI",
                      workflow_file="messaging-ci.yml", branch="main", workflow_url="u")

    class Mon:
        forgotten = []
        def detect_failures(self): return [ev]
        def forget(self, iid): self.forgotten.append(iid)

    client = NS(config=NS(owner="faizm10", repo="agentic-mongodb"),
                get_failure_context=lambda run_id, attempt: NS(logs="FAILED test_x", failed_jobs=["backend tests"], failed_job="backend tests"))
    [run] = o.MonitorAdapter(client, Mon()).poll_failed_runs()
    assert run["app"] == "messaging" and run["repo"] == "faizm10/agentic-mongodb"
    assert run["failed_logs"] == "FAILED test_x" and run["run_id"] == 77
    iid, created = o.ingest_failed_run(run, db=db)
    assert created and o.branch_for(m.incidents(db).find_one({"_id": iid})) == "agent/fix-77-1"


def test_github_adapter_uses_hamza_publisher_and_survives_crash(db):
    """Full run through GitHubAdapter: crash after publish, resume adopts the same PR."""
    from types import SimpleNamespace as NS

    from agents.github_publisher import validate_request
    from agents.github_types import STATUS_VERIFIED, GitHubOperationResult

    prs = {}

    class Client:  # Hamza's GitHubClient.find_pr(branch, state)
        def find_pr(self, branch, state="all"):
            return prs.get(branch)

    class Publisher:  # Hamza's PullRequestPublisher.create_or_find_pr(request)
        calls = []

        def create_or_find_pr(self, req):
            assert validate_request(req) is None, validate_request(req)
            Publisher.calls.append(req)
            branch = f"agent/fix-{req.incident_id}"
            prs.setdefault(branch, NS(number=42, url="https://github.com/x/y/pull/42", state="open", merge_sha=None))
            return GitHubOperationResult(status=STATUS_VERIFIED, branch=branch, pr_number=42,
                                         pr_url="https://github.com/x/y/pull/42")

    class CrashAfterPublish(o.GitHubAdapter):
        def open_draft_pr(self, *a, **kw):
            super().open_draft_pr(*a, **kw)
            raise Crash()

    run = {**SAMPLE_RUN, "commit_sha": "a" * 40}
    iid, _ = o.ingest_failed_run(run, db=db)
    with pytest.raises(Crash):
        o.Orchestrator(CrashAfterPublish(Client(), Publisher()), FakeRepairer(), db=db,
                       worker_id="w1").run_incident(iid)
    req = Publisher.calls[0]
    assert req.incident_id == "9000001-1" and req.base_sha == "a" * 40
    assert [f.path for f in req.files] == ["shopping/src/shopping/cart.py"]

    m.incidents(db).update_one({"_id": iid}, {"$set": {
        "lease.expires_at": m.utcnow() - timedelta(seconds=1)}})
    inc = o.Orchestrator(o.GitHubAdapter(Client(), Publisher()), FakeRepairer(), db=db,
                         worker_id="w2").run_incident(iid)
    assert inc["status"] == m.STATUS_AWAITING_MERGE and inc["pr"]["number"] == 42
    assert len(Publisher.calls) == 1  # resumed worker found the PR, didn't publish again


def test_github_adapter_failed_publish_raises(db):
    from agents.github_types import STATUS_FAILED, GitHubOperationResult

    pub = type("P", (), {"create_or_find_pr": lambda self, r: GitHubOperationResult(
        status=STATUS_FAILED, detail="stale base")})()
    adapter = o.GitHubAdapter(None, pub)
    with pytest.raises(o.PublishFailed):
        adapter.open_draft_pr("r", "agent/fix-1-1", "a" * 40, "t", "b",
                              {"files": {"shopping/backend/app/pricing.py": "x"}})


def test_reembed_moves_memories_to_the_current_model_from_their_stored_text(db, monkeypatch):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    coll = m.memories(db)
    coll.insert_many([
        {"_id": "old", "failure_text": "checkout timed out", "embedding": [0.0], "embedding_model": "voyage-3.5"},
        {"_id": "current", "failure_text": "x", "embedding": [9.0], "embedding_model": memory.embedding_model()},
    ])
    assert memory.reembed(coll, "failure_text") == 1
    old = coll.find_one({"_id": "old"})
    assert old["embedding_model"] == memory.embedding_model()
    assert old["embedding"] == memory.embed("checkout timed out")
    assert coll.find_one({"_id": "current"})["embedding"] == [9.0], "already on the current model: untouched"
    assert memory.reembed(coll, "failure_text") == 0
