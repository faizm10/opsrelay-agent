"""Production entrypoint for the OpsRelay investigation worker (see docs/runbook-agent-worker.md).

    python -m agents.worker check    # validate config, Atlas, Vector Search, GitHub, verifier; exit 2 on error
    python -m agents.worker run      # supervised loop; SIGTERM drains in-flight incidents, then exits
    python -m agents.worker once     # one cycle, for a scheduled job (Cloud Run); waits for in-flight incidents
    python -m agents.worker health   # container healthcheck: exit 1 unless the heartbeat is fresh

Server-side configuration only (environment, never the repo):
- Report-only by default. Draft PRs are enabled per repository with OPSRELAY_PUBLISH_REPOS.
- GITHUB_TOKEN is read-only (runs, logs, PR lookup, compare). GITHUB_PUBLISH_TOKEN is read
  once, removed from the environment and handed only to the PR publisher.
- Incidents run in a bounded thread pool (OPSRELAY_MAX_CONCURRENCY). Leases make a crash or a
  restart safe: another worker resumes from the last verified stage.
- Each cycle upserts a heartbeat into Atlas (worker_heartbeats) and the local heartbeat file.
  CI polling, live intake and incident progress are reported separately.
"""
from __future__ import annotations

import argparse
import contextvars
import io
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

from agents import mongodb as m
from agents import verifier
from agents.contracts import APPS_CONFIG_PATH

log = logging.getLogger("agents.worker")
SECRET_ENV = ("MONGODB_URI", "OPENAI_API_KEY", "GITHUB_TOKEN", "GITHUB_PUBLISH_TOKEN", "VOYAGE_API_KEY")
_incident: contextvars.ContextVar[str | None] = contextvars.ContextVar("incident_id", default=None)


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    env: str
    repo: str
    repo_path: str
    publish_repos: set[str]
    max_concurrency: int
    poll_seconds: int
    shutdown_grace: int
    heartbeat_file: Path
    live_intake: bool
    runner: str
    fallbacks: list[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return "draft_pr" if self.publish_repos else "report_only"


def load_config(env: dict[str, str] | None = None) -> tuple[Config, list[str]]:
    """The config and every problem that should stop the worker from starting."""
    env = os.environ if env is None else env
    errors = [f"{k} is required" for k in ("MONGODB_URI", "OPENAI_API_KEY", "GITHUB_TOKEN", "GITHUB_OWNER")
              if not env.get(k)]
    repo = f"{env.get('GITHUB_OWNER', '')}/{env.get('GITHUB_REPO', 'opsrelay-agent')}"
    cfg = Config(
        env=env.get("OPSRELAY_ENV", "production"), repo=repo, repo_path=env.get("OPSRELAY_REPO_PATH", ""),
        publish_repos={r.strip() for r in env.get("OPSRELAY_PUBLISH_REPOS", "").split(",") if r.strip()},
        max_concurrency=max(1, int(env.get("OPSRELAY_MAX_CONCURRENCY", "2"))),
        poll_seconds=int(env.get("POLL_SECONDS", "30")),
        shutdown_grace=int(env.get("OPSRELAY_SHUTDOWN_GRACE_SECONDS", "60")),
        heartbeat_file=Path(env.get("OPSRELAY_HEARTBEAT_FILE", "/tmp/opsrelay-heartbeat.json")),
        live_intake=env.get("OPSRELAY_LIVE_INTAKE", "1") not in ("0", "false", "no"),
        runner=env.get("OPSRELAY_VERIFIER_RUNNER", "auto"),
    )
    if cfg.runner == "auto":
        cfg.runner = "docker" if env.get("OPSRELAY_VERIFIER_IMAGE") and verifier.docker_reachable() else "process"
    if not cfg.repo_path or not (Path(cfg.repo_path) / ".git").exists():
        errors.append("OPSRELAY_REPO_PATH must be a git clone of the target repository")
    if env.get("GITHUB_DRAFT_PRS", "true").lower() in ("0", "false", "no"):
        errors.append("GITHUB_DRAFT_PRS must stay true: merges stay with humans")
    if unknown := cfg.publish_repos - {repo}:
        errors.append(f"OPSRELAY_PUBLISH_REPOS lists {sorted(unknown)}, but this worker is scoped to {repo}")
    if cfg.publish_repos:
        if not env.get("GITHUB_PUBLISH_TOKEN"):
            errors.append("GITHUB_PUBLISH_TOKEN is required when OPSRELAY_PUBLISH_REPOS is set")
        elif env.get("GITHUB_PUBLISH_TOKEN") == env.get("GITHUB_TOKEN"):
            errors.append("GITHUB_PUBLISH_TOKEN must differ from the read-only GITHUB_TOKEN")
        apps = tomllib.loads(Path(env.get("OPSRELAY_APPS_CONFIG") or APPS_CONFIG_PATH).read_text())["apps"]
        errors += [f"OPSRELAY_{a.upper()}_{k}_URL is required to verify merged fixes are deployed"
                   for a in apps for k in ("VERSION", "PROBE") if not env.get(f"OPSRELAY_{a.upper()}_{k}_URL")]
    if cfg.runner == "docker":
        if not env.get("OPSRELAY_VERIFIER_IMAGE"):
            errors.append("OPSRELAY_VERIFIER_IMAGE is required with OPSRELAY_VERIFIER_RUNNER=docker")
    else:
        cfg.fallbacks.append("verifier: process runner, no Docker (scrubbed env; no network or filesystem isolation)")
    if not env.get("VOYAGE_API_KEY"):
        cfg.fallbacks.append("embeddings: local hashing model (VOYAGE_API_KEY unset)")
    return cfg, errors


# --------------------------------------------------------------------------- #
# Structured, redacted logs                                                    #
# --------------------------------------------------------------------------- #
_PATTERNS = [re.compile(p) for p in (
    r"gh[opsu]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"sk-[A-Za-z0-9_-]{16,}",
    r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)", r"(?i)(?<=bearer )[A-Za-z0-9._~+/=-]{8,}",
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",  # customer email addresses
)]


