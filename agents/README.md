# OpsRelay AI diagnosis and repair

Takes a failed CI run **or a failed live service check** for an app in `apps.toml`. It returns a
**verified** minimal patch, or **needs_review** with a reason and the next diagnostic to run.
For live failures it also confirms recovery with a fresh delivery probe.

```
live_check.py     one-shot health + send-and-deliver probe (Hamza's loop, Mo after a fix, CLI)
live_incident.py  route a failed live check; confirm_recovery(); diagnose_incident() for both kinds
verifier.py       failed commit in a temp copy: reproduce (x2) -> apply each candidate -> rerun
investigator.py   CI log / live probe + service logs + reproduction + diff + source -> grounded Diagnosis
patcher.py        Diagnosis (+ failed attempts) -> up to 3 minimal diffs, scope-checked; Repairer for Mo
intake.py         new uptime incidents (Taha's monitor) -> live incidents in Mo's incidents table
live_repair.py    Repairer's live path: service health vs code, evidence gathering, PR investigation text
regression.py     writes + proves a new regression test when the existing suite misses a live bug
pipeline.py       repair_ci_failure()
```

## Incident agent (end to end)

When a mapped app breaks, the agent investigates, writes a tested fix and opens a
**draft PR that a person must approve**. It never merges anything itself.

```
Taha's uptime monitor ──> uptime_incidents ──> intake.py ──> incidents table (source="live")
Hamza's CI monitor  ──────────────────────────────────────> incidents table (CI run)
                                                                │  Mo's orchestrator, crash-safe stages
                                                                ▼
         Repairer (this module): deep dive with OpenAI -> reproduce -> fix -> verify
                                                                │  only verified fixes go on
                                                                ▼
         Hamza's publisher: branch agent/fix-... + DRAFT PR (evidence, tests, investigation)
                                                                │  a human reviews on GitHub
                                                                ▼
         check_reviews: merged -> "resolved"   closed -> "rejected" (fix memory switched off)
```

**What the deep dive does for a live incident** (`live_repair.py`):

1. **Service down, timing out or returning errors:** no AI call and no code change. The
   incident goes to a person with a checklist.
2. **Up but a business check fails** (e.g. checkout totals wrong):
   - The agent gathers the failing probe checks, including the app's own hint of which
     function is wrong, plus the last commits that touched the app, the source and related tests.
   - OpenAI diagnoses. Every piece of evidence must be quoted word for word from those inputs.
     A misquoted diagnosis gets one retry and is otherwise rejected.
3. **Reproduce:**
   - If the app's tests fail at the deployed commit, that failure is the reproduction.
   - If they all pass, the tests miss the bug. The agent writes a new
     `tests/test_opsrelay_*.py`. It is accepted only if it fails on the deployed code with an
     assertion error, on every rerun, and the rest of the suite still passes.
4. **Fix:** up to 3 small candidate fixes. The smallest one that makes the failing tests and
   the whole suite pass wins. The new test ships in the PR next to the fix.
5. **Anything uncertain** becomes `needs_review` with the reason and the next thing to check.

**Run it** (needs `MONGODB_URI`, `OPENAI_API_KEY`, `GITHUB_TOKEN`, `GITHUB_OWNER` in `.env`):

```bash
python -m agents.orchestrator setup     # once: collections, indexes, vector index
python -m agents.orchestrator worker    # forever: CI + live intake, deep dive, PRs, review tracking
# one-shot steps, handy for demos:
python -m agents.orchestrator intake    # record new uptime incidents
python -m agents.orchestrator resume    # work every open incident
python -m agents.orchestrator reviews   # record merged / closed agent PRs
python -m agents.orchestrator list
```

Set `OPSRELAY_LIVE_INTAKE=0` to run the worker on CI failures only. The uptime monitor must
use MongoDB storage (it does whenever `MONGODB_URI` is set) and the same `MONGODB_DB`.

## How it gets to a precise fix

1. **Reproduce first.** The failing tests run `baseline_runs` (2) times at the failed commit
   before any model call. If they pass on any rerun the result is `needs_review` with
   `flaky=True`; if they never fail it is "baseline did not reproduce". Neither costs a model call.
2. **Traceback-guided diagnosis.** Frames from the CI log *and* the local rerun are parsed
   (pytest and Python formats, GitHub runner absolute paths included) and mapped to repo
   paths. The model sees the frame list and line-numbered source, suspect files are ranked
   deepest frame first, and the local rerun output counts as grounded evidence.
