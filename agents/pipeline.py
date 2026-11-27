"""Single entry point for the orchestrator: reproduce, diagnose, patch, verify.

Order matters for precision and cost:
1. Reproduce the failing tests locally (``baseline_runs`` times) before any model
   call. Flaky or non-reproducible failures stop here as needs_review.
2. Diagnose with the CI log *and* the fresh local traceback.
3. Ask for up to ``max_candidates`` alternative patches, verify each in its own
   copy, and keep the smallest passing one.
4. If none pass, feed the failed candidates and their test output back for
   another round, up to ``max_rounds``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .contracts import CandidateAttempt, RepairRequest, RepairResult, load_app_config
from .investigator import investigate, parse_failing_tests
from .llm import StructuredLLM, default_llm
from .patcher import new_file_diff, propose_patches
from .verifier import VerificationError, VerificationSession


def repair_ci_failure(request: RepairRequest, llm: StructuredLLM | None = None) -> RepairResult:
    """Return a verified patch or a ``needs_review`` result with a reason.

    Does no GitHub or MongoDB writes; the caller persists ``result.model_dump()``
    and hands a verified ``result.patch`` to the PR module. Model/API errors are
    raised so the orchestrator can apply its own retry policy.
    """
    app = load_app_config(request.app)
    started = datetime.now(UTC)

    def done(outcome: str, reason: str, **fields) -> RepairResult:
        return RepairResult(
            outcome=outcome, reason=reason, app=app.name, commit_sha=request.commit_sha,
            started_at=started, finished_at=datetime.now(UTC), **fields,
        )

    failing = parse_failing_tests(request.failed_test_logs, app)
    if not failing and app.kind == "python":
        return done("needs_review", "no failing pytest node ids found in the CI log")

    try:
        with VerificationSession(
            app, request.repo_path, request.commit_sha, failing, added_tests=request.added_tests
        ) as session:
            baseline = session.reproduce()
            if baseline.flaky:
                return done(
                    "needs_review", "flaky: the failing tests passed on at least one baseline rerun",
                    flaky=True, baseline=baseline.runs,
                )
            if not baseline.reproduced:
                return done("needs_review", "baseline did not reproduce the CI failure", baseline=baseline.runs)

            llm = llm or default_llm()
            diagnosis = investigate(request, llm, app, reproduction=baseline.runs[0].output_tail,
                                    require_failing_tests=app.kind == "python")
            if diagnosis.outcome == "needs_review":
                return done(
                    "needs_review", f"diagnosis: {diagnosis.review_reason}",
                    diagnosis=diagnosis, baseline=baseline.runs,
                )

            attempts: list[CandidateAttempt] = []
            for round_no in range(1, app.max_rounds + 1):
                previous = [a for a in attempts if a.patch]
                round_attempts = []
                for index, patch in enumerate(propose_patches(request, diagnosis, llm, app, previous), 1):
                    if not patch.accepted or patch.diff is None:
                        round_attempts.append(CandidateAttempt(
                            round=round_no, index=index, summary=patch.summary, patch=patch.diff,
                            changed_files=patch.changed_files, changed_lines=patch.changed_lines,
                            passed=False, reason=f"patch rejected: {patch.rejection_reason}",
                        ))
                        continue
                    check = session.check(patch.diff)
                    round_attempts.append(CandidateAttempt(
                        round=round_no, index=index, summary=patch.summary, patch=patch.diff,
                        changed_files=check.changed_files, changed_lines=check.changed_lines,
                        passed=check.passed, reason=check.reason, files=check.files,
                        reproduce_runs=check.reproduce_runs, suite=check.suite,
                    ))
                attempts += round_attempts

                passing = [a for a in round_attempts if a.passed]
                if passing:
                    best = min(passing, key=lambda a: (a.changed_lines, len(a.changed_files), a.index))
                    # Agent-added regression tests ship with the fix so reviewers see the proof.
                    added = list(request.added_tests)
                    return done(
                        "verified",
                        f"{best.reason}; chose candidate {best.round}.{best.index} "
                        f"({best.changed_lines} changed lines) of {len(attempts)} tried",
                        diagnosis=diagnosis,
                        patch=best.patch + "".join(new_file_diff(p, request.added_tests[p]) for p in added),
                        changed_files=best.changed_files + added,
                        patched_files={**best.files, **request.added_tests}, added_tests=added,
                        baseline=baseline.runs, reproduce_after_patch=best.reproduce_runs,
                        patched_suite=best.suite, attempts=attempts,
                    )
                if not any(a.patch for a in round_attempts):
                    break  # nothing testable to learn from; another round would repeat itself

    except VerificationError as exc:
        return done("needs_review", str(exc))

    tested = [a for a in attempts if a.reproduce_runs]
    last = (tested or attempts)[-1].reason if attempts else "no candidates"
    return done(
        "needs_review",
        f"no candidate passed after {len({a.round for a in attempts}) or 1} round(s); "
        f"last tested: {last}",
        diagnosis=diagnosis, baseline=baseline.runs, attempts=attempts,
    )
