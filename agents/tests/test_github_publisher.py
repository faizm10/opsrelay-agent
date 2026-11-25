"""Offline tests for Phase 2: the PR publisher against a stateful fake GitHub.

``FakeGitHub`` keeps real state (commits, trees, branches, pull requests) behind
httpx.MockTransport, so these tests prove the behaviours that matter:
running twice makes ONE pr, and a lost response never makes a duplicate.

Run from the repo root:   pytest agents/test_github_publisher.py -v
"""
from __future__ import annotations

import base64
import hashlib
import json
import re

import httpx
import pytest

from agents.github_client import GitHubClient, GitHubConfig
from agents.github_publisher import PullRequestPublisher, validate_request
from agents.github_types import (
    STATUS_FAILED,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    GitHubOperationResult,
    PatchFile,
    PublishRequest,
)

OWNER, REPO = "acme", "agentic-mongodb"
CONFIG = GitHubConfig(token="ghp_secret", owner=OWNER, repo=REPO, base_branch="main")
PRICING = "shopping/backend/app/pricing.py"
OTHER = "shopping/backend/app/other.py"
BRANCH = "agent/fix-101-1"
FIXED = "def discount(x):\n    return x * 0.9\n"


class FakeGitHub:
    """Just enough of the GitHub REST API, with state."""

    def __init__(self):
        self._n = 0
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.pulls: list[dict] = []
        self.draft_unsupported = False
        self.requests: list[tuple[str, str]] = []
        self.faults: list[dict] = []
        tree = self._new_tree({
            PRICING: "def discount(x):\n    return x - 10\n",
            OTHER: "x = 1\n",
            "README.md": "hello\n",
        })
        self.base_sha = self._new_commit(tree, [])
        self.refs["main"] = self.base_sha

    # -- state helpers ------------------------------------------------------ #

    def _id(self, kind: str) -> str:
        self._n += 1
        return hashlib.sha1(f"{kind}-{self._n}".encode()).hexdigest()

    def _new_tree(self, files):
        tid = self._id("tree")
        self.trees[tid] = dict(files)
        return tid

    def _new_commit(self, tree, parents):
        cid = self._id("commit")
        self.commits[cid] = {"tree": tree, "parents": list(parents)}
        return cid

    def files_at(self, ref):
        sha = self.refs.get(ref) or (ref if ref in self.commits else None)
        return self.trees[self.commits[sha]["tree"]]

    def advance(self, branch, changes):
        """Someone pushes a commit to ``branch`` (e.g. main moves on)."""
        head = self.refs[branch]
        files = dict(self.files_at(branch))
        files.update(changes)
        self.refs[branch] = self._new_commit(self._new_tree(files), [head])

    def commits_on(self, branch):
        n, sha = 0, self.refs[branch]
        while sha != self.base_sha:
            n, sha = n + 1, self.commits[sha]["parents"][0]
        return n

    def fail(self, method, route, mode, times=1):
        """mode='before': request is lost. mode='after': it is applied but the reply is lost."""
        self.faults.append({"method": method, "route": route, "mode": mode, "left": times})

    def count(self, method, route_prefix):
        return sum(1 for m, r in self.requests if m == method and r.startswith(route_prefix))

    # -- HTTP --------------------------------------------------------------- #

    def handler(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/repos/{OWNER}/{REPO}"
        path = request.url.path
        assert path.startswith(prefix), path
        route = path[len(prefix):]
        self.requests.append((request.method, route))

        fault = next((f for f in self.faults
                      if f["left"] and f["method"] == request.method and route.startswith(f["route"])), None)
        if fault:
            fault["left"] -= 1
            if fault["mode"] == "before":
                raise httpx.ReadTimeout("request lost", request=request)
        response = self._dispatch(request.method, route, request)
        if fault and fault["mode"] == "after":
            raise httpx.ReadTimeout("response lost", request=request)
        return response

    @staticmethod
    def _json(status, body):
        return httpx.Response(status, json=body)

    def _msg(self, status, message, errors=None):
        body = {"message": message}
        if errors:
            body["errors"] = [{"message": e} for e in errors]
        return self._json(status, body)

    def _dispatch(self, method, route, request):
        body = json.loads(request.content) if request.content else {}
        params = dict(request.url.params)

        if method == "GET" and (m := re.fullmatch(r"/git/ref/heads/(.+)", route)):
            sha = self.refs.get(m[1])
            return self._json(200, {"object": {"sha": sha}}) if sha else self._msg(404, "Not Found")

        if method == "POST" and route == "/git/refs":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.refs:
                return self._msg(422, "Reference already exists")
            if body["sha"] not in self.commits:
                return self._msg(422, "Object does not exist")
            self.refs[name] = body["sha"]
            return self._json(201, {"object": {"sha": body["sha"]}})

        if method == "GET" and (m := re.fullmatch(r"/git/commits/(\w+)", route)):
            c = self.commits.get(m[1])
            return self._json(200, {"sha": m[1], "tree": {"sha": c["tree"]}}) if c else self._msg(404, "Not Found")

        if method == "POST" and route == "/git/trees":
            tree = dict(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                tree[entry["path"]] = entry["content"]
            return self._json(201, {"sha": self._new_tree(tree)})

        if method == "POST" and route == "/git/commits":
            return self._json(201, {"sha": self._new_commit(body["tree"], body["parents"])})

        if method == "PATCH" and (m := re.fullmatch(r"/git/refs/heads/(.+)", route)):
            current = self.refs.get(m[1])
            if current is None:
                return self._msg(404, "Not Found")
            if not body.get("force") and current not in self.commits[body["sha"]]["parents"]:
                return self._msg(422, "Update is not a fast forward")
            self.refs[m[1]] = body["sha"]
            return self._json(200, {"object": {"sha": body["sha"]}})

        if method == "GET" and (m := re.fullmatch(r"/contents/(.+)", route)):
            ref = params.get("ref")
            sha = self.refs.get(ref) or (ref if ref in self.commits else None)
            content = self.trees[self.commits[sha]["tree"]].get(m[1]) if sha else None
            if content is None:
                return self._msg(404, "Not Found")
            return self._json(200, {
                "type": "file", "sha": hashlib.sha1(content.encode()).hexdigest(),
                "encoding": "base64", "content": base64.b64encode(content.encode()).decode()})

        if method == "GET" and (m := re.fullmatch(r"/commits/(\w+)", route)):
            if m[1] not in self.commits:
                return self._msg(404, "No commit found")
            return self._json(200, {"sha": m[1], "html_url": "u",
                                    "commit": {"message": "m", "author": {"name": "dev"}}, "files": []})

        if method == "GET" and (m := re.fullmatch(r"/compare/(\w+)\.\.\.(\w+)", route)):
            if m[1] not in self.commits or m[2] not in self.commits:
                return self._msg(404, "Not Found")
            a, b = (self.trees[self.commits[x]["tree"]] for x in (m[1], m[2]))
            changed = sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))
            return self._json(200, {"files": [{"filename": p} for p in changed]})

        if method == "GET" and route == "/pulls":
            owner, _, ref = params["head"].partition(":")
            return self._json(200, [self._pull_json(p) for p in reversed(self.pulls) if p["head_ref"] == ref])

        if method == "POST" and route == "/pulls":
            if any(p["head_ref"] == body["head"] and p["state"] == "open" for p in self.pulls):
                return self._msg(422, "Validation Failed",
                                 [f"A pull request already exists for {OWNER}:{body['head']}."])
            if body["head"] not in self.refs:
                return self._msg(422, "Validation Failed", ["Head ref must be a branch"])
            if self.files_at(body["head"]) == self.files_at(body["base"]):
                return self._msg(422, "Validation Failed", [f"No commits between {body['base']} and {body['head']}"])
            if body.get("draft") and self.draft_unsupported:
                return self._msg(422, "Validation Failed",
                                 ["Draft pull requests are not supported in this repository."])
            pull = {"number": len(self.pulls) + 1, "title": body["title"], "body": body["body"],
                    "head_ref": body["head"], "state": "open", "merged": False,
                    "draft": bool(body.get("draft"))}
            self.pulls.append(pull)
            return self._json(201, self._pull_json(pull))

        if method == "GET" and (m := re.fullmatch(r"/pulls/(\d+)", route)):
            return self._json(200, self._pull_json(self.pulls[int(m[1]) - 1]))

        return self._msg(404, f"fake has no route for {method} {route}")

    def _pull_json(self, p):
        return {"number": p["number"], "html_url": f"https://github.com/{OWNER}/{REPO}/pull/{p['number']}",
                "state": p["state"], "merged_at": "2026-01-01T00:00:00Z" if p["merged"] else None,
                "title": p["title"], "body": p["body"],
                "head": {"ref": p["head_ref"], "sha": self.refs[p["head_ref"]]}, "base": {"ref": "main"}}


