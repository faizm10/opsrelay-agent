"""Live-check incidents in Atlas: open/update on failure, collect evidence, hand to the investigator.

Flow per service, every POLL_SECONDS (driven by the orchestrator worker or `orchestrator live`):

    run_system_check(service)          Taha's app: availability (/health) + functional probe
      -> record_check()                failed: open the service's incident or update it
                                       passed: resolve the open incident (fresh probe id = recovery)
      -> collect_evidence()            failed step, deployed commit, recent changes, logs,
                                       CI status, similar incidents (Atlas Vector Search)
      -> investigate()                 diagnose_incident(LiveIncident) exactly once per incident

At most one unresolved incident per service: `active_service` is set only while the
incident is unresolved and has a unique sparse index, so repeated failures every 30 s
update the same document instead of opening new ones.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from pymongo import ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from agents import memory
from agents import mongodb as m

MAX_DIFF_CHARS = 12_000
MAX_LOG_CHARS = 8_000
RECENT_COMMITS = 3
KEEP_CHECKS = 20
MAX_INVESTIGATIONS = 3

LIVE_OPEN = "open"
LIVE_INVESTIGATING = "investigating"
LIVE_DIAGNOSED = "diagnosed"
LIVE_RESOLVED = "resolved"


def _coll(db: Database | None):
    return m.live_incidents(db)


# --------------------------------------------------------------------------- #
# Failed step                                                                 #
# --------------------------------------------------------------------------- #
def failed_step(check) -> dict[str, Any] | None:
    """The exact probe step that failed (health / send / readback / synthetic)."""
    for step in check.steps:
        if step.failure_kind is not None:
            return {"name": step.name, "method": step.method, "url": step.url,
                    "status_code": step.status_code, "failure_kind": step.failure_kind,
                    "error": step.error, "latency_ms": step.latency_ms}
    if not check.ok:  # e.g. read-back answered 200 but never contained our probe id
        last = check.steps[-1] if check.steps else None
        return {"name": last.name if last else "unknown", "method": last.method if last else None,
                "url": last.url if last else None, "status_code": check.http_status,
                "failure_kind": check.failure_kind, "error": check.error, "latency_ms": None}
    return None


def _slim(check) -> dict[str, Any]:
    return {"probe_id": check.probe_id, "checked_at": check.checked_at, "ok": check.ok,
            "reachable": check.reachable, "healthy": check.healthy, "delivery": check.delivery,
            "failure_kind": check.failure_kind, "error": check.error, "http_status": check.http_status,
            "target": check.target}


# --------------------------------------------------------------------------- #
# Open / update / resolve                                                     #
# --------------------------------------------------------------------------- #
def record_check(check, db: Database | None = None, worker_id: str = "live-monitor"
                 ) -> tuple[dict | None, str]:
    """Persist one live-check result. Returns (incident, event).

    event: "opened" | "updated" (failed) or "resolved" | "healthy" (passed).
    """
    now = m.utcnow()
    coll = _coll(db)
    if check.ok:
        inc = coll.find_one_and_update(
            {"active_service": check.service},
            {"$set": {"status": LIVE_RESOLVED, "resolved_at": now, "updated_at": now,
                      "recovery": {"probe_id": check.probe_id, "checked_at": check.checked_at,
                                   "delivery_latency_ms": check.delivery_latency_ms}},
             "$unset": {"active_service": ""}},
            return_document=ReturnDocument.AFTER,
        )
        if inc is None:
            return None, "healthy"
        m.log_step(inc["_id"], "live_resolved", "completed", worker_id,
                   {"recovery_probe_id": check.probe_id,
                    "fresh_probe": check.probe_id not in inc.get("probe_ids", [])}, db=db)
        return inc, "resolved"

    step = failed_step(check)
    iid = f"live:{check.service}:{check.probe_id}"
    try:
        inc = coll.find_one_and_update(
            {"active_service": check.service},
            {"$setOnInsert": {"_id": iid, "service": check.service, "app": check.app,
                              "status": LIVE_OPEN, "opened_at": now, "first_check": _slim(check),
                              "failed_step": step},
             "$set": {"last_check": _slim(check), "last_failed_step": step,
                      "last_failed_at": now, "updated_at": now},
             "$inc": {"failure_count": 1},
             "$push": {"probe_ids": {"$each": [check.probe_id], "$slice": -KEEP_CHECKS}}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:  # another monitor opened it concurrently -> update that one
        return record_check(check, db, worker_id)
    # active_service is the dedup key; set separately so $setOnInsert can't race with the filter.
    if "active_service" not in inc:
        inc = coll.find_one_and_update({"_id": inc["_id"]},
                                       {"$set": {"active_service": check.service}},
                                       return_document=ReturnDocument.AFTER)
    opened = inc["failure_count"] == 1
    m.log_step(inc["_id"], "live_check_failed", "completed", worker_id,
               {"probe_id": check.probe_id, "failed_step": step,
                "failure_count": inc["failure_count"]}, db=db)
    return inc, "opened" if opened else "updated"


# --------------------------------------------------------------------------- #
# Evidence                                                                    #
# --------------------------------------------------------------------------- #
def _git(repo: str, *args: str) -> str | None:
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def deployed_commit(service: str, repo_path: str, base_branch: str = "main") -> tuple[str | None, str]:
    """(sha, source). An explicit deploy marker wins; otherwise the checkout the apps run from.

    The demo runs Taha's apps from OPSRELAY_REPO_PATH, so its HEAD is what is serving traffic.
    """
    env = os.getenv(f"OPSRELAY_{service.upper()}_DEPLOYED_SHA")
    if env:
        return env.strip(), "env"
    for ref, label in (("HEAD", "HEAD of the running checkout"),
                       (f"origin/{base_branch}", f"origin/{base_branch} (assumed deployed)")):
        sha = _git(repo_path, "rev-parse", "--verify", "--quiet", ref)
        if sha:
            return sha.strip(), label
    return None, "unknown"


def recent_changes(repo_path: str, sha: str, app_root: str, n: int = RECENT_COMMITS) -> str:
    out = _git(repo_path, "log", "-p", f"-n{n}", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n    %s%n",
               sha, "--", app_root) or ""
    return out[:MAX_DIFF_CHARS] + ("\n… (truncated)" if len(out) > MAX_DIFF_CHARS else "")


def service_logs(service: str, probe_ids: list[str]) -> tuple[str, str]:
    """(log text, source). Lines mentioning our probe ids or errors first, then the tail."""
    path = os.getenv(f"OPSRELAY_{service.upper()}_LOG_FILE")
    if not path or not Path(path).is_file():
        return "", "none configured" if not path else f"missing: {path}"
    lines = Path(path).read_text(errors="replace").splitlines()[-2000:]
    keys = tuple(probe_ids) + ("ERROR", "error", "Traceback", "probe_failed", "Exception")
    relevant = [ln for ln in lines if any(k in ln for k in keys)]
    tail = lines[-100:]
    text = "\n".join(relevant[-200:] + ["--- tail ---"] + tail)
    return text[-MAX_LOG_CHARS:], path


def ci_status(github, app: str, base_branch: str = "main") -> str:
    """Latest CI conclusion for this app's workflow on the base branch."""
    if github is None:
        return "unknown"
    from agents.github_types import application_for_workflow_path

    try:
        runs = github.list_workflow_runs(branch=base_branch, per_page=20)
    except Exception:  # noqa: BLE001 - evidence is best-effort
        return "unknown"
    for run in runs:
        if application_for_workflow_path(run.get("path")) == app and run.get("status") == "completed":
            return "passing" if run.get("conclusion") == "success" else "failing"
    return "unknown"


