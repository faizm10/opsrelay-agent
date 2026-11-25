"""MongoDB storage (Atlas in production; any mongod or mongomock in development and tests).

Uses the same connection convention as agents/mongodb.py (MONGODB_URI, MONGODB_DB, the same
credential escaping) and the same database, but **dedicated `uptime_*` collections**: the agents'
`incidents` collection has a CI-specific JSON schema (run_id, run_attempt, commit_sha) and a unique
run index, so uptime incidents must not be written there. That collection is only *read* here, to
link repair PRs and CI runs to uptime incidents.

agents.mongodb is not imported directly because importing the `agents` package loads the whole
AI stack (LangChain/OpenAI); the connection helper below mirrors it instead.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from ..models import CheckRecord, Incident, Sample, ServiceConfig, ServiceState, WorkerState
from ..sealing import seal, unseal
from .base import WORKER_ID

SERVICES = "uptime_services"
CHECKS = "uptime_checks"
STATUS = "uptime_status"
INCIDENTS = "uptime_incidents"
WORKER = "uptime_worker"
ACCOUNTS = "opsrelay_"  # + users | emails | sessions | workspaces
AGENT_INCIDENTS = "incidents"  # owned by agents/mongodb.py; read-only here
AGENT_STEPS = "workflow_steps"  # owned by agents/mongodb.py; read-only here
AGENT_MEMORIES = "memories"  # owned by agents/memory.py; read-only here
RL_EXPERIENCES = "rl_experiences"  # owned by agents/rl_policy.py; read-only here
RL_TRAINING_RUNS = "rl_training_runs"  # owned by agents/rl_policy.py; read-only here
MOCK_INCIDENTS = "mock_incidents"  # owned by agents/mock_incidents.py; read-only here


def escape_credentials(uri: str) -> str:
    """Same as agents.mongodb._escape_credentials: URL-encode user/password as pasted."""
    from urllib.parse import quote_plus, unquote_plus

    scheme, sep, rest = uri.partition("://")
    if not sep:
        return uri
    userinfo, at, hostpart = rest.rpartition("@")
    if not at:
        return uri
    user, colon, pwd = userinfo.partition(":")
    user = quote_plus(unquote_plus(user))
    pwd = quote_plus(unquote_plus(pwd)) if colon else ""
    return f"{scheme}://{user}{':' + pwd if colon else ''}@{hostpart}"


def is_atlas(uri: str) -> bool:
    return "mongodb.net" in uri or uri.startswith("mongodb+srv://")


def connect(uri: str | None = None, db_name: str | None = None) -> tuple[Database, str]:
    uri = uri or os.environ.get("MONGODB_URI", "")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set")
    if "<" in uri:
        raise RuntimeError("MONGODB_URI still contains <placeholders>")
    name = db_name or os.environ.get("MONGODB_DB", "opsrelay")
    vercel_env = os.environ.get("VERCEL_ENV")
    if vercel_env == "preview" and not name.endswith("_preview"):
        raise RuntimeError("Vercel previews require a MongoDB database ending in '_preview'")
    if vercel_env == "production" and name.endswith("_preview"):
        raise RuntimeError("Vercel production cannot use a preview MongoDB database")
    # Local runs load the repo-root .env, so without this a dev server writes to production.
    if name == "opsrelay" and "production" not in (vercel_env, os.environ.get("OPSRELAY_ENV")):
        raise RuntimeError("Only production may use the 'opsrelay' database; set MONGODB_DB=opsrelay_dev locally")
    client = MongoClient(escape_credentials(uri), appname="opsrelay-uptime", tz_aware=True,
                         serverSelectionTimeoutMS=8000)
    return client[name], uri


def _doc(model, **extra) -> dict[str, Any]:
    data = model.model_dump(mode="python")
    data.update(extra)
    return data


def _strip(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    doc.pop("open_key", None)
    doc.pop("expires_at", None)
    return doc


class MongoStorage:
    def __init__(self, db: Database, *, uri: str = "", retention_days: int = 35, incident_retention_days: int = 180,
                 ttl_indexes: bool = True) -> None:
        self.db = db
        self.ttl_indexes = ttl_indexes
        self.uri = uri
        self.retention_days = retention_days
        self.incident_retention_days = incident_retention_days

    def init(self) -> None:
        db = self.db
        db[CHECKS].create_index([("service_id", ASCENDING), ("checked_at", ASCENDING)], name="by_service_time")
        # Retention: Atlas deletes raw samples automatically after retention_days.
        if self.ttl_indexes:
            db[CHECKS].create_index("checked_at", expireAfterSeconds=self.retention_days * 86400, name="ttl_checks")
        # Dedup: `open_key` is set (= service_id) only while an incident is open, so a service can
        # never have two open incidents, even if two workers or a restarted worker race.
        db[INCIDENTS].create_index("open_key", unique=True, sparse=True, name="one_open_incident")
        db[INCIDENTS].create_index([("service_id", ASCENDING), ("started_at", DESCENDING)], name="by_service_start")
        if self.ttl_indexes:
            db[INCIDENTS].create_index("expires_at", expireAfterSeconds=0, name="ttl_resolved_incidents")

    def describe(self) -> dict[str, Any]:
        atlas = is_atlas(self.uri)
        host = self.uri.rpartition("@")[2].split("/")[0].split("?")[0] if self.uri else "in-process"
        return {
            "backend": "mongodb",
            "label": "MongoDB Atlas" if atlas else ("MongoDB (self-hosted)" if self.uri else "MongoDB (in-memory test double)"),
            "persistent": bool(self.uri),
            "atlas": atlas,
            "location": f"{host}/{self.db.name}",
        }

    def close(self) -> None:
        try:
            self.db.client.close()
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------------- services
    def upsert_service(self, service: ServiceConfig, *, keep_enabled: bool = True) -> ServiceConfig:
        existing = self.get_service(service.id)
        if existing is not None and keep_enabled:
            service = service.model_copy(update={"enabled": existing.enabled})
        self.db[SERVICES].replace_one({"_id": service.id}, _doc(service, _id=service.id), upsert=True)
        return service

    def list_services(self) -> list[ServiceConfig]:
        return [ServiceConfig.model_validate(_strip(d)) for d in self.db[SERVICES].find().sort("_id", ASCENDING)]

    def get_service(self, service_id: str) -> ServiceConfig | None:
        d = _strip(self.db[SERVICES].find_one({"_id": service_id}))
        return ServiceConfig.model_validate(d) if d else None

    def set_service_enabled(self, service_id: str, enabled: bool) -> ServiceConfig | None:
        d = self.db[SERVICES].find_one_and_update({"_id": service_id}, {"$set": {"enabled": enabled}},
                                                  return_document=ReturnDocument.AFTER)
        return ServiceConfig.model_validate(_strip(d)) if d else None

    def delete_service(self, service_id: str) -> None:
        self.db[SERVICES].delete_one({"_id": service_id})

    # ----------------------------------------------------------------- checks
    def insert_check(self, check: CheckRecord) -> bool:
        try:
            self.db[CHECKS].insert_one(_doc(check, _id=check.id))
            return True
        except DuplicateKeyError:
            return False

    def get_check(self, check_id: str) -> CheckRecord | None:
        d = _strip(self.db[CHECKS].find_one({"_id": check_id}))
        return CheckRecord.model_validate(d) if d else None

    def list_checks(self, service_id: str, *, limit: int = 50, before: datetime | None = None) -> list[CheckRecord]:
        query: dict[str, Any] = {"service_id": service_id}
        if before is not None:
            query["checked_at"] = {"$lt": before}
        cursor = self.db[CHECKS].find(query).sort("checked_at", DESCENDING).limit(limit)
        return [CheckRecord.model_validate(_strip(d)) for d in cursor]

    def list_samples(self, service_id: str, since: datetime, until: datetime) -> list[Sample]:
        cursor = self.db[CHECKS].find(
            {"service_id": service_id, "checked_at": {"$gte": since, "$lte": until}},
            {"_id": 0, "checked_at": 1, "status": 1, "latency_ms": 1, "functional_latency_ms": 1, "interval_seconds": 1},
        ).sort("checked_at", ASCENDING)
        return [Sample.model_validate(d) for d in cursor]

    def first_check_at(self, service_id: str) -> datetime | None:
        d = self.db[CHECKS].find_one({"service_id": service_id}, {"checked_at": 1}, sort=[("checked_at", ASCENDING)])
        if d is None:
            return None
        t = d["checked_at"]
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t

    def count_checks(self, service_id: str | None = None) -> int:
        return self.db[CHECKS].count_documents({"service_id": service_id} if service_id else {})

    # ------------------------------------------------------------------ state
    def get_state(self, service_id: str) -> ServiceState | None:
        d = _strip(self.db[STATUS].find_one({"_id": service_id}))
        return ServiceState.model_validate(d) if d else None

    def put_state(self, state: ServiceState) -> None:
        self.db[STATUS].replace_one({"_id": state.service_id}, _doc(state, _id=state.service_id), upsert=True)

    # -------------------------------------------------------------- incidents
    def get_open_incident(self, service_id: str) -> Incident | None:
        d = _strip(self.db[INCIDENTS].find_one({"service_id": service_id, "state": "open"}))
        return Incident.model_validate(d) if d else None

    def create_incident(self, incident: Incident) -> bool:
        try:
            self.db[INCIDENTS].insert_one(_doc(incident, _id=incident.id, open_key=incident.service_id))
            return True
        except DuplicateKeyError:
            return False

    def save_incident(self, incident: Incident) -> None:
        doc = _doc(incident, _id=incident.id)
        if incident.state == "open":
            doc["open_key"] = incident.service_id
        elif incident.resolved_at is not None:
            doc["expires_at"] = incident.resolved_at + timedelta(days=self.incident_retention_days)
        self.db[INCIDENTS].replace_one({"_id": incident.id}, doc, upsert=True)

    def get_incident(self, incident_id: str) -> Incident | None:
        d = _strip(self.db[INCIDENTS].find_one({"_id": incident_id}))
        return Incident.model_validate(d) if d else None

    def list_incidents(self, *, service_id: str | None = None, state: str | None = None, limit: int = 50) -> list[Incident]:
        query: dict[str, Any] = {}
        if service_id:
            query["service_id"] = service_id
        if state:
            query["state"] = state
        cursor = self.db[INCIDENTS].find(query).sort("started_at", DESCENDING).limit(limit)
        return [Incident.model_validate(_strip(d)) for d in cursor]

    # ----------------------------------------------------------------- worker
    def acquire_lease(self, owner: str, now: datetime, ttl_seconds: int) -> bool:
        expires = now + timedelta(seconds=ttl_seconds)
        try:
            doc = self.db[WORKER].find_one_and_update(
                {"_id": WORKER_ID, "$or": [{"owner": owner}, {"owner": None}, {"lease_expires_at": {"$lt": now}}]},
                {"$set": {"owner": owner, "lease_expires_at": expires}},
                upsert=True, return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:  # document exists and is held by someone else
            return False
        return doc is not None and doc.get("owner") == owner

    def release_lease(self, owner: str) -> None:
        self.db[WORKER].update_one({"_id": WORKER_ID, "owner": owner}, {"$set": {"owner": None, "lease_expires_at": None}})

    def get_worker(self) -> WorkerState | None:
        d = self.db[WORKER].find_one({"_id": WORKER_ID})
        if d is None:
            return None
        d = dict(d)
        d.pop("_id")
        return WorkerState.model_validate(d)

    def update_worker(self, owner: str, fields: dict[str, Any]) -> None:
        # Fenced on the owner: a worker that lost its lease cannot overwrite the new owner's state.
        self.db[WORKER].update_one({"_id": WORKER_ID, "owner": {"$in": [owner, None]}}, {"$set": fields})

    def request_check(self, now: datetime) -> None:
        self.db[WORKER].update_one({"_id": WORKER_ID}, {"$set": {"check_requested_at": now}}, upsert=True)

    # -------------------------------------------------------------- retention
    def purge(self, checks_before: datetime, incidents_before: datetime) -> dict[str, int]:
        # TTL indexes do this automatically on a real server; explicit purge keeps mongomock/tests honest.
        checks = self.db[CHECKS].delete_many({"checked_at": {"$lt": checks_before}}).deleted_count
        incidents = self.db[INCIDENTS].delete_many({"state": "resolved", "resolved_at": {"$lt": incidents_before}}).deleted_count
        return {"checks": checks, "incidents": incidents}

    def find_agent_incidents(self, app: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """CI incidents recorded by the agents' orchestrator around this window (read-only)."""
        cursor = self.db[AGENT_INCIDENTS].find(
            {"app": app, "created_at": {"$gte": start - timedelta(hours=1), "$lte": end + timedelta(hours=6)}},
            {"_id": 1, "app": 1, "status": 1, "stage": 1, "html_url": 1, "pr": 1, "commit_sha": 1, "created_at": 1},
        ).sort("created_at", DESCENDING).limit(5)
        return [dict(d) for d in cursor]

    def get_agent_activity(self, uptime_incident_id: str) -> dict[str, Any] | None:
        """Read only the agent run linked to this exact uptime incident.

        Never return audit evidence or errors: these may contain private logs and
        would also reveal which outage scenario the operator selected.
        """
        doc = self.db[AGENT_INCIDENTS].find_one(
            {"source": "live", "live.uptime_incident_id": uptime_incident_id},
            {"_id": 1, "status": 1, "stage": 1, "created_at": 1, "updated_at": 1,
             "lease.expires_at": 1, "pr.url": 1},
        )
        if doc is None:
            return None
        step = self.db[AGENT_STEPS].find_one(
            {"incident_id": doc["_id"]},
            {"_id": 0, "stage": 1, "outcome": 1, "at": 1},
            sort=[("at", DESCENDING)],
        )
        return {"status": doc.get("status"), "stage": doc.get("stage"),
                "created_at": doc.get("created_at"), "updated_at": doc.get("updated_at"),
                "lease_expires_at": (doc.get("lease") or {}).get("expires_at"),
                "pr_url": (doc.get("pr") or {}).get("url"), "latest_action": step}

    def get_learning_summary(self) -> dict[str, Any] | None:
        """What the agents' reinforcement-learning fix policy has learned (read-only).

        Only aggregate numbers and playbook names leave the database: never failure logs,
        state text, embeddings or diffs.
        """
        db = self.db
        if RL_EXPERIENCES not in db.list_collection_names():
            return None
        exp = db[RL_EXPERIENCES]
        strategies = [
            {"app": r["_id"]["app"], "action": r["_id"]["action"], "tries": r["tries"],
             "successes": r["successes"], "success_rate": round(r["success_rate"], 3),
             "real_tries": r["real_tries"]}
            for r in exp.aggregate([
                {"$group": {"_id": {"app": "$app", "action": "$action"}, "tries": {"$sum": 1},
                            "successes": {"$sum": "$reward"}, "success_rate": {"$avg": "$reward"},
                            "real_tries": {"$sum": {"$cond": [{"$eq": ["$source", "mock"]}, 0, 1]}}}},
                {"$sort": {"_id.app": 1, "success_rate": -1, "tries": -1}},
            ])
        ]
        run = db[RL_TRAINING_RUNS].find_one({}, {"_id": 0}, sort=[("at", DESCENDING)])
        decisions = [
            {"incident_id": str(d["_id"]), "app": d.get("app"), "source": d.get("source") or "ci",
             "status": d.get("status"), "created_at": d.get("created_at"),
             "action": (d.get("rl") or {}).get("action"),
             "expected_success": (d.get("rl") or {}).get("expected_success"),
             "hinted": bool((d.get("rl") or {}).get("hinted")),
             "pr_url": (d.get("pr") or {}).get("url")}
            for d in db[AGENT_INCIDENTS].find(
                {"rl.action": {"$exists": True}},
                {"_id": 1, "app": 1, "source": 1, "status": 1, "created_at": 1, "rl": 1, "pr.url": 1},
            ).sort("created_at", DESCENDING).limit(6)
        ]
        return {
            "totals": {
                "experiences": exp.count_documents({}),
                "real_experiences": exp.count_documents({"source": {"$ne": "mock"}}),
                "mock_incidents": db[MOCK_INCIDENTS].count_documents({}),
                "memories": db[AGENT_MEMORIES].count_documents({"active": {"$ne": False}}),
            },
            "strategies": strategies,
            "training": run,
            "decisions": decisions,
            "vector_indexes": self._vector_indexes(),
        }

    def _vector_indexes(self) -> list[dict[str, Any]]:
        out = []
        for coll in (AGENT_MEMORIES, RL_EXPERIENCES):
            try:
                for ix in self.db[coll].list_search_indexes():
                    out.append({"collection": coll, "name": ix.get("name"), "status": ix.get("status"),
                                "queryable": bool(ix.get("queryable"))})
            except Exception:  # noqa: BLE001 - not Atlas (local mongod / mongomock): nothing to show
                pass
        return out

    def get_agent_findings(self, uptime_incident_id: str) -> dict[str, Any] | None:
        """What the agent concluded about this uptime incident, for the dashboard assistant.

        Only its summary fields: never raw evidence, logs or diffs.
        """
        doc = self.db[AGENT_INCIDENTS].find_one(
            {"source": "live", "live.uptime_incident_id": uptime_incident_id},
            {"_id": 0, "status": 1, "stage": 1, "status_reason": 1, "patch.summary": 1, "patch.root_cause": 1,
             "pr.url": 1, "pr.number": 1, "review.pr_state": 1, "updated_at": 1},
        )
        if doc is None:
            return None
        patch, pr = doc.get("patch") or {}, doc.get("pr") or {}
        return {"status": doc.get("status"), "stage": doc.get("stage"), "status_reason": doc.get("status_reason"),
                "summary": patch.get("summary"), "root_cause": patch.get("root_cause"),
                "pr_url": pr.get("url"), "pr_number": pr.get("number"),
                "pr_state": (doc.get("review") or {}).get("pr_state"), "updated_at": doc.get("updated_at")}

    # Step evidence keys safe to show. Past-fix summaries and diffs stay out: they could describe the fix.
    _STEP_FIELDS = ("tests_passed", "changed_files", "summary", "branch", "pr_number", "url", "recovered_existing",
                    "memory_id", "pr_state", "status", "reason", "rl_action")

    def get_agent_run(self, uptime_incident_id: str) -> dict[str, Any] | None:
        doc = self.db[AGENT_INCIDENTS].find_one(
            {"source": "live", "live.uptime_incident_id": uptime_incident_id},
            {"_id": 1, "status": 1, "stage": 1, "status_reason": 1, "created_at": 1, "updated_at": 1,
             "lease.expires_at": 1, "patch.summary": 1, "patch.root_cause": 1, "patch.changed_files": 1,
             "patch.added_tests": 1, "patch.recommendation": 1, "verification": 1, "pr.url": 1,
             "pr.number": 1, "review.pr_state": 1},
        )
        if doc is None:
            return None
        steps = []
        for s in self.db[AGENT_STEPS].find({"incident_id": doc["_id"]}, {"_id": 0}).sort("at", ASCENDING):
            ev = s.get("evidence") or {}
            details = {k: ev[k] for k in self._STEP_FIELDS if k in ev}
            if "matches" in ev:
                details["matches"] = [{"memory_id": x.get("memory_id"), "score": x.get("score")} for x in ev["matches"]]
            steps.append({"stage": s.get("stage"), "outcome": s.get("outcome"), "worker_id": s.get("worker_id"),
                          "at": s.get("at"), "details": details})
        verification = doc.get("verification") or {}
        doc["verification"] = {"tests_passed": verification.get("tests_passed"), "verified_at": verification.get("verified_at"),
                               "test_output": (verification.get("test_output") or "")[-1500:]}
        doc["id"] = str(doc.pop("_id"))
        return {"incident": doc, "steps": steps}

    # --------------------------------------------------------------- accounts
    def insert_doc(self, kind: str, doc_id: str, doc: dict[str, Any]) -> bool:
        try:
            self.db[ACCOUNTS + kind].insert_one({**seal(kind, doc), "_id": doc_id})
        except DuplicateKeyError:
            return False
        return True

    def put_doc(self, kind: str, doc_id: str, doc: dict[str, Any]) -> None:
        self.db[ACCOUNTS + kind].replace_one({"_id": doc_id}, {**seal(kind, doc), "_id": doc_id}, upsert=True)

    def get_doc(self, kind: str, doc_id: str) -> dict[str, Any] | None:
        doc = self.db[ACCOUNTS + kind].find_one({"_id": doc_id})
        return unseal(kind, {k: v for k, v in doc.items() if k != "_id"}) if doc else None

    def find_docs(self, kind: str, field: str, value: str) -> list[dict[str, Any]]:
        return [unseal(kind, {k: v for k, v in d.items() if k != "_id"})
                for d in self.db[ACCOUNTS + kind].find({field: value}).sort("_id", ASCENDING)]

    def all_docs(self, kind: str) -> list[dict[str, Any]]:
        return [unseal(kind, {k: v for k, v in d.items() if k != "_id"}) for d in self.db[ACCOUNTS + kind].find().sort("_id", ASCENDING)]

    def delete_doc(self, kind: str, doc_id: str) -> None:
        self.db[ACCOUNTS + kind].delete_one({"_id": doc_id})