# --------------------------------------------------------------------------- #

@pytest.fixture
def fake():
    return FakeGitHub()


def publisher(fake: FakeGitHub) -> PullRequestPublisher:
    client = GitHubClient(CONFIG, transport=httpx.MockTransport(fake.handler), retry_backoff=0)
    return PullRequestPublisher(client, max_attempts=3, retry_delay=0)


def request(fake: FakeGitHub, **overrides) -> PublishRequest:
    data = dict(
        incident_id="101-1", base_sha=fake.base_sha, files=[PatchFile(PRICING, FIXED)],
        root_cause="discount subtracts a flat 10 instead of taking 10 percent",
        tests_passed=True, application="shopping", summary="Apply the discount as a percentage",
        test_summary="9 passed in 0.4s", workflow_url="https://github.com/acme/r/actions/runs/101",
    )
    data.update(overrides)
    return PublishRequest(**data)


# --------------------------------------------------------------------------- #
# Happy path + duplicate protection
# --------------------------------------------------------------------------- #

def test_publishes_branch_commit_and_pr(fake):
    result = publisher(fake).create_or_find_pr(request(fake))

    assert result.status == STATUS_VERIFIED, result.detail
    assert result.branch == BRANCH and result.pr_number == 1 and result.pr_state == "open"
    assert result.pr_url == f"https://github.com/{OWNER}/{REPO}/pull/1"
    assert result.commit_sha == fake.refs[BRANCH]
    assert fake.files_at(BRANCH)[PRICING] == FIXED
    assert fake.files_at(BRANCH)[OTHER] == "x = 1\n"           # untouched files preserved
    assert fake.commits[fake.refs[BRANCH]]["parents"] == [fake.base_sha]
    assert fake.files_at("main")[PRICING] != FIXED              # main is never modified
    body = fake.pulls[0]["body"]
    assert "101-1" in body and "percent" in body and "9 passed" in body and PRICING in body
    assert "The agent never merges on its own." in body
    assert result.to_dict()["pr_number"] == 1                   # snake_case contract for Dev 3


