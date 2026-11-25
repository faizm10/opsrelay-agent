"""Start fresh: clear incidents in Atlas, the dev logs and old agent branches.

    python -m agents.reset            # asks before deleting anything
    python -m agents.reset --yes      # no prompt
    python -m agents.reset --learning # also clear fix memories and RL experiences

Service configuration (``uptime_services``) is kept.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from agents import mongodb as m
from agents import rl_policy

ROOT = Path(__file__).resolve().parents[1]
INCIDENT_COLLECTIONS = [m.INCIDENTS, m.WORKFLOW_STEPS, m.LIVE_INCIDENTS, "uptime_incidents", "uptime_checks",
                        "uptime_status", "uptime_worker"]
LEARNING_COLLECTIONS = [m.MEMORIES, rl_policy.RL_EXPERIENCES, rl_policy.TRAINING_RUNS]


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout


def agent_branches() -> list[str]:
    """Remote ``agent/*`` branches without an open PR."""
    remote = [line.split("refs/heads/", 1)[1] for line in _git("ls-remote", "origin", "refs/heads/agent/*").splitlines()]
    out = subprocess.run(["gh", "pr", "list", "--state", "open", "--json", "headRefName"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    open_heads = {pr["headRefName"] for pr in json.loads(out or "[]")}
    return [b for b in remote if b not in open_heads]


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m agents.reset")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--learning", action="store_true", help="also clear fix memories and RL experiences")
    a = p.parse_args()

    db = m.get_db()
    collections = INCIDENT_COLLECTIONS + (LEARNING_COLLECTIONS if a.learning else [])
    counts = {c: db[c].count_documents({}) for c in collections}
    branches = agent_branches()

    print(f"Atlas database {db.name}:")
    print("\n".join(f"  {c}: {n} document(s)" for c, n in counts.items()))
    print("The logs in .dev/logs")
    print("Remote branches:", *(f"  {b}" for b in branches) or ["  none"], sep="\n")
    if not a.yes and input("Delete all of the above? [y/N] ").strip().lower() != "y":
        print("Nothing deleted.")
        return

    for c in collections:
        db[c].delete_many({})
    for log in (ROOT / ".dev/logs").glob("*.log"):
        log.write_text("")
    if branches:
        _git("push", "origin", "--delete", *branches)
    print("Reset done. Restart ./dev.sh.")


if __name__ == "__main__":
    main()
