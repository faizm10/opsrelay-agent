"""Persistence, dedup, leases, retention and indexes for both storage backends."""

import asyncio
import json
from datetime import timedelta

import mongomock
import pytest
from cryptography.fernet import Fernet

from uptime.storage import mongo
from uptime.storage.mongo import MongoStorage
from uptime.storage.sqlite import SqliteStorage

from .conftest import NOVAMART, T0


def test_sqlite_history_survives_reopen(tmp_path, monitor_factory, clock, apps):
    path = tmp_path / "uptime.db"
    first = SqliteStorage(path)
    first.init()
    apps.mode["novamart"] = "functional_bug"
    asyncio.run(monitor_factory(first).run_cycle())
    first.close()

    reopened = SqliteStorage(path)  # simulated process restart
    reopened.init()
    assert reopened.count_checks() == 2
    assert reopened.get_open_incident("novamart") is not None
    assert reopened.get_state("novamart").status == "degraded"
    assert reopened.describe()["persistent"] is True and reopened.describe()["atlas"] is False


def test_mongo_history_survives_new_storage_object():
    client = mongomock.MongoClient(tz_aware=True)
    first = MongoStorage(client["opsrelay"], ttl_indexes=False)
    first.init()
    first.upsert_service(NOVAMART)
    second = MongoStorage(client["opsrelay"], ttl_indexes=False)  # new process, same database
    assert second.get_service("novamart").name == "NovaMart"


@pytest.mark.parametrize(("vercel_env", "database", "allowed"), [
    ("preview", "opsrelay", False),
    ("preview", "opsrelay_preview", True),
    ("production", "opsrelay_preview", False),
    ("production", "opsrelay", True),
    ("", "opsrelay", False),
    ("", "opsrelay_dev", True),
])
def test_vercel_environments_use_separate_databases(monkeypatch, vercel_env, database, allowed):
    monkeypatch.setenv("VERCEL_ENV", vercel_env)
    monkeypatch.delenv("OPSRELAY_ENV", raising=False)
    monkeypatch.setenv("MONGODB_DB", database)
    monkeypatch.setattr(mongo, "MongoClient", lambda *args, **kwargs: mongomock.MongoClient())
    if not allowed:
        with pytest.raises(RuntimeError, match="preview|production"):
            mongo.connect("mongodb://localhost")
        return
    db, _ = mongo.connect("mongodb://localhost")
    assert db.name == database


def test_duplicate_check_ids_are_rejected(storage, monitor_factory):
    monitor = monitor_factory(storage)
    record = asyncio.run(monitor.check_service(__import__("httpx").AsyncClient(transport=monitor.transport), NOVAMART, T0))
    assert storage.insert_check(record) is True
    assert storage.insert_check(record) is False
    assert storage.count_checks() == 1


def test_samples_are_ordered_and_windowed(storage, monitor_factory, clock):
    monitor = monitor_factory(storage)
    for _ in range(5):
        asyncio.run(monitor.run_cycle())
        clock.advance(30)
    samples = storage.list_samples("novamart", T0 + timedelta(seconds=30), T0 + timedelta(seconds=90))
    assert [s.checked_at for s in samples] == [T0 + timedelta(seconds=30 * i) for i in (1, 2, 3)]
    assert all(s.checked_at.tzinfo is not None for s in samples)
    assert storage.first_check_at("novamart") == T0


def test_lease_semantics(storage):
    assert storage.acquire_lease("A", T0, 60)
    assert storage.acquire_lease("A", T0 + timedelta(seconds=30), 60), "owner can renew"
    assert not storage.acquire_lease("B", T0 + timedelta(seconds=60), 60), "held until expiry"
    assert storage.acquire_lease("B", T0 + timedelta(seconds=91), 60), "expired lease can be taken"
    storage.release_lease("A")  # A no longer owns it: no effect
    assert storage.get_worker().owner == "B"
    storage.release_lease("B")
    assert storage.get_worker().owner is None


def test_retention_purges_old_checks_and_resolved_incidents(storage, monitor_factory, clock, apps):
    monitor = monitor_factory(storage)
    apps.mode["novamart"] = "functional_bug"
    asyncio.run(monitor.run_cycle())
    clock.advance(30)
    apps.mode["novamart"] = "healthy"
    asyncio.run(monitor.run_cycle())
    removed = storage.purge(T0 + timedelta(seconds=10), T0 + timedelta(days=1))
    assert removed == {"checks": 2, "incidents": 1}
    assert storage.count_checks() == 2