def test_running_twice_creates_exactly_one_pr_and_one_commit(fake):
    pub = publisher(fake)
    first = pub.create_or_find_pr(request(fake))
    writes_after_first = [r for r in fake.requests if r[0] in ("POST", "PATCH")]
    second = pub.create_or_find_pr(request(fake))

    assert first.status == second.status == STATUS_VERIFIED
    assert first.pr_number == second.pr_number == 1
    assert len(fake.pulls) == 1 and fake.commits_on(BRANCH) == 1
    assert [r for r in fake.requests if r[0] in ("POST", "PATCH")] == writes_after_first  # 2nd run wrote nothing


# --------------------------------------------------------------------------- #
# Lost responses and crashes
# --------------------------------------------------------------------------- #

def test_pr_created_but_response_lost_does_not_duplicate(fake):
    """The exact hackathon scenario: GitHub made the PR, the agent never heard back."""
    fake.fail("POST", "/pulls", "after")
    result = publisher(fake).create_or_find_pr(request(fake))

    assert result.status == STATUS_VERIFIED, result.detail
    assert len(fake.pulls) == 1 and result.pr_number == 1
    assert fake.count("POST", "/pulls") == 1                    # never even retried the POST
    assert any("ambiguous" in s for s in result.steps)


def test_pr_request_lost_before_reaching_github_is_retried(fake):
    fake.fail("POST", "/pulls", "before")
    result = publisher(fake).create_or_find_pr(request(fake))

    assert result.status == STATUS_VERIFIED and len(fake.pulls) == 1
    assert fake.count("POST", "/pulls") == 2


def test_branch_ref_created_but_response_lost(fake):
    fake.fail("POST", "/git/refs", "after")
    result = publisher(fake).create_or_find_pr(request(fake))

    assert result.status == STATUS_VERIFIED, result.detail
    assert fake.count("POST", "/git/refs") == 1 and len(fake.pulls) == 1


def test_commit_applied_but_response_lost_does_not_commit_twice(fake):
    fake.fail("PATCH", "/git/refs/heads/", "after")
    result = publisher(fake).create_or_find_pr(request(fake))

    assert result.status == STATUS_VERIFIED, result.detail
    assert fake.commits_on(BRANCH) == 1 and len(fake.pulls) == 1


def test_crash_midway_then_resume(fake):
    """GitHub is down while opening the PR; a later call resumes without redoing work."""
    fake.fail("POST", "/pulls", "before", times=99)
    first = publisher(fake).create_or_find_pr(request(fake))
    assert first.status == STATUS_UNKNOWN and not fake.pulls
    assert BRANCH in fake.refs and fake.commits_on(BRANCH) == 1   # branch + commit survived

    fake.faults.clear()                                           # GitHub is back
    second = publisher(fake).create_or_find_pr(request(fake))

    assert second.status == STATUS_VERIFIED and len(fake.pulls) == 1
    assert fake.commits_on(BRANCH) == 1                           # commit was not repeated
    assert any("reusing" in s for s in second.steps)


