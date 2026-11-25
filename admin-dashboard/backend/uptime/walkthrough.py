"""One incident as an ordered list of steps, from what the monitor and the repair agent
recorded. A step with no record is waiting or skipped; nothing is inferred."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import Incident

# Seconds a step may run before it counts as slow. The human merge has no limit.
EXPECTED = {"similar": 120, "investigated": 600, "pr": 120, "fresh": 300}
AGENT_STEPS = {"similar": "memory_retrieved", "investigated": "patch_verified", "pr": "pr_confirmed"}


def _last(steps: list[dict], stage: str, outcome: str | None = None) -> dict | None:
    found = [s for s in steps if s["stage"] == stage and (outcome is None or s["outcome"] == outcome)]
    return found[-1] if found else None


def build(incident: Incident, run: dict | None, now: datetime) -> dict[str, Any]:
    agent = (run or {}).get("incident") or {}
    steps = (run or {}).get("steps") or []
    status = agent.get("status")
    verified = {"verification": agent.get("verification")}
    out: list[dict[str, Any]] = []

    def add(key, label, state, at=None, summary="", details=None, started=None):
        out.append({"key": key, "label": label, "state": state, "ended_at": at, "started_at": started,
                    "summary": summary, "details": details or {}})

    add("detected", "Detected", "done", incident.started_at, incident.title,
        {"failure_type": incident.failure_type, "route": incident.investigation_route,
         "failed_checks": incident.failed_check_count})

    no_agent = "skipped" if incident.investigation_route == "service_health" else "waiting"
    no_agent_text = ("Service health problem: no code repair" if no_agent == "skipped"
                     else "No agent run recorded yet")
    for key, label in (("similar", "Similar incidents found"), ("investigated", "Investigated and tested a fix"),
                       ("pr", "Draft PR opened")):
        done = _last(steps, AGENT_STEPS[key], "completed")
        failed = _last(steps, AGENT_STEPS[key], "failed")
        started = _last(steps, AGENT_STEPS[key], "started")
        if run is None:
            add(key, label, no_agent, summary=no_agent_text)
        elif done:
            d = done["details"]
            summary = {"similar": f"{len(d.get('matches') or [])} similar past incident(s)",
                       "investigated": d.get("summary") or "Patch passed the tests",
                       "pr": f"Draft PR #{d.get('pr_number')}" if d.get("pr_number") else "Draft PR opened"}[key]
            details = dict(d)
            if key == "investigated":
                details |= {"recommendation": (agent.get("patch") or {}).get("recommendation"),
                            "root_cause": (agent.get("patch") or {}).get("root_cause"),
                            "added_tests": (agent.get("patch") or {}).get("added_tests"),
                            "verification": verified["verification"]}
            add(key, label, "done", done["at"], summary, details, started and started["at"])
        elif failed or status in ("needs_review", "failed"):
            recommendation = (agent.get("patch") or {}).get("recommendation")
            add(key, label, "needs_review" if status == "needs_review" else "failed", (failed or {}).get("at"),
                agent.get("status_reason") or "The agent stopped", {"recommendation": recommendation,
                                                                    "verification": verified["verification"]})
            break
        else:
            add(key, label, "running" if status == "open" else "waiting", summary="In progress",
                started=started and started["at"])
            break
    else:
        review = _last(steps, "review", "completed")
        merged = next((e for e in incident.events if e.type == "fix_merged"), None)
        if review and review["details"].get("pr_state") == "closed":
            add("merge", "Human merge", "failed", review["at"], "The PR was closed without merging", review["details"])
        elif review or merged:
            add("merge", "Human merge", "done", review["at"] if review else merged.at,
                "A person merged the fix", (review or {}).get("details"))
        elif run is not None:
            add("merge", "Human merge", "running", summary="Waiting for a person to review and merge the draft PR",
                details={"pr_url": (agent.get("pr") or {}).get("url")})
        else:
            add("merge", "Human merge", no_agent, summary=no_agent_text)

    fresh_at = incident.resolved_at
    add("fresh", "Fresh check passed", "done" if fresh_at else "waiting", fresh_at,
        "A fresh check passed; the service is operational" if fresh_at else "Waiting for a passing check",
        {"check_id": incident.recovery_check_id})

    add("resolved", "Resolved", "done" if incident.state == "resolved" else "waiting", incident.resolved_at,
        "Incident closed" if incident.state == "resolved" else "Open")

    # A step starts when the previous one ended, unless it recorded its own start.
    for prev, step in zip(out, out[1:]):
        step["started_at"] = step["started_at"] or (prev["ended_at"] if step["state"] != "waiting" else None)
    for step in out:
        end = step["ended_at"] or (now if step["state"] == "running" else None)
        step["duration_seconds"] = (end - step["started_at"]).total_seconds() if end and step["started_at"] else None
    return {"incident_id": incident.id, "title": incident.title, "state": incident.state,
            "steps": out, "health": _health(incident, out)}


def _health(incident: Incident, steps: list[dict]) -> dict[str, str]:
    if incident.state == "resolved":
        return {"state": "done", "reason": "Resolved"}
    for step in steps:
        if step["state"] in ("failed", "needs_review"):
            return {"state": "attention", "reason": f"{step['label']}: {step['summary']}"}
    running = next((s for s in steps if s["state"] == "running"), None)
    if running is None:
        return {"state": "on_track", "reason": "Waiting for the next step"}
    if running["key"] == "merge":
        return {"state": "attention", "reason": "A person needs to review and merge the draft PR"}
    limit = EXPECTED.get(running["key"])
    if limit and (running["duration_seconds"] or 0) > limit:
        return {"state": "slow", "reason": f"{running['label']} has run longer than {limit // 60} min"}
    return {"state": "on_track", "reason": f"{running['label']}: in progress"}
