"""Typed models shared by the monitor, the storage backends and the API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _utc(value: datetime) -> datetime:
    """MongoDB (and some drivers) hand back naive datetimes; they are always UTC here."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_utc)]

Status = Literal["operational", "degraded", "down", "unknown"]
ProbeKind = Literal["frontend", "availability", "functional"]
ErrorKind = Literal[
    "timeout",              # no response within the timeout
    "connection_refused",   # nothing listening
    "connection_error",     # DNS / TLS / other network failure before any response
    "http_error",           # non-2xx (for the functional probe: other than its documented 503)
    "invalid_response",     # 2xx but the body breaks the probe contract
    "probe_failed",         # functional probe answered and reported a business-rule failure
]
FailureType = Literal[
    "backend_unreachable", "backend_timeout", "availability_http_error",
    "frontend_unreachable", "frontend_timeout", "frontend_http_error",
    "functional_probe_failed", "functional_probe_timeout", "functional_probe_error",
    "monitor_error",
]
InvestigationRoute = Literal["service_health", "investigate"]


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ServiceConfig(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")
    name: str
    app: str = Field(default="", description="Key used by the OpsRelay agents (agents/apps.toml), if any.")
    description: str = ""
    frontend_url: str
    # A site added from the dashboard has only a page URL and, optionally, a health URL (backend_url, no path).
    backend_url: str = ""
    availability_path: str = ""
    functional_path: str = ""
    functional_method: Literal["GET", "POST"] = "GET"
    interval_seconds: int = Field(default=30, ge=1, le=3600)
    timeout_seconds: float = Field(default=5.0, gt=0, le=60, description="Frontend + availability request timeout.")
    functional_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    failure_threshold: int = Field(default=1, ge=1, le=10, description="Consecutive failing checks before an incident opens.")
    recovery_threshold: int = Field(default=1, ge=1, le=10, description="Consecutive operational checks before it resolves.")
    enabled: bool = True
    workspace_id: str = Field(default="demo", description="The workspace that owns this service.")

    @property
    def availability_url(self) -> str | None:
        return self.backend_url.rstrip("/") + self.availability_path if self.backend_url else None

    @property
    def functional_url(self) -> str | None:
        return self.backend_url.rstrip("/") + self.functional_path if self.backend_url and self.functional_path else None


class ProbeResult(Model):
    kind: ProbeKind
    url: str
    ok: bool
    http_status: int | None = None
    latency_ms: float | None = None
    error_kind: ErrorKind | None = None
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CheckRecord(Model):
    """One monitoring sample of one service. ``id`` is deterministic per scheduled slot,
    so a restarted worker can never record the same slot twice."""

    id: str
    service_id: str
    slot: UTCDateTime
    checked_at: UTCDateTime
    scheduled: bool = True
    status: Status
    failure_type: FailureType | None = None
    summary: str
    latency_ms: float | None = Field(default=None, description="Availability endpoint latency.")
    functional_latency_ms: float | None = None
    frontend: ProbeResult | None = None
    availability: ProbeResult | None = None
    functional: ProbeResult | None = None
    interval_seconds: int
    worker_id: str


class Sample(Model):
    """Lightweight projection of a check, used for uptime maths and charts."""

    checked_at: UTCDateTime
    status: Status
    latency_ms: float | None = None
    functional_latency_ms: float | None = None
    interval_seconds: int


class ServiceState(Model):
    service_id: str
    status: Status = "unknown"
    status_since: UTCDateTime | None = None
    summary: str = "No checks recorded yet."
    failure_type: FailureType | None = None
    last_checked_at: UTCDateTime | None = None
    last_check_id: str | None = None
    last_success_at: UTCDateTime | None = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    open_incident_id: str | None = None


class IncidentEvent(Model):
    at: UTCDateTime
    type: Literal["opened", "escalated", "deescalated", "failure_changed", "resolved", "acknowledged", "link_added",
                  "fix_confirmed", "fix_disputed", "fix_merged", "service_recovered", "relapsed"]
    status: Status | None = None
    message: str
    check_id: str | None = None


class IncidentLink(Model):
    kind: Literal["github_actions_run", "repair_pr", "other"]
    url: str
    label: str = ""
    source: str = "manual"


class Incident(Model):
    id: str
    service_id: str
    app: str
    state: Literal["open", "resolved"]
    severity: Literal["degraded", "down"]
    peak_severity: Literal["degraded", "down"]
    failure_type: FailureType | None
    investigation_route: InvestigationRoute
    title: str
    summary: str
    started_at: UTCDateTime
    last_failure_at: UTCDateTime
    resolved_at: UTCDateTime | None = None
    failed_check_count: int = 1
    first_check_id: str
    last_check_id: str
    recovery_check_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    events: list[IncidentEvent] = Field(default_factory=list)
    links: list[IncidentLink] = Field(default_factory=list)
    acknowledged_at: UTCDateTime | None = None
    acknowledged_by: str | None = None

    def elapsed_seconds(self, now: datetime) -> float:
        end = self.resolved_at or now
        return max(0.0, (end - self.started_at).total_seconds())


class WorkerState(Model):
    id: str = "uptime-monitor"
    owner: str | None = None
    lease_expires_at: UTCDateTime | None = None
    started_at: UTCDateTime | None = None
    heartbeat_at: UTCDateTime | None = None
    last_cycle_started_at: UTCDateTime | None = None
    last_cycle_finished_at: UTCDateTime | None = None
    last_cycle_duration_ms: float | None = None
    cycles_completed: int = 0
    skipped_slots: int = 0
    duplicate_slots: int = 0
    interval_seconds: int = 30
    check_requested_at: UTCDateTime | None = None
