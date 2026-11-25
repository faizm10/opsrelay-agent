# Runbook: OpsRelay investigation worker

The worker (`python -m agents.worker`) turns failed GitHub Actions runs and live uptime incidents
into a structured recommendation: `verified_patch`, `ops_recommendation` or `needs_review`. State
lives in Atlas. The dashboard is not part of this deployment. Local development (`./dev.sh`) is unchanged.

## What it does in production

- **Report-only by default.** A verified patch ends as status `reported`. Draft PRs are enabled per
  repository with `OPSRELAY_PUBLISH_REPOS`, and merges always stay with humans.
- **Two GitHub tokens.** `GITHUB_TOKEN` is read-only (Actions, Contents, Pull requests, Metadata:
  read). `GITHUB_PUBLISH_TOKEN` (Contents and Pull requests: write, same single repository) is
  removed from the environment at startup and given only to the PR publisher.
- **Publishing is crash-safe.** The PR intent is saved before publishing. A retry or a new worker
  looks the branch up on GitHub first. If the base branch moved, the patch is re-verified on the
  new head before it is published.
- **After a merge**, the incident becomes `awaiting_deployment`. It resolves only when
  `OPSRELAY_<APP>_VERSION_URL` reports a commit containing the merge commit and
  `OPSRELAY_<APP>_PROBE_URL` passes a fresh probe. Missing evidence is recorded as
  `unavailable`, and the incident stays open.
- **Verification runs in throwaway containers when Docker is available** (`OPSRELAY_VERIFIER_RUNNER=auto`,
  the default, with `OPSRELAY_VERIFIER_IMAGE` set). Without Docker, tests run as plain processes with a
  scrubbed environment, and the heartbeat lists this as a fallback. Containers add:
  - no network, a read-only root and no capabilities;
  - user 65534, with CPU, memory and pid limits;
  - the workspace is streamed in over stdin, so `DOCKER_HOST` can be a separate runner host
    that holds no production credentials.

## Build and launch

```bash
# Build from the repo root
TAG=$(git rev-parse --short HEAD)
docker build -f agents/Dockerfile --build-arg OPSRELAY_RELEASE=$TAG -t opsrelay-agent-worker:$TAG .

# One-time: a clone of the target repo, owned by the container user
docker volume create opsrelay-repo
docker run --rm -v opsrelay-repo:/repo opsrelay-agent-worker:$TAG \
  git clone https://github.com/faizm10/opsrelay-agent /repo

# Config lives on the host (start from deploy/agent-worker.env.example), never in the repo
docker run --rm --env-file /etc/opsrelay/agent-worker.env -v opsrelay-repo:/repo \
  opsrelay-agent-worker:$TAG python -m agents.worker check      # exit 0 = ready, 2 = see the logs

docker run -d --name opsrelay-agent-worker --restart unless-stopped --stop-timeout 90 \
  --read-only --tmpfs /tmp:rw,size=2g \
  --env-file /etc/opsrelay/agent-worker.env -v opsrelay-repo:/repo \
  -v /etc/opsrelay/docker-certs:/certs:ro \
  opsrelay-agent-worker:$TAG
```

`check` validates the configuration and refuses to start when any of these hold:

- the demo shortcut is enabled;
- non-draft PRs are configured;
- the two GitHub tokens are the same;
- deployment URLs are missing while publishing is enabled;
- `OPSRELAY_VERIFIER_RUNNER=docker` is set but Docker is not reachable.

It then pings Atlas and runs setup, which is idempotent and keeps existing records valid. It
writes and reads back a checkpoint, runs one Vector Search query and checks the GitHub token.
Any fallback (local cosine search, local embeddings, the process verifier) is logged as a
fallback.

## Running without Docker

Docker is optional: the worker runs from a virtualenv, and verification falls back to plain processes.

```bash
python3.12 -m venv agents/.venv && agents/.venv/bin/pip install -r requirements.txt
set -a; . /etc/opsrelay/agent-worker.env; set +a     # leave OPSRELAY_VERIFIER_IMAGE unset
agents/.venv/bin/python -m agents.worker check         # lists "verifier: process runner" under fallbacks
nohup agents/.venv/bin/python -m agents.worker >> /var/log/opsrelay-agent-worker.log 2>&1 &
```

Without containers, the code under test can reach the network and read files the worker's user can
read. Run the worker as its own unprivileged user, keep report-only mode, and keep tokens out of files.

## Cloud Run Job (current production)

