# Contributing to OpsRelay

Thanks for helping. Bug reports, fixes and small focused improvements are all welcome.

## Before you start

- For anything bigger than a bug fix, open an issue first so we can agree on the approach.
- Read [`docs/system-architecture.md`](docs/system-architecture.md) for how the pieces fit together.
- Coding agents (Claude Code, Codex) follow [`AGENTS.md`](AGENTS.md); its rules apply to people too.

## Set up

You need Python 3.12, Node.js 20 and npm. MongoDB is optional: the dashboard falls back to SQLite.

```bash
cp .env.example .env     # fill in only what you need
./dev.sh setup           # installs every component
./dev.sh                 # starts the apps, the uptime API and the dashboard
```

The dashboard runs at http://localhost:3300. Sign up, then add a site under **Sites**.

## Make a change

- Keep pull requests small and about one thing. Match the surrounding style; don't reformat code you
  didn't change.
- Add a test only for behaviour that can break silently (data integrity, workspace isolation,
  idempotency, crash recovery). One focused test per behaviour, in an existing test file when possible.
- Never commit secrets. `.env` files are ignored; use `.env.example` for new settings.

## Run the checks

Run the suite for what you changed:

| Component | Command (from the repository root) |
| --- | --- |
| Uptime API | `cd admin-dashboard/backend && .venv/bin/python -m pytest -q` |
| Dashboard | `cd admin-dashboard/web && npx tsc --noEmit && npm run build` |
| Agents | `agents/.venv/bin/python -m pytest agents/tests -q` |

CI runs the same checks on every pull request.

## Open the pull request

Describe what changed, why, and how you verified it (the template will prompt you). Pull requests opened
by the OpsRelay repair agent need a human review like any other; the agent never merges its own work.

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
