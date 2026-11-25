"""Deep dive for a live incident from Taha's uptime monitor, behind Mo's ``Repairer`` contract.

``Repairer.repair(failed_run, similar)`` calls ``repair_live_incident`` when
``failed_run["source"] == "live"``. ``failed_run["live"]`` is the incident in the shape
of Taha's handoff (``admin-dashboard/backend/uptime/handoff.py``, schema
``opsrelay.uptime_incident.v1``): route, failure type, ``live_check`` and the apps' own
``probe_failures`` with ``source`` hints such as ``backend/app/pricing.py::compute_discount``.

1. ``investigation_route == "service_health"`` (down, timing out, HTTP errors): no model
   call and no code change. The result is needs_review with the checks to run.
2. Otherwise gather evidence at the deployed commit: probe failures, recent commits that
   touched the app's source, the source files, and the tests most related to the hinted
   modules. Then ``live_incident.diagnose_live_failure`` diagnoses, reproduces (app suite,
   or a new regression test when the suite misses the bug), and repairs.

Returns Mo's PatchResult dict plus ``investigation`` (markdown for the PR and the
incident record) and ``added_tests`` (new test files included in ``files``).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .contracts import LiveCheckResult, PriorIncident, RepairResult, load_app_config

if TYPE_CHECKING:
    from .patcher import Repairer

MAX_RECENT_DIFF_CHARS = 20_000
MAX_TEST_EXAMPLES = 2
RECENT_COMMITS = 5

_FAILURE_KINDS = {"connection_timeout", "connection_refused", "connection_error", "http_error",
                  "invalid_response", "not_delivered"}

SERVICE_HEALTH_STEPS = [
    "Check whether the service process/container is running and when it last restarted.",
    "Check the latest deployment and any infrastructure change around the incident start.",
    "Read the service's startup and error logs around the incident start.",
    "Check network and host health between the monitor and the service.",
]

PROVIDER_STEPS = [
    "Check the provider's status page and confirm the outage and its expected recovery time.",
    "Tell customers the feature is affected; there is no code fix to merge.",
    "The uptime monitor resolves the incident when the provider recovers and the next probe passes.",
]


# Recovery for an uptime incident is the monitor's next passing check, not the agent's word.
APPROVAL_STEPS = [
    "Review the diff (and any new test) and the evidence above.",
    "If it is right, mark this draft ready and merge it; the agent never merges.",
    "Deploy it. The uptime monitor resolves the incident when its next functional probe passes.",
]


def live_check_from_handoff(live: dict[str, Any]) -> LiveCheckResult:
    """Taha's ``live_check`` block as a ``LiveCheckResult`` (his functional probes are synthetic)."""
    check = live.get("live_check") or {}
    kind = check.get("failure_kind")
    return LiveCheckResult(
        service=check.get("service") or live.get("service_id") or "unknown",
        app=check.get("app") or live["app"],
        checked_at=check.get("checked_at") or live.get("started_at") or datetime.now(UTC),
        base_url=check.get("base_url") or "",
        target="live",
        probe_id=live.get("uptime_incident_id") or live.get("incident_id") or "uptime",
        mode="synthetic",
        reachable=bool(check.get("reachable", True)),
        healthy=bool(check.get("healthy", True)),
        delivery="not_attempted",
        failure_kind=kind if kind in _FAILURE_KINDS else None,
        error=check.get("error"),
        http_status=check.get("http_status"),
    )


def probe_text(failures: list[dict]) -> str:
    """Failing probe checks, one field per line, so the model can quote them verbatim."""
    lines = []
    for f in failures:
        lines.append(f"- check: {f.get('check') or f.get('name')} (component: {f.get('component') or 'n/a'})")
        for key in ("source", "reason", "expected", "actual"):
            value = f.get(key)
            if value not in (None, ""):
                text = value if isinstance(value, str) else json.dumps(value, default=str)
                lines.append(f"  {key}: {text}")
    return "\n".join(lines)