def failure_text(check, step: dict | None) -> str:
    """What we embed to find similar incidents (same shape as memory.failure_text input)."""
    s = step or {}
    return (f"live check failed for {check.service}: step {s.get('name')} {s.get('method')} "
            f"{s.get('url')} -> {s.get('failure_kind')} HTTP {s.get('status_code')}: {s.get('error')}\n"
            f"delivery={check.delivery} reachable={check.reachable} healthy={check.healthy}")


def collect_evidence(inc: dict, check, repo_path: str, github=None,
                     db: Database | None = None, worker_id: str = "live-monitor"):
    """Build Faiz's LiveIncident for this failure and store a reviewable summary in Atlas."""
    from agents.contracts import PriorIncident, load_app_config
    from agents.live_incident import LiveIncident
    from agents.patcher import Repairer

    app = load_app_config(check.app)
    step = failed_step(check)
    sha, sha_source = deployed_commit(check.service, repo_path)
    similar = memory.find_similar({"app": check.app, "failed_logs": failure_text(check, step)},
                                  k=3, db=db)
    logs, log_source = service_logs(check.service, inc.get("probe_ids", [check.probe_id]))
    status = ci_status(github, check.app)

    source_files: dict[str, str] = {}
    diff = ""
    if sha:
        req = Repairer(repo_path=repo_path).request_for(
            {"app": check.app, "commit_sha": sha, "failed_logs": ""}, similar)
        source_files = req.source_files
        diff = recent_changes(repo_path, sha, app.root)

    evidence = LiveIncident(
        check=check, repo_path=repo_path if sha else None, deployed_sha=sha,
        service_logs=logs, recent_diff=diff, source_files=source_files,
        similar_incidents=[PriorIncident(id=s["memory_id"], summary=s.get("summary") or "",
                                         root_cause=s.get("root_cause"), similarity=s.get("score"))
                           for s in similar],
        ci_status=status,
    )
    summary = {
        "collected_at": m.utcnow(), "probe_id": check.probe_id, "failed_step": step,
        "deployed_sha": sha, "deployed_sha_source": sha_source,
        "recent_diff": diff, "service_logs": logs, "service_logs_source": log_source,
        "source_files": sorted(source_files), "ci_status": status,
        "similar": [{"memory_id": s["memory_id"], "score": round(s["score"], 3),
                     "summary": s.get("summary")} for s in similar],
    }
    _coll(db).update_one({"_id": inc["_id"]}, {"$set": {"evidence": summary, "updated_at": m.utcnow()}})
    m.log_step(inc["_id"], "evidence_collected", "completed", worker_id, {
        "failed_step": (step or {}).get("name"), "deployed_sha": sha, "deployed_sha_source": sha_source,
        "ci_status": status, "log_lines": logs.count("\n") + bool(logs),
        "source_files": len(source_files), "similar": summary["similar"]}, db=db)
    return evidence


