"""MongoDB Atlas layer for OpsRelay.

Owns the connection, collections, schema validation and indexes.

Collections
-----------
incidents       One document per failed CI run (deduped on repo+run_id+run_attempt), or
                per live incident from the uptime monitor (source="live", deduped on _id).
                Holds the *current verified state* of the workflow, so a restarted
                worker resumes by reading one document - no history replay.
workflow_steps  Append-only audit log. Every stage transition is written here with
                its evidence (who did it, what was verified, when).
memories        Past incidents + fixes with embeddings, searched via Atlas Vector Search.

Run `python -m agents.mongodb` once to create everything (idempotent).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import CollectionInvalid, OperationFailure

from agents.github_types import APP_ROOTS

try:  # .env support is optional
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

INCIDENTS = "incidents"
WORKFLOW_STEPS = "workflow_steps"
MEMORIES = "memories"
LIVE_INCIDENTS = "live_incidents"
WORKER_HEARTBEATS = "worker_heartbeats"  # one document per running worker, expires when it stops
UPTIME_INCIDENTS = "uptime_incidents"  # owned by the uptime monitor (admin-dashboard); read only here

VECTOR_INDEX_NAME = "memory_vector_index"
EMBEDDING_DIMS = int(os.getenv("EMBEDDING_DIMS", "1024"))

# Ordered workflow stages. An incident's `stage` is the last stage that was
# *verified* complete. Resume always continues from the stage after it.
STAGES = [
    "detected",          # failed run recorded (Hamza -> Mo)
    "memory_retrieved",  # similar incidents fetched from Vector Search
    "patch_verified",    # Faiz produced a patch AND pytest passed
    "pr_intent_saved",   # branch name / PR title persisted BEFORE calling GitHub
    "pr_confirmed",      # PR exists on GitHub (verified by lookup, not just "sent")
    "memory_saved",      # fix stored as a memory for future incidents
]

# Incident status values
STATUS_OPEN = "open"                  # waiting for / being processed by a worker
STATUS_NEEDS_REVIEW = "needs_review"  # agent stopped (e.g. tests failed) - human needed
STATUS_AWAITING_MERGE = "awaiting_merge"  # draft PR opened and verified - done for the agent
STATUS_FAILED = "failed"              # unrecoverable error after max attempts
STATUS_RESOLVED = "resolved"          # a human approved and merged the PR
STATUS_REJECTED = "rejected"          # a human closed the PR without merging
STATUS_REPORTED = "reported"          # verified patch recorded; draft PRs not enabled for the repo
STATUS_AWAITING_DEPLOYMENT = "awaiting_deployment"  # merged; waiting for a deploy and a fresh probe

SOURCE_CI = "ci"      # failed GitHub Actions run (Hamza's monitor); the default when absent
SOURCE_LIVE = "live"  # failed live probe (Taha's uptime monitor)

_client: MongoClient | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _escape_credentials(uri: str) -> str:
    """URL-encode user/password so passwords with @ : / ? # % work as pasted."""
    from urllib.parse import quote_plus, unquote_plus

    scheme, sep, rest = uri.partition("://")
    if not sep:
        return uri
    # Host part never contains '@', so the LAST '@' separates credentials.
    userinfo, at, hostpart = rest.rpartition("@")
    if not at:
        return uri
    user, colon, pwd = userinfo.partition(":")
    user = quote_plus(unquote_plus(user))
    pwd = quote_plus(unquote_plus(pwd)) if colon else ""
    return f"{scheme}://{user}{':' + pwd if colon else ''}@{hostpart}"


def get_client(uri: str | None = None) -> MongoClient:
    global _client
    if _client is None:
        uri = uri or os.getenv("MONGODB_URI")
        if not uri:
            raise RuntimeError("MONGODB_URI is not set (see .env.example)")
        if "<" in uri:
            raise RuntimeError("MONGODB_URI in .env still has <placeholders>; paste the real "
                               "string from Atlas -> Connect -> Drivers")
        # python.org builds on macOS ship without root certificates; certifi's bundle verifies Atlas everywhere.
        import certifi
        _client = MongoClient(_escape_credentials(uri), appname="opsrelay", tz_aware=True,
                              tlsCAFile=os.environ.get("SSL_CERT_FILE") or certifi.where())
    return _client


def get_db(db: Database | None = None) -> Database:
    """Return the OpsRelay database. Pass `db` to inject one (tests use mongomock)."""
    if db is not None:
        return db
    return get_client()[os.getenv("MONGODB_DB", "opsrelay")]