def redact(text: str) -> str:
    for name in SECRET_ENV:
        if (value := os.environ.get(name)) and len(value) >= 8:
            text = text.replace(value, "[redacted]")
    for pattern in _PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


class JsonFormatter(logging.Formatter):
    def __init__(self, worker_id: str):
        super().__init__()
        self.worker_id = worker_id

    def format(self, record: logging.LogRecord) -> str:
        doc = {"at": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
               "logger": record.name, "worker_id": self.worker_id,
               "incident_id": getattr(record, "incident_id", None) or _incident.get(),
               "stage": getattr(record, "stage", None), "msg": redact(record.getMessage())}
        if record.exc_info:
            doc["error"] = redact(self.formatException(record.exc_info))[-2000:]
        return json.dumps({k: v for k, v in doc.items() if v is not None})


class _PrintsToLog(io.TextIOBase):
    """The pipeline modules print progress; turn each line into a structured log record."""

    def __init__(self):
        self._local = threading.local()  # print() writes a line in pieces; buffer per thread

    def write(self, text: str) -> int:
        buf = getattr(self._local, "buf", "") + text
        *lines, self._local.buf = buf.split("\n")
        for line in lines:
            if line.strip():
                logging.getLogger("agents.pipeline").info(line.strip())
        return len(text)


def setup_logging(worker_id: str) -> None:
    handler = logging.StreamHandler(sys.__stdout__)
    handler.setFormatter(JsonFormatter(worker_id))
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.stdout = _PrintsToLog()


# --------------------------------------------------------------------------- #
# Wiring                                                                      #
# --------------------------------------------------------------------------- #
class _ReportOnly:
    def create_or_find_pr(self, req):
        raise RuntimeError("draft PR publication is not enabled for this repository")


def build(cfg: Config):
    from agents.deployment import HttpDeployments
    from agents.git_sync import SyncingRepairer
    from agents.github_client import GitHubClient
    from agents.github_publisher import PullRequestPublisher
    from agents.intake import LiveIntake
    from agents.monitor import FailureMonitor
    from agents.orchestrator import GitHubAdapter, MonitorAdapter, Orchestrator
    from agents.patcher import Repairer

    publish_token = os.environ.pop("GITHUB_PUBLISH_TOKEN", None)  # only the publisher holds it
    read = GitHubClient()
    publisher = (PullRequestPublisher(GitHubClient(replace(read.config, token=publish_token)), draft=True)
                 if cfg.publish_repos else _ReportOnly())
    orch = Orchestrator(GitHubAdapter(read, publisher),
                        SyncingRepairer(Repairer(repo_path=cfg.repo_path), cfg.repo_path),
                        publish_repos=cfg.publish_repos, deployments=HttpDeployments(read),
                        reverify=lambda inc, head: reverify(cfg.repo_path, inc, head))
    intake = LiveIntake(cfg.repo, db=orch.db, base_branch=read.config.base_branch) if cfg.live_intake else None
    return orch, MonitorAdapter(read, FailureMonitor(read)), intake, read