3. **Multiple candidates.** The model returns up to `max_candidates` (3) alternative fixes.
   Each is applied to its own fresh copy and must pass the failing tests on every rerun plus
   the full app suite. The smallest passing patch (fewest changed lines) wins.
4. **Retry with feedback.** If none pass, round 2 sends the failed patches and their real
   test output back to the model (`max_rounds` = 2). Repeated diffs are rejected without rerunning.

No GitHub or MongoDB writes happen here. The orchestrator stores
`result.model_dump()`, and the PR module opens a PR from a verified `result.patch`.

## Live checks

```bash
agents/.venv/bin/python -m agents live-check messaging                       # URL from services.toml
agents/.venv/bin/python -m agents live-check messaging --url https://msg.example --timeout 3
OPSRELAY_MESSAGING_URL=https://msg.example agents/.venv/bin/python -m agents live-check messaging
```

The JSON result is printed to stdout and a one-line verdict to stderr. The exit code is
0 when the probe is delivered, 1 when the check fails, and 2 on bad usage. `--fixture`
labels the result `"target": "fixture"` so fixture runs are never mistaken for the live app.

Each check sends a probe with a fresh id (`opsrelay-<uuid4>`) and reports one failure kind:

| `failure_kind` | Meaning | `reachable` |
|---|---|---|
| `connection_timeout` | no response within `timeout_seconds`; the cause is unknown | false |
| `connection_refused` | nothing accepting connections at the URL | false |
| `connection_error` | DNS, TLS or another network failure before any HTTP response | false |
| `http_error` | 4xx/5xx from health, send or read-back (`http_status` is set) | true |
| `invalid_response` | 2xx, but the body breaks the contract below | true |
| `not_delivered` | send accepted, but this probe id never appeared in read-back within `delivery_wait_seconds` | true |

### Live check probe contract (Taha's Messaging endpoints)

Taha's endpoints don't exist yet, so everything here was verified against the local
fixture in `tests/messaging_support.py`. That fixture implements exactly this contract.
All paths are configurable in `services.toml`.

```
GET  /health                                   -> 2xx (JSON or plain text)
POST /probe/messages                           {"probe_id", "inbox": "opsrelay-probe", "body"}
                                               -> 2xx {"probe_id", "message_id", "status"}
GET  /probe/inbox/{inbox}?probe_id=<id>        -> 200 {"messages": [{"probe_id", "message_id", ...}]}
```

The probe is delivered only when the read-back list contains this exact `probe_id`.
`opsrelay-probe` is a dedicated test inbox. If the app would rather run its own probe,
set `mode = "synthetic"`:

```
POST /probe/delivery  {"probe_id", "inbox"}  -> 200 {"probe_id": <echoed>, "delivered": bool, "latency_ms"?, "error"?}
```

### Calling it

```python
# Hamza: inside the 30-second polling loop. It never raises on network errors.
from agents import run_live_check
check = run_live_check("messaging", base_url=None, timeout=None)   # -> LiveCheckResult
if not check.ok:
    ...  # hand to Mo as live incident evidence: check.model_dump()

# Mo: diagnose. Accepts a CI RepairRequest or a LiveIncident.
from agents import LiveIncident, diagnose_incident
result = diagnose_incident(LiveIncident(
    check=check, repo_path="/srv/checkout", deployed_sha="3f2c9e1",
    service_logs=recent_log_text, recent_diff=diff_text, source_files={...},
    ci_status="passing",            # "passing" | "failing" | "unknown"
))
result.outcome      # "healthy" | "needs_review" | "verified"
result.route        # "none" | "service_health" | "investigate" | "code_repair"
result.likely_cause, result.recommended_actions, result.next_diagnostic
result.recovery     # "pending_deploy" when a patch is verified
result.repair       # RepairResult with the verified patch (route == "code_repair")

# Mo: after the fix is deployed or the service is restored.
from agents import confirm_recovery
recovery = confirm_recovery("messaging", previous_probe_id=check.probe_id)
recovery.recovered  # True only if a *fresh* probe id was confirmed delivered
```

### How a failed live check is routed

1. **Unreachable** (timeout, refused, network error): route `service_health`. The result
   recommends checking deployment, process and host health. There is no model call and no
   code repair. A timeout alone leaves the cause `unknown`. If `ci_status="passing"`, the
   reason notes that a code regression is not the first suspect.
