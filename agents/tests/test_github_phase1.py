"""Offline tests for Phase 1 (no network, no token needed).

GitHub is replaced by httpx.MockTransport, so these prove OUR logic is right.
Run from the repo root:   pytest agents/test_github_phase1.py -v
"""
from __future__ import annotations

import base64

import httpx
import pytest

from agents.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubConfig,
    GitHubConfigError,
    GitHubUnavailable,
    clean_logs,
    extract_log_paths,
)
from agents.github_types import (
    app_root,
    application_for_workflow_path,
    fix_branch_name,
    make_incident_id,
)
from agents.monitor import FailureMonitor

CONFIG = GitHubConfig(token="ghp_secret", owner="acme", repo="agentic-mongodb", base_branch="main")
REPO = "/repos/acme/agentic-mongodb"


def make_client(routes: dict, calls: list | None = None) -> GitHubClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        route = routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if callable(route):
            return route(request)
        status, body = route
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    return GitHubClient(CONFIG, transport=httpx.MockTransport(handler), retry_backoff=0)


def run(run_id, path, branch="main", conclusion="failure", attempt=1, sha="abc123"):
    return {
        "id": run_id, "name": "CI", "path": f".github/workflows/{path}", "head_branch": branch,
        "head_sha": sha, "status": "completed", "conclusion": conclusion,
        "run_attempt": attempt, "html_url": f"https://github.com/acme/r/actions/runs/{run_id}",
        "created_at": f"2026-01-01T00:00:{run_id % 60:02d}Z",
    }


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# --------------------------------------------------------------------------- #

def test_config_requires_token_and_owner(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_OWNER", raising=False)
    monkeypatch.setattr("agents.github_client._load_dotenv", lambda path: None)
    with pytest.raises(GitHubConfigError):
        GitHubConfig.from_env()


def test_token_never_appears_in_repr():
    assert "ghp_secret" not in repr(CONFIG)


def test_verify_auth_reports_repo_and_permissions():
    client = make_client({
        REPO: (200, {"full_name": "acme/agentic-mongodb", "private": True,
                     "default_branch": "main", "permissions": {"push": True}}),
        f"{REPO}/actions/runs": (200, {"workflow_runs": []}),
        f"{REPO}/contents": (200, []),
    })
    info = client.verify_auth()
    assert info["full_name"] == "acme/agentic-mongodb"
    assert info["checks"] == {"actions_read": True, "contents_read": True}


def test_bad_token_raises_auth_error():
    client = make_client({REPO: (401, {"message": "Bad credentials"})})
    with pytest.raises(GitHubAuthError):
        client.get_repo()


def test_get_retries_on_5xx_then_succeeds():
    state = {"n": 0}

    def flaky(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"full_name": "acme/agentic-mongodb"})

    client = make_client({REPO: flaky})
    assert client.get_repo()["full_name"] == "acme/agentic-mongodb"
    assert state["n"] == 2


def test_persistent_5xx_raises_unavailable():
    client = make_client({REPO: (503, "down")})
    with pytest.raises(GitHubUnavailable):
        client.get_repo()


def test_detect_failures_filters_and_deduplicates():
    runs = [
        run(101, "shopping-ci.yml"),                         # keep
        run(102, "messaging-ci.yml"),                        # keep
        run(103, "docs.yml"),                                # unrelated workflow
        run(104, "shopping-ci.yml", branch="agent/fix-1-1"),  # agent's own branch
        run(105, "shopping-ci.yml", conclusion="success"),   # not a failure
        run(106, "shopping-ci.yml", branch="feature-x"),     # not main
        run(107, "shopping-ci.yml", attempt=2),              # re-run attempt -> new incident
        run(108, "shopping-ci.yml", branch="dependabot/npm_and_yarn/x", sha="d1"),  # Dependabot push run
        run(109, "shopping-ci.yml", branch="dependabot/npm_and_yarn/x", sha="d1"),  # its PR run: same commit
    ]
    client = make_client({f"{REPO}/actions/runs": (200, {"workflow_runs": runs})})
    monitor = FailureMonitor(client)

    events = monitor.detect_failures()
    assert [e.incident_id for e in events] == ["101-1", "102-1", "107-2", "108-1"]
    assert {e.application for e in events} == {"shopping", "messaging"}
    assert events[0].to_dict()["incidentId"] == "101-1"

    assert monitor.detect_failures() == []          # already seen
    monitor.forget("101-1")
    assert [e.incident_id for e in monitor.detect_failures()] == ["101-1"]