# --------------------------------------------------------------------------- #
# Investigation hand-off                                                      #
# --------------------------------------------------------------------------- #
def investigate(inc: dict, evidence, diagnose: Callable | None = None,
                db: Database | None = None, worker_id: str = "live-monitor") -> dict | None:
    """Run the investigator once per incident (atomic claim), store its result."""
    claimed = _coll(db).find_one_and_update(
        {"_id": inc["_id"], "status": LIVE_OPEN, "investigation": None,
         "investigation_attempts": {"$not": {"$gte": MAX_INVESTIGATIONS}}},
        {"$set": {"status": LIVE_INVESTIGATING, "investigation_started": {
            "at": m.utcnow(), "by": worker_id}}, "$inc": {"investigation_attempts": 1}},
        return_document=ReturnDocument.AFTER)
    if claimed is None:
        return None  # already investigated, being investigated, or resolved
    if diagnose is None:
        from agents.live_incident import diagnose_incident as diagnose
    try:
        result = diagnose(evidence)
    except Exception as e:  # noqa: BLE001 - leave it open for the next tick
        _coll(db).update_one({"_id": inc["_id"], "status": LIVE_INVESTIGATING},
                             {"$set": {"status": LIVE_OPEN, "last_error": f"{type(e).__name__}: {e}"},
                              "$unset": {"investigation_started": ""}})
        m.log_step(inc["_id"], "live_investigated", "failed", worker_id, {"error": str(e)}, db=db)
        return None
    out = {k: getattr(result, k, None) for k in ("outcome", "route", "reason", "likely_cause",
                                                 "recommended_actions", "next_diagnostic", "recovery")}
    out["at"] = m.utcnow()
    doc = _coll(db).find_one_and_update(
        {"_id": inc["_id"], "status": LIVE_INVESTIGATING},
        {"$set": {"status": LIVE_DIAGNOSED, "investigation": out, "updated_at": m.utcnow()}},
        return_document=ReturnDocument.AFTER)
    m.log_step(inc["_id"], "live_investigated", "completed", worker_id,
               {k: out[k] for k in ("outcome", "route", "likely_cause")}, db=db)
    return doc