def repair_live_incident(repairer: Repairer, failed_run: dict, similar: list[dict]) -> dict:
    """The repair result plus one ``recommendation``: a code fix, an operational step or a review."""
    out = _repair_live(repairer, failed_run, similar)
    operational = out.pop("_operational", False)
    action = ("propose_code_fix" if out.get("outcome") == "verified"
              else "operational_step" if operational else "ask_for_review")
    rec = {"action": action, "failure_point": "see the failing probe checks",
           "cause": out.get("root_cause") or "unknown", "evidence": [], "detail": out.get("reason") or ""}
    out["recommendation"] = rec
    out["investigation"] = (out.get("investigation") or "") + _recommendation_markdown(rec)
    return out


def _recommendation_markdown(rec: dict[str, Any]) -> str:
    label = {"propose_code_fix": "Propose a code fix", "operational_step": "Operational step",
             "ask_for_review": "Ask for review"}[rec["action"]]
    return "\n".join(["", "", f"**Recommendation:** {label}. {rec['detail']}",
                      f"- Failure point: {rec['failure_point']}", f"- Cause: {rec['cause']}"])


def _repair_live(repairer: Repairer, failed_run: dict, similar: list[dict]) -> dict:
    from .live_incident import LiveIncident, diagnose_live_failure

    app = load_app_config(failed_run["app"])
    live = failed_run.get("live") or {}
    sha = failed_run["commit_sha"]
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise ValueError(f"invalid commit sha {sha!r}")

    title = live.get("title") or f"{app.name} live incident"
    if live.get("investigation_route") == "service_health":
        reason = (f"{title}: {live.get('failure_type') or 'service unhealthy'} is a service health problem "
                  f"(process, deployment or network), not a code bug, so no code change was attempted")
        return {**_needs_review(reason, live.get("summary") or reason,
                                _markdown(live, reason, None, SERVICE_HEALTH_STEPS)), "_operational": True}

    failures = live.get("probe_failures") or []
    if failures and all(str(f.get("source") or "").startswith("external:") for f in failures):
        external = sorted({str(f["source"]).removeprefix("external:").strip() for f in failures})
        reason = (f"{title}: the failing check is a third-party dependency ({', '.join(external)}), "
                  f"not {app.name} code, so no code change was attempted")
        return {**_needs_review(reason, f"{', '.join(external)} outage",
                                _markdown(live, reason, None, PROVIDER_STEPS)), "_operational": True}

    source_files = repairer._source_files(sha, app)
    incident = LiveIncident(
        check=live_check_from_handoff(live),
        repo_path=repairer.repo_path,
        deployed_sha=sha,
        recent_diff=_recent_diff(repairer, sha, app),
        source_files=source_files,
        similar_incidents=[_prior(s, i) for i, s in enumerate(similar)],
        ci_status=live.get("ci_status") or "unknown",
        probe_details=probe_text(live.get("probe_failures") or []),
        test_examples=_test_examples(repairer, sha, app, live),
        service_logs=live.get("service_logs") or "",
    )
    result = diagnose_live_failure(incident, repairer.llm)
    repair: RepairResult | None = result.repair
    actions = (APPROVAL_STEPS if result.outcome == "verified" else
               [a for a in [result.next_diagnostic, *result.recommended_actions] if a])
    markdown = _markdown(live, result.reason, result, actions)
    if result.outcome != "verified" or repair is None or repair.outcome != "verified":
        next_step = f" Next: {result.next_diagnostic}" if result.next_diagnostic else ""
        return _needs_review(result.reason + next_step, result.likely_cause, markdown,
                             output=result.reproduction or "")
    chosen = next((a for a in repair.attempts if a.passed and a.patch and repair.patch.startswith(a.patch)), None)
    return {
        "tests_passed": True,
        "test_output": repair.patched_suite.output_tail if repair.patched_suite else repair.reason,
        "diff": repair.patch,
        "files": repair.patched_files,
        "changed_files": repair.changed_files,
        "added_tests": repair.added_tests,
        "summary": chosen.summary if chosen else repair.reason,
        "root_cause": result.likely_cause,
        "investigation": markdown,
        "outcome": "verified",
        "reason": result.reason,
    }


def _needs_review(reason: str, root_cause: str, markdown: str, output: str = "") -> dict:
    return {"tests_passed": False, "test_output": output or reason, "diff": "", "files": {},
            "changed_files": [], "summary": reason, "root_cause": root_cause,
            "investigation": markdown, "outcome": "needs_review", "reason": reason}