Production runs `python -m agents.worker once` as a Cloud Run Job in Google Cloud project
`opsrelay-worker-3c0bff` (`us-central1`), started every 15 minutes by Cloud Scheduler. Each run
clones the repo into `/repo`, runs one cycle, waits for its in-flight incidents (at most
`OPSRELAY_MAX_CONCURRENCY`, default 2) and exits; queued incidents wait for the next run. A run
that hits the 30-minute task timeout leaves its incidents to lease expiry. `POLL_SECONDS=900`
keeps the Atlas heartbeat alive between runs. There is no Docker, so verification uses the
process runner: keep this job on repositories you trust and in report-only mode.

- Atlas user `opsrelay_worker`: `readWrite` and `dbAdmin` on `opsrelay` only (`dbAdmin` lets
  setup update collection validators).
- Secrets in Secret Manager (`mongodb-uri`, `openai-api-key`, `github-token`, `voyage-api-key`),
  readable only by the `opsrelay-worker` service account. Other settings are plain env vars on the job.
- Sized for the Cloud Run free tier: 1 vCPU, 2 GiB, about 2,900 runs a month. A 1 CAD budget
  alert covers the project.

```bash
P=opsrelay-worker-3c0bff; R=us-central1; TAG=$(git rev-parse --short HEAD)
IMG=$R-docker.pkg.dev/$P/opsrelay/agent-worker:$TAG
# Build (Cloud Build, from a clean export of the committed tree)
rm -rf /tmp/w && mkdir -p /tmp/w && git archive HEAD agents requirements.txt | tar -x -C /tmp/w
cat > /tmp/cloudbuild.yaml <<EOF
steps: [{name: gcr.io/cloud-builders/docker, args: [build, -f, agents/Dockerfile, --build-arg, OPSRELAY_RELEASE=$TAG, -t, $IMG, .]}]
images: [$IMG]
options: {logging: CLOUD_LOGGING_ONLY}
EOF
gcloud builds submit /tmp/w --project $P --config /tmp/cloudbuild.yaml
gcloud run jobs update opsrelay-worker --project $P --region $R --image $IMG   # upgrade or roll back
gcloud run jobs execute opsrelay-worker --project $P --region $R --wait          # run now
gcloud scheduler jobs pause opsrelay-worker-every-15m --project $P --location $R # stop scheduling
gcloud logging read 'resource.type=cloud_run_job' --project $P --limit 50        # logs
```

## Health

- Container healthcheck: `python -m agents.worker health`. It fails when the heartbeat file is
  older than 3 × `POLL_SECONDS` + 60 s.
- Atlas: `db.worker_heartbeats.find()` returns one document per worker. It holds the mode, the
  release, the incidents in flight, and separate `sources.ci_monitor`, `sources.live_intake`,
  `sources.incidents`, `sources.reviews` and `sources.deployments` (each with `ok`, `at` and
  `error`). It also holds counts by status and the fallbacks. Documents expire automatically
  when a worker stops beating.
- Logs are JSON lines with `incident_id`, `stage` and `worker_id`. Tokens, connection-string
  passwords and email addresses are redacted.
- Blocked work: `incidents.blocked` records the stage, the reason and the attempt number. After
  `MAX_ATTEMPTS`, the incident's status is `failed` with a `status_reason`.
- Kill switch: `python -m agents.orchestrator stop <incident_id> --reason "..."`

## Upgrade and rollback

1. Build the new tag, then run `check` with it.
2. Run `docker stop opsrelay-agent-worker`. SIGTERM finishes in-flight stages for up to
   `OPSRELAY_SHUTDOWN_GRACE_SECONDS`. Anything still running is left for lease expiry
   (`LEASE_SECONDS`), and the next worker resumes it from its last verified stage.
3. Start the new tag. Two workers can overlap safely: claims are atomic and writes are fenced on
   the lease and the stage.

To roll back, start the previous tag the same way. Schema changes are additive: the new statuses
are `reported` and `awaiting_deployment`. An older worker ignores incidents in those statuses, so
it will not resolve `awaiting_deployment` incidents until you roll forward again.

## Enabling draft PRs for a repository

1. Create the write token (Contents and Pull requests: write) for that one repository, and set
   `GITHUB_PUBLISH_TOKEN`.
2. Set `OPSRELAY_<APP>_VERSION_URL` and `OPSRELAY_<APP>_PROBE_URL` for each app in `agents/apps.toml`. Each deployment must
   set `OPSRELAY_DEPLOYED_SHA` so that `/api/version` reports the commit.
3. Set `OPSRELAY_PUBLISH_REPOS=faizm10/opsrelay-agent`, then run `check` and restart.