# --------------------------------------------------------------------------- #
# System check against Taha's apps                                            #
# --------------------------------------------------------------------------- #
# Endpoints confirmed from the apps' code (same as admin-dashboard/backend/services.toml).
# Both functional probes are side-effect free: 200 healthy / 503 {"failed_checks": [...]}.
SYSTEM_CHECKS = {
    "shopping": {"base_url": "http://localhost:8100", "health": "/api/health",
                 "functional": "/api/health/checkout"},
    "messaging": {"base_url": "http://localhost:8200", "health": "/health",
                  "functional": "/api/monitor/probe"},
}
SYSTEM_CHECK_TIMEOUT = float(os.getenv("OPSRELAY_LIVE_TIMEOUT", "10"))


def _http_step(name: str, url: str, timeout: float):
    """One GET -> (ProbeStep, json_body). Network errors become steps, never exceptions."""
    import time

    import httpx

    from agents.contracts import ProbeStep

    start = time.perf_counter()
    try:
        resp = httpx.get(url, timeout=timeout)
    except httpx.TimeoutException as e:
        return ProbeStep(name=name, method="GET", url=url, failure_kind="connection_timeout",
                         error=f"no response within {timeout}s ({type(e).__name__})"), None
    except httpx.ConnectError as e:
        kind = "connection_refused" if "refused" in str(e).lower() else "connection_error"
        return ProbeStep(name=name, method="GET", url=url, failure_kind=kind, error=str(e)), None
    except httpx.HTTPError as e:
        return ProbeStep(name=name, method="GET", url=url, failure_kind="connection_error",
                         error=str(e)), None
    latency = round((time.perf_counter() - start) * 1000, 1)
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code >= 400:
        failed = (body or {}).get("failed_checks") if isinstance(body, dict) else None
        detail = f"failed checks: {failed}" if failed else resp.text[:300]
        return ProbeStep(name=name, method="GET", url=url, status_code=resp.status_code,
                         latency_ms=latency, failure_kind="http_error",
                         error=f"HTTP {resp.status_code}: {detail}"), body
    if not isinstance(body, dict):
        return ProbeStep(name=name, method="GET", url=url, status_code=resp.status_code,
                         latency_ms=latency, failure_kind="invalid_response",
                         error="response is not a JSON object"), body
    return ProbeStep(name=name, method="GET", url=url, status_code=resp.status_code,
                     latency_ms=latency), body


