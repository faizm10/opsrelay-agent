"""Per-workspace GitHub connection and repository -> service mappings.

A workspace connects GitHub with a token (a fine-grained personal access token works); the
repositories it can list are the ones the workspace may map. The token is stored server-side and
never returned by the API.
"""

from __future__ import annotations

import os
import re
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .storage.base import Storage, iso

GITHUB_API = "https://api.github.com"
AGENT_APPS = Path(__file__).resolve().parents[3] / "agents" / "apps.toml"


class RepoError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.token, self.transport = token, transport

    async def get(self, path: str, **params: Any) -> Any:
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}",
                   "User-Agent": "opsrelay-dashboard"}
        try:
            async with httpx.AsyncClient(base_url=GITHUB_API, timeout=15, transport=self.transport) as client:
                res = await client.get(path, headers=headers, params=params)
        except httpx.RequestError as exc:
            raise RepoError(502, "Cannot reach the GitHub API. Try again in a moment.") from exc
        if res.status_code == 401:
            raise RepoError(422, "GitHub rejected the token. Check it hasn't expired, or connect a new one.")
        if res.status_code in (403, 429) and res.headers.get("x-ratelimit-remaining") == "0":
            raise RepoError(429, "GitHub's rate limit for this token is used up. Try again later.")
        if res.status_code in (403, 404):
            raise RepoError(404, "Not found on GitHub, or the token can't access it.")
        if res.status_code >= 400:
            raise RepoError(502, f"GitHub answered HTTP {res.status_code}.")
        return res.json()


def _repo_path(full_name: str) -> str:
    owner, _, name = full_name.partition("/")
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def connection(storage: Storage, ws: str) -> dict | None:
    return storage.get_doc("github", ws)


def client(storage: Storage, ws: str, transport=None) -> GitHub:
    conn = connection(storage, ws)
    if conn is None:
        raise RepoError(409, "Connect GitHub for this workspace first.")
    return GitHub(conn["token"], transport)


async def connect(storage: Storage, ws: str, token: str, now: datetime, transport=None) -> dict:
    user = await GitHub(token.strip(), transport).get("/user")
    conn = {"token": token.strip(), "login": user["login"], "connected_at": iso(now)}
    storage.put_doc("github", ws, conn)
    return public_connection(conn)


def public_connection(conn: dict | None) -> dict:
    return {"connected": True, "login": conn["login"], "connected_at": conn["connected_at"]} if conn else {"connected": False}


async def list_repos(gh: GitHub) -> list[dict]:
    repos = await gh.get("/user/repos", per_page=100, sort="updated")
    return [{"full_name": r["full_name"], "default_branch": r["default_branch"], "private": r["private"]} for r in repos]


async def repo_options(gh: GitHub, full_name: str) -> dict:
    path = _repo_path(full_name)
    branches = await gh.get(f"{path}/branches", per_page=100)
    workflows = (await gh.get(f"{path}/actions/workflows", per_page=100)).get("workflows", [])
    return {"branches": [b["name"] for b in branches],
            "workflows": [{"file": w["path"].rsplit("/", 1)[-1], "name": w["name"]} for w in workflows]}


async def add_mapping(storage: Storage, ws: str, gh: GitHub, body: dict, now: datetime) -> dict:
    path = _repo_path(body["repo"])
    repo = await gh.get(path)
    try:
        await gh.get(f"{path}/branches/{quote(body['base_branch'], safe='')}")
    except RepoError as exc:
        raise RepoError(422, f"Branch '{body['base_branch']}' doesn't exist in {repo['full_name']}.") from exc
    if body.get("workflow"):
        try:
            await gh.get(f"{path}/actions/workflows/{quote(body['workflow'], safe='')}")
        except RepoError as exc:
            raise RepoError(422, f"Workflow '{body['workflow']}' isn't in {repo['full_name']}.") from exc
    service_id = re.sub(r"[^a-z0-9]+", "-", body["service_name"].lower()).strip("-")[:40] or "service"
    mapping = {"id": f"{ws}:{service_id}", "workspace_id": ws, "service_id": service_id, "service_name": body["service_name"].strip(),
               "repo": repo["full_name"], "base_branch": body["base_branch"], "deployed_url": body["deployed_url"],
               "workflow": body.get("workflow") or None, "created_at": iso(now)}
    if not storage.insert_doc("repos", mapping["id"], mapping):
        raise RepoError(409, f"This workspace already has a service called '{mapping['service_name']}'.")
    return public_mapping(mapping)