def reverify(repo_path: str, inc: dict, head: str) -> tuple[bool, str, dict]:
    """Run the verified patch (and its regression tests) against the moved base branch."""
    from agents.contracts import load_app_config
    from agents.git_sync import ensure_commit
    from agents.verifier import VerificationError, VerificationSession

    if not ensure_commit(repo_path, head):
        return False, f"commit {head[:7]} is not available locally", {}
    patch = inc.get("patch") or {}
    files = patch.get("files") or {}
    added = {p: files[p] for p in patch.get("added_tests") or [] if p in files}
    try:
        with VerificationSession(load_app_config(inc["app"]), repo_path, head, added_tests=added) as session:
            check = session.check_suite(patch.get("diff") or "")
    except VerificationError as exc:
        return False, str(exc), {}
    return check.passed, check.reason, check.files


def check(cfg: Config) -> list[str]:
    """Connectivity and setup checks. Repeatable: setup only creates what is missing."""
    from agents import memory, rl_policy
    from agents.github_client import GitHubClient

    errors = []
    try:
        db = m.get_db()
        db.command("ping")
        m.ensure_setup(db)
        rl_policy.ensure_setup(db)
        probe_id = f"check:{uuid.uuid4().hex[:8]}"
        db[m.WORKER_HEARTBEATS].insert_one({"_id": probe_id, "expires_at": m.utcnow() + timedelta(minutes=5)})
        if not db[m.WORKER_HEARTBEATS].find_one({"_id": probe_id}):
            errors.append("Atlas: checkpoint write was not readable")
        db[m.WORKER_HEARTBEATS].delete_one({"_id": probe_id})
        log.info("Atlas: ping, setup and checkpoint read-back ok")
        try:
            hits = memory._vector_search(memory.embed("checkout timeout", "query"), {"active": True}, 1, db)
            log.info("Vector Search: query ok (%d hit(s))", len(hits))
        except Exception as exc:  # noqa: BLE001
            cfg.fallbacks.append(f"vector search: local cosine ({type(exc).__name__})")
            log.warning("Vector Search unavailable (%s); similar incidents use the local cosine fallback", exc)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Atlas: {type(exc).__name__}: {str(exc)[:300]}")
    try:
        auth = GitHubClient().verify_auth()
        log.info("GitHub read token ok for %s", auth.get("repo") or cfg.repo)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"GitHub: {type(exc).__name__}: {exc}")
    if cfg.runner == "docker":
        ok = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                            capture_output=True, text=True, timeout=30)
        if ok.returncode != 0:
            errors.append(f"verifier: docker is not reachable: {ok.stderr.strip()[:200]}")
    return errors


