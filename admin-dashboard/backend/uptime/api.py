"""Dashboard API. Every route except health and sign-in needs a session cookie, and data routes only
see the services and incidents of a workspace the signed-in account owns.

Every response is ``{"data": ..., "meta": {"generated_at", "storage"}}``; every error is
``{"error": {"code", "message"}}``. No secrets (MongoDB URI) are ever returned.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Generic, Literal, TypeVar

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__, accounts, actions, alerts, assistant, dependabot, netguard, notify, repositories, rollup, sentry, vercel, walkthrough
from .config import MonitorSettings, load_settings
from .dependencies import DependencyCheck, check_dependencies
from .handoff import build_handoff
from .models import CheckRecord, Incident, IncidentEvent, IncidentLink, ServiceConfig, ServiceState, Status, WorkerState
from .storage import open_storage
from .storage.base import Storage
from .uptime_calc import WINDOWS, compute_uptime, latency_series, needed_since, uptime_bars
from .worker import UptimeMonitor, utcnow

logger = logging.getLogger("opsrelay.uptime.api")
T = TypeVar("T")
SESSION_COOKIE = "opsrelay_session"
WORKSPACE_COOKIE = "opsrelay_workspace"
Window = Literal["24h", "7d", "30d"]


# ------------------------------------------------------------------ envelopes
class Meta(BaseModel):
    generated_at: datetime
    storage: dict[str, Any]
    version: str = __version__


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta


class CurrentStatus(BaseModel):
    status: Status
    summary: str
    since: datetime | None
    last_checked_at: datetime | None
    last_success_at: datetime | None
    stale: bool
    failure_type: str | None
    open_incident_id: str | None


class PublicServiceConfig(BaseModel):
    id: str
    name: str
    app: str
    description: str
    frontend_url: str
    backend_url: str
    availability_path: str
    functional_path: str
    functional_method: str
    interval_seconds: int
    timeout_seconds: float
    functional_timeout_seconds: float
    failure_threshold: int
    recovery_threshold: int
    enabled: bool


class ServiceOverview(BaseModel):
    id: str
    name: str
    app: str
    description: str
    current: CurrentStatus
    uptime: dict[str, Any]
    bars: list[dict[str, Any]] | None = None
    latest_check: CheckRecord | None = None


class IncidentOut(Incident):
    duration_seconds: float
    service_name: str
    related_ci: list[dict[str, Any]] = Field(default_factory=list)
    service_health: Status = "unknown"


class MonitoringHealth(BaseModel):
    status: Literal["running", "stale", "not_started"]
    message: str
    interval_seconds: int
    worker: WorkerState | None
    lease_held: bool
    next_cycle_expected_at: datetime | None
    total_checks: int
    storage: dict[str, Any]


class AgentAction(BaseModel):
    stage: str
    outcome: str
    at: datetime


class AgentActivity(BaseModel):
    incident_id: str
    service_name: str
    state: Literal["unavailable", "waiting", "working", "awaiting_review", "reported", "needs_attention", "finished"]
    stage: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None
    latest_action: AgentAction | None = None
    pr_url: str | None = None
    # After the agent's PR is merged, whether a person confirmed that it fixed the problem.
    fix_confirmation: Literal["confirmed", "disputed"] | None = None


class LearningStrategy(BaseModel):
    app: str
    action: str
    tries: int
    successes: float
    success_rate: float
    real_tries: int


class LearningCurvePoint(BaseModel):
    epoch: int
    incidents: str
    first_try_rate: float
    avg_attempts: float


class LearningTraining(BaseModel):
    at: datetime
    embedding_model: str | None = None
    accuracy_before: float
    accuracy_after: float
    per_bug: dict[str, float] = {}
    eval_incidents: int | None = None
    curve: list[LearningCurvePoint] = []
    mock_incidents: int | None = None
    epochs: int | None = None
    experiences: int | None = None
    real_experiences: int | None = None


class LearningDecision(BaseModel):
    incident_id: str
    app: str | None
    source: str
    status: str | None
    created_at: datetime | None
    action: str | None
    expected_success: float | None
    hinted: bool
    pr_url: str | None = None


class VectorIndex(BaseModel):
    collection: str
    name: str | None
    status: str | None
    queryable: bool


class Learning(BaseModel):
    available: bool
    totals: dict[str, int] = {}
    strategies: list[LearningStrategy] = []
    training: LearningTraining | None = None
    decisions: list[LearningDecision] = []
    vector_indexes: list[VectorIndex] = []


class Summary(BaseModel):
    overall: dict[str, Any]
    window: Window
    services: list[ServiceOverview]
    active_incidents: list[IncidentOut]
    recent_incidents: list[IncidentOut]
    monitoring: MonitoringHealth
    agent_activity: list[AgentActivity]
    repositories: list[dict[str, Any]] = []
    setup: dict[str, Any] = {}


class AgentSettingsIn(BaseModel):
    enabled: bool
    mode: Literal["diagnose", "report_only", "draft_pr"] = "report_only"
    app: str | None = Field(default=None, max_length=40)


class AckIn(BaseModel):
    by: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)


class LinkIn(BaseModel):
    kind: Literal["github_actions_run", "repair_pr", "other"]
    url: str = Field(pattern=r"^https://", max_length=500)
    label: str = Field(default="", max_length=120)
    source: str = Field(default="manual", max_length=60)


class ServicePatch(BaseModel):
    enabled: bool


class CredentialsIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    password: str = Field(min_length=8, max_length=200)


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class SiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(pattern=r"^https?://\S+$", max_length=500)
    health_url: str | None = Field(default=None, pattern=r"^https?://\S+$", max_length=500)


class GitHubConnectIn(BaseModel):
    token: str = Field(min_length=20, max_length=255)


class MonitorMappingIn(BaseModel):
    monitor_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")


class SentryProjectIn(BaseModel):
    project: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")


class VercelIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    team_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")


class VercelProjectIn(BaseModel):
    project: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,100}$")


class SentrySecretIn(BaseModel):
    client_secret: str = Field(min_length=1, max_length=200)


class RepositoryIn(BaseModel):
    repo: str = Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", max_length=140)
    service_name: str = Field(min_length=1, max_length=60)
    base_branch: str = Field(min_length=1, max_length=255)
    deployed_url: str = Field(pattern=r"^https?://[^\s]+$", max_length=500)
    workflow: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]+\.ya?ml$", max_length=200)


class FixConfirmationIn(BaseModel):
    resolved: bool


# -------------------------------------------------------------------- the app
def create_app(
    storage: Storage | None = None,
    settings: MonitorSettings | None = None,
    *,
    embed_worker: bool = False,
    clock=utcnow,
    monitor_kwargs: dict[str, Any] | None = None,
    assistant_transport: Any = None,
    github_transport: Any = None,
    notify_transport: Any = None,
    vercel_transport: Any = None,
) -> FastAPI:
    settings = settings or load_settings()
    storage = storage or open_storage(retention_days=settings.retention_days,
                                      incident_retention_days=settings.incident_retention_days)
    stop = asyncio.Event()
    # Without the embedded loop, cycles run on request: "check now", a new site, or the scheduled /cron/check call.
    # Every serverless instance then shares one worker id, so any of them can renew the lease.
    monitor = UptimeMonitor(storage, settings.services, interval_seconds=settings.interval_seconds,
                            lease_ttl_seconds=settings.lease_ttl, retention_days=settings.retention_days,
                            incident_retention_days=settings.incident_retention_days, clock=clock,
                            **{"worker_id": None if embed_worker else "api", **(monitor_kwargs or {})})

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(monitor.run_forever(stop)) if embed_worker else None
        yield
        stop.set()
        if task:
            await task

    app = FastAPI(title="OpsRelay Uptime API", version=__version__, lifespan=lifespan)
    app.state.storage = storage
    app.state.monitor = monitor
    dependency_lock = asyncio.Lock()
    dependency_cache: list[DependencyCheck] | None = None
    dependency_expires_at = 0.0
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o for o in os.environ.get("OPSRELAY_DASHBOARD_ORIGINS", "http://localhost:3300,http://127.0.0.1:3300").split(",") if o],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------- helpers
    def meta() -> dict[str, Any]:
        return {"generated_at": clock(), "storage": storage.describe(), "version": __version__}

    def ok(data: Any) -> dict[str, Any]:
        return {"data": data, "meta": meta()}

    def signed_in(request: Request) -> dict[str, Any]:
        user = accounts.session_user(storage, request.cookies.get(SESSION_COOKIE), clock())
        if user is None:
            raise HTTPException(401, "Sign in to view this dashboard.")
        return user

    def workspace(request: Request, user: dict[str, Any] = Depends(signed_in)) -> str:
        """The workspace this request reads, which the signed-in account must own (404 otherwise, so ids don't leak)."""
        requested = request.headers.get("x-opsrelay-workspace") or request.cookies.get(WORKSPACE_COOKIE)
        ws = accounts.owned_workspace(storage, user["id"], requested)
        if ws is None and not requested:
            raise HTTPException(409, "Create a workspace first.")
        if ws is None:
            raise HTTPException(404, f"Unknown workspace '{requested}'.")
        return ws["id"]

    # Per-instance sliding windows: they slow guessing and runaway costs down, but serverless instances
    # don't share them, so they are a brake rather than a hard quota.
    hits: dict[str, deque[float]] = defaultdict(deque)

    def limit(key: str, max_hits: int, per_seconds: int) -> None:
        now, window = time.monotonic(), hits[key]
        while window and window[0] <= now - per_seconds:
            window.popleft()
        if len(window) >= max_hits:
            retry = int(window[0] + per_seconds - now) + 1
            raise HTTPException(429, f"Too many requests. Try again in {retry}s.", headers={"Retry-After": str(retry)})
        window.append(now)

    def client_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "")
        return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")

    def start_session(response: JSONResponse, request: Request, user: dict[str, Any]) -> JSONResponse:
        response.set_cookie(SESSION_COOKIE, accounts.start_session(storage, user["id"], clock()),
                            max_age=accounts.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
        return response

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT",
                429: "RATE_LIMITED"}.get(exc.status_code, "ERROR")
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": code, "message": exc.detail}},
                            headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        message = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_FAILED", "message": message}})

    def services(ws: str) -> list[ServiceConfig]:
        return [s for s in storage.list_services() if s.workspace_id == ws]

    def service_or_404(service_id: str, ws: str) -> ServiceConfig:
        for s in services(ws):
            if s.id == service_id:
                return s
        raise HTTPException(404, f"Unknown service '{service_id}'.")

    def service_keys(ws: str) -> list[str]:
        """Monitored services plus mapped repositories (Better Stack alerts) and their per-source rollups."""
        mapped = [m["id"] for m in storage.find_docs("repos", "workspace_id", ws)]
        return [s.id for s in services(ws)] + mapped + [rollup.key(m, s) for m in mapped for s in rollup.SOURCES]

    def service_name(service_id: str) -> str:
        if site := storage.get_service(service_id):
            return site.name
        mapping = storage.get_doc("repos", service_id.split("#")[0]) if ":" in service_id else None
        return mapping["service_name"] if mapping else service_id

    def incidents(ws: str, *, state: str | None = None, limit: int = 50) -> list[Incident]:
        found = [i for key in service_keys(ws) for i in storage.list_incidents(service_id=key, state=state, limit=limit)]
        return sorted(found, key=lambda i: i.started_at, reverse=True)[:limit]

    def current(service: ServiceConfig, now: datetime) -> CurrentStatus:
        state = storage.get_state(service.id) or ServiceState(service_id=service.id)
        stale_after = timedelta(seconds=settings.stale_after_intervals * service.interval_seconds)
        if not service.enabled:
            status, summary, stale = "unknown", "Monitoring is paused for this service.", False
        elif state.last_checked_at is None:
            status, summary, stale = "unknown", "No checks recorded yet.", False
        elif now - state.last_checked_at > stale_after:
            age = int((now - state.last_checked_at).total_seconds())
            status, summary, stale = "unknown", f"Last check was {age}s ago; the monitoring worker may be stopped.", True
        else:
            status, summary, stale = state.status, state.summary, False
        return CurrentStatus(status=status, summary=summary, since=state.status_since, last_checked_at=state.last_checked_at,
                             last_success_at=state.last_success_at, stale=stale, failure_type=state.failure_type,
                             open_incident_id=state.open_incident_id)

    def uptime(service: ServiceConfig, window: str, now: datetime, *, bars: bool) -> tuple[dict, list | None]:
        samples = storage.list_samples(service.id, needed_since(window, now, service.interval_seconds), now)
        stats = compute_uptime(samples, window, now, storage.first_check_at(service.id))
        return stats, (uptime_bars(samples, window, now) if bars else None)

    def overview(service: ServiceConfig, window: str, now: datetime, *, bars: bool,
                 include_latest: bool = False) -> ServiceOverview:
        stats, b = uptime(service, window, now, bars=bars)
        checks = storage.list_checks(service.id, limit=1) if include_latest else []
        return ServiceOverview(id=service.id, name=service.name, app=service.app, description=service.description,
                               current=current(service, now), uptime=stats, bars=b, latest_check=checks[0] if checks else None)

    def incident_out(incident: Incident, now: datetime, *, related: bool = False) -> IncidentOut:
        related_ci = []
        if related:
            for doc in storage.find_agent_incidents(incident.app, incident.started_at, incident.resolved_at or now):
                related_ci.append({
                    "id": str(doc.get("_id")), "status": doc.get("status"), "stage": doc.get("stage"),
                    "run_url": doc.get("html_url"), "pr_url": (doc.get("pr") or {}).get("url"),
                    "commit_sha": doc.get("commit_sha"), "created_at": doc.get("created_at"),
                })
        state = storage.get_state(incident.service_id)
        return IncidentOut(**incident.model_dump(), duration_seconds=incident.elapsed_seconds(now),
                           service_name=service_name(incident.service_id), related_ci=related_ci,
                           service_health=alert_health(incident) or (state.status if state else "unknown"))

    def alert_health(incident: Incident) -> str | None:
        """For alert-only sources the open incident is the health signal: its severity while open."""
        if incident.evidence.get("source") not in ("betterstack", "sentry", "vercel"):
            return None
        current = storage.get_open_incident(incident.service_id)
        return current.severity if current else "operational"

    def repository_overview(ws: str) -> list[dict[str, Any]]:
        out = []
        for mapping in storage.find_docs("repos", "workspace_id", ws):
            open_incident = storage.get_open_incident(mapping["id"])
            out.append({**repositories.public_mapping(mapping), "open_incident_id": open_incident.id if open_incident else None,
                        "health": "down" if open_incident else "operational" if mapping.get("betterstack_monitor_id") else "unknown"})
        return out

    def agent_activity(incident: Incident, now: datetime) -> AgentActivity:
        name = service_name(incident.service_id)
        run = storage.get_agent_activity(incident.id)
        if run is None:
            state = "unavailable" if storage.describe()["backend"] != "mongodb" else "waiting"
            return AgentActivity(incident_id=incident.id, service_name=name, state=state)
        status = run.get("status")
        lease = run.get("lease_expires_at")
        if status == "open":
            state = "working" if lease and lease > now else "waiting"
        elif status == "awaiting_merge":
            state = "awaiting_review"
        elif status == "reported":
            state = "reported"
        elif status in ("needs_review", "failed", "rejected"):
            state = "needs_attention"
        elif status == "resolved":
            state = "finished"
        else:
            state = "waiting"
        pr_url = run.get("pr_url")
        return AgentActivity(incident_id=incident.id, service_name=name, state=state,
                             stage=run.get("stage"), started_at=run.get("created_at"),
                             updated_at=run.get("updated_at"), latest_action=run.get("latest_action"),
                             pr_url=pr_url if isinstance(pr_url, str) and pr_url.startswith("https://") else None,
                             fix_confirmation=fix_confirmation(incident))

    def fix_confirmation(incident: Incident) -> str | None:
        answers = [e.type for e in incident.events if e.type in ("fix_confirmed", "fix_disputed")]
        return None if not answers else "confirmed" if answers[-1] == "fix_confirmed" else "disputed"

    def assistant_context(focus: str | None, now: datetime, ws: str) -> dict[str, Any]:
        """What the dashboard assistant may know: status, open incidents and the agent's findings."""
        open_incidents = incidents(ws, state="open")
        if focus and not any(i.id == focus for i in open_incidents):
            open_incidents.append(incident_or_404(focus, ws))
        return {
            "now": now,
            "services": [{"name": o.name, "status": o.current.status, "summary": o.current.summary}
                         for o in (overview(s, "24h", now, bars=False) for s in services(ws))],
            "viewing_incident": focus,
            "incidents": [{
                "id": i.id, "app": service_name(i.service_id), "state": i.state, "severity": i.severity,
                "title": i.title, "started_at": i.started_at, "resolved_at": i.resolved_at,
                "failed_checks": assistant.plain_failures(i.evidence),
                "dependabot_alerts": i.evidence.get("alerts") if i.evidence.get("source") == "dependabot" else None,
                "fix_confirmation_by_person": fix_confirmation(i),
                "repair_agent": storage.get_agent_findings(i.id),
            } for i in open_incidents],
        }

    def monitoring(now: datetime, ws: str) -> MonitoringHealth:
        worker = storage.get_worker()
        interval = settings.interval_seconds
        lease_held = bool(worker and worker.owner and worker.lease_expires_at and worker.lease_expires_at > now)
        last = worker.last_cycle_started_at if worker else None
        if last is None:
            status, message = "not_started", "The monitoring worker has not completed a cycle yet."
        elif now - last > timedelta(seconds=settings.stale_after_intervals * interval):
            status, message = "stale", f"No cycle for {int((now - last).total_seconds())}s; check the scheduled GitHub Actions run (uptime-checks) or start python -m uptime worker."
        else:
            status, message = "running", f"Checking {len(services(ws))} services every {interval}s."
        return MonitoringHealth(status=status, message=message, interval_seconds=interval, worker=worker,
                                lease_held=lease_held, next_cycle_expected_at=(last + timedelta(seconds=interval)) if last else None,
                                total_checks=sum(storage.count_checks(s.id) for s in services(ws)), storage=storage.describe())

    # ------------------------------------------------------------- routes
    @app.get("/api/v1/health", response_model=Envelope[dict[str, Any]])
    def api_health():
        return ok({"status": "ok", "service": "opsrelay-uptime-api"})

    def account(user: dict[str, Any], request: Request) -> dict[str, Any]:
        owned = accounts.workspaces(storage, user["id"])
        current = accounts.owned_workspace(storage, user["id"], request.cookies.get(WORKSPACE_COOKIE))
        return {"user": user, "workspaces": [{"id": w["id"], "name": w["name"]} for w in owned],
                "workspace_id": current["id"] if current else None}

    @app.post("/api/v1/auth/sign-up", response_model=Envelope[dict[str, Any]])
    def sign_up(body: CredentialsIn, request: Request):
        limit(f"sign-up:{client_ip(request)}", 5, 3600)
        try:
            user = accounts.sign_up(storage, body.email, body.password, clock())
        except accounts.AccountError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return start_session(JSONResponse(jsonable_encoder(ok(account(user, request)))), request, user)

    @app.post("/api/v1/auth/sign-in", response_model=Envelope[dict[str, Any]])
    def sign_in(body: CredentialsIn, request: Request):
        limit(f"sign-in:{client_ip(request)}", 10, 60)
        limit(f"sign-in:{body.email.lower()}", 10, 900)
        try:
            user = accounts.sign_in(storage, body.email, body.password)
        except accounts.AccountError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return start_session(JSONResponse(jsonable_encoder(ok(account(user, request)))), request, user)

    @app.post("/api/v1/auth/sign-out", response_model=Envelope[dict[str, Any]])
    def sign_out(request: Request):
        if token := request.cookies.get(SESSION_COOKIE):
            accounts.end_session(storage, token)
        response = JSONResponse(jsonable_encoder(ok({"signed_out": True})))
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(WORKSPACE_COOKIE)
        return response

    @app.get("/api/v1/auth/me", response_model=Envelope[dict[str, Any]])
    def me(request: Request, user: dict[str, Any] = Depends(signed_in)):
        return ok(account(user, request))

    async def github_call(coro):
        try:
            return await coro
        except repositories.RepoError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/api/v1/github", response_model=Envelope[dict[str, Any]])
    def github_status(ws: str = Depends(workspace)):
        return ok(repositories.public_connection(repositories.connection(storage, ws)))

    @app.put("/api/v1/github", response_model=Envelope[dict[str, Any]])
    async def github_connect(body: GitHubConnectIn, ws: str = Depends(workspace)):
        return ok(await github_call(repositories.connect(storage, ws, body.token, clock(), github_transport)))

    @app.delete("/api/v1/github", response_model=Envelope[dict[str, Any]])
    def github_disconnect(ws: str = Depends(workspace)):
        storage.delete_doc("github", ws)
        return ok({"connected": False})

    @app.get("/api/v1/github/repos", response_model=Envelope[list[dict[str, Any]]])
    async def github_repos(ws: str = Depends(workspace)):
        async def run():
            return await repositories.list_repos(repositories.client(storage, ws, github_transport))
        return ok(await github_call(run()))

    @app.get("/api/v1/github/repos/{owner}/{name}", response_model=Envelope[dict[str, Any]])
    async def github_repo_options(owner: str, name: str, ws: str = Depends(workspace)):
        async def run():
            return await repositories.repo_options(repositories.client(storage, ws, github_transport), f"{owner}/{name}")
        return ok(await github_call(run()))

    @app.get("/api/v1/repositories", response_model=Envelope[list[dict[str, Any]]])
    def list_repositories(ws: str = Depends(workspace)):
        return ok(repositories.mappings(storage, ws))

    @app.post("/api/v1/repositories", response_model=Envelope[dict[str, Any]])
    async def add_repository(body: RepositoryIn, ws: str = Depends(workspace)):
        async def run():
            gh = repositories.client(storage, ws, github_transport)
            return await repositories.add_mapping(storage, ws, gh, body.model_dump(), clock())
        return ok(await github_call(run()))

    @app.delete("/api/v1/repositories/{service_id}", response_model=Envelope[dict[str, Any]])
    def remove_repository(service_id: str, ws: str = Depends(workspace)):
        try:
            repositories.remove_mapping(storage, ws, service_id)
        except repositories.RepoError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return ok({"removed": service_id})

    @app.get("/api/v1/agent/config", response_model=Envelope[dict[str, Any]], dependencies=[Depends(signed_in)])
    def agent_config():
        return ok(repositories.agent_config())

    @app.put("/api/v1/repositories/{service_id}/agent", response_model=Envelope[dict[str, Any]])
    def set_repository_agent(service_id: str, body: AgentSettingsIn, ws: str = Depends(workspace)):
        try:
            return ok(repositories.set_agent(storage, ws, service_id, body.model_dump()))
        except repositories.RepoError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/api/v1/betterstack", response_model=Envelope[dict[str, Any]])
    def betterstack_settings(ws: str = Depends(workspace)):
        return ok(alerts.settings(storage, ws))

    @app.post("/api/v1/betterstack", response_model=Envelope[dict[str, Any]])
    def betterstack_rotate(ws: str = Depends(workspace)):
        """Create or replace this workspace's webhook URL and secret (the secret is only returned here)."""
        return ok(alerts.rotate(storage, ws, clock()))

    @app.put("/api/v1/repositories/{service_id}/betterstack", response_model=Envelope[dict[str, Any]])
    def map_betterstack_monitor(service_id: str, body: MonitorMappingIn, ws: str = Depends(workspace)):
        try:
            return ok(alerts.map_monitor(storage, ws, service_id, body.monitor_id))
        except alerts.AlertError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/api/v1/vercel", response_model=Envelope[dict[str, Any]])
    def vercel_connection(ws: str = Depends(workspace)):
        return ok(vercel.connection(storage, ws))

    @app.put("/api/v1/vercel", response_model=Envelope[dict[str, Any]])
    async def vercel_connect(body: VercelIn, ws: str = Depends(workspace)):
        try:
            return ok(await vercel.connect(storage, ws, body.token, body.team_id, clock(), vercel_transport))
        except vercel.VercelError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.delete("/api/v1/vercel", response_model=Envelope[dict[str, Any]])
    def vercel_disconnect(ws: str = Depends(workspace)):
        vercel.disconnect(storage, ws)
        return ok({"connected": False})

    @app.put("/api/v1/repositories/{service_id}/vercel", response_model=Envelope[dict[str, Any]])
    def map_vercel_project(service_id: str, body: VercelProjectIn, ws: str = Depends(workspace)):
        try:
            return ok(vercel.map_project(storage, ws, service_id, body.project))
        except vercel.VercelError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/api/v1/sentry", response_model=Envelope[dict[str, Any]])
    def sentry_settings(ws: str = Depends(workspace)):
        return ok(sentry.settings(storage, ws))

    @app.post("/api/v1/sentry", response_model=Envelope[dict[str, Any]])
    def sentry_create(ws: str = Depends(workspace)):
        return ok(sentry.create(storage, ws, clock()))

    @app.put("/api/v1/sentry/secret", response_model=Envelope[dict[str, Any]])
    def sentry_secret(body: SentrySecretIn, ws: str = Depends(workspace)):
        try:
            return ok(sentry.set_secret(storage, ws, body.client_secret))
        except sentry.SentryError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.put("/api/v1/repositories/{service_id}/sentry", response_model=Envelope[dict[str, Any]])
    def map_sentry_project(service_id: str, body: SentryProjectIn, ws: str = Depends(workspace)):
        try:
            return ok(sentry.map_project(storage, ws, service_id, body.project))
        except sentry.SentryError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/api/v1/webhooks/sentry/{hook_id}", response_model=Envelope[dict[str, Any]])
    async def sentry_webhook(hook_id: str, request: Request):
        """Called by Sentry: authenticated by its HMAC signature of the raw body."""
        try:
            result = sentry.receive(storage, hook_id, request.headers.get(sentry.SIGNATURE_HEADER),
                                    request.headers.get("sentry-hook-resource"), await request.body(), clock())
        except sentry.SentryError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        logger.info("Sentry webhook %s: %s", hook_id[:6], result)
        return ok(result)

    @app.get("/api/v1/notifications", response_model=Envelope[list[dict[str, Any]]])
    def notification_settings(ws: str = Depends(workspace)):
        return ok(notify.settings(storage, ws))

    @app.put("/api/v1/notifications/{kind}", response_model=Envelope[dict[str, Any]])
    def save_notification(kind: str, body: dict[str, Any], ws: str = Depends(workspace)):
        try:
            return ok(notify.save(storage, ws, kind, body, clock()))
        except notify.NotifyError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/api/v1/notifications/{kind}/test", response_model=Envelope[dict[str, Any]])
    async def test_notification(kind: str, request: Request, ws: str = Depends(workspace)):
        limit(f"notify-test:{ws}", 10, 3600)
        try:
            return ok(await notify.test(storage, ws, kind, str(request.base_url), clock(), notify_transport))
        except notify.NotifyError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.delete("/api/v1/notifications/{kind}", response_model=Envelope[dict[str, Any]])
    def remove_notification(kind: str, ws: str = Depends(workspace)):
        try:
            notify.remove(storage, ws, kind)
        except notify.NotifyError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return ok({"kind": kind, "removed": True})

    @app.post("/api/v1/webhooks/betterstack/{hook_id}", response_model=Envelope[dict[str, Any]])
    async def betterstack_webhook(hook_id: str, request: Request):
        """Called by Better Stack, not the dashboard: authenticated by the per-workspace secret header."""
        try:
            payload = await request.json()
        except ValueError:  # empty or non-JSON body: let the secret and payload checks reject it
            payload = {}
        try:
            result = alerts.receive(storage, hook_id, request.headers.get(alerts.SECRET_HEADER),
                                    payload if isinstance(payload, dict) else {}, clock())
        except alerts.AlertError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        logger.info("Better Stack webhook %s: %s", hook_id[:6], result)
        return ok(result)

    @app.post("/api/v1/workspaces", response_model=Envelope[dict[str, Any]])
    def create_workspace(body: WorkspaceIn, user: dict[str, Any] = Depends(signed_in)):
        ws = accounts.create_workspace(storage, user["id"], body.name, clock())
        return ok({"id": ws["id"], "name": ws["name"]})

    @app.get("/api/v1/monitoring", response_model=Envelope[MonitoringHealth])
    def get_monitoring(ws: str = Depends(workspace)):
        return ok(monitoring(clock(), ws))

    @app.get("/api/v1/dependencies", response_model=Envelope[list[DependencyCheck]], dependencies=[Depends(signed_in)])
    async def get_dependencies():
        nonlocal dependency_cache, dependency_expires_at
        if dependency_cache is None or time.monotonic() >= dependency_expires_at:
            async with dependency_lock:
                if dependency_cache is None or time.monotonic() >= dependency_expires_at:
                    dependency_cache = await check_dependencies()
                    dependency_expires_at = time.monotonic() + 30
        return ok(dependency_cache)

    @app.get("/api/v1/summary", response_model=Envelope[Summary])
    def summary(window: Window = "24h", ws: str = Depends(workspace)):
        now = clock()
        overviews = [overview(s, window, now, bars=True, include_latest=True) for s in services(ws)]
        statuses = [o.current.status for o in overviews]
        if "down" in statuses:
            overall = {"status": "down", "label": "Major outage"}
        elif "degraded" in statuses:
            overall = {"status": "degraded", "label": "Partial degradation"}
        elif statuses and all(s == "operational" for s in statuses):
            overall = {"status": "operational", "label": "All systems operational"}
        else:
            overall = {"status": "unknown", "label": "Monitoring data incomplete"}
        affected = [o.name for o in overviews if o.current.status in ("down", "degraded")]
        overall["affected_services"] = affected
        open_incidents = incidents(ws, state="open")
        active = [incident_out(i, now) for i in open_incidents]
        recent = [incident_out(i, now) for i in incidents(ws, state="resolved", limit=10)]
        return ok(Summary(overall=overall, window=window, services=overviews, active_incidents=active,
                          recent_incidents=recent, monitoring=monitoring(now, ws),
                          agent_activity=[agent_activity(i, now) for i in open_incidents],
                          repositories=repository_overview(ws),
                          setup={"workspace_id": ws, "github_connected": repositories.connection(storage, ws) is not None,
                                 "betterstack_configured": storage.get_doc("betterstack", ws) is not None,
                                 "notifications_configured": bool(storage.find_docs("notify", "workspace_id", ws))}))

    @app.get("/api/v1/learning", response_model=Envelope[Learning])
    def learning(ws: str = Depends(workspace)):
        """What the RL fix policy has learned, from rl_experiences / rl_training_runs (read-only)."""
        apps = {s.app for s in services(ws)}
        data = storage.get_learning_summary() if apps else None
        if not data:
            return ok(Learning(available=False))
        decisions = [{**d, "pr_url": d["pr_url"] if isinstance(d.get("pr_url"), str)
                      and d["pr_url"].startswith("https://") else None} for d in data["decisions"] if d.get("app") in apps]
        data["strategies"] = [st for st in data["strategies"] if st.get("app") in apps]
        return ok(Learning(available=True, **{**data, "decisions": decisions}))

    @app.get("/api/v1/services", response_model=Envelope[list[ServiceOverview]])
    def list_services(window: Window = "24h", ws: str = Depends(workspace)):
        now = clock()
        return ok([overview(s, window, now, bars=False) for s in services(ws)])

    @app.get("/api/v1/services/{service_id}", response_model=Envelope[dict[str, Any]])
    def service_detail(service_id: str, ws: str = Depends(workspace)):
        now = clock()
        service = service_or_404(service_id, ws)
        windows = {w: uptime(service, w, now, bars=False)[0] for w in WINDOWS}
        incidents = storage.list_incidents(service_id=service_id, limit=20)
        return ok({
            "service": PublicServiceConfig(**service.model_dump()).model_dump(),
            "current": current(service, now).model_dump(),
            "uptime": windows,
            "incidents": [incident_out(i, now).model_dump() for i in incidents],
            "incident_ids": [i.id for i in incidents],
        })

    @app.get("/api/v1/services/{service_id}/uptime", response_model=Envelope[dict[str, Any]])
    def service_uptime(service_id: str, window: Window = "24h", ws: str = Depends(workspace)):
        now = clock()
        stats, bars = uptime(service_or_404(service_id, ws), window, now, bars=True)
        return ok({"stats": stats, "bars": bars})

    @app.get("/api/v1/services/{service_id}/checks", response_model=Envelope[list[dict[str, Any]]])
    def service_checks(service_id: str, limit: int = Query(default=30, ge=1, le=500), before: datetime | None = None,
                       ws: str = Depends(workspace)):
        service_or_404(service_id, ws)
        return ok([c.model_dump() for c in storage.list_checks(service_id, limit=limit, before=before)])

    @app.get("/api/v1/services/{service_id}/latency", response_model=Envelope[list[dict[str, Any]]])
    def service_latency(service_id: str, window: Window = "24h", points: int = Query(default=120, ge=10, le=500),
                        ws: str = Depends(workspace)):
        now = clock()
        service = service_or_404(service_id, ws)
        samples = storage.list_samples(service_id, needed_since(window, now, service.interval_seconds), now)
        return ok(latency_series(samples, window, now, points))

    @app.get("/api/v1/incidents", response_model=Envelope[list[IncidentOut]])
    def list_incidents(state: Literal["open", "resolved", "all"] = "all", service_id: str | None = None,
                       limit: int = Query(default=50, ge=1, le=200), ws: str = Depends(workspace)):
        now = clock()
        incident_state = None if state == "all" else state
        if service_id is None:
            items = incidents(ws, state=incident_state, limit=limit)
        else:
            service_or_404(service_id, ws)
            items = storage.list_incidents(service_id=service_id, state=incident_state, limit=limit)
        return ok([incident_out(i, now) for i in items])

    @app.get("/api/v1/incidents/{incident_id}/walkthrough", response_model=Envelope[dict[str, Any]])
    def incident_walkthrough(incident_id: str, ws: str = Depends(workspace)):
        """What the monitor and the repair agent recorded, as ordered steps."""
        incident = incident_or_404(incident_id, ws)
        return ok(walkthrough.build(incident, storage.get_agent_run(incident_id), clock()))

    def incident_or_404(incident_id: str, ws: str) -> Incident:
        incident = storage.get_incident(incident_id)
        if incident is None or incident.service_id not in service_keys(ws):
            raise HTTPException(404, f"Unknown incident '{incident_id}'.")
        return incident

    @app.get("/api/v1/incidents/{incident_id}", response_model=Envelope[IncidentOut])
    def get_incident(incident_id: str, ws: str = Depends(workspace)):
        return ok(incident_out(incident_or_404(incident_id, ws), clock(), related=True))

    @app.get("/api/v1/incidents/{incident_id}/evidence", response_model=Envelope[dict[str, Any]])
    def incident_evidence(incident_id: str, ws: str = Depends(workspace)):
        incident = incident_or_404(incident_id, ws)
        service = next((s for s in services(ws) if s.id == incident.service_id), None)
        return ok(build_handoff(incident, service, clock()))

    @app.get("/api/v1/incidents/{incident_id}/diagnosis", response_model=Envelope[dict[str, Any] | None])
    def incident_diagnosis(incident_id: str, ws: str = Depends(workspace)):
        """The agent worker's diagnosis (diagnose mode), or null until it has one."""
        incident_or_404(incident_id, ws)
        return ok(storage.get_doc("diagnoses", incident_id))

    # ---------------------------------------------------------- mutations
    @app.get("/api/v1/sites", response_model=Envelope[list[dict[str, Any]]])
    def list_sites(ws: str = Depends(workspace)):
        return ok([{"id": s.id, "name": s.name, "url": s.frontend_url, "health_url": s.availability_url} for s in services(ws)])

    @app.post("/api/v1/sites", response_model=Envelope[dict[str, Any]])
    async def add_site(body: SiteIn, ws: str = Depends(workspace)):
        limit(f"add-site:{ws}", 20, 3600)
        for url in filter(None, (body.url, body.health_url)):
            if reason := await asyncio.to_thread(netguard.check_url, url):
                raise HTTPException(422, f"Sites must be on the public internet: {reason}")
        site = ServiceConfig(id=f"site-{secrets.token_hex(5)}", name=body.name.strip(), frontend_url=body.url,
                             backend_url=body.health_url or "", interval_seconds=settings.interval_seconds, workspace_id=ws)
        storage.upsert_service(site)
        await monitor.run_cycle(scheduled=False, workspace_id=ws)  # a status now, not at the next scheduled check
        return ok({"id": site.id})

    @app.delete("/api/v1/sites/{service_id}", response_model=Envelope[dict[str, Any]])
    def remove_site(service_id: str, ws: str = Depends(workspace)):
        service_or_404(service_id, ws)
        storage.delete_service(service_id)
        return ok({"id": service_id, "removed": True})

    @app.post("/api/v1/cron/check", response_model=Envelope[dict[str, Any]])
    async def scheduled_check(request: Request):
        """One check cycle for every workspace, for a scheduler (GitHub Actions) where no worker runs."""
        secret = os.environ.get("OPSRELAY_CRON_SECRET", "")
        given = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
            raise HTTPException(401, "Missing or wrong cron secret.")
        if not storage.acquire_lease(monitor.worker_id, clock(), monitor.lease_ttl):
            raise HTTPException(409, "Another monitoring worker holds the lease.")
        result = await monitor.run_cycle()
        if result is None:
            raise HTTPException(409, "A monitoring cycle is already running.")
        synced = [await dependabot.sync(storage, m, clock(), github_transport) for m in storage.all_docs("repos")]
        workflows = [await actions.sync(storage, m, clock(), github_transport) for m in storage.all_docs("repos")]
        deploys = [await vercel.sync(storage, m, clock(), vercel_transport)
                   for m in storage.all_docs("repos") if m.get("vercel_project")]
        notified = {"sent": 0, "failed": 0}
        for ws in notify.workspaces(storage):
            counts = await notify.dispatch(storage, ws, incidents(ws), service_name, str(request.base_url), clock(), notify_transport)
            notified = {k: notified[k] + counts[k] for k in notified}
        return ok({"checked": len(result["statuses"]), "recorded": len(result["recorded"]), "duplicates": result["duplicates"],
                   "dependabot": {r["result"]: sum(x["result"] == r["result"] for x in synced) for r in synced},
                   "vercel": {r["result"]: sum(x["result"] == r["result"] for x in deploys) for r in deploys},
                   "actions": {r["result"]: sum(x["result"] == r["result"] for x in workflows) for r in workflows},
                   "notifications": notified})

    @app.patch("/api/v1/services/{service_id}", response_model=Envelope[PublicServiceConfig])
    def patch_service(service_id: str, body: ServicePatch, ws: str = Depends(workspace)):
        service_or_404(service_id, ws)
        updated = storage.set_service_enabled(service_id, body.enabled)
        return ok(PublicServiceConfig(**updated.model_dump()))

    @app.post("/api/v1/monitor/check-now", response_model=Envelope[dict[str, Any]])
    async def check_now(ws: str = Depends(workspace)):
        limit(f"check-now:{ws}", 6, 60)
        result = await monitor.run_cycle(scheduled=False, workspace_id=ws)
        if result is None:
            raise HTTPException(409, "A monitoring cycle is already running.")
        return ok({"ran": True, "statuses": result["statuses"], "recorded": result["recorded"]})

    @app.post("/api/v1/incidents/{incident_id}/acknowledge", response_model=Envelope[IncidentOut])
    def acknowledge(incident_id: str, body: AckIn, ws: str = Depends(workspace)):
        incident = incident_or_404(incident_id, ws)
        now = clock()
        incident.acknowledged_at, incident.acknowledged_by = now, body.by
        incident.events.append(IncidentEvent(at=now, type="acknowledged", message=f"Acknowledged by {body.by}. {body.note}".strip()))
        storage.save_incident(incident)
        return ok(incident_out(incident, now))

    @app.post("/api/v1/incidents/{incident_id}/fix-confirmation", response_model=Envelope[IncidentOut])
    def confirm_fix(incident_id: str, body: FixConfirmationIn, ws: str = Depends(workspace)):
        """A person says whether the agent's merged PR fixed the problem.

        Yes asks for a fresh check, which resolves the incident once the site passes. No records it
        and leaves the incident open.
        """
        incident = incident_or_404(incident_id, ws)
        findings = storage.get_agent_findings(incident_id)
        if not findings or findings.get("status") != "resolved":
            raise HTTPException(409, "The repair PR for this incident has not been merged yet.")
        now = clock()
        incident.events.append(IncidentEvent(
            at=now, type="fix_confirmed" if body.resolved else "fix_disputed",
            message="A person confirmed the merged fix resolved the problem."
            if body.resolved else "A person reported the problem is still happening after the fix was merged."))
        storage.save_incident(incident)
        storage.request_check(now)
        return ok(incident_out(incident, now))

    @app.post("/api/v1/assistant/chat", response_model=Envelope[dict[str, Any]])
    async def assistant_chat(body: assistant.ChatIn, ws: str = Depends(workspace)):
        limit(f"assistant:{ws}", 30, 3600)  # each message is a paid model call
        try:
            reply = await assistant.answer(assistant_context(body.incident_id, clock(), ws), body.messages,
                                           transport=assistant_transport)
        except assistant.AssistantError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return ok({"reply": reply})

    @app.post("/api/v1/incidents/{incident_id}/links", response_model=Envelope[IncidentOut])
    def add_link(incident_id: str, body: LinkIn, ws: str = Depends(workspace)):
        incident = incident_or_404(incident_id, ws)
        now = clock()
        if not any(link.url == body.url for link in incident.links):
            incident.links.append(IncidentLink(**body.model_dump()))
            incident.events.append(IncidentEvent(at=now, type="link_added", message=f"{body.kind}: {body.url}"))
            storage.save_incident(incident)
        return ok(incident_out(incident, now))

    return app