def run_system_check(service: str):
    """Availability (health) + functional probe for one of Taha's apps, as a LiveCheckResult.

    URL: OPSRELAY_<SERVICE>_URL env, else Taha's local default port.
    """
    import uuid
    from datetime import UTC, datetime

    from agents.contracts import UNREACHABLE, LiveCheckResult

    cfg = SYSTEM_CHECKS[service]
    base = (os.getenv(f"OPSRELAY_{service.upper()}_URL") or cfg["base_url"]).rstrip("/")
    probe_id = f"opsrelay-{uuid.uuid4().hex}"
    steps = []

    def done(**fields):
        return LiveCheckResult(service=service, app=service, checked_at=datetime.now(UTC),
                               base_url=base, target="live", probe_id=probe_id, mode="synthetic",
                               steps=steps, **fields)

    health, _ = _http_step("health", base + cfg["health"], SYSTEM_CHECK_TIMEOUT)
    steps.append(health)
    if health.failure_kind in UNREACHABLE:
        return done(reachable=False, healthy=False, delivery="not_attempted",
                    failure_kind=health.failure_kind, error=health.error)
    if health.failure_kind:
        return done(reachable=True, healthy=False, delivery="not_attempted",
                    failure_kind=health.failure_kind, error=health.error,
                    http_status=health.status_code)

    probe, body = _http_step("synthetic", base + cfg["functional"], SYSTEM_CHECK_TIMEOUT)
    steps.append(probe)
    if probe.failure_kind:
        return done(reachable=True, healthy=True, delivery="not_delivered",
                    failure_kind="not_delivered" if probe.failure_kind == "http_error" else probe.failure_kind,
                    error=probe.error, http_status=probe.status_code)
    if isinstance(body, dict) and body.get("status") not in (None, "healthy", "ok"):
        probe.failure_kind, probe.error = "invalid_response", f"probe status {body.get('status')!r}"
        return done(reachable=True, healthy=True, delivery="not_delivered",
                    failure_kind="invalid_response", error=probe.error, http_status=probe.status_code)
    return done(reachable=True, healthy=True, delivery="delivered",
                delivery_latency_ms=probe.latency_ms)


# --------------------------------------------------------------------------- #
# Watcher                                                                     #
# --------------------------------------------------------------------------- #
class LiveWatcher:
    """One tick = one live check per service, then incident bookkeeping."""

    def __init__(self, services: list[str], repo_path: str, github=None, db: Database | None = None,
                 run_check: Callable | None = None, diagnose: Callable | None = None,
                 investigate: bool = True, worker_id: str = "live-monitor"):
        self.services = services
        self.repo_path = repo_path
        self.github = github
        self.db = db
        self.diagnose = diagnose
        self.investigate = investigate
        self.worker_id = worker_id
        self.run_check = run_check or run_system_check

    def tick(self) -> list[tuple[str, str]]:
        events = []
        for service in self.services:
            check = self.run_check(service)
            inc, event = record_check(check, db=self.db, worker_id=self.worker_id)
            events.append((service, event))
            if event == "resolved":
                print(f"✔ live {service}: recovered ({inc['_id']})")
            elif event in ("opened", "updated"):
                print(f"✘ live {service}: {event} {inc['_id']} "
                      f"(failure #{inc['failure_count']}, step={(inc.get('last_failed_step') or {}).get('name')})")
                needs_diagnosis = (self.investigate and inc["status"] == LIVE_OPEN
                                   and not inc.get("investigation")
                                   and inc.get("investigation_attempts", 0) < MAX_INVESTIGATIONS)
                if "evidence" not in inc or needs_diagnosis:
                    evidence = collect_evidence(inc, check, self.repo_path, self.github,
                                                db=self.db, worker_id=self.worker_id)
                    if needs_diagnosis:
                        investigate(inc, evidence, self.diagnose, db=self.db, worker_id=self.worker_id)
        return events


def live_services() -> list[str]:
    """Services to watch, from OPSRELAY_LIVE_SERVICES (e.g. ``messaging``). Off when unset or ``none``."""
    raw = os.getenv("OPSRELAY_LIVE_SERVICES", "")
    if raw.strip().lower() in ("none", "off", "0", ""):
        return []
    services = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [s for s in services if s not in SYSTEM_CHECKS]
    if unknown:
        raise SystemExit(f"unknown live service(s) {unknown}; choose from {sorted(SYSTEM_CHECKS)}")
    return services