# --------------------------------------------------------------------------- #
# Safety checks: nothing is published when it should not be
# --------------------------------------------------------------------------- #

def test_untested_patch_is_refused_without_touching_github(fake):
    result = publisher(fake).create_or_find_pr(request(fake, tests_passed=False))
    assert result.status == STATUS_FAILED and "tests" in result.detail
    assert fake.requests == []


@pytest.mark.parametrize("path", [".github/workflows/shopping-ci.yml", "../etc/passwd", "/abs.py", ".env", "a/.env.prod"])
def test_unsafe_paths_are_refused_without_touching_github(fake, path):
    result = publisher(fake).create_or_find_pr(request(fake, files=[PatchFile(path, "x")]))
    assert result.status == STATUS_FAILED and "unsafe" in result.detail
    assert fake.requests == []


def test_base_moved_and_patched_file_changed_is_refused(fake):
    fake.advance("main", {PRICING: "def discount(x):\n    return x - 11\n"})
    result = publisher(fake).create_or_find_pr(request(fake))

    assert result.status == STATUS_FAILED and "regenerate" in result.detail and PRICING in result.detail
    assert BRANCH not in fake.refs and not fake.pulls


def test_base_moved_elsewhere_still_publishes_from_the_original_base(fake):
    fake.advance("main", {OTHER: "x = 2\n"})
    result = publisher(fake).create_or_find_pr(request(fake))

    assert result.status == STATUS_VERIFIED, result.detail
    assert fake.commits[fake.refs[BRANCH]]["parents"] == [fake.base_sha]
    assert any("moved" in s for s in result.steps)


def test_patch_for_a_file_that_does_not_exist_is_refused(fake):
    result = publisher(fake).create_or_find_pr(
        request(fake, files=[PatchFile("shopping/backend/app/nope.py", "x = 1\n")]))
    assert result.status == STATUS_FAILED and "do not exist" in result.detail
    assert BRANCH not in fake.refs


def test_patch_that_changes_nothing_is_refused(fake):
    same = fake.files_at("main")[PRICING]
    result = publisher(fake).create_or_find_pr(request(fake, files=[PatchFile(PRICING, same)]))
    assert result.status == STATUS_FAILED and "does not change" in result.detail
    assert BRANCH not in fake.refs


def test_unknown_base_commit_is_refused(fake):
    result = publisher(fake).create_or_find_pr(request(fake, base_sha="deadbeef" * 5))
    assert result.status == STATUS_FAILED and "does not exist" in result.detail


# --------------------------------------------------------------------------- #
# Existing PRs
# --------------------------------------------------------------------------- #

def test_pr_closed_by_a_person_is_not_recreated(fake):
    pub = publisher(fake)
    pub.create_or_find_pr(request(fake))
    fake.pulls[0]["state"] = "closed"

    result = pub.create_or_find_pr(request(fake))
    assert result.status == STATUS_VERIFIED and result.pr_state == "closed"
    assert len(fake.pulls) == 1


def test_merged_pr_is_reported_as_merged(fake):
    pub = publisher(fake)
    pub.create_or_find_pr(request(fake))
    fake.pulls[0].update(state="closed", merged=True)
    assert pub.create_or_find_pr(request(fake)).pr_state == "merged"


def test_existing_pr_whose_files_differ_needs_a_human(fake):
    pub = publisher(fake)
    pub.create_or_find_pr(request(fake))
    fake.advance(BRANCH, {PRICING: "tampered\n"})               # someone pushed to the fix branch

    result = pub.create_or_find_pr(request(fake))
    assert result.status == STATUS_FAILED and "differ" in result.detail
    assert len(fake.pulls) == 1


# --------------------------------------------------------------------------- #
# Input shapes from the other developers
# --------------------------------------------------------------------------- #

def test_accepts_the_repairer_dict_shape(fake):
    # Shape returned by Repairer.repair(): no incident id or commit sha inside it.
    repairer_output = {
        "tests_passed": True, "outcome": "verified", "diff": "--- a\n+++ b\n",
        "files": {PRICING: FIXED}, "changed_files": [PRICING],
        "summary": "Apply the discount as a percentage", "root_cause": "flat amount", "test_output": "9 passed",
    }
    with pytest.raises(ValueError):
        PublishRequest.from_dict(repairer_output)
    req = PublishRequest.from_dict(
        repairer_output, incident_id="101-1", base_sha=fake.base_sha, application="shopping")
    assert req.files == [PatchFile(PRICING, FIXED)] and req.tests_passed and req.test_summary == "9 passed"
    assert publisher(fake).create_or_find_pr(req).status == STATUS_VERIFIED


