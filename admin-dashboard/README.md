# OpsRelay Uptime Monitoring & Status Dashboard

Real availability **and** functional monitoring for each workspace's sites, with a dark,
status-page-style dashboard.

- A **monitoring worker** probes every monitored service on a fixed interval. It runs independently of any browser, records every check, tracks status, and opens, updates and resolves incidents.
- An **uptime API** (FastAPI) serves typed status, uptime, check, latency and incident data. Mutation endpoints need no token, so keep the API on localhost.
- A **status dashboard** (Next.js) refreshes live without page reloads.
- A **hand-off document** per incident lets the existing AI investigation pipeline pick up the evidence.

A homepage returning HTTP 200 is **not** treated as healthy. Each service gets three probes per cycle:

| Probe | Request | Failing it means |
| --- | --- | --- |
| Website | `GET` the page URL | **down** |
| Availability | `GET` the health URL → `{"status":"ok"}` | **down** |
| Functional | `GET`/`POST` the functional path → 200 `healthy` / 503 `unhealthy`, with the failed checks | **degraded** |

The availability and functional probes are optional per service (`uptime/models.py` `ServiceConfig`). A functional probe
should be side-effect free.

| Status | Meaning |
| --- | --- |
| **Operational** | Website, availability and the critical workflow all work |
| **Degraded** | Website and availability work, but the functional probe fails (or times out) |
| **Down** | The availability check fails or the website is unreachable |
| **Unknown** | No checks yet, data is stale (worker stopped for more than 3 intervals), monitoring is paused, or the monitor itself errored |

---

## Contents

