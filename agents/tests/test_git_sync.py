"""Offline tests for git_sync: the repair agent must have the failing commit locally.

Uses real throwaway git repos (a "remote" and a stale clone), no network.
Run from the repo root:   pytest agents/test_git_sync.py -v
"""
from __future__ import annotations

import subprocess

import pytest

from agents.git_sync import CommitUnavailable, SyncingRepairer, ensure_commit, has_commit


def git(cwd, *args):
    out = subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "a.txt").write_text("1\n")
    git(origin, "add", "."); git(origin, "commit", "-qm", "first")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    (origin / "a.txt").write_text("2\n")                  # a new commit the clone has not seen
    git(origin, "commit", "-qam", "second")
    return origin, clone, git(origin, "rev-parse", "HEAD")


def test_missing_commit_is_fetched(repos):
    _, clone, new_sha = repos
    assert not has_commit(clone, new_sha)
    assert ensure_commit(clone, new_sha) is True
    assert has_commit(clone, new_sha)


def test_present_commit_needs_no_fetch(repos):
    _, clone, _ = repos
    assert ensure_commit(clone, git(clone, "rev-parse", "HEAD")) is True


def test_unknown_commit_returns_false_without_raising(repos):
    _, clone, _ = repos
    assert ensure_commit(clone, "0" * 40) is False
    assert ensure_commit(clone, "not-a-sha") is False
    assert ensure_commit(clone / "missing-dir", "a" * 40) is False


def test_checked_out_branch_is_untouched(repos):
    _, clone, new_sha = repos
    head = git(clone, "rev-parse", "HEAD")
    ensure_commit(clone, new_sha)
    assert git(clone, "rev-parse", "HEAD") == head and (clone / "a.txt").read_text() == "1\n"


class Inner:
    def __init__(self): self.calls = []
    def repair(self, failed_run, similar):
        self.calls.append(failed_run); return {"outcome": "verified"}


def test_syncing_repairer_fetches_then_repairs(repos):
    _, clone, new_sha = repos
    inner = Inner()
    out = SyncingRepairer(inner, clone).repair({"commit_sha": new_sha}, [])
    assert out == {"outcome": "verified"} and len(inner.calls) == 1 and has_commit(clone, new_sha)


def test_syncing_repairer_raises_a_clear_retryable_error(repos):
    _, clone, _ = repos
    inner = Inner()
    with pytest.raises(CommitUnavailable, match="not in the local clone"):
        SyncingRepairer(inner, clone).repair({"commit_sha": "f" * 40}, [])
    assert inner.calls == []                                # repair never ran on a missing commit
