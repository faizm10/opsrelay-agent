"""Live incident intake: Taha's uptime monitor -> the agents' incidents table.

Taha's monitor (admin-dashboard/backend/uptime) writes ``uptime_incidents`` to the same
MongoDB database when a monitored service is down or failing its functional probe. It never calls the agents itself. ``LiveIntake.poll``
reads new open uptime incidents (read only) and records each one once as a
``source="live"`` incident, which the orchestrator then runs through the same stages as a
CI failure: memory -> diagnose/repair -> PR intent -> draft PR -> memory.

The incident is pinned to the commit that was deployed when it was detected: the head
of the base branch (``GITHUB_BASE_BRANCH``, default ``main``) after a fetch. If an
incident from CI is already open for the same app and commit, the live one is skipped,
so one bug never produces two PRs.

``handoff_from_uptime`` mirrors Taha's ``build_handoff`` (schema
``opsrelay.uptime_incident.v1``), reading the raw document so the agents don't import
the dashboard package.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timedelta
from typing import Any, Callable

from pymongo.database import Database

from agents import mongodb as m
from agents.orchestrator import ingest_live_incident, live_incident_id

log = logging.getLogger("agents.intake")

UPTIME_INCIDENTS = "uptime_incidents"  # owned by Taha's uptime monitor; read only here
REPO_MAPPINGS = "opsrelay_repos"  # dashboard repository -> service mappings, with per-repo agent settings; read only
MAX_AGE_HOURS = 24

# Taha's ErrorKind -> agents.contracts.FailureKind, where they map 1:1
_FAILURE_KIND = {
    "timeout": "connection_timeout",
    "connection_refused": "connection_refused",
    "connection_error": "connection_error",
    "http_error": "http_error",
    "invalid_response": "invalid_response",
}


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def handoff_from_uptime(doc: dict[str, Any]) -> dict[str, Any]:
    """The parts of an uptime incident the repair agent needs, JSON-safe."""
    evidence = doc.get("evidence") or {}
    latest = evidence.get("latest_failure") or {}
    probes = latest.get("probes") or {}
    availability = probes.get("availability") or {}
    functional = probes.get("functional") or {}
    failures = (functional.get("details") or {}).get("failures") or []
    failing = next((p for p in (availability, probes.get("frontend") or {}, functional) if p and not p.get("ok")), {})
    return {
        "schema": "opsrelay.uptime_incident.v1",
        "uptime_incident_id": doc["_id"],
        "service_id": doc.get("service_id"),
        "app": doc.get("app"),
        "title": doc.get("title"),
        "summary": doc.get("summary"),
        "severity": doc.get("severity"),
        "failure_type": doc.get("failure_type"),
        "investigation_route": doc.get("investigation_route"),
        "route_reason": evidence.get("route_reason"),
        "started_at": _iso(doc.get("started_at")),
        "probe_failures": failures,
        "suspect_sources": sorted({f["source"] for f in failures if f.get("source")}),
        "live_check": {
            "service": doc.get("service_id"),
            "app": doc.get("app"),
            "checked_at": latest.get("checked_at"),
            "base_url": None,
            "reachable": availability.get("http_status") is not None,
            "healthy": bool(availability.get("ok")),
            "failure_kind": _FAILURE_KIND.get(failing.get("error_kind") or ""),
            "error": failing.get("error"),
            "http_status": failing.get("http_status"),
        },
    }


def deployed_sha(repo_path: str, base_branch: str, remote: str = "origin") -> str | None:
    """Head of the base branch after a fetch (what the demo deploys), or None."""
    try:
        subprocess.run(["git", "-C", repo_path, "fetch", "--quiet", remote, base_branch],
                       capture_output=True, timeout=60, check=False)
        out = subprocess.run(["git", "-C", repo_path, "rev-parse", f"{remote}/{base_branch}"],
                             capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not resolve %s/%s: %s", remote, base_branch, exc)
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and len(sha) == 40 else None


class LiveIntake:
    def __init__(
        self,
        repo: str,
        *,
        db: Database | None = None,
        repo_path: str | None = None,
        base_branch: str | None = None,
        resolve_sha: Callable[[], str | None] | None = None,
        max_age_hours: float = MAX_AGE_HOURS,
        clock: Callable[[], datetime] = m.utcnow,
    ):
        self.repo = repo
        self.db = m.get_db(db)
        repo_path = repo_path or os.getenv("OPSRELAY_REPO_PATH") or "."
        base_branch = base_branch or os.getenv("GITHUB_BASE_BRANCH") or "main"
        self.resolve_sha = resolve_sha or (lambda: deployed_sha(repo_path, base_branch))
        self.max_age = timedelta(hours=max_age_hours)
        self.clock = clock
        self._skipped: set[str] = set()

    def poll(self) -> list[str]:
        """Record new open uptime incidents. Returns the ids of incidents created."""
        query = {"state": "open", "started_at": {"$gte": self.clock() - self.max_age},
                 "$or": [{"app": {"$in": ["shopping", "messaging"]}}, {"evidence.source": "betterstack"}]}
        fresh = [(d, live) for d in self.db[UPTIME_INCIDENTS].find(query).sort("started_at", 1)
                 if d["_id"] not in self._skipped
                 and not m.incidents(self.db).count_documents({"_id": live_incident_id(self.repo, d["_id"])}, limit=1)
                 and (live := self._handoff(d))]
        if not fresh:
            return []
        sha = self.resolve_sha()
        if not sha:
            log.warning("deployed commit unknown; will retry %d live incident(s) next poll", len(fresh))
            return []
        created = []
        for doc, live in fresh:
            duplicate = m.incidents(self.db).find_one(
                {"app": live["app"], "commit_sha": sha, "source": {"$ne": m.SOURCE_LIVE},
                 "status": {"$in": [m.STATUS_OPEN, m.STATUS_AWAITING_MERGE]}}, {"_id": 1})
            if duplicate:
                log.info("live incident %s: CI incident %s already covers %s at %s",
                         doc["_id"], duplicate["_id"], live["app"], sha[:7])
                self._skipped.add(doc["_id"])
                continue
            iid, was_created = ingest_live_incident(self.repo, live, sha, db=self.db)
            if was_created:
                created.append(iid)
        return created

    def _handoff(self, doc: dict) -> dict | None:
        """Monitor incidents always; Better Stack alerts only for a mapped repo whose agent is switched on."""
        if (doc.get("evidence") or {}).get("source") != "betterstack":
            return handoff_from_uptime(doc)
        mapping = self.db[REPO_MAPPINGS].find_one({"_id": doc.get("service_id")}) or {}
        agent = mapping.get("agent") or {}
        if not agent.get("enabled") or agent.get("mode") == "diagnose" or (mapping.get("repo") or "").lower() != self.repo.lower():
            return None  # checked again next poll, so switching the agent on picks up open alerts; diagnose mode is agents/diagnose.py
        evidence = doc["evidence"]
        live = handoff_from_uptime({**doc, "app": agent["app"]})  # the code area the agent repairs, not the repo name
        return {**live, "report_only": agent.get("mode") != "draft_pr",
                "alert": {k: evidence.get(k) for k in ("cause", "url", "response_url", "betterstack_incident_id")}}