1. [Architecture](#architecture)
2. [Setup](#setup)
3. [Running it](#running-it)
4. [Environment variables](#environment-variables)
5. [MongoDB configuration](#mongodb-configuration)
6. [Monitoring behaviour](#monitoring-behaviour)
7. [Uptime calculation methodology](#uptime-calculation-methodology)
8. [Incidents](#incidents)
9. [API reference](#api-reference)
10. [Integration with the OpsRelay agents (Developers 1–3)](#integration-with-the-opsrelay-agents)
11. [Tests](#tests)
12. [Sites and dashboard features](#sites-and-dashboard-features)
13. [Known limitations](#known-limitations)

---

## Architecture

```
admin-dashboard/
├── backend/                       Python 3.11+ (tested on 3.14)
│   ├── services.toml              monitored services, endpoints, 30 s interval, retention
│   ├── uptime/
│   │   ├── worker.py              scheduler: immediate first cycle, aligned 30 s slots, concurrency, lease
│   │   ├── probes.py              website / availability / functional HTTP probes (never raise)
│   │   ├── classify.py            operational / degraded / down + investigation route
│   │   ├── incidents.py           open → update → escalate → resolve; one open incident per service
│   │   ├── uptime_calc.py         observed uptime, coverage, timeline bars, latency series
│   │   ├── handoff.py             opsrelay.uptime_incident.v1 evidence for the AI pipeline
│   │   ├── api.py                 FastAPI dashboard API (typed envelopes, open mutations)
│   │   ├── storage/               MongoDB (Atlas) + local development (SQLite) backends
│   │   └── __main__.py            CLI: serve | worker | api | check | status
│   └── tests/                     82 deterministic tests (fake clock, mock transport, both backends)
└── web/                           Next.js 15 + TypeScript + Tailwind status dashboard (port 3300)
    └── src/app/                   /  ·  /services/[id]  ·  /incidents/[id]
```

```
 Monitored sites ◄── probes on an interval ── worker ──┬──────────────► MongoDB Atlas (uptime_* collections)
                                                      │                or local dev SQLite (clearly labelled)
                                                      └── incidents ──► /incidents/{id}/evidence ──► AI pipeline
 Browser ── :3300 Next.js ── /api proxy (server-side) ── :8300 uptime API ── storage
```

The browser only talks to Next.js. `/api/*` is proxied server-side, so the database URI never reaches the client.

---

## Setup

Prerequisites: Python 3.11+ and Node 18.18+.

```bash
cd admin-dashboard
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
cd web && npm ci && cd ..
cp .env.example .env        # optional; everything has a sensible default
```

Python dependencies: FastAPI, Uvicorn, httpx, PyMongo, python-dotenv, plus pytest and mongomock for tests. The dashboard needs Next.js, React, Tailwind and lucide-react. There is no chart library: the charts are hand-built SVG.

---

## Running it

**Option A: one process (simplest for the demo).** The API and worker run together:

```bash
cd admin-dashboard/backend && .venv/bin/python -m uptime serve        # API on :8300, checks every 30 s
cd admin-dashboard/web && npm run build && npm run start              # dashboard on :3300
```

**Option B: separate processes (production-like).** The worker keeps monitoring even with no API or browser:

```bash
cd admin-dashboard/backend && .venv/bin/python -m uptime worker       # monitoring only
cd admin-dashboard/backend && .venv/bin/python -m uptime api          # read API on :8300
```

Open **http://localhost:3300**.

Other useful commands:

```bash
.venv/bin/python -m uptime check     # one immediate cycle, printed as JSON (exit 1 if anything is not operational)
.venv/bin/python -m uptime status    # storage mode, worker lease and heartbeat, current statuses
```

`make serve`, `make worker`, `make dashboard`, `make test`, `make status` and `make check-now` are shortcuts for the above.

---

## Environment variables

See `.env.example`, which contains non-secret examples only. `admin-dashboard/.env` is git-ignored and loaded automatically.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPSRELAY_STORAGE` | `auto` | `auto`: MongoDB if `MONGODB_URI` is set, else local dev SQLite. `mongodb`: required, never falls back. `sqlite`: force local. |
| `MONGODB_URI` / `MONGODB_DB` | — / `opsrelay` | Same variables as `agents/mongodb.py` |
| `OPSRELAY_SQLITE_PATH` | `backend/data/uptime-dev.db` | Local development history file |
| `OPSRELAY_MONITOR_INTERVAL` | `300` | Seconds between cycles (tests inject their own) |
| `OPSRELAY_CHECK_RETENTION_DAYS` | `35` | Raw check retention (covers the 30-day window) |
| `OPSRELAY_LEASE_TTL` | `2 × interval + 10` | How long a crashed worker blocks a successor |
| `OPSRELAY_API_HOST` / `OPSRELAY_API_PORT` | `127.0.0.1` / `8300` | API bind address |
| `OPSRELAY_UPTIME_API_URL` | `http://127.0.0.1:8300` | Dashboard's server-side proxy target (local dev) |
| `OPSRELAY_CRON_SECRET` | — | Bearer token for `POST /api/v1/cron/check` (the GitHub Actions schedule) |
| `OPSRELAY_BLOCK_PRIVATE_URLS` | off | `1` refuses sites and check requests (redirects included) that resolve to loopback, private or other non-public addresses. On for the Vercel deployment |

---

## MongoDB configuration

**Coordination with `agents/mongodb.py`.** The monitor uses the same `MONGODB_URI` and `MONGODB_DB` (database `opsrelay`) and the same credential escaping. It deliberately does **not** write to the agents' `incidents` collection. That collection's JSON schema is CI-specific (it requires `run_id`, `run_attempt` and `commit_sha`, with a unique `repo+run_id+run_attempt` index), and uptime incidents have none of those. The monitor uses dedicated collections instead:

| Collection | Contents | Indexes and retention |
| --- | --- | --- |
| `uptime_services` | Service configuration (endpoints, interval, thresholds, enabled flag) | `_id` = service id |
| `uptime_checks` | One document per check: three probe results, status, latencies, worker id | `_id` = `<service>:<slot>` (**dedup across restarts**); `(service_id, checked_at)`; **TTL 35 days** on `checked_at` |
| `uptime_status` | Current status per service, consecutive counters, last success, open incident | `_id` = service id |
| `uptime_incidents` | Incidents with evidence, event timeline and links | **unique sparse `open_key`** (only one open incident per service); `(service_id, started_at)`; **TTL** on `expires_at` = resolved + 180 days |
| `uptime_worker` | Lease owner and expiry, heartbeat, last cycle, counters | single document |

The agents' `incidents` collection is only **read**, to show GitHub Actions run and repair PR links next to an uptime incident (`related_ci`).

`agents.mongodb` is not imported directly, because importing the `agents` package loads the whole AI stack (LangChain/OpenAI). The ~15-line connection helper is mirrored in `storage/mongo.py`.

**Development storage.** Without `MONGODB_URI`, history goes to `backend/data/uptime-dev.db` (SQLite). It persists across restarts on this machine, but it is labelled everywhere (the API's `meta.storage`, the dashboard header badge and the monitoring panel) as **"Local development storage (SQLite), not MongoDB Atlas"**. It is never presented as Atlas history. `make reset-data` deletes only this local file.

Credentials never leave the server: `meta.storage.location` shows only `host/db`, and a test asserts that the user and password are absent.

---

## Monitoring behaviour

| Requirement | How it's done |
| --- | --- |
| Every 30 seconds | `services.toml` → `interval_seconds = 30`; `OPSRELAY_MONITOR_INTERVAL` overrides it; tests inject a fake clock |
| Immediate first check | `run_forever` runs a cycle as soon as it holds the lease, then waits for the next aligned slot |
| Concurrent | All services, and all three probes per service, run under `asyncio.gather` |
| Timeouts | 3 s connect; 5 s website and availability; 10 s functional probe (all configurable) |
| No overlapping cycles | Sequential loop plus a cycle lock; a manual trigger during a cycle returns 409. Overrunning slots are skipped and counted (`skipped_slots`), never queued. |
| One failure doesn't stop others | Probes never raise; a monitor bug yields an `unknown` check (`monitor_error`) and the loop continues |
| Timestamps and latency | `checked_at`, per-probe `latency_ms`, availability and functional latency on every check |
| No duplicates after restart | Scheduled check id = `<service>:<slot>`, so the same slot can't be recorded twice. Storage also guarantees at most one open incident per service. |
| Single active worker | Lease in `uptime_worker`. A clean stop releases it immediately; a crashed worker's lease expires after 70 s. Writes are fenced on the owner. |
| Independent of browsers | The worker is a backend process; the dashboard only reads |
| Retention | MongoDB TTL indexes; the SQLite backend purges hourly |

---

## Uptime calculation methodology

Uptime is **observed** from recorded checks. It is never assumed.

1. **Coverage.** Each check vouches for the service state from its timestamp until the next check, for at most **1.5 × interval** (45 s at 30 s). Time beyond that is a **gap**.
2. **Excluded time.** Time before monitoring began, gaps, and checks with status `unknown` are **not observed**. They are excluded from the denominator and never count as uptime.
3. **Formulas:**
   - `observed = operational + degraded + down` seconds
   - `uptime_pct = operational / observed` (the service fully worked, including its critical workflow)
   - `availability_pct = (operational + degraded) / observed` (the website was reachable)
   - `coverage_pct = observed / window`
4. **Insufficient history.** Below 90% coverage, a window is `sufficient: false`. The dashboard then shows **"Insufficient history"** with the basis (for example "100% of 4m 50s observed (0.34% of window)") instead of a headline percentage. A few minutes of data is therefore never shown as "100% over 30 days".
5. **Timeline bars.** The bars are 96 × 15 min (24h), 84 × 2 h (7d) and 90 × 8 h (30d). Each bar shows the **worst** state observed in it. A bar with no observed time is **No data** (grey), not green. Bar opacity reflects that bar's coverage.
6. **Latency.** Averages and p95 use successful responses only, not *down* checks. Empty chart buckets are left blank, not drawn as zero.

---

## Incidents

- An incident **opens** when a service is degraded or down for `failure_threshold` consecutive checks (default 1, so the incident appears within one 30 s cycle).
- Further failing checks during the same outage **update** that incident. They increment `failed_check_count` and refresh `latest_failure`; no new incident is created.
- **Degraded → down** escalates the same incident, and down → degraded de-escalates it. `peak_severity` is kept, and a change of failure type is recorded as an event.
- The first operational check (`recovery_threshold`, default 1) **resolves** the incident, with `resolved_at`, the duration and the recovery check id.
- **Unknown** results never open or resolve incidents.

**Where to see them:** the overview's Incidents panel (active and recently resolved), each service's "Incidents & outages" list, and `/incidents/<id>`. The incident page shows the timeline, the failing probe checks with expected and actual values and source hints, the investigation route, CI and repair links, and the raw hand-off payload.

**Investigation routes** use the agents' vocabulary from `agents/live_incident.py`:

| Failure | Route | Meaning |
| --- | --- | --- |
| Backend or website unreachable, timeout, health HTTP error | `service_health` | Check the process, deployment, host and network. **Not** a code repair. |
| Functional probe failed (a business rule broke) | `investigate` | Reachable, but logic is wrong. Compare with recent changes and CI, then reproduce before repairing. |
| Functional probe timed out or errored | `investigate` | Cause unknown; check dependencies and logs first |

---

## API reference

Base URL `http://localhost:8300`. Every success is `{"data": …, "meta": {"generated_at", "storage", "version"}}`. Every error is `{"error": {"code", "message"}}`.

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/v1/health` | API liveness |
| GET | `/api/v1/monitoring` | Worker status (`running` / `stale` / `not_started`), lease, last cycle, interval, total checks, storage |
| GET | `/api/v1/summary?window=24h\|7d\|30d` | Overall status, services with current status, uptime stats and timeline bars, active and recent incidents, monitoring health |
| GET | `/api/v1/learning` | What the agents' RL fix policy has learned: totals, success rate per fix strategy, latest training run (before/after accuracy, learning curve), recent decisions on real incidents, Atlas Vector Search index status. Aggregates only, never logs or embeddings. `available: false` without MongoDB |
| GET | `/api/v1/services?window=` | Services with current status and uptime |
| GET | `/api/v1/services/{id}` | Configuration, current status, uptime for all three windows, incidents, incident ids |
| GET | `/api/v1/services/{id}/uptime?window=` | Uptime stats and timeline bars |
| GET | `/api/v1/services/{id}/checks?limit=&before=` | Check history (newest first) with all three probe results |
| GET | `/api/v1/services/{id}/latency?window=&points=` | Availability and functional latency series (avg/max per bucket) |
| GET | `/api/v1/incidents?state=open\|resolved\|all&service_id=&limit=` | Incidents |
| GET | `/api/v1/incidents/{id}` | Incident with timeline, links and `related_ci` (agents' CI runs and PRs, MongoDB only) |
| GET | `/api/v1/incidents/{id}/evidence` | **AI hand-off** (`opsrelay.uptime_incident.v1`) |
| PATCH | `/api/v1/services/{id}` | `{"enabled": bool}`: pause or resume monitoring of a service |
| POST | `/api/v1/monitor/check-now` | Check this workspace's sites now |
| GET | `/api/v1/sites` | This workspace's sites (`id`, `name`, `url`, `health_url`) |
| POST | `/api/v1/sites` | `{"name", "url", "health_url"?}`: add a site and check it once |
| DELETE | `/api/v1/sites/{id}` | Stop monitoring a site |
| POST | `/api/v1/cron/check` | One cycle for every workspace; needs `Authorization: Bearer $OPSRELAY_CRON_SECRET` (the GitHub Actions schedule) |
| POST | `/api/v1/incidents/{id}/acknowledge` | `{"by", "note"}` |
| POST | `/api/v1/incidents/{id}/fix-confirmation` | `{"resolved": bool}` after the agent's PR is merged. `true` asks for a recheck; `false` records that it is still broken |
| POST | `/api/v1/assistant/chat` | `{"messages": [{"role", "content"}], "incident_id"?}`: plain-language answers about current incidents (needs `OPENAI_API_KEY`) |
| POST | `/api/v1/incidents/{id}/links` | `{"kind": "github_actions_run"\|"repair_pr"\|"other", "url": "https://…", "label"}`: idempotent per URL |

Mutation endpoints are unauthenticated. Don't expose the API beyond localhost without putting your own auth in front of it.

```bash
curl -s localhost:8300/api/v1/summary | jq '.data.overall'
curl -s -XPOST localhost:8300/api/v1/monitor/check-now
```

---

## Integration with the OpsRelay agents

The uptime monitor **adds** a live-service signal. It does not replace or modify the GitHub-CI monitor (`agents/monitor.py`), the orchestrator or the repair pipeline. **No files under `agents/` were changed.**

**What is operational today:**
1. The monitor detects availability failures and functional failures separately.
2. It opens, updates, escalates and resolves one incident per outage, with structured evidence.
3. `GET /api/v1/incidents/{id}/evidence` exposes the hand-off document:
   - `investigation_route`: the same route names as `agents/live_incident.py`
   - `code_repair_candidate`
   - `probe_failures`: the apps' failing checks, with expected and actual values and `source` hints such as `backend/app/pricing.py::compute_discount`
   - `suspect_sources`
   - `live_check`: shaped like `agents.contracts.LiveCheckResult`
   - first and latest failing checks, and `next_steps`
4. Resolution is detected by the monitor and recorded on the incident.
5. With MongoDB configured, CI incidents and repair PRs recorded by the orchestrator for the same `app` around the incident window are shown as `related_ci`.

**Not wired yet (required interfaces):**

| Owner | What to connect | Interface |
| --- | --- | --- |
| **Developer 1** (GitHub monitoring) | Optionally attach the failing workflow run to an uptime incident | `POST /api/v1/incidents/{id}/links` with `kind=github_actions_run` |
| **Developer 2** (diagnosis and repair) | Consume uptime incidents whose route is `investigate` | Poll `GET /api/v1/incidents?state=open` (or watch `uptime_incidents` via a change stream), then read `/evidence`. Treat it as **evidence, not proof**: reproduce with the app's pytest suite before any patch. `live_check.delivery` is always `not_attempted`, because these probes are not the messaging send-and-readback probe. A functional failure therefore carries `failure_kind: "http_error"` (HTTP 503) and `healthy: true`, and should be mapped to `diagnose_incident`'s "reachable but failing" branch. Route `service_health` must not trigger code repair. |
| **Developer 3** (MongoDB / workflow) | Persist and resume | The monitor already writes to Atlas (`uptime_*` collections) when `MONGODB_URI` is set. To open a workflow incident from an uptime incident, write an orchestrator-specific document; the schemas intentionally differ. Repair PR links can be posted back via the links endpoint. |

There is no automatic call from the monitor into `agents.diagnose_incident`. That call needs a repository checkout, the deployed SHA and an LLM key, and those belong to the orchestrator.

---

## Tests

```bash
cd admin-dashboard/backend && .venv/bin/python -m pytest      # 82 tests, ~7 s
```

Every storage-touching test runs twice: on SQLite and on MongoDB (`mongomock`). The tests use a fake clock and fake sleep, so 30-second schedules run instantly, plus `httpx.MockTransport` fake apps. They also use real sockets for the unreachable and timeout cases.

| File | Covers |
| --- | --- |
| `test_probes.py` | Classification of 7 failure modes; "HTTP 200 homepage is not enough"; both apps' probe report formats; investigation routes; **real** refused port; **real** hanging server timing out at 0.3 s |
| `test_worker.py` | 30 s default and env override; configured endpoints match the apps; **immediate first cycle then exact 30 s cadence**; concurrent services (two 0.4 s services finish in under 0.75 s); **non-overlapping cycles**; one service down doesn't stop the other; monitor bug → unknown; **restart in the same slot doesn't duplicate**; **crashed worker's lease expires and the new worker continues the same incident**; clean shutdown hands over at once; only one worker polls |
| `test_uptime_and_incidents.py` | 100% over a full day; degraded lowers uptime but not availability; **10 minutes of data isn't 30-day uptime**; gaps are unknown; unknown excluded and down counted; empty data; bars show unknown before monitoring and the worst state per bucket; latency ignores down; **one incident per ongoing outage**; storage rejects a second open incident; escalation; **recovery with duration**, then a new incident; failure threshold |
| `test_storage.py` | **Persistence across reopen**; duplicate check ids rejected; ordered samples with timezones; lease semantics; retention purge; MongoDB TTL and unique indexes; dedicated collections (agents' `incidents` is read, never written); no credential leaks |
| `test_api.py` | Envelope; unknown before any check; **independent statuses**; uptime computed from stored checks; detail, checks and latency endpoints; hand-off evidence and routes; stale → unknown; mutations without a token; idempotent links; **the full section-9 demo scenario** |
| `test_dependencies.py` | GitHub reachability and repository access, Atlas ping, OpenAI API access, safe failure messages, and 30-second response caching |

---

## Sites and dashboard features

Each workspace adds its own sites under **Sites**: a page URL and, optionally, a health URL. The page must load
(2xx/3xx); a health URL must answer 2xx and, if it returns JSON with a `status`, report ok/healthy. A site is checked
once when it is added, then every 5 minutes by the `uptime-checks` GitHub Actions workflow, which calls
`POST /api/v1/cron/check` with the `OPSRELAY_CRON_SECRET` repository secret (the same value is set on the API).
Locally, `python -m uptime serve` checks on its own schedule instead. Entries under `[[services]]` in
`services.toml` still work for a self-hosted setup; they belong to the `demo` workspace.

Sign-in, sign-up, adding sites, **Check now** and the assistant are rate-limited per instance (HTTP 429 with
`Retry-After`). That slows password guessing and runaway model costs; it is not a shared quota across serverless instances.

The **Agent dependencies** section checks GitHub, MongoDB Atlas, and OpenAI every 30 seconds and shows connection or access failures with a next step. GitHub reachability is checked even without an agent token; repository access is checked when `GITHUB_TOKEN` and `GITHUB_OWNER` are configured. MongoDB uses a read-only `ping` when `MONGODB_URI` is present. OpenAI checks API access when `OPENAI_API_KEY` is present; it does not run a model or incur generation costs. Missing settings appear as **Not configured**. The `/api/v1/dependencies` response never includes credentials, connection strings, or provider response bodies.

The **AI learning** section shows what the repair agent's reinforcement-learning fix policy (`agents/rl_policy.py`) has learned. It reads `rl_experiences`, `rl_training_runs` and `mock_incidents` read-only: first-pick accuracy before and after training, the learning curve, success rate per fix strategy for each app, and whether the Atlas Vector Search indexes are ready. It appears once `python -m agents.rl_policy seed` and `train` have been run against the same database.

When an incident opens, the overview also shows **AI repair agent** activity for that incident: its state, last verified stage, latest recorded action, timestamps, and a repair PR link when available. This panel refreshes with the status page every 10 seconds and does not show raw failure evidence or the selected demo scenario. The data comes from the repair agent's `incidents` and `workflow_steps` collections in the **same MongoDB database** as the uptime monitor. In local SQLite mode, the panel says the agent is not connected. The root `dev.sh` launcher also starts the repair agent worker when `.env` sets `MONGODB_URI`. Set `DEV_AGENT=0` to skip it, or run `python -m agents.orchestrator worker` yourself.

When the agent's PR is merged (the agent checks GitHub every 30 seconds), its panel asks **Did this fix the problem?** **Yes** asks for a new check, so the incident resolves once the site passes. **No** records that it is still broken and leaves the incident open.

**Ask the assistant** (bottom right on every page) answers questions about current incidents and the agent's progress in everyday words. On an incident page it focuses on that incident. It uses `OPENAI_API_KEY` (model: `OPSRELAY_ASSISTANT_MODEL`, else `OPENAI_MODEL`, else `gpt-4.1-mini`) and only sees service status, failed checks without code locations, and the agent's summary. It avoids file and code details unless asked for technical details.

## Known limitations

- **No live Atlas test was possible from this machine** (no `MONGODB_URI`, and the Docker daemon was stopped). The MongoDB backend is covered by the same tests as SQLite, running on `mongomock`, which is the approach `agents/tests` uses too. Real Atlas behaviour such as TTL deletion timing is not exercised locally.
- **Single active worker by design** (lease-based). Monitoring from several regions would need a per-region lease and per-region checks.
- **Uptime is computed on read** from raw samples. That's fine at 2 services × 30 s (~86k samples per 30 days per service). Far larger fleets would want pre-aggregated rollups.
- **The agent hand-off is pull-based** (an API, or Mongo documents); the monitor does not push into the AI pipeline.
- **The dashboard has no login.** Demo controls and other mutations need no token. Put the dashboard behind your own auth if it's exposed beyond localhost.
- **Probes are run from one host.** A network partition between the monitor and an app looks like *down* (route `service_health`), which is why such incidents are never sent to code repair automatically.