2. **Health endpoint errors**: route `service_health`.
3. **Reachable but not delivered**: the model compares the probe result with service logs,
   the recent diff and source. Evidence must quote those inputs verbatim. The pipeline
   continues to code repair only when:
   - the diagnosis is grounded and has `failure_class == "code_bug"`, **and**
   - the app's pytest suite fails at the deployed commit, so the failure reproduces.

   Then the standard repair pipeline runs on that local failure. Otherwise the result is
   `needs_review` with `next_diagnostic`, for example "check the delivery worker" or
   "write a regression test that reproduces ...".
4. **Verified patch**: `recovery = "pending_deploy"`. Recovery is reported only by
   `confirm_recovery`, once a fresh probe is confirmed delivered.

## Setup

```bash
uv venv --python 3.12 agents/.venv            # or: python3.12 -m venv agents/.venv
uv pip install --python agents/.venv/bin/python -r agents/requirements.txt
export OPENAI_API_KEY=...                     # required for live model calls
export OPENAI_MODEL=gpt-4.1-mini              # optional, this is the default
# or, with no API key, your Claude Pro/Max subscription (`claude login` first; your own repositories only):
export OPSRELAY_LLM=claude-code CLAUDE_MODEL=sonnet
```

## Tests

```bash
agents/.venv/bin/python -m pytest agents/tests -q
```

Run the live incident scenarios (healthy, unreachable while CI is green, responds but no
delivery then fix and recovery, and a paused queue). They all run against the Messaging fixture:

```bash
agents/.venv/bin/python -m pytest agents/tests/test_live_scenarios.py -s -q
```

To try the CLI by hand, start the fixture in one terminal and check it from another:

```bash
agents/.venv/bin/python -m agents.tests.messaging_support --mode bug --port 8002   # healthy | bug | paused | hang
agents/.venv/bin/python -m agents live-check messaging --fixture
```

Replay the Shopping discount regression end to end, printing the failing-before and
passing-after pytest output and the patch:

```bash
agents/.venv/bin/python -m pytest agents/tests/test_shopping_repair.py::test_shopping_discount_failure_reproduced_then_fixed -s -q
```

These tests use a stub model. `test_live_openai.py` runs the same regression
against the real model. Set a valid `OPENAI_API_KEY` and opt in explicitly:

```bash
OPSRELAY_RUN_LIVE_TESTS=1 agents/.venv/bin/python -m pytest agents/tests/test_live_openai.py -q
```

## Interface for the orchestrator

Mo's orchestrator (`feature/mongodb-workflow-recovery`) imports `agents.patcher.Repairer`:

```python
from agents.patcher import Repairer
patch = Repairer(repo_path="/srv/checkout").repair(failed_run, similar)  # Mo's FailedRun, memory matches
patch["tests_passed"], patch["diff"], patch["files"], patch["changed_files"], patch["summary"], patch["root_cause"]
```

It reads source files and the recent diff from `repo_path` (or `OPSRELAY_REPO_PATH`) at
`commit_sha`. Then it runs the pipeline below and returns Mo's `PatchResult` dict, where
`files` holds the full patched file contents, plus `outcome` and `reason`.

The typed call underneath:

```python
from agents import PriorIncident, RepairRequest, repair_ci_failure

result = repair_ci_failure(RepairRequest(
    app="shopping",                        # key in agents/apps.toml
    repo_path="/srv/opsrelay/checkouts/demo",  # local clone containing commit_sha (read only)
    commit_sha="3f2c9e1",
    failed_test_logs=job_log_text,         # raw GitHub Actions job log
    recent_diff=diff_text,                 # e.g. `git diff <parent> <sha>`
    source_files={"shopping/backend/app/pricing.py": pricing_source_at_sha},
    similar_incidents=[PriorIncident(id="inc-42", summary="...", root_cause="...")],
))

result.outcome          # "verified" | "needs_review"
result.reason           # why, in one line
result.flaky            # failing tests passed on a baseline rerun
result.diagnosis        # likely_cause, explanation, evidence[], suspect_files (ranked),
                        # failing_tests, traceback_frames[], confidence
result.patch            # chosen unified diff against commit_sha (git apply -p1)
result.changed_files
result.baseline         # [TestRun] failing tests on the original code, one per rerun
result.reproduce_after_patch  # [TestRun] same tests with the chosen patch
result.patched_suite    # TestRun: full app suite after the patch
result.attempts         # [CandidateAttempt] every candidate, round, outcome and output
result.started_at, result.finished_at
```

