"""Local development storage (SQLite).

Persistent on this machine only. It is always labelled "local development storage" in
the API and dashboard so it is never presented as MongoDB Atlas history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import CheckRecord, Incident, Sample, ServiceConfig, ServiceState, WorkerState
from ..sealing import seal, unseal
from .base import WORKER_ID, iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (id TEXT PRIMARY KEY, doc TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checks (
    id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    latency_ms REAL,
    functional_latency_ms REAL,
    interval_seconds INTEGER NOT NULL,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS checks_by_service_time ON checks(service_id, checked_at);
CREATE TABLE IF NOT EXISTS states (service_id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    resolved_at TEXT,
    doc TEXT NOT NULL
);
-- Dedup guard: at most one open incident per service, even across worker restarts.
CREATE UNIQUE INDEX IF NOT EXISTS one_open_incident ON incidents(service_id) WHERE state = 'open';
CREATE INDEX IF NOT EXISTS incidents_by_start ON incidents(started_at);
CREATE TABLE IF NOT EXISTS worker (id TEXT PRIMARY KEY, owner TEXT, lease_expires_at TEXT, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account_docs (kind TEXT NOT NULL, id TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY (kind, id));
"""


class SqliteStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ setup
    def init(self) -> None:
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "sqlite",
            "label": "Local development storage (SQLite), not MongoDB Atlas",
            "persistent": self.path != ":memory:",
            "atlas": False,
            "location": self.path,
        }

    def close(self) -> None:
        self._conn.close()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # --------------------------------------------------------------- services
    def upsert_service(self, service: ServiceConfig, *, keep_enabled: bool = True) -> ServiceConfig:
        with self._lock:
            existing = self.get_service(service.id)
            if existing is not None and keep_enabled:
                service = service.model_copy(update={"enabled": existing.enabled})
            self._exec(
                "INSERT INTO services(id, doc, updated_at) VALUES (?,?,datetime('now')) "
                "ON CONFLICT(id) DO UPDATE SET doc=excluded.doc, updated_at=excluded.updated_at",
                (service.id, service.model_dump_json()),
            )
            return service

    def list_services(self) -> list[ServiceConfig]:
        return [ServiceConfig.model_validate_json(r["doc"]) for r in self._all("SELECT doc FROM services ORDER BY id")]

    def get_service(self, service_id: str) -> ServiceConfig | None:
        row = self._one("SELECT doc FROM services WHERE id = ?", (service_id,))
        return ServiceConfig.model_validate_json(row["doc"]) if row else None

    def set_service_enabled(self, service_id: str, enabled: bool) -> ServiceConfig | None:
        with self._lock:
            service = self.get_service(service_id)
            if service is None:
                return None
            return self.upsert_service(service.model_copy(update={"enabled": enabled}), keep_enabled=False)

    def delete_service(self, service_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM services WHERE id = ?", (service_id,))

    # ----------------------------------------------------------------- checks
    def insert_check(self, check: CheckRecord) -> bool:
        cur = self._exec(
            "INSERT OR IGNORE INTO checks(id, service_id, checked_at, status, latency_ms, functional_latency_ms, "
            "interval_seconds, doc) VALUES (?,?,?,?,?,?,?,?)",
            (check.id, check.service_id, iso(check.checked_at), check.status, check.latency_ms,
             check.functional_latency_ms, check.interval_seconds, check.model_dump_json()),
        )
        return cur.rowcount == 1

    def get_check(self, check_id: str) -> CheckRecord | None:
        row = self._one("SELECT doc FROM checks WHERE id = ?", (check_id,))
        return CheckRecord.model_validate_json(row["doc"]) if row else None

    def list_checks(self, service_id: str, *, limit: int = 50, before: datetime | None = None) -> list[CheckRecord]:
        if before is not None:
            rows = self._all("SELECT doc FROM checks WHERE service_id = ? AND checked_at < ? ORDER BY checked_at DESC LIMIT ?",
                             (service_id, iso(before), limit))
        else:
            rows = self._all("SELECT doc FROM checks WHERE service_id = ? ORDER BY checked_at DESC LIMIT ?", (service_id, limit))
        return [CheckRecord.model_validate_json(r["doc"]) for r in rows]

    def list_samples(self, service_id: str, since: datetime, until: datetime) -> list[Sample]:
        rows = self._all(
            "SELECT checked_at, status, latency_ms, functional_latency_ms, interval_seconds FROM checks "
            "WHERE service_id = ? AND checked_at >= ? AND checked_at <= ? ORDER BY checked_at",
            (service_id, iso(since), iso(until)),
        )
        return [Sample(checked_at=datetime.fromisoformat(r["checked_at"].replace("Z", "+00:00")), status=r["status"],
                       latency_ms=r["latency_ms"], functional_latency_ms=r["functional_latency_ms"],
                       interval_seconds=r["interval_seconds"]) for r in rows]

    def first_check_at(self, service_id: str) -> datetime | None:
        row = self._one("SELECT MIN(checked_at) AS t FROM checks WHERE service_id = ?", (service_id,))
        return datetime.fromisoformat(row["t"].replace("Z", "+00:00")) if row and row["t"] else None

    def count_checks(self, service_id: str | None = None) -> int:
        if service_id:
            return self._one("SELECT COUNT(*) AS n FROM checks WHERE service_id = ?", (service_id,))["n"]
        return self._one("SELECT COUNT(*) AS n FROM checks")["n"]

    # ------------------------------------------------------------------ state
    def get_state(self, service_id: str) -> ServiceState | None:
        row = self._one("SELECT doc FROM states WHERE service_id = ?", (service_id,))
        return ServiceState.model_validate_json(row["doc"]) if row else None

    def put_state(self, state: ServiceState) -> None:
        self._exec("INSERT INTO states(service_id, doc) VALUES (?,?) ON CONFLICT(service_id) DO UPDATE SET doc=excluded.doc",
                   (state.service_id, state.model_dump_json()))

    # -------------------------------------------------------------- incidents
    def get_open_incident(self, service_id: str) -> Incident | None:
        row = self._one("SELECT doc FROM incidents WHERE service_id = ? AND state = 'open'", (service_id,))
        return Incident.model_validate_json(row["doc"]) if row else None

    def create_incident(self, incident: Incident) -> bool:
        try:
            self._exec("INSERT INTO incidents(id, service_id, state, started_at, resolved_at, doc) VALUES (?,?,?,?,?,?)",
                       (incident.id, incident.service_id, incident.state, iso(incident.started_at), None,
                        incident.model_dump_json()))
            return True
        except sqlite3.IntegrityError:
            return False

    def save_incident(self, incident: Incident) -> None:
        self._exec("UPDATE incidents SET state = ?, resolved_at = ?, doc = ? WHERE id = ?",
                   (incident.state, iso(incident.resolved_at) if incident.resolved_at else None,
                    incident.model_dump_json(), incident.id))

    def get_incident(self, incident_id: str) -> Incident | None:
        row = self._one("SELECT doc FROM incidents WHERE id = ?", (incident_id,))
        return Incident.model_validate_json(row["doc"]) if row else None

    def list_incidents(self, *, service_id: str | None = None, state: str | None = None, limit: int = 50) -> list[Incident]:
        sql, params = "SELECT doc FROM incidents WHERE 1=1", []
        if service_id:
            sql += " AND service_id = ?"
            params.append(service_id)
        if state:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        return [Incident.model_validate_json(r["doc"]) for r in self._all(sql, tuple(params))]

    # ----------------------------------------------------------------- worker
    def acquire_lease(self, owner: str, now: datetime, ttl_seconds: int) -> bool:
        from datetime import timedelta

        expires = iso(now + timedelta(seconds=ttl_seconds))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT owner, lease_expires_at, doc FROM worker WHERE id = ?", (WORKER_ID,)).fetchone()
                if row is None:
                    doc = WorkerState(owner=owner, lease_expires_at=now + timedelta(seconds=ttl_seconds))
                    self._conn.execute("INSERT INTO worker(id, owner, lease_expires_at, doc) VALUES (?,?,?,?)",
                                       (WORKER_ID, owner, expires, doc.model_dump_json()))
                    self._conn.execute("COMMIT")
                    return True
                held = row["owner"] and row["owner"] != owner and row["lease_expires_at"] and row["lease_expires_at"] > iso(now)
                if held:
                    self._conn.execute("COMMIT")
                    return False
                doc = WorkerState.model_validate_json(row["doc"]).model_copy(
                    update={"owner": owner, "lease_expires_at": now + timedelta(seconds=ttl_seconds)})
                self._conn.execute("UPDATE worker SET owner = ?, lease_expires_at = ?, doc = ? WHERE id = ?",
                                   (owner, expires, doc.model_dump_json(), WORKER_ID))
                self._conn.execute("COMMIT")
                return True
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def release_lease(self, owner: str) -> None:
        with self._lock:
            row = self._one("SELECT doc FROM worker WHERE id = ? AND owner = ?", (WORKER_ID, owner))
            if row is None:
                return
            doc = WorkerState.model_validate_json(row["doc"]).model_copy(update={"owner": None, "lease_expires_at": None})
            self._exec("UPDATE worker SET owner = NULL, lease_expires_at = NULL, doc = ? WHERE id = ? AND owner = ?",
                       (doc.model_dump_json(), WORKER_ID, owner))

    def get_worker(self) -> WorkerState | None:
        row = self._one("SELECT doc FROM worker WHERE id = ?", (WORKER_ID,))
        return WorkerState.model_validate_json(row["doc"]) if row else None

    def update_worker(self, owner: str, fields: dict[str, Any]) -> None:
        with self._lock:
            row = self._one("SELECT doc FROM worker WHERE id = ?", (WORKER_ID,))
            if row is None:
                return
            doc = WorkerState.model_validate_json(row["doc"])
            if doc.owner not in (owner, None):
                return  # fenced: a worker that lost its lease cannot overwrite the new owner's state
            doc = doc.model_copy(update=fields)
            self._exec("UPDATE worker SET lease_expires_at = ?, doc = ? WHERE id = ?",
                       (iso(doc.lease_expires_at) if doc.lease_expires_at else None, doc.model_dump_json(), WORKER_ID))

    def request_check(self, now: datetime) -> None:
        with self._lock:
            row = self._one("SELECT doc FROM worker WHERE id = ?", (WORKER_ID,))
            doc = WorkerState.model_validate_json(row["doc"]) if row else WorkerState()
            doc = doc.model_copy(update={"check_requested_at": now})
            self._exec("INSERT INTO worker(id, owner, lease_expires_at, doc) VALUES (?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET doc = excluded.doc",
                       (WORKER_ID, doc.owner, iso(doc.lease_expires_at) if doc.lease_expires_at else None, doc.model_dump_json()))

    # -------------------------------------------------------------- retention
    def purge(self, checks_before: datetime, incidents_before: datetime) -> dict[str, int]:
        checks = self._exec("DELETE FROM checks WHERE checked_at < ?", (iso(checks_before),)).rowcount
        incidents = self._exec("DELETE FROM incidents WHERE state = 'resolved' AND resolved_at < ?",
                               (iso(incidents_before),)).rowcount
        return {"checks": checks, "incidents": incidents}

    def find_agent_incidents(self, app: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return []  # the agents' CI incidents live in MongoDB only

    def get_agent_activity(self, uptime_incident_id: str) -> dict[str, Any] | None:
        return None  # agent incidents and workflow steps live in MongoDB only

    # --------------------------------------------------------------- accounts
    def insert_doc(self, kind: str, doc_id: str, doc: dict[str, Any]) -> bool:
        cur = self._exec("INSERT OR IGNORE INTO account_docs(kind, id, doc) VALUES (?,?,?)", (kind, doc_id, json.dumps(seal(kind, doc))))
        return cur.rowcount == 1

    def put_doc(self, kind: str, doc_id: str, doc: dict[str, Any]) -> None:
        self._exec("INSERT INTO account_docs(kind, id, doc) VALUES (?,?,?) ON CONFLICT(kind, id) DO UPDATE SET doc=excluded.doc",
                   (kind, doc_id, json.dumps(seal(kind, doc))))

    def get_doc(self, kind: str, doc_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT doc FROM account_docs WHERE kind = ? AND id = ?", (kind, doc_id))
        return unseal(kind, json.loads(row["doc"])) if row else None

    def find_docs(self, kind: str, field: str, value: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT doc FROM account_docs WHERE kind = ? AND json_extract(doc, ?) = ? ORDER BY id",
                         (kind, f"$.{field}", value))
        return [unseal(kind, json.loads(r["doc"])) for r in rows]

    def all_docs(self, kind: str) -> list[dict[str, Any]]:
        return [unseal(kind, json.loads(r["doc"])) for r in self._all("SELECT doc FROM account_docs WHERE kind = ? ORDER BY id", (kind,))]

    def delete_doc(self, kind: str, doc_id: str) -> None:
        self._exec("DELETE FROM account_docs WHERE kind = ? AND id = ?", (kind, doc_id))

    def get_agent_findings(self, uptime_incident_id: str) -> dict[str, Any] | None:
        return None  # agent incidents live in MongoDB only

    def get_agent_run(self, uptime_incident_id: str) -> dict[str, Any] | None:
        return None

    def dump_json(self) -> str:  # debugging helper
        return json.dumps({"checks": self.count_checks()})

    def get_learning_summary(self) -> dict[str, Any] | None:
        return None  # the RL policy's experiences live in MongoDB only