def test_mongo_indexes_for_retention_and_dedup():
    storage = MongoStorage(mongomock.MongoClient(tz_aware=True)["opsrelay"], retention_days=35)
    storage.init()
    checks = storage.db["uptime_checks"].index_information()
    incidents = storage.db["uptime_incidents"].index_information()
    assert checks["ttl_checks"]["expireAfterSeconds"] == 35 * 86400
    assert incidents["one_open_incident"]["unique"] is True and incidents["one_open_incident"]["sparse"] is True
    assert "ttl_resolved_incidents" in incidents


def test_mongo_uses_dedicated_collections_and_reads_agent_incidents(monitor_factory, clock, apps):
    storage = MongoStorage(mongomock.MongoClient(tz_aware=True)["opsrelay"], ttl_indexes=False)
    storage.init()
    storage.db["incidents"].insert_one({  # written by the agents' orchestrator
        "_id": "faizm10/agentic-mongodb#123.1", "app": "shopping", "status": "awaiting_merge", "stage": "pr_confirmed",
        "html_url": "https://github.com/faizm10/agentic-mongodb/actions/runs/123", "commit_sha": "abc1234",
        "pr": {"number": 42, "url": "https://github.com/faizm10/agentic-mongodb/pull/42"}, "created_at": T0,
    })
    apps.mode["novamart"] = "functional_bug"
    asyncio.run(monitor_factory(storage).run_cycle())
    assert storage.db["incidents"].count_documents({}) == 1, "the agents' incidents collection is never written"
    assert storage.db["uptime_incidents"].count_documents({}) == 1
    related = storage.find_agent_incidents("shopping", T0, T0 + timedelta(minutes=5))
    assert related[0]["pr"]["url"].endswith("/pull/42")


def test_mongo_describe_never_leaks_credentials():
    storage = MongoStorage(mongomock.MongoClient()["opsrelay"],
                           uri="mongodb+srv://opsuser:S3cr3t%40pass@cluster0.abcd.mongodb.net/?retryWrites=true")
    info = storage.describe()
    assert info["atlas"] is True and info["label"] == "MongoDB Atlas"
    assert "S3cr3t" not in str(info) and "opsuser" not in str(info)


def test_connector_credentials_are_encrypted_at_rest(storage, monkeypatch):
    from uptime.sealing import PREFIX, reseal_all
    from uptime.storage.mongo import ACCOUNTS

    def raw(kind, doc_id):
        if isinstance(storage, SqliteStorage):
            return json.loads(storage._one("SELECT doc FROM account_docs WHERE kind = ? AND id = ?", (kind, doc_id))["doc"])
        return storage.db[ACCOUNTS + kind].find_one({"_id": doc_id})

    storage.put_doc("workspaces", "w1", {"id": "w1"})
    storage.put_doc("github", "w1", {"token": "ghp_secret", "login": "octo"})
    assert raw("github", "w1")["token"].startswith(PREFIX) and "ghp_secret" not in str(raw("github", "w1"))
    assert storage.get_doc("github", "w1") == {"token": "ghp_secret", "login": "octo"}

    # Saved before encryption: still readable, and seal-secrets encrypts it in place.
    if isinstance(storage, SqliteStorage):
        storage._exec("UPDATE account_docs SET doc = ? WHERE kind = 'github' AND id = 'w1'",
                      (json.dumps({"token": "ghp_old", "login": "octo"}),))
    else:
        storage.db[ACCOUNTS + "github"].replace_one({"_id": "w1"}, {"token": "ghp_old", "login": "octo"})
    assert storage.get_doc("github", "w1")["token"] == "ghp_old"
    assert reseal_all(storage) == 1
    assert raw("github", "w1")["token"].startswith(PREFIX) and storage.get_doc("github", "w1")["token"] == "ghp_old"

    # After OPSRELAY_SECRET_KEY is rotated, credentials sealed with the old key read as not set until saved again.
    storage.put_doc("notify", "w1:slack", {"workspace_id": "w1", "kind": "slack", "url": "https://hooks.slack.com/x"})
    monkeypatch.setenv("OPSRELAY_SECRET_KEY", Fernet.generate_key().decode())
    assert storage.get_doc("github", "w1") is None and storage.find_docs("notify", "workspace_id", "w1") == []
    storage.put_doc("github", "w1", {"token": "ghp_new", "login": "octo"})
    assert storage.get_doc("github", "w1")["token"] == "ghp_new"