def test_builds_a_request_from_a_repair_result(fake):
    repair_result = {
        "outcome": "verified", "reason": "9 passed; chose candidate 1.1", "app": "shopping",
        "commit_sha": fake.base_sha, "patched_files": {PRICING: FIXED}, "patch": "diff",
        "diagnosis": {"likely_cause": "flat amount instead of percent"},
        "patched_suite": {"output_tail": "9 passed"},
    }
    req = PublishRequest.from_repair_result("101-1", repair_result)
    assert req.tests_passed and req.root_cause == "flat amount instead of percent"
    assert req.test_summary == "9 passed" and req.application == "shopping"
    assert publisher(fake).create_or_find_pr(req).status == STATUS_VERIFIED


def test_unverified_repair_result_is_not_publishable(fake):
    req = PublishRequest.from_repair_result(
        "101-1", {"outcome": "needs_review", "commit_sha": fake.base_sha, "patched_files": {}})
    assert validate_request(req) is not None


def test_result_has_the_contract_keys_for_developer_3(fake):
    d = publisher(fake).create_or_find_pr(request(fake)).to_dict()
    assert {"status", "pr_number", "pr_url", "branch", "commit_sha"} <= set(d)
    assert isinstance(GitHubOperationResult(status="verified").to_dict(), dict)


# --------------------------------------------------------------------------- #
# Draft PRs, caller-supplied title, and memory context in the body
# --------------------------------------------------------------------------- #

def test_pr_is_opened_as_a_draft_by_default(fake):
    result = publisher(fake).create_or_find_pr(request(fake))
    assert result.status == STATUS_VERIFIED and fake.pulls[0]["draft"] is True


def test_draft_can_be_turned_off(fake):
    client = GitHubClient(CONFIG, transport=httpx.MockTransport(fake.handler), retry_backoff=0)
    PullRequestPublisher(client, retry_delay=0, draft=False).create_or_find_pr(request(fake))
    assert fake.pulls[0]["draft"] is False


def test_repo_without_draft_support_gets_a_normal_pr(fake):
    fake.draft_unsupported = True
    result = publisher(fake).create_or_find_pr(request(fake))
    assert result.status == STATUS_VERIFIED and len(fake.pulls) == 1
    assert fake.pulls[0]["draft"] is False


def test_caller_title_and_context_notes_reach_the_pr(fake):
    req = request(fake, title="[OpsRelay] Fix shopping CI failure (run 101)",
                  context_notes="**Similar past incidents (Atlas Vector Search):**\n- (0.91) discount bug")
    publisher(fake).create_or_find_pr(req)
    pull = fake.pulls[0]
    assert pull["title"] == "[OpsRelay] Fix shopping CI failure (run 101)"
    assert "<summary>Similar past incidents</summary>" in pull["body"] and "(0.91) discount bug" in pull["body"]
    assert "The agent never merges on its own." in pull["body"]


def test_context_notes_cannot_break_the_incident_marker(fake):
    publisher(fake).create_or_find_pr(request(fake, context_notes="x --> <!-- evil"))
    body = fake.pulls[0]["body"]
    assert "evil" in body and body.count("<!--") == 1 and body.count("-->") == 1


def test_orchestrator_adapter_passes_title_and_memory_to_the_pr(fake):
    from agents.orchestrator import GitHubAdapter

    client = GitHubClient(CONFIG, transport=httpx.MockTransport(fake.handler), retry_backoff=0)
    adapter = GitHubAdapter(client, PullRequestPublisher(client, retry_delay=0))
    body = ("Automated repair for run 101.\n\n**Root cause:** x\n\n"
            "**Similar past incidents (Atlas Vector Search):**\n- (0.88) earlier discount bug\n\n"
            "OpsRelay incident `101-1` needs review.")
    patch = {"files": {PRICING: FIXED}, "root_cause": "discount bug", "summary": "fix discount"}
    out = adapter.open_draft_pr("acme/agentic-mongodb", BRANCH, fake.base_sha,
                                "[OpsRelay] Fix shopping CI failure", body, patch)
    pull = fake.pulls[0]
    assert out["number"] == 1 and pull["title"] == "[OpsRelay] Fix shopping CI failure"
    assert "(0.88) earlier discount bug" in pull["body"] and "· shopping ·" in pull["body"]
    assert "OpsRelay incident `101-1`" not in pull["body"]      # only the memory block is copied
