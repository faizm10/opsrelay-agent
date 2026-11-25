"""Deployment evidence for merged fixes: what is deployed, whether it contains the fix, and a
fresh live probe. Configured per app with server-side URLs:

    OPSRELAY_<APP>_VERSION_URL  JSON with "commit_sha"
    OPSRELAY_<APP>_PROBE_URL    200 when healthy, anything else is a failing probe

Every method returns None when it cannot tell, so the orchestrator records the evidence as
unavailable and keeps the incident open instead of guessing.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("agents.deployment")


def url(app: str, kind: str) -> str | None:
    return os.getenv(f"OPSRELAY_{app.upper()}_{kind}_URL") or None


class HttpDeployments:
    def __init__(self, github, timeout: float = 15.0, transport: httpx.BaseTransport | None = None):
        self.github = github  # read-only GitHubClient: compares commits
        self.timeout = timeout
        self.transport = transport

    def _get(self, target: str | None) -> httpx.Response | None:
        if not target:
            return None
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                return client.get(target)
        except httpx.HTTPError as exc:
            log.warning("GET %s failed: %s", target, type(exc).__name__)
            return None

    def deployed_sha(self, app: str) -> str | None:
        response = self._get(url(app, "VERSION"))
        if response is None or not response.is_success:
            return None
        try:
            return (response.json() or {}).get("commit_sha") or None
        except ValueError:
            return None

    def includes(self, repo: str, fix_sha: str, deployed_sha: str) -> bool | None:
        if fix_sha == deployed_sha:
            return True
        try:
            return self.github.contains(fix_sha, deployed_sha)
        except Exception as exc:  # noqa: BLE001 - unknown, not "not deployed"
            log.warning("compare %s...%s failed: %s", fix_sha[:7], deployed_sha[:7], exc)
            return None

    def probe(self, app: str) -> dict | None:
        response = self._get(url(app, "PROBE"))
        if response is None:
            return None
        try:
            summary = (response.json() or {}).get("summary")
        except ValueError:
            summary = None
        return {"passed": response.status_code == 200, "detail": summary or f"HTTP {response.status_code}"}
