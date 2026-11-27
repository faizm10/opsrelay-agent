"""One open incident per mapped repository and source, listing that source's current problems.

Dependabot alerts, unresolved Sentry issues, a failed Vercel production deploy and open pull requests and
issues all work the same way: the source reports the full list of what's wrong now, the repository's incident for that source
opens with the first item, is updated as items come and go, and resolves once the list is empty.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Callable

from .models import Incident, IncidentEvent
from .storage.base import Storage

SOURCES = ("dependabot", "sentry", "vercel", "actions", "backlog")


def key(mapping_id: str, source: str) -> str:
    return f"{mapping_id}#{source}"


def apply(storage: Storage, mapping: dict, source: str, items: list[dict], now: datetime, *, label: str,
          noun: str, cleared: str, severity: Callable[[list[dict]], str], describe: Callable[[dict], str],
          field: str = "items") -> dict:
    """``items`` each have an ``id``; they are stored as ``evidence[field]``."""
    service = key(mapping["id"], source)
    incident = storage.get_open_incident(service)
    if not items:
        if incident is None:
            return {"result": "clear", "open": 0}
        incident.state, incident.resolved_at = "resolved", now
        incident.events.append(IncidentEvent(at=now, type="resolved", status="operational", message=cleared))
        storage.save_incident(incident)
        return {"result": "resolved", "open": 0, "incident_id": incident.id}

    level, n = severity(items), len(items)
    title = f"{mapping['service_name']}: {n} {noun}{'' if n == 1 else 's'}"
    summary = "; ".join(describe(i) for i in items[:5]) + (f"; {n - 5} more" if n > 5 else "")
    evidence = {"source": source, "repo": mapping["repo"], field: items}
    if incident is None:
        incident = Incident(
            id=f"{source[:3].upper()}-{hashlib.sha256(service.encode()).hexdigest()[:8]}-{int(now.timestamp())}",
            service_id=service, app=mapping["repo"], state="open", severity=level, peak_severity=level,
            failure_type=None, investigation_route="investigate", title=title, summary=summary, started_at=now,
            last_failure_at=now, first_check_id=f"{source}:{service}", last_check_id=f"{source}:{service}",
            evidence=evidence, events=[IncidentEvent(at=now, type="opened", status=level, message=f"{label}: {summary}")])
        storage.create_incident(incident)
        return {"result": "created", "open": n, "incident_id": incident.id}

    if [i["id"] for i in incident.evidence.get(field, [])] != [i["id"] for i in items]:
        incident.events.append(IncidentEvent(at=now, type="failure_changed", status=level, message=f"{label}: {summary}"))
    incident.title, incident.summary, incident.severity, incident.evidence = title, summary, level, evidence
    incident.peak_severity = "down" if "down" in (incident.peak_severity, level) else level
    incident.last_failure_at = now
    storage.save_incident(incident)
    return {"result": "updated", "open": n, "incident_id": incident.id}