def incidents(db: Database | None = None) -> Collection:
    return get_db(db)[INCIDENTS]


def workflow_steps(db: Database | None = None) -> Collection:
    return get_db(db)[WORKFLOW_STEPS]


def memories(db: Database | None = None) -> Collection:
    return get_db(db)[MEMORIES]


def live_incidents(db: Database | None = None) -> Collection:
    return get_db(db)[LIVE_INCIDENTS]


# --------------------------------------------------------------------------- #
# Schema validation                                                           #
# --------------------------------------------------------------------------- #
INCIDENT_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "repo", "app", "commit_sha", "status", "stage", "created_at", "updated_at"],
        # CI incidents are identified by their run; live incidents carry the uptime evidence.
        "anyOf": [
            {"required": ["run_id", "run_attempt"]},
            {"required": ["source", "live"], "properties": {"source": {"enum": [SOURCE_LIVE]}}},
        ],
        "properties": {
            "source": {"enum": [SOURCE_CI, SOURCE_LIVE]},
            "live": {"bsonType": "object", "required": ["uptime_incident_id"]},
            "_id": {"bsonType": "string"},
            "repo": {"bsonType": "string"},
            "app": {"enum": sorted(APP_ROOTS)},
            "run_id": {"bsonType": ["long", "int"]},
            "run_attempt": {"bsonType": ["long", "int"], "minimum": 1},
            "commit_sha": {"bsonType": "string", "minLength": 7},
            "status": {"enum": [STATUS_OPEN, STATUS_NEEDS_REVIEW, STATUS_AWAITING_MERGE,
                                STATUS_FAILED, STATUS_RESOLVED, STATUS_REJECTED,
                                STATUS_REPORTED, STATUS_AWAITING_DEPLOYMENT]},
            "stage": {"enum": STAGES},
            "pr_intent": {
                "bsonType": "object",
                "required": ["branch", "title", "saved_at"],
                "properties": {"branch": {"bsonType": "string", "pattern": "^agent/"}},
            },
            "pr": {
                "bsonType": "object",
                "required": ["number", "url"],
                "properties": {"number": {"bsonType": ["int", "long"]},
                               "url": {"bsonType": "string"}},
            },
        },
    }
}

STEP_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["incident_id", "stage", "outcome", "worker_id", "at"],
        "properties": {
            "incident_id": {"bsonType": "string"},
            "stage": {"bsonType": "string"},
            "outcome": {"enum": ["started", "completed", "failed", "recovered", "skipped"]},
            "worker_id": {"bsonType": "string"},
            "evidence": {"bsonType": "object"},
        },
    }
}

MEMORY_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["app", "summary", "embedding", "embedding_model", "created_at"],
        "properties": {
            "app": {"enum": sorted(APP_ROOTS)},
            "summary": {"bsonType": "string"},
            "embedding": {"bsonType": "array"},
            "embedding_model": {"bsonType": "string"},
            "active": {"bsonType": "bool"},
        },
    }
}


LIVE_INCIDENT_SCHEMA = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "service", "app", "status", "opened_at", "failure_count"],
        "properties": {
            "_id": {"bsonType": "string", "pattern": "^live:"},
            "service": {"bsonType": "string"},
            "app": {"bsonType": "string"},
            "status": {"enum": ["open", "investigating", "diagnosed", "resolved"]},
            "failure_count": {"bsonType": ["int", "long"], "minimum": 1},
        },
    }
}


def _ensure_collection(db: Database, name: str, schema: dict) -> None:
    try:
        db.create_collection(name, validator=schema, validationLevel="moderate")
    except CollectionInvalid:  # already exists -> update validator
        try:
            db.command("collMod", name, validator=schema, validationLevel="moderate")
        except (OperationFailure, NotImplementedError):
            pass
    except NotImplementedError:  # mongomock (tests) has no validators
        db[name].insert_one({"_id": "__init__"})
        db[name].delete_one({"_id": "__init__"})