def test_clean_logs_strips_noise_and_keeps_tail():
    raw = "2026-01-01T00:00:00.1234567Z \x1b[31mAssertionError: expected 90, got 100\x1b[0m\n"
    assert clean_logs(raw) == "AssertionError: expected 90, got 100\n"
    long = "\n".join(f"line {i}" for i in range(5000))
    trimmed = clean_logs(long, max_chars=200)
    assert "truncated" in trimmed and trimmed.endswith("line 4999")


def test_extract_log_paths_handles_absolute_and_relative():
    logs = (
        'File "/home/runner/work/r/r/shopping/logic.py", line 12, in total\n'
        "tests/test_logic.py:20: AssertionError\n"
        'File "/usr/lib/python3.12/site-packages/pytest/x.py", line 1\n'
    )
    assert extract_log_paths(logs, "shopping") == [
        "shopping/logic.py", "shopping/tests/test_logic.py",
    ]


def test_get_failure_context_end_to_end():
    log_text = (
        "2026-01-01T00:00:00.0000000Z ##[group]Run pytest\n"
        '2026-01-01T00:00:01.0000000Z File "/home/runner/work/r/r/shopping/logic.py", line 12\n'
        "2026-01-01T00:00:02.0000000Z AssertionError: expected 90, got 100\n"
    )
    client = make_client({
        f"{REPO}/actions/runs/101": (200, run(101, "shopping-ci.yml")),
        f"{REPO}/actions/runs/101/attempts/1/jobs": (200, {"jobs": [
            {"id": 9, "name": "shopping-tests", "conclusion": "failure", "html_url": "u",
             "steps": [{"name": "Run tests", "conclusion": "failure"}]},
            {"id": 10, "name": "lint", "conclusion": "success", "steps": []},
        ]}),
        f"{REPO}/actions/jobs/9/logs": (200, log_text),
        f"{REPO}/commits/abc123": (200, {
            "sha": "abc123", "html_url": "c",
            "commit": {"message": "change discount", "author": {"name": "Dev"}},
            "files": [
                {"filename": "shopping/test_logic.py", "status": "modified",
                 "additions": 1, "deletions": 1, "patch": "@@ -1 +1 @@"},
                {"filename": "README.md", "status": "modified"},
            ],
        }),
        f"{REPO}/contents/shopping/test_logic.py": (200, {
            "type": "file", "sha": "blob1", "encoding": "base64", "content": b64("def test(): ...\n")}),
        f"{REPO}/contents/shopping/logic.py": (200, {
            "type": "file", "sha": "blob2", "encoding": "base64", "content": b64("def total(): ...\n")}),
    })

    ctx = client.get_failure_context(101)

    assert ctx.incident_id == "101-1"
    assert ctx.application == "shopping"
    assert ctx.failed_job == "shopping-tests"
    assert ctx.commit_sha == "abc123"
    assert "AssertionError: expected 90, got 100" in ctx.logs
    assert "0000000Z" not in ctx.logs                       # timestamps stripped
    assert [f.path for f in ctx.changed_files] == ["shopping/test_logic.py"]  # README filtered
    # changed file first, then the file named in the traceback; blob shas come from the repo
    assert [(f.path, f.sha) for f in ctx.source_files] == [
        ("shopping/test_logic.py", "blob1"), ("shopping/logic.py", "blob2")]
    data = ctx.to_dict()
    assert data["workflowRunId"] == 101 and data["failedJob"] == "shopping-tests"


def test_incident_and_branch_naming():
    assert make_incident_id(123456789, 1) == "123456789-1"
    assert fix_branch_name("123456789-1") == "agent/fix-123456789-1"


def test_messaging_uses_app_key_but_message_platform_folder():
    assert application_for_workflow_path(".github/workflows/messaging-ci.yml") == "messaging"
    assert app_root("messaging") == "message-platform"
    logs = 'File "/home/runner/work/r/r/message-platform/src/send.py", line 3, in send'
    assert extract_log_paths(logs, "message-platform") == ["message-platform/src/send.py"]


def test_failure_context_converts_to_failed_run():
    from agents.github_types import FailureContext

    ctx = FailureContext(
        incident_id="101-1", application="shopping", workflow_run_id=101, run_attempt=1,
        commit_sha="abc123", failed_job="backend", workflow_url="u",
        logs="FAILED tests/test_discounts.py::test_welcome10 - AssertionError\nFAILED tests/test_discounts.py::test_welcome10 - x\n",
    )
    run = ctx.to_failed_run("acme/agentic-mongodb")
    assert run["repo"] == "acme/agentic-mongodb" and run["app"] == "shopping"
    assert run["run_id"] == 101 and run["commit_sha"] == "abc123"
    assert run["failed_tests"] == ["tests/test_discounts.py::test_welcome10"]  # de-duplicated