def public_mapping(m: dict) -> dict:
    return {**{k: m[k] for k in ("service_id", "service_name", "repo", "base_branch", "deployed_url", "workflow", "created_at")},
            "betterstack_monitor_id": m.get("betterstack_monitor_id"), "dependabot": m.get("dependabot"), "actions": m.get("actions"),
            "sentry_project": m.get("sentry_project"), "vercel_project": m.get("vercel_project"), "vercel": m.get("vercel"),
            "agent": m.get("agent") or {"enabled": False, "mode": "report_only", "app": None}}


def agent_config() -> dict:
    """What the shared Python worker can investigate: its repository and the code areas in agents/apps.toml."""
    owner, name = os.environ.get("GITHUB_OWNER", ""), os.environ.get("GITHUB_REPO", "opsrelay-agent")
    # Frontend (node) apps only take CI failures, so they aren't offered for live incidents.
    apps = sorted(k for k, a in tomllib.loads(AGENT_APPS.read_text()).get("apps", {}).items()
                  if a.get("kind", "python") == "python") if AGENT_APPS.exists() else []
    return {"worker_repo": f"{owner}/{name}" if owner else None, "apps": apps}


def set_agent(storage: Storage, ws: str, service_id: str, settings: dict) -> dict:
    mapping = storage.get_doc("repos", f"{ws}:{service_id}")
    if mapping is None:
        raise RepoError(404, f"Unknown service '{service_id}'.")
    if settings["mode"] == "diagnose":  # reads logs and commits only, so any mapped repository qualifies
        settings = {**settings, "app": None}
    elif settings["enabled"]:
        conf = agent_config()
        if conf["worker_repo"] is None:
            raise RepoError(409, "Set GITHUB_OWNER and GITHUB_REPO so the shared worker knows its repository.")
        if mapping["repo"].lower() != conf["worker_repo"].lower():
            raise RepoError(409, f"The shared worker investigates {conf['worker_repo']} only; "
                                 f"{mapping['repo']} needs its own worker and repair config; choose Diagnose instead.")
        if not conf["apps"]:
            raise RepoError(409, "No code areas are set up for repair in agents/apps.toml; choose Diagnose instead.")
        if settings["app"] not in conf["apps"]:
            raise RepoError(422, f"Choose which code area the agent repairs: {', '.join(conf['apps'])}.")
    agent = {"enabled": settings["enabled"], "mode": settings["mode"], "app": settings["app"]}
    storage.put_doc("repos", mapping["id"], {**mapping, "agent": agent})
    return {"service_id": service_id, "agent": agent}


def mappings(storage: Storage, ws: str) -> list[dict]:
    return [public_mapping(m) for m in storage.find_docs("repos", "workspace_id", ws)]


def remove_mapping(storage: Storage, ws: str, service_id: str) -> None:
    mapping = storage.get_doc("repos", f"{ws}:{service_id}")
    if mapping is None:
        raise RepoError(404, f"Unknown service '{service_id}'.")
    if mapping.get("betterstack_monitor_id"):
        storage.delete_doc("bs_monitors", f"{ws}:{mapping['betterstack_monitor_id']}")
    if mapping.get("sentry_project"):
        storage.delete_doc("sentry_projects", f"{ws}:{mapping['sentry_project']}")
    storage.delete_doc("repos", f"{ws}:{service_id}")
