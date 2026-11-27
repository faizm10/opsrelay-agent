"""Diagnose a failed live check, route it, and confirm recovery with a fresh probe.

Routing, in order:
1. Probe delivered                       -> healthy, nothing to do.
2. No HTTP response (timeout, refused)   -> service_health. No model call, no code repair.
   A timeout alone leaves the cause unknown.
3. Health endpoint returned an error     -> service_health.
4. Reachable but delivery failed         -> the model compares the probe with service
   logs and recent changes. Only a grounded ``code_bug`` diagnosis goes on, and only if
   the failure reproduces locally: either the app's pytest suite fails at the deployed
   commit, or (when the suite passes) the model writes a new regression test that
   fails there for the reported reason (``regression.py``). That failure then runs
   through the CI repair pipeline (reproduce, candidates, verify), and an added test
   ships with the fix. Anything else is ``needs_review`` with the next diagnostic.
5. A verified patch is "pending_deploy". Recovery is reported only by
   ``confirm_recovery``: a fresh probe id that is confirmed delivered.

    from agents.live_incident import LiveIncident, diagnose_incident, confirm_recovery
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .contracts import (
    AppConfig,
    Diagnosis,
    LiveCheckResult,
    PriorIncident,
    RepairRequest,
    RepairResult,
    load_app_config,
)
from .investigator import investigate
from .live_check import ServiceConfig, run_live_check
from .llm import StructuredLLM, default_llm
from .pipeline import repair_ci_failure
from .regression import write_regression_test
from .verifier import VerificationError, VerificationSession

SERVICE_HEALTH_STEPS = [
    "Check the deployment status and when it last changed (is the latest deploy running?).",
    "Check the service process or container: running, restarting, or crash-looping?",
    "Read the service's startup and error logs around the check time.",
    "Check host, network and load balancer health between the checker and the service.",
]


class LiveIncident(BaseModel):
    """A failed live check plus whatever context is available to explain it."""

    check: LiveCheckResult
    repo_path: str | None = Field(None, description="Local clone containing deployed_sha (read only).")
    deployed_sha: str | None = None
    service_logs: str = ""
    recent_diff: str = ""
    source_files: dict[str, str] = Field(default_factory=dict)
    similar_incidents: list[PriorIncident] = Field(default_factory=list)
    ci_status: Literal["passing", "failing", "unknown"] = "unknown"
    probe_details: str = Field("", description="The app's failing probe checks (JSON), with source hints.")
    test_examples: dict[str, str] = Field(
        default_factory=dict, description="Existing test files (and conftest) to model a regression test on."
    )
    write_regression_test: bool = Field(
        True, description="When the suite passes, try to write a new test that reproduces the failure."
    )


class LiveIncidentResult(BaseModel):
    outcome: Literal["healthy", "needs_review", "verified"]
    route: Literal["none", "service_health", "investigate", "code_repair"]
    reason: str
    likely_cause: str
    recommended_actions: list[str] = Field(default_factory=list)
    next_diagnostic: str | None = None
    recovery: Literal["not_applicable", "pending_deploy"] = "not_applicable"
    check: LiveCheckResult
    diagnosis: Diagnosis | None = None
    reproduction: str | None = Field(None, description="Tail of the app suite run at deployed_sha.")
    repair: RepairResult | None = None


class RecoveryResult(BaseModel):
    recovered: bool
    reason: str
    check: LiveCheckResult


def diagnose_incident(
    evidence: RepairRequest | LiveIncident, llm: StructuredLLM | None = None
) -> RepairResult | LiveIncidentResult:
    """Single entry point: a failed CI run (RepairRequest) or a failed live check (LiveIncident)."""
    if isinstance(evidence, LiveIncident):
        return diagnose_live_failure(evidence, llm)
    return repair_ci_failure(evidence, llm)


def diagnose_live_failure(incident: LiveIncident, llm: StructuredLLM | None = None) -> LiveIncidentResult:
    check = incident.check
    app = load_app_config(check.app)

    def result(outcome, route, reason, cause, **fields) -> LiveIncidentResult:
        return LiveIncidentResult(outcome=outcome, route=route, reason=reason, likely_cause=cause, check=check, **fields)

    if check.ok:
        return result("healthy", "none", "probe delivered", "none")

    if not check.reachable:
        return result("needs_review", "service_health", _unreachable_reason(check, incident.ci_status),
                      _unreachable_cause(check), recommended_actions=SERVICE_HEALTH_STEPS,
                      next_diagnostic=SERVICE_HEALTH_STEPS[0])

    if not check.healthy:
        return result("needs_review", "service_health",
                      f"service is reachable but its health check failed: {check.error}",
                      f"service reports itself unhealthy (HTTP {check.http_status})",
                      recommended_actions=SERVICE_HEALTH_STEPS[1:],
                      next_diagnostic="Read the service's error logs and its dependencies' health at the check time.")

    # Reachable and healthy, but the probe did not arrive (or send/read-back failed).
    if not incident.repo_path or not incident.deployed_sha:
        return result("needs_review", "investigate", "delivery failed but no deployed commit was provided",
                      "unknown", next_diagnostic="Provide the deployed commit SHA and a repo clone so the failure "
                                                 "can be compared with recent changes and reproduced.")

    llm = llm or default_llm()
    request = _request_for(incident, failed_test_logs="")
    diagnosis = investigate(request, llm, app, require_failing_tests=False)
    if diagnosis.outcome == "needs_review":
        return result("needs_review", "investigate", f"diagnosis: {diagnosis.review_reason}",
                      diagnosis.likely_cause, diagnosis=diagnosis,
                      next_diagnostic=diagnosis.next_diagnostic or "Collect service logs for the probe id.")
    if diagnosis.failure_class != "code_bug":
        return result("needs_review", "investigate",
                      f"evidence points to {diagnosis.failure_class.replace('_', ' ')}, not a code bug",
                      diagnosis.likely_cause, diagnosis=diagnosis, next_diagnostic=diagnosis.next_diagnostic)

    suite = _run_suite(app, incident)
    if isinstance(suite, str):
        return result("needs_review", "investigate", suite, diagnosis.likely_cause, diagnosis=diagnosis,
                      next_diagnostic=diagnosis.next_diagnostic)
    if suite.passed and incident.write_regression_test:
        written = write_regression_test(request, diagnosis, llm, app, incident.test_examples)
        if written.test is None:
            return result(
                "needs_review", "investigate",
                f"diagnosis points at code ({', '.join(diagnosis.suspect_files)}) but the {app.name} test "
                f"suite passes at {incident.deployed_sha[:7]} and {written.reason}",
                diagnosis.likely_cause, diagnosis=diagnosis, reproduction=suite.output_tail,
                next_diagnostic=diagnosis.next_diagnostic
                or f"Write a pytest regression test that reproduces: {diagnosis.likely_cause}",
            )
        test = written.test
        logs = f"Regression test {test.path} (asserts: {test.asserts}) at {incident.deployed_sha[:7]}:\n"
        repair = repair_ci_failure(
            _request_for(incident, failed_test_logs=logs + test.failing_run.output_tail)
            .model_copy(update={"added_tests": {test.path: test.content}}),
            llm,
        )
        return _repair_result(result, repair, diagnosis, test.failing_run.output_tail, check, app,
                              f"reproduced by new regression test {test.path}")
    if suite.passed:
        return result(
            "needs_review", "investigate",
            f"diagnosis points at code ({', '.join(diagnosis.suspect_files)}) but the {app.name} "
            f"test suite passes at {incident.deployed_sha[:7]}, so the failure is not reproduced",
            diagnosis.likely_cause, diagnosis=diagnosis, reproduction=suite.output_tail,
            next_diagnostic=diagnosis.next_diagnostic
            or f"Write a pytest regression test that reproduces: {diagnosis.likely_cause}",
        )

    repair = repair_ci_failure(_request_for(incident, failed_test_logs=suite.output_tail), llm)
    return _repair_result(result, repair, diagnosis, suite.output_tail, check, app,
                          f"reproduced by the {app.name} test suite")


def _repair_result(result, repair: RepairResult, diagnosis: Diagnosis, reproduction: str,
                   check: LiveCheckResult, app: AppConfig, how: str) -> LiveIncidentResult:
    if repair.outcome == "verified":
        return result(
            "verified", "code_repair", f"{how} and fixed: {repair.reason}",
            diagnosis.likely_cause, diagnosis=repair.diagnosis or diagnosis, reproduction=reproduction,
            repair=repair, recovery="pending_deploy",
            recommended_actions=["Deploy the verified patch.",
                                 f"Call confirm_recovery({check.service!r}) - recovery needs a fresh delivered probe."],
        )
    return result("needs_review", "code_repair", f"repair: {repair.reason}", diagnosis.likely_cause,
                  diagnosis=repair.diagnosis or diagnosis, reproduction=reproduction, repair=repair,
                  next_diagnostic=diagnosis.next_diagnostic)


def confirm_recovery(
    service: str,
    base_url: str | None = None,
    timeout: float | None = None,
    *,
    previous_probe_id: str | None = None,
    target: Literal["live", "fixture"] = "live",
    config: ServiceConfig | None = None,
) -> RecoveryResult:
    """Rerun the live probe with a fresh id. Recovered only if that exact message is delivered."""
    check = run_live_check(service, base_url, timeout, target=target, config=config)
    if previous_probe_id is not None and check.probe_id == previous_probe_id:
        raise RuntimeError("recovery probe reused the incident's probe id")
    if check.ok:
        return RecoveryResult(recovered=True, check=check,
                              reason=f"fresh probe {check.probe_id} delivered in {check.delivery_latency_ms} ms")
    return RecoveryResult(recovered=False, check=check,
                          reason=f"fresh probe {check.probe_id} failed: {check.failure_kind}: {check.error}")


def _unreachable_cause(check: LiveCheckResult) -> str:
    if check.failure_kind == "connection_timeout":
        return "unknown: the service did not respond within the timeout, which alone does not identify a cause"
    if check.failure_kind == "connection_refused":
        return "nothing is accepting connections at the service URL (stopped, crashed, or not deployed)"
    return f"network failure before any HTTP response ({check.error})"


def _unreachable_reason(check: LiveCheckResult, ci_status: str) -> str:
    reason = f"{check.service} is unreachable ({check.failure_kind}); check service and deployment health"
    if ci_status == "passing":
        reason += ". CI is green, so a code regression is not the first suspect"
    return reason


def _request_for(incident: LiveIncident, failed_test_logs: str) -> RepairRequest:
    return RepairRequest(
        app=incident.check.app,
        repo_path=incident.repo_path or "",
        commit_sha=incident.deployed_sha or "",
        failed_test_logs=failed_test_logs,
        recent_diff=incident.recent_diff,
        source_files=incident.source_files,
        similar_incidents=incident.similar_incidents,
        live_check=incident.check,
        service_logs=incident.service_logs,
        probe_details=incident.probe_details,
    )


def _run_suite(app: AppConfig, incident: LiveIncident):
    try:
        with VerificationSession(app, incident.repo_path, incident.deployed_sha) as session:
            return session.run_suite()
    except VerificationError as exc:
        return f"could not run the {app.name} suite at the deployed commit: {exc}"
