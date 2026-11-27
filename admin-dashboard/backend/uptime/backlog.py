"""Open pull requests and issues on a mapped repository -> one OpsRelay incident per repository.

A repository added with a backlog (Dependabot version bumps nobody merged, issues nobody closed) gets
one open incident listing them, so the agent worker in diagnose mode investigates them like any other
incident: which can be merged now, which need changes and which to close. The scheduled check reads
them with the workspace's GitHub token (a fine-grained token needs "Issues: read" and "Pull requests:
read"); draft PRs, the repair agent's own among them, are left out. It resolves once nothing is open.
"""

from __future__ import annotations

from datetime import datetime

from . import repositories, rollup
from .storage.base import Storage, iso


def _item(i: dict) -> dict:
    return {"id": i["number"], "number": i["number"], "kind": "pull_request" if "pull_request" in i else "issue",
            "title": i.get("title") or "", "author": (i.get("user") or {}).get("login"),
            "labels": [label["name"] for label in i.get("labels") or []], "url": i.get("html_url"), "opened_at": i.get("created_at")}


async def sync(storage: Storage, mapping: dict, now: datetime, transport=None) -> dict:
    try:
        gh = repositories.client(storage, mapping["workspace_id"], transport)
        # The issues endpoint lists pull requests too, oldest first so the longest-waiting lead the incident.
        raw = await gh.get(f"{repositories._repo_path(mapping['repo'])}/issues", state="open", sort="created",
                           direction="asc", per_page=100)
        result = _apply(storage, mapping, [_item(i) for i in raw if not i.get("draft")], now)
    except repositories.RepoError as exc:
        result = {"result": "skipped", "reason": str(exc)}
    storage.put_doc("repos", mapping["id"], {**mapping, "backlog": {"checked_at": iso(now), **result}})
    return result


def _apply(storage: Storage, mapping: dict, items: list[dict], now: datetime) -> dict:
    return rollup.apply(
        storage, mapping, "backlog", items, now, label="Open pull requests and issues", noun="open backlog item",
        cleared="Every pull request and issue is merged or closed.",
        severity=lambda items: "degraded", describe=lambda i: f"#{i['number']} {i['title'][:60]}")