def _prior(s: dict, i: int) -> PriorIncident:
    return PriorIncident(id=str(s.get("memory_id") or s.get("id") or i), summary=s.get("summary") or "",
                         root_cause=s.get("root_cause"), fix_summary=s.get("fix_summary"),
                         similarity=s.get("score"))


def _recent_diff(repairer: Repairer, sha: str, app) -> str:
    """Patches of the last few commits that touched the app's source, newest first."""
    log = repairer._git("log", f"-{RECENT_COMMITS}", "-p", "--format=commit %h %s (%an, %ad)",
                        "--date=short", sha, "--", *app.source_dirs, default="") or ""
    return log[:MAX_RECENT_DIFF_CHARS]


def _hinted_modules(live: dict) -> set[str]:
    """Module names from probe source hints: ``backend/app/pricing.py::x`` -> ``pricing``."""
    names = set()
    for failure in live.get("probe_failures") or []:
        path = str(failure.get("source") or "").split("::", 1)[0]
        if path.endswith(".py"):
            names.add(path.rsplit("/", 1)[-1].removesuffix(".py"))
    return names


def _test_examples(repairer: Repairer, sha: str, app, live: dict) -> dict[str, str]:
    """conftest/helpers plus the existing tests most related to the hinted modules."""
    test_root = f"{app.root.rstrip('/')}/{app.test_dir.strip('/')}"
    listing = (repairer._git("ls-tree", "-r", "--name-only", sha, "--", test_root, default="") or "").split()
    files = {p: repairer._git("show", f"{sha}:{p}", default="") or "" for p in listing if p.endswith(".py")}
    helpers = {p: c for p, c in files.items()
               if p.rsplit("/", 1)[-1] in ("conftest.py", "_assertions.py", "helpers.py")}
    modules = _hinted_modules(live)

    def score(item: tuple[str, str]) -> int:
        path, content = item
        return sum(content.count(f"{m}") + 5 * (m in path) for m in modules)

    tests = sorted(((p, c) for p, c in files.items() if p.rsplit("/", 1)[-1].startswith("test_")),
                   key=score, reverse=True)
    return {**helpers, **dict(tests[:MAX_TEST_EXAMPLES])}


def _markdown(live: dict, reason: str, result, actions: list[str]) -> str:
    """What the agent looked at and concluded, for the PR body and the incident record."""
    lines = [f"**Live incident:** {live.get('title') or 'n/a'} "
             f"(`{live.get('uptime_incident_id') or 'n/a'}`, {live.get('severity') or 'n/a'}, "
             f"started {live.get('started_at') or 'n/a'})",
             f"**Failure type:** {live.get('failure_type') or 'n/a'} - route `{live.get('investigation_route')}`"]
    failures = live.get("probe_failures") or []
    if failures:
        lines.append("\n**Failing probe checks:**")
        for f in failures[:6]:
            lines.append(f"- `{f.get('check') or f.get('name')}` ({f.get('source') or 'no source hint'}): "
                         f"{f.get('reason') or f.get('message') or ''}")
    diagnosis = getattr(result, "diagnosis", None)
    if diagnosis is not None:
        lines += ["", f"**Diagnosis** (confidence {diagnosis.confidence:.2f}, {diagnosis.failure_class}): "
                      f"{diagnosis.likely_cause}", "", diagnosis.explanation]
        if diagnosis.evidence:
            lines.append("\n**Evidence (quoted verbatim from the inputs):**")
            lines += [f"- [{e.source}: {e.reference}] `{e.excerpt[:200]}`: {e.supports}"
                      for e in diagnosis.evidence[:6]]
    repair = getattr(result, "repair", None)
    if repair is not None and repair.added_tests:
        lines.append(f"\n**Reproduction:** the existing suite passed, so the agent added "
                     f"{', '.join(f'`{p}`' for p in repair.added_tests)}. It fails on the deployed "
                     "code and passes with the fix.")
    elif getattr(result, "reproduction", None):
        lines.append("\n**Reproduction:** the app's existing test suite fails at the deployed commit.")
    lines += ["", f"**Outcome:** {reason}"]
    if actions:
        lines += ["", "**Next steps:**", *[f"- {a}" for a in actions]]
    return "\n".join(lines)