def ensure_setup(db: Database | None = None, vector_index: bool = True) -> None:
    """Idempotently create collections, validators, indexes and the vector index."""
    db = get_db(db)
    _ensure_collection(db, INCIDENTS, INCIDENT_SCHEMA)
    _ensure_collection(db, WORKFLOW_STEPS, STEP_SCHEMA)
    _ensure_collection(db, MEMORIES, MEMORY_SCHEMA)

    inc = db[INCIDENTS]
    # Dedup guard: the same GitHub run attempt can only ever create one incident. Partial, so
    # live incidents (no run_id) don't all collide on a null key.
    _ensure_index(inc, [("repo", ASCENDING), ("run_id", ASCENDING), ("run_attempt", ASCENDING)],
                  name="uniq_run", unique=True, partialFilterExpression={"run_id": {"$exists": True}})
    inc.create_index([("status", ASCENDING), ("lease.expires_at", ASCENDING)], name="resumable")
    inc.create_index([("pr_intent.branch", ASCENDING)], sparse=True, name="pr_branch")

    db[WORKFLOW_STEPS].create_index([("incident_id", ASCENDING), ("at", ASCENDING)],
                                    name="by_incident")
    db[MEMORIES].create_index([("app", ASCENDING), ("created_at", DESCENDING)], name="by_app")
    # Memories with expires_at are removed automatically by Atlas.
    db[MEMORIES].create_index("expires_at", expireAfterSeconds=0, name="ttl")

    # Live-check incidents: at most one unresolved incident per service.
    _ensure_collection(db, LIVE_INCIDENTS, LIVE_INCIDENT_SCHEMA)
    live = db[LIVE_INCIDENTS]
    live.create_index("active_service", unique=True, sparse=True, name="one_open_per_service")
    live.create_index([("service", ASCENDING), ("opened_at", DESCENDING)], name="by_service")

    db[WORKER_HEARTBEATS].create_index("expires_at", expireAfterSeconds=0, name="ttl")

    if vector_index:
        ensure_vector_index(db)


def _ensure_index(coll: Collection, keys: list, name: str, **options) -> None:
    """create_index, replacing an existing index of the same name whose options changed."""
    try:
        coll.create_index(keys, name=name, **options)
    except OperationFailure as e:
        if e.code not in (85, 86):  # IndexOptionsConflict / IndexKeySpecsConflict
            raise
        coll.drop_index(name)
        coll.create_index(keys, name=name, **options)


def ensure_vector_index(db: Database | None = None) -> None:
    """Create the Atlas Vector Search index on memories.embedding (Atlas only)."""
    from pymongo.operations import SearchIndexModel

    coll = memories(db)
    try:
        existing = {ix["name"] for ix in coll.list_search_indexes()}
    except OperationFailure as e:  # not on Atlas / local mongod
        print(f"[mongodb] vector search unavailable: {e}")
        return
    if VECTOR_INDEX_NAME in existing:
        return
    coll.create_search_index(SearchIndexModel(
        name=VECTOR_INDEX_NAME,
        type="vectorSearch",
        definition={"fields": [
            {"type": "vector", "path": "embedding",
             "numDimensions": EMBEDDING_DIMS, "similarity": "cosine"},
            {"type": "filter", "path": "app"},
            {"type": "filter", "path": "embedding_model"},
            {"type": "filter", "path": "active"},
        ]},
    ))
    print(f"[mongodb] created vector index '{VECTOR_INDEX_NAME}' (takes ~1 min to build)")


# --------------------------------------------------------------------------- #
# Audit log                                                                   #
# --------------------------------------------------------------------------- #
def log_step(incident_id: str, stage: str, outcome: str, worker_id: str,
             evidence: dict[str, Any] | None = None, db: Database | None = None) -> None:
    workflow_steps(db).insert_one({
        "incident_id": incident_id,
        "stage": stage,
        "outcome": outcome,
        "worker_id": worker_id,
        "evidence": evidence or {},
        "at": utcnow(),
    })


def incident_timeline(incident_id: str, db: Database | None = None) -> list[dict]:
    """Everything that happened to an incident, in order (for reviewers / the demo)."""
    return list(workflow_steps(db).find({"incident_id": incident_id}, {"_id": 0})
                .sort("at", ASCENDING))


def watch_incidents(db: Database | None = None):
    """Yield live incident changes via a change stream (Atlas / replica set only)."""
    pipeline = [{"$match": {"operationType": {"$in": ["insert", "update"]}}}]
    with incidents(db).watch(pipeline, full_document="updateLookup") as stream:
        for change in stream:
            yield change


if __name__ == "__main__":
    ensure_setup()
    print(f"[mongodb] setup complete on db '{get_db().name}'")
    for name in (INCIDENTS, WORKFLOW_STEPS, MEMORIES, LIVE_INCIDENTS):
        print(f"  {name}: {get_db()[name].count_documents({})} docs")