# --------------------------------------------------------------------------- #
# Supervised loop                                                             #
# --------------------------------------------------------------------------- #
class Worker:
    def __init__(self, cfg: Config, orch, monitor, intake):
        self.cfg, self.orch, self.monitor, self.intake = cfg, orch, monitor, intake
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=cfg.max_concurrency, thread_name_prefix="incident")
        self.in_flight: dict[str, Future] = {}
        self.status: dict[str, dict] = {}
        self.started_at = m.utcnow()
        self.llm = None  # built on first use by diagnose mode

    def _part(self, name: str, fn) -> None:
        """Run one part of the cycle; record its health separately from the others."""
        try:
            detail = fn()
            self.status[name] = {"ok": True, "at": m.utcnow(), **({"detail": detail} if detail is not None else {})}
        except Exception as exc:  # noqa: BLE001 - one failing source must not stop the others
            self.status[name] = {"ok": False, "at": m.utcnow(), "error": redact(f"{type(exc).__name__}: {exc}")[:500]}
            log.warning("%s failed: %s", name, exc)

    def _ci(self) -> int:
        from agents.orchestrator import ingest_failed_run, live_incident_covering

        runs = self.monitor.poll_failed_runs()
        for run in runs:
            if covering := live_incident_covering(run, db=self.orch.db):
                log.info("CI run %s correlated with live incident %s", run["run_id"], covering,
                         extra={"incident_id": covering})
                continue
            iid, created = ingest_failed_run(run, db=self.orch.db)
            if created:
                log.info("new CI incident", extra={"incident_id": iid, "stage": "detected"})
        return len(runs)

    def _diagnose(self) -> int:
        from agents import diagnose
        from agents.llm import OpenAIStructuredLLM, default_llm

        self.llm = self.llm or default_llm()
        return diagnose.run(self.orch.db, self.llm, redact, own_llm=lambda key: OpenAIStructuredLLM(api_key=key))

    def _intake(self) -> int:
        new = self.intake.poll()
        for iid in new:
            log.info("new live incident", extra={"incident_id": iid, "stage": "detected"})
        return len(new)

    def _run(self, iid: str) -> None:
        _incident.set(iid)
        try:
            self.orch.run_incident(iid)
        except Exception:  # noqa: BLE001 - logged; the lease expires and the incident is retried
            log.exception("incident run crashed")

    def _dispatch(self) -> int:
        for iid in [i for i, f in self.in_flight.items() if f.done()]:
            del self.in_flight[iid]
        for iid in self.orch.resumable_ids():
            if len(self.in_flight) >= self.cfg.max_concurrency or self.stop.is_set():
                break
            if iid not in self.in_flight:
                self.in_flight[iid] = self.pool.submit(self._run, iid)
        return len(self.in_flight)

    def heartbeat(self, state: str = "running") -> None:
        now = m.utcnow()
        counts = {d["_id"]: d["n"] for d in m.incidents(self.orch.db).aggregate(
            [{"$group": {"_id": "$status", "n": {"$sum": 1}}}])}
        doc = {"worker_id": self.orch.worker_id, "state": state, "env": self.cfg.env, "mode": self.cfg.mode,
               "publish_repos": sorted(self.cfg.publish_repos), "release": os.getenv("OPSRELAY_RELEASE", "dev"),
               "started_at": self.started_at, "at": now, "in_flight": sorted(self.in_flight),
               "max_concurrency": self.cfg.max_concurrency, "sources": self.status, "incidents": counts,
               "fallbacks": self.cfg.fallbacks,
               "expires_at": now + timedelta(seconds=self.cfg.poll_seconds * 3 + 60)}
        m.get_db(self.orch.db)[m.WORKER_HEARTBEATS].replace_one({"_id": self.orch.worker_id}, doc, upsert=True)
        self.cfg.heartbeat_file.write_text(json.dumps({"at": now.isoformat(), "state": state,
                                                       "worker_id": self.orch.worker_id}))

    def cycle(self) -> None:
        self._part("ci_monitor", self._ci)
        if self.intake is not None:
            self._part("live_intake", self._intake)
        self._part("incidents", self._dispatch)
        self._part("reviews", lambda: len(self.orch.check_reviews()))
        self._part("deployments", lambda: len(self.orch.check_deployments()))
        self._part("diagnoses", self._diagnose)
        self._part("heartbeat", self.heartbeat)

    def once(self) -> None:
        """One cycle for a scheduled job. In-flight incidents finish before exit; the job timeout bounds
        this, and a killed run leaves its incidents to lease expiry like any crash."""
        log.info("worker run-once: env=%s mode=%s repo=%s fallbacks=%s", self.cfg.env, self.cfg.mode,
                 self.cfg.repo, self.cfg.fallbacks or "none")
        self.cycle()
        for f in list(self.in_flight.values()):
            f.result()
        self._part("heartbeat", lambda: self.heartbeat("scheduled"))
        self.pool.shutdown()

    def run(self) -> int:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        log.info("worker starting: env=%s mode=%s repo=%s concurrency=%d fallbacks=%s", self.cfg.env,
                 self.cfg.mode, self.cfg.repo, self.cfg.max_concurrency, self.cfg.fallbacks or "none")
        while not self.stop.is_set():
            self.cycle()
            self.stop.wait(self.cfg.poll_seconds)
        log.info("shutdown: draining %d in-flight incident(s) for up to %ds", len(self.in_flight), self.cfg.shutdown_grace)
        deadline = time.monotonic() + self.cfg.shutdown_grace
        for f in list(self.in_flight.values()):
            try:
                f.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:  # noqa: BLE001 - timeout: its lease expires and another worker resumes it
                pass
        unfinished = [i for i, f in self.in_flight.items() if not f.done()]
        try:
            self.heartbeat("stopped")
        except Exception:  # noqa: BLE001
            pass
        log.info("stopped; %d incident(s) left to lease expiry: %s", len(unfinished), unfinished or "none")
        sys.stdout.flush()
        os._exit(0)  # do not wait on threads still inside a stage


def health(path: Path, max_age: int) -> int:
    try:
        beat = json.loads(path.read_text())
    except (OSError, ValueError):
        return 1
    age = time.time() - path.stat().st_mtime
    return 0 if beat.get("state") == "running" and age <= max_age else 1


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m agents.worker")
    p.add_argument("cmd", choices=["check", "run", "once", "health"])
    a = p.parse_args()
    cfg, errors = load_config()
    if a.cmd == "health":
        raise SystemExit(health(cfg.heartbeat_file, cfg.poll_seconds * 3 + 60))
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
    setup_logging(worker_id)
    for err in errors:
        log.error("config: %s", err)
    if not errors:
        errors = check(cfg)
        for err in errors:
            log.error("startup: %s", err)
    if errors:
        raise SystemExit(2)
    if a.cmd == "check":
        log.info("check passed: mode=%s fallbacks=%s", cfg.mode, cfg.fallbacks or "none")
        return
    orch, monitor, intake, _ = build(cfg)
    orch.worker_id = worker_id
    worker = Worker(cfg, orch, monitor, intake)
    worker.once() if a.cmd == "once" else worker.run()


if __name__ == "__main__":
    main()
