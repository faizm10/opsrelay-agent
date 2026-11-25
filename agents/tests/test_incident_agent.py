"""Uptime incidents through intake and the orchestrator: what is investigated, and which incidents
go to a human without a model call or a code change. Mongomock, a fake GitHub and a stub model."""

from datetime import timedelta

import mongomock
import pytest

from agents import Repairer
from agents import mongodb as m
from agents import orchestrator as o
from agents.fakes import FakeGitHub
from agents.github_publisher import validate_request
from agents.github_types import PatchFile, PublishRequest
from agents.intake import UPTIME_INCIDENTS, LiveIntake, handoff_from_uptime

from .support import StubLLM

REPO = "faizm10/agentic-mongodb"


def uptime_doc(started_at, route="investigate", **overrides):
    """An uptime incident as Taha's monitor stores it (Incident.model_dump + _id)."""
    failures = [{"check": "promotion_discounts", "component": "pricing",
                 "reason": "Promotion discounts do not match the advertised percentage/amount.",
                 "source": "backend/app/pricing.py::compute_discount",
                 "expected": {"WELCOME10@20000": 2000}, "actual": {"WELCOME10@20000": 20000}}]
    functional = {"kind": "functional", "url": "http://localhost:8100/api/health/checkout", "ok": False,
                  "http_status": 503, "error_kind": "http_error", "error": "HTTP 503",
                  "details": {"failures": failures if route == "investigate" else []}}
    availability = ({"kind": "availability", "ok": True, "http_status": 200} if route == "investigate"
                    else {"kind": "availability", "ok": False, "error_kind": "connection_refused",
                          "error": "connection refused"})
    doc = {
        "_id": f"UPT-novamart-{started_at:%Y%m%d-%H%M%S}", "service_id": "novamart", "app": "shopping",
        "state": "open", "severity": "degraded" if route == "investigate" else "down",
        "failure_type": "functional_probe_failed" if route == "investigate" else "backend_unreachable",
        "investigation_route": route, "title": "NovaMart is degraded", "summary": "checkout probe failed",
        "started_at": started_at,
        "evidence": {"latest_failure": {"checked_at": started_at.isoformat(),
                                        "probes": {"availability": availability, "functional": functional}}},
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def db():
    db = mongomock.MongoClient(tz_aware=True)["opsrelay_agent_test"]
    m.ensure_setup(db, vector_index=False)
    return db


@pytest.fixture
def agent(db, tmp_path):
    github, llm = FakeGitHub(), StubLLM({})  # any model call fails the test
    orch = o.Orchestrator(github, Repairer(repo_path=str(tmp_path), llm=llm), db=db, worker_id="test-worker")
    intake = LiveIntake(REPO, db=db, resolve_sha=lambda: "a" * 40)
    return orch, intake, github, llm


def test_intake_records_each_uptime_incident_once(db):
    now = m.utcnow()
    db[UPTIME_INCIDENTS].insert_many([
        uptime_doc(now - timedelta(minutes=2)),
        uptime_doc(now - timedelta(days=3)),                       # too old
        uptime_doc(now - timedelta(minutes=1), state="resolved"),  # already over
    ])
    intake = LiveIntake(REPO, db=db, resolve_sha=lambda: "a" * 40, clock=lambda: now)

    created = intake.poll()

    assert created == [f"{REPO}#UPT-novamart-{now - timedelta(minutes=2):%Y%m%d-%H%M%S}"]
    assert intake.poll() == []
    inc = m.incidents(db).find_one({"_id": created[0]})
    assert (inc["source"], inc["app"], inc["status"], inc["stage"]) == ("live", "shopping", "open", "detected")
    assert inc["live"]["suspect_sources"] == ["backend/app/pricing.py::compute_discount"]
    assert inc["live"]["live_check"]["reachable"] and inc["live"]["live_check"]["healthy"]
    assert o.branch_for(inc).startswith("agent/fix-live-upt-novamart-")


def test_intake_skips_bug_already_reported_by_ci(db):
    o.ingest_failed_run({"repo": REPO, "app": "shopping", "run_id": 7, "run_attempt": 1, "commit_sha": "b" * 40,
                         "failed_logs": "FAILED tests/test_discounts.py::test_x"}, db=db)
    db[UPTIME_INCIDENTS].insert_one(uptime_doc(m.utcnow()))

    assert LiveIntake(REPO, db=db, resolve_sha=lambda: "b" * 40).poll() == []
    assert m.incidents(db).count_documents({"source": "live"}) == 0


def test_service_down_goes_to_a_human_without_model_or_code_change(db, agent):
    orch, intake, github, llm = agent
    db[UPTIME_INCIDENTS].insert_one(uptime_doc(m.utcnow(), route="service_health"))
    [iid] = intake.poll()

    orch.run_pending()

    inc = m.incidents(db).find_one({"_id": iid})
    assert inc["status"] == m.STATUS_NEEDS_REVIEW
    assert "service health problem" in inc["status_reason"]
    assert "Check whether the service process" in inc["patch"]["investigation"]
    assert llm.calls == [] and github.create_calls == 0


def test_third_party_outage_goes_to_a_human_without_model_or_code_change(db, agent):
    orch, intake, github, llm = agent
    doc = uptime_doc(m.utcnow())
    doc["evidence"]["latest_failure"]["probes"]["functional"]["details"]["failures"] = [
        {"check": "payment_provider", "component": "payments", "source": "external: Stripe API",
         "reason": "Stripe API is unavailable (HTTP 503 from api.stripe.com).",
         "expected": {"stripe_api": "available"}, "actual": {"stripe_api": "503 Service Unavailable"}}]
    db[UPTIME_INCIDENTS].insert_one(doc)
    [iid] = intake.poll()

    orch.run_pending()

    inc = m.incidents(db).find_one({"_id": iid})
    assert inc["status"] == m.STATUS_NEEDS_REVIEW
    assert "third-party dependency (Stripe API)" in inc["status_reason"]
    assert "status page" in inc["patch"]["investigation"]
    assert llm.calls == [] and github.create_calls == 0


def test_handoff_mapping_matches_taha_contract():
    doc = uptime_doc(m.utcnow())
    live = handoff_from_uptime(doc)
    assert live["schema"] == "opsrelay.uptime_incident.v1"
    assert live["live_check"]["failure_kind"] == "http_error" and live["live_check"]["http_status"] == 503
    assert live["probe_failures"][0]["source"] == "backend/app/pricing.py::compute_discount"


@pytest.mark.parametrize(("path", "ok"), [
    ("shopping/backend/tests/test_opsrelay_discount.py", True),
    ("message-platform/backend/tests/test_opsrelay_dupes.py", True),
    ("shopping/backend/app/new_module.py", False),
    ("shopping/backend/tests/test_discounts.py", False),
    (".github/workflows/tests/test_opsrelay_x.py", False),
])
def test_publisher_only_creates_agent_regression_tests(path, ok):
    req = PublishRequest(incident_id="live-upt-1", base_sha="a" * 40, root_cause="x", tests_passed=True,
                         files=[PatchFile(path=path, content="x = 1\n")], new_files=[path])
    assert (validate_request(req) is None) is ok
