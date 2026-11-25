"""Hand-off from uptime incidents to the OpsRelay AI investigation pipeline.

The monitor does NOT call the agents itself, and it does not replace the GitHub-CI monitor or the
orchestrator. It produces a structured, versioned evidence document per incident
(``GET /api/v1/incidents/{id}/evidence``) that the existing pipeline can consume:

* ``investigation_route`` uses the same names as ``agents/live_incident.py``:
  ``service_health`` (unreachable: process/deploy/network, *no code repair*) or
  ``investigate`` (reachable but a business-rule probe fails: compare with recent changes/CI).
* ``live_check`` is shaped like ``agents.contracts.LiveCheckResult`` where the concepts line up
  (service, app, checked_at, base_url, target, reachable, healthy, failure_kind, error,
  http_status). ``delivery`` is always ``not_attempted``, because these probes are not the
  messaging send-and-readback probe. See README "Integration" for what Developer 2 must map.
* ``probe_failures`` carries the apps' own failing checks, including their ``source`` hints
  (e.g. ``backend/app/pricing.py::compute_discount``), as candidate suspects, not proof.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import Incident, ServiceConfig

SCHEMA = "opsrelay.uptime_incident.v1"

# agents.contracts.FailureKind values for the parts that map 1:1
_FAILURE_KIND = {
    "timeout": "connection_timeout",
    "connection_refused": "connection_refused",
    "connection_error": "connection_error",
    "http_error": "http_error",
    "invalid_response": "invalid_response",
}

NEXT_STEPS = {
    "service_health": [
        "Check whether the service process/container is running and when it last restarted.",
        "Check the deployment and recent infrastructure changes.",
        "Read the service's startup and error logs around started_at.",
        "Check network/host health between the monitor and the service.",
    ],
    "investigate": [
        "Read the failing probe checks below; they name the business rule and a source hint.",
        "Compare with the latest commits and the app's CI status.",
        "Reproduce with the app's pytest suite before proposing any patch (agents verifier).",
    ],
}


def build_handoff(incident: Incident, service: ServiceConfig | None, now: datetime) -> dict[str, Any]:
    first = incident.evidence.get("first_failure", {})
    latest = incident.evidence.get("latest_failure", {})
    probes = latest.get("probes", {})
    availability = probes.get("availability") or {}
    functional = probes.get("functional") or {}
    failures = (functional.get("details") or {}).get("failures") or []
    failing_probe = next((p for p in (availability, probes.get("frontend") or {}, functional) if p and not p.get("ok")), {})

    return {
        "schema": SCHEMA,
        "incident_id": incident.id,
        "service_id": incident.service_id,
        "app": incident.app,
        "state": incident.state,
        "severity": incident.severity,
        "peak_severity": incident.peak_severity,
        "failure_type": incident.failure_type,
        "investigation_route": incident.investigation_route,
        "route_reason": incident.evidence.get("route_reason"),
        "code_repair_candidate": incident.investigation_route == "investigate" and bool(failures),
        "started_at": incident.started_at,
        "resolved_at": incident.resolved_at,
        "duration_seconds": incident.elapsed_seconds(now),
        "failed_check_count": incident.failed_check_count,
        "first_check_id": incident.first_check_id,
        "last_check_id": incident.last_check_id,
        "probe_failures": failures,
        "suspect_sources": sorted({f["source"] for f in failures if f.get("source")}),
        "live_check": {
            "service": incident.service_id,
            "app": incident.app,
            "checked_at": latest.get("checked_at"),
            "base_url": service.backend_url if service else None,
            "target": "live",
            "reachable": availability.get("http_status") is not None,
            "healthy": bool(availability.get("ok")),
            "delivery": "not_attempted",
            "failure_kind": _FAILURE_KIND.get(failing_probe.get("error_kind") or ""),
            "error": failing_probe.get("error"),
            "http_status": failing_probe.get("http_status"),
        },
        "evidence": {"first_failure": first, "latest_failure": latest},
        "links": [link.model_dump() for link in incident.links],
        "next_steps": NEXT_STEPS[incident.investigation_route],
    }