`repair_ci_failure(request, llm=...)` accepts any object with
`generate(schema, system, user)` in place of the default OpenAI model.
Model and API errors are raised so the orchestrator can apply its own retry policy.

## Safety rules

- **Evidence is grounded.** Each excerpt must appear verbatim in the input it cites.
  At least one must come from the current run, because similar incidents are hints,
  not proof. Suspect files must be supplied and sit inside the app's source dirs.
  If any of these checks fails, the result is `needs_review`.
- **Untrusted input stays data.** CI logs, diffs, source and incidents reach the
  model inside delimited blocks. Attempts to close a block early are neutralised.
- **Patches stay in scope.** A patch may only make in-place edits to existing files
  under `source_dirs`, with at most 3 files and 60 changed lines. Tests, CI config,
  dependency or build manifests, secrets, hidden files, new or deleted files,
  renames, mode changes and binary patches are all rejected. The verifier checks
  scope again before applying anything.
- **Verification is isolated.** The verifier exports the commit with `git archive`
  into a temp directory, so the developer's checkout is never modified. It runs
  pytest with a scrubbed environment (no API keys, `MONGODB_URI` or `GITHUB_TOKEN`),
  using fixed arguments from `apps.toml` plus validated test ids, under a timeout
  that kills the whole process group.
- **Not a container.** Tests can still reach the network, and the app's
  dependencies must be installed in the verifier's Python environment.

## App configuration

`apps.toml` sets each app's root, editable `source_dirs`, `test_dir`, fixed
`pytest_args` and timeout. `baseline_runs`, `max_candidates` and `max_rounds` default to
2, 3 and 2 and can be set per app.

The shipped `apps.toml` has no apps yet; its header shows a Python and a Node entry to copy.

- **Python apps:** the verifier runs pytest with the agents interpreter, so the app's test
  dependencies must be installed in `agents/.venv`.
- **Frontends** (`<app>-web`, `kind = "node"`): a CI run where only the
  frontend job failed is routed here. The verifier runs `install` (`npm ci`) once at the failed
  commit with the process runner, since it needs the network, then every command in `checks`
  (`npm run typecheck`, `npm run build`) must pass. Patches may edit `src/` and `tsconfig.json`,
  never `package.json` or the lockfile, so an upgrade that needs another package goes to review.
- **Dependabot:** failed runs on `dependabot/*` branches are picked up too, one per branch and
  commit. The fix PR targets the Dependabot branch: merge it there, let that PR go green, then
  merge the Dependabot PR. The incident resolves when the fix PR merges, since nothing deploys
  on its own. Once you push to its branch, Dependabot stops rebasing that PR.
- **Overrides:** set `OPSRELAY_APPS_CONFIG` to use a different file. The test suite points
  it at `tests/apps.toml`, which describes the stand-in fixtures.

---



## MongoDB & workflow recovery (Mo)

| File | What it does |
|---|---|
| `mongodb.py` | Atlas connection, `incidents` / `workflow_steps` / `memories` collections, JSON-schema validation, unique dedup index, TTL index, Vector Search index, change stream helper |
| `memory.py` | Voyage embeddings (offline fallback), `find_similar` via `$vectorSearch` scoped per app, `remember_fix`, `seed_memory`, `forget` |
| `orchestrator.py` | Stage machine with leases, fenced writes, PR-intent-before-GitHub, verify-after-send, resume, kill switch, CLI |
| `fakes.py`, `dev_demo.py` | Fake GitHub/LLM so the full flow + crash demo runs before the other modules land |

### Stages (incident.stage = last *verified* stage)
`detected → memory_retrieved → patch_verified → pr_intent_saved → pr_confirmed → memory_saved`

### Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill MONGODB_URI
python -m agents.mongodb            # creates collections, indexes, vector index
python -m pytest agents/tests -q    # runs offline with mongomock
```

### Crash demo (fake GitHub, real Atlas)
```bash
python -m agents.dev_demo reset
OPSRELAY_CRASH_AT=pr_created python -m agents.dev_demo run   # PR created, then worker dies
python -m agents.dev_demo run                                # new worker resumes, adopts same PR
python -m agents.orchestrator status "faizm10/opsrelay-agent#9000001.1"
```

### Interfaces the other modules must provide
See the docstring at the top of `orchestrator.py`. Summary:

- **Hamza** – `monitor.poll_failed_runs() -> [FailedRun]`, `github.find_pr(repo, branch)`, `github.open_draft_pr(repo, branch, base_sha, title, body, patch)` (idempotent on branch; commits `patch["files"]`).
- **Faiz** – `repairer.repair(failed_run, similar) -> {tests_passed, test_output, diff, files, changed_files, summary, root_cause}`.
- Wire the real classes in `_build_real()` in `orchestrator.py`.

### Live incidents: system check every 30 s (Mo)

`agents/live_incidents.py`. When `OPSRELAY_LIVE_SERVICES` names services, the worker
(`python -m agents.orchestrator worker`) checks them every `POLL_SECONDS` (30), alongside CI. Use
`python -m agents.orchestrator live [--once]` for live checks only. Each service's availability and
side-effect-free functional probe paths are in `SYSTEM_CHECKS`; set its URL with `OPSRELAY_<SERVICE>_URL`.

- **Fails:** opens the service's incident in the `live_incidents` collection, or updates it. There is at most one unresolved incident per service.
- **Evidence:** the exact failed step (URL, HTTP status, failed checks), the deployed commit (`HEAD` of `OPSRELAY_REPO_PATH`), the last 3 commits' changes under the app, the app's log lines, the latest CI status, and similar incidents from Atlas Vector Search. This is stored on the incident as `evidence`.
- **Investigation:** hands Faiz's `LiveIncident` to `diagnose_incident` once per incident, retrying up to 3 times on errors. The result is stored as `investigation`.
- **Passes again:** the incident is resolved, with the fresh probe id recorded as proof of recovery.
- Every step is logged to `workflow_steps`.

Settings: `OPSRELAY_LIVE_SERVICES` (off when unset or `none`), `OPSRELAY_<SERVICE>_URL`,
`OPSRELAY_<APP>_LOG_FILE`, `OPSRELAY_<APP>_DEPLOYED_SHA`.

## Reinforcement-learning fix policy (Mo)

OpsRelay learns **which kind of fix works for which kind of failure** from rewards, and
stores everything it learns in Atlas. It's a contextual bandit (Thompson sampling), so the
LLM itself is not retrained: the policy's pick is passed to the repair agent as a hint.

| | |
|---|---|
| **State** | the failure: app, failing tests or probe checks, and the signal lines of the log, embedded like incident memory |
| **Actions** | fix playbooks in `rl_policy.PLAYBOOKS`: one targeted fix per known bug (discount divisor, quantity multiplier, stock check, idempotency guard, delivery status, message ordering) plus generic moves (revert last commit, defensive guard, retry flaky test, raise timeout) |
| **Reward** | 1 if the patch passed the tests, 0 if it failed. It's revised when a human merges the PR (1) or closes it (0) |
| **Memory** | every attempt is a document in `rl_experiences`, searched with the Atlas Vector Search index `experience_vector_index` |

**How a decision is made:** find the 25 most similar past attempts with Vector Search, then
build a Beta(successes, failures) belief per action, weighted by similarity. Sample from each
belief and pick the highest. Actions that failed on similar incidents lose weight, and on a
retry the action just tried is excluded, so the system learns from its mistakes.

**Wired into the orchestrator:**
- At `memory_retrieved`, the pick and its ranking are saved on the incident (`rl`). If the pick is backed by enough evidence, it's also shown to the repair agent after the memory matches.
- At `patch_verified`, the reward is recorded. The action credited is the one the patch actually implements (recognised from the diff).
- In `check_reviews`, merged or closed PRs update the reward.

**Mock training data:** `mock_incidents.py` generates 100 incidents from the real injectable
bugs (same files, tests, probe checks and symptoms), as both CI and live-check failures, plus
2 infrastructure failure types. They're stored in `mock_incidents`, never in `incidents`, so the dashboard
stays clean. A simulator, with a 95% fix rate for the right playbook, 45% for reverting the last commit
and close to 0% for the wrong one, provides the rewards during training.

```bash
python -m agents.rl_policy seed          # store 100 mock incidents in Atlas
python -m agents.rl_policy train         # 2 passes; also learns from finished real incidents
python -m agents.rl_policy recommend --app shopping --logs "checkout total is 0 after WELCOME10"
python -m agents.rl_policy stats         # success rate per app/action (an Atlas aggregation)
python -m agents.rl_policy clear         # remove all mock data (real experiences are kept)
```

On Atlas, first-pick accuracy on unseen incidents goes from **13% before training to 83–95%
after**, depending on the exploration path. Successful mock fixes are also stored as
`memories` (flagged `mock: true`), so incident-memory Vector Search has history too.
