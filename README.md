# OpsRelay

[![opsrelay-ci](https://github.com/faizm10/opsrelay-agent/actions/workflows/opsrelay-ci.yml/badge.svg)](https://github.com/faizm10/opsrelay-agent/actions/workflows/opsrelay-ci.yml)
[![uptime-checks](https://github.com/faizm10/opsrelay-agent/actions/workflows/uptime-checks.yml/badge.svg)](https://github.com/faizm10/opsrelay-agent/actions/workflows/uptime-checks.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

OpsRelay watches your sites and, when something breaks, sends an AI agent to investigate. Add your sites to a
workspace and OpsRelay checks them every 5 minutes, opens an incident when one goes down, and can take alerts
from Better Stack. For repositories you connect, the repair agent reproduces the failure, proposes a fix, verifies
it with the app's tests and opens a pull request for a person to review. It never merges on its own.

Live dashboard: https://opsrelay-dashboard.vercel.app

**How it fits together**
- **Dashboard and uptime API** ([`admin-dashboard/`](admin-dashboard/README.md)): Next.js and FastAPI, deployed
  as one Vercel project.
- **Repair agents** ([`agents/`](agents/README.md)): Python, with incident state and repair memory in MongoDB Atlas.

## Repository map

| Directory | What it contains | Local URL |
| --- | --- | --- |
| [`admin-dashboard/`](admin-dashboard/README.md) | Uptime worker, status API, and Next.js dashboard | Dashboard: http://localhost:3300 · API: http://localhost:8300 |
| [`agents/`](agents/README.md) | CI failure monitor, diagnosis and repair pipeline, MongoDB workflow state, and GitHub PR publisher | CLI and worker |
| [`.github/workflows/`](.github/workflows/) | CI for every component, plus the 5-minute scheduled uptime checks | GitHub Actions |

The uptime service uses local SQLite by default and can use MongoDB when configured. The agents use MongoDB for incident state and repair memory. See the [database migration guide](docs/database-migration.md) to copy MongoDB data to another project, Supabase or Neon.

## Run locally

Use **Python 3.12**, **Node.js 20**, and npm. From the repository root, install and start the uptime API, the dashboard and the AI agent worker in one terminal:

```bash
./dev.sh setup
./dev.sh
```

The launcher starts the uptime API (port 8300) and the dashboard (3300), writes logs under `.dev/logs/`, and stops both with Ctrl+C. Run `./dev.sh logs` in another terminal to follow the logs. If those ports are occupied, use `DEV_PORT_OFFSET=1000 ./dev.sh` to shift them. When `.env` sets `MONGODB_URI`, the launcher also starts the AI agent worker after the dashboard is ready. The worker watches new uptime incidents, works on outages, and logs to `.dev/logs/agent-worker.log`. It also needs the agent credentials described below. If the worker stops, the servers keep running. Use `DEV_AGENT=0 ./dev.sh` to start only the servers. See [the system architecture](docs/system-architecture.md) for how an incident moves through the system. To run the investigation worker in production (report-only by default), see [the worker runbook](docs/runbook-agent-worker.md).

To run the dashboard components separately, use their Make targets. First install them:

```bash
cd admin-dashboard && make install
```

Then start these commands in separate terminals, each from the repository root:

```bash
cd admin-dashboard && make serve
cd admin-dashboard && make dashboard-dev
```

Open the [status dashboard](http://localhost:3300). The dashboard begins recording checks when its worker starts. For settings and probes, see the READMEs linked in the table above.

## Run the CI repair agent

The agent worker needs a MongoDB connection, a GitHub token with access to this repository, and an OpenAI API key. Copy [`.env.example`](.env.example) to `.env` at the repository root and fill in `MONGODB_URI`, `GITHUB_TOKEN`, and `GITHUB_OWNER`. Set `OPENAI_API_KEY` in your environment or add it to the untracked `.env` file. `VOYAGE_API_KEY` is optional; without it, repair memory uses a local embedding fallback.

```bash
python3.12 -m venv agents/.venv
agents/.venv/bin/python -m pip install -r requirements.txt
agents/.venv/bin/python -m agents.orchestrator setup
agents/.venv/bin/python -m agents.orchestrator worker
```

The worker polls failed GitHub Actions runs on the configured base branch, saves and resumes incidents in MongoDB, verifies candidate patches with the app tests, and publishes a PR when a repair passes. It leaves the merge to a reviewer. See [the agent guide](agents/README.md) for the repair process, one-shot live checks, and incident commands.

## Checks

Run these from the repository root after installing the relevant component:

```bash
cd admin-dashboard && make test && make typecheck
```

For the agent tests, run `agents/.venv/bin/python -m pytest agents/tests -q` from the root. The workflows in [`.github/workflows/`](.github/workflows/) run the same checks when a component's files change.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) to get set up, and the
[code of conduct](CODE_OF_CONDUCT.md). Report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
