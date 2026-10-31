# Operating Merlin

Merlin runs an authenticated API, background workers, PostgreSQL 17, and a Caddy
HTTPS edge. Gemini makes model calls and E2B runs repository commands, language
servers, and browser sessions. The host runs trusted service and maintenance code;
never clone a task repository or run its build on the host.

The deployment is for one trusted owner. Anyone holding `MERLIN_OWNER_TOKEN` can
control tasks and approve publishing. Use a separate deployment for another owner.

## Configure providers and secrets

Use Python 3.13, uv, Docker Engine, and Docker Compose v2 or later. Point the
hostname's DNS at the host and allow inbound ports 80 and 443 for Caddy's HTTPS
certificate issuance. Run the commands below from this repository's root.

```sh
cp .env.example .env
chmod 600 .env
mkdir -m 700 secrets
uv sync --dev
uv run merlin new-token
```

Save the generated owner token in `.env`. Generate a different PostgreSQL password,
set `POSTGRES_PASSWORD`, and use its URL-encoded value in `MERLIN_DATABASE_URL`.
Keep the database hostname `postgres` for Compose. Set `MERLIN_HOSTNAME` to the
real hostname; set `MERLIN_GITHUB_CALLBACK_URL` to
`https://YOUR_HOSTNAME/v1/github/callback`. Do not put `.env` or `secrets/` in Git.
Use a UTF8 PostgreSQL database; the Compose image initializes this by default.
When provisioning PostgreSQL separately, verify `SHOW server_encoding` reports
`UTF8` before migrations. A bare `SQL_ASCII` test cluster does not provide the
Unicode text behavior required by the application and its driver.
Use the committed `uv.lock` with `uv sync --locked --dev`; update the lock with
`uv lock` whenever dependencies change. The Dockerfile installs locked production
dependencies with uv and a non-editable package; its verification stage adds the
locked development tools. Preserve the tested image by digest for deployment.
The pattern follows [uv's Docker guidance](https://docs.astral.sh/uv/guides/integration/docker/).

Obtain a Gemini API key from your Google AI project and set
`MERLIN_GEMINI_API_KEY`. Set `MERLIN_GEMINI_MODEL` to a model available to that
project. The example model name is configurable and has not been validated against
a live account. Set the input/output cost estimates and output-token limit for
that model before relying on task budget estimates. These estimates are not a
provider billing guarantee.

Create an E2B account/key, set `MERLIN_E2B_API_KEY`, and build the template using
[deploy/e2b/README.md](../deploy/e2b/README.md). Set `MERLIN_E2B_TEMPLATE` to the
resulting template ID or alias. Use an E2B account dedicated to this deployment.

Create a GitHub App with repository permissions for Contents, Pull requests, and
Issues set to read/write, plus GitHub's mandatory Metadata read permission. Install
it only on repositories Merlin should access. Record its App ID and installation
ID in `MERLIN_GITHUB_APP_ID` and `MERLIN_GITHUB_INSTALLATION_ID`. Generate an App
private key and save it as `secrets/github.pem`. Compose mounts it read-only at
`/run/secrets/github.pem`, matching `MERLIN_GITHUB_PRIVATE_KEY_FILE`.

```sh
chmod 700 secrets
chmod 444 secrets/github.pem
```

The protected parent directory restricts host access; the readable file permits
the non-root container user (UID 10001) to read the file-backed Compose secret.
File-backed Compose secrets retain host file permissions. Keep
`GITHUB_PRIVATE_KEY_PATH=../secrets/github.pem`: relative paths are resolved from
`deploy/compose.yaml`, including this secret path.

For browser-based GitHub linking, enable user authorization for the App and add the
exact callback URL above to the App settings. Set `MERLIN_GITHUB_CLIENT_ID` and
`MERLIN_GITHUB_CLIENT_SECRET`. After starting Merlin, run `merlin github connect`,
open its printed URL, authorize an account with access to the installation, and
check `merlin github status`. The connection and mutation journal are persisted
under `/data/github-state`. Without OAuth settings, the service uses the
operator-configured installation directly; `github connect` requires OAuth.

App write credentials stay in the broker. Every publishing operation also requires
task permission or approval enforced by the server. Child tasks cannot publish.
Changing the App installation requires reviewing its repository grants again.

## Start and verify

For development, the trusted API and worker can run locally while all repository
work remains remote in E2B. In your protected `.env`, use
`MERLIN_DATABASE_URL=sqlite+pysqlite:///.merlin/dev.db`,
`MERLIN_ARTIFACT_DIR=.merlin/artifacts`, and an empty `MERLIN_OTLP_ENDPOINT`.
Use the real host path for the GitHub key rather than the container secret path.
Then run `uv run merlin init-db`, `uv run merlin serve`, and, in another terminal,
`uv run merlin-worker`. This does not start a local sandbox. The worker requires
configured Gemini/E2B credentials and a published E2B template.

For the hosted deployment, use PostgreSQL and the Compose paths described above.

Validate syntax using only the sample environment, without expanding service env
files or requiring the private key to exist:

```sh
MERLIN_ENV_FILE=../.env.example docker compose --env-file .env.example -f deploy/compose.yaml config --no-env-resolution --quiet
```

Build and start the deployment with real configuration:

```sh
docker compose --env-file .env -f deploy/compose.yaml up -d --build
docker compose --env-file .env -f deploy/compose.yaml ps -a
docker compose --env-file .env -f deploy/compose.yaml logs --tail=100 migrate api worker
curl --fail https://YOUR_HOSTNAME/healthz
curl --fail https://YOUR_HOSTNAME/readyz
```

The one-shot `migrate` service applies `alembic upgrade head`. API and worker
startup waits for migrations and PostgreSQL health. Caddy waits for the API's
readiness healthcheck. `/healthz` is process liveness. `/readyz` returns 200 only
when database metadata, the current schema, and a recent worker heartbeat are
available; otherwise it returns 503. The worker container also checks its own
local heartbeat marker so another worker cannot mask a stalled process. Neither
probe makes provider calls or proves Gemini credentials, GitHub grants, or E2B
template readiness. `migrate` exiting with status 0 is expected. Docker marks
failing healthchecks unhealthy; alert on this state because health status alone
does not restart a running container.

## Runtime limits and recovery

The default settings bound model work and API admission. Set them through
`MERLIN_` environment variables, then restart the affected API or worker:

| Setting suffix | Default | Purpose |
| --- | --- | --- |
| `MODEL_FIRST_CHUNK_TIMEOUT_SECONDS` | 60 | Bound provider connection and first response chunk. |
| `MODEL_IDLE_TIMEOUT_SECONDS` | 60 | Pause a stream that stops producing chunks. |
| `MODEL_TOTAL_TIMEOUT_SECONDS` | 300 | Bound the complete model generation. |
| `MODEL_STREAM_FLUSH_SECONDS` | 0.15 | Bound buffered terminal text between size-triggered updates. |
| `MODEL_TOKEN_CACHE_SIZE` | 64 | Cache exact token counts for unchanged provider inputs. Zero disables it. |
| `WORKSPACE_SETUP_TIMEOUT_SECONDS` | 180 | Bound workspace creation/bootstrap/restoration. |
| `CHECKPOINT_TIMEOUT_SECONDS` | 120 | Bound capture and download before a tool is dispatched or completed. |
| `CLEANUP_TIMEOUT_SECONDS` | 15 | Bound workspace cleanup during completion and cancellation. |
| `TOOL_TIMEOUT_SECONDS` | 660 | Outer tool deadline, including a Bash timeout of up to 600 seconds. |
| `PARALLEL_READ_TOOLS` | 4 | Overlap bounded independent reads; mutations remain barriers. |
| `CONTEXT_CACHE_ENTRIES` | 128 | Bound repository instruction/context cache entries. |
| `INSTRUCTION_TTL_SECONDS` | 30 | Recheck cached applicable instructions; writes invalidate affected guidance. |
| `MAX_REQUEST_BYTES` | 1048576 | Reject oversized API requests, including streamed/chunked bodies. |
| `MAX_EVENT_STREAMS` | 32 | Bound simultaneous durable-event streams per API process. |
| `MAX_QUEUED_TASKS` | 100 | Bound queued submissions while preserving idempotent retries. |
| `READINESS_WORKER_MAX_AGE_SECONDS` | 30 | Maximum accepted database/local worker heartbeat age. |

`MAX_INPUT_TOKENS` includes a reserved framing margin. Context compaction retains
the current objective, applicable instructions, complete native provider
interactions, and bounded sourced memory. `history_lookup` retrieves older
evidence when needed. Instructions discovered for a new edit target must be
shown to the model before that edit can execute. If mandatory context cannot fit,
the task pauses before reserving another model call. Context and instruction data
cannot grant publishing permission.

Only complete `STOP` model responses can dispatch new tools or finish a task.
Output limits, blocked responses, incomplete streams, malformed calls, and
duplicate invocation IDs are reported explicitly. HTTP retries honor a bounded
`Retry-After`; unknown transport outcomes and partial streams are not replayed.
The adapter reports cached input and thinking tokens separately. Set
`CACHED_INPUT_COST_PER_MILLION` only to a verified rate; its default uses the normal
input rate conservatively. Provider-reported usage remains an estimate at the
operator's configured rates.

Generation costs are reserved after context/token preflight. A request proven
undispatched releases its reservation. Interrupted or usage-incomplete requests
retain their reservation until investigated; absence of a response is not evidence
of zero provider cost. These owner-authenticated endpoints support recovery:

| Endpoint | Behavior |
| --- | --- |
| `GET /v1/tasks/TASK_ID/usage` | Inspect root-shared usage and outstanding reserved cost. |
| `GET /v1/tasks/TASK_ID/reservations` | List up to 100 outstanding reservation records for this task. |
| `POST /v1/tasks/TASK_ID/reservations/RESERVATION_ID/resolve` | Set inspected `actual_cost_usd` with a nonempty `note` on a stopped/paused task. Resolution is immutable. |
| `POST /v1/tasks/TASK_ID/budget` | Increase a paused root's complete `budget` with a `note`; all new limits must cover recorded and reserved usage. |
| `GET /v1/tasks/TASK_ID/history?cursor=0&limit=20` | Retrieve a bounded history page. |

Budget changes preserve permissions and do not resume a task. Resume explicitly
after inspecting uncertainty and amending limits. Reconciling provider usage
does not acknowledge an uncertain repository/GitHub operation; that still needs
the separate tool-resolution flow. Keep recovery notes free of credentials.

The terminal CLI exposes these recovery operations:

```sh
uv run merlin reservations TASK_ID --json
uv run merlin resolve-usage TASK_ID RESERVATION_ID --actual-cost-usd 0.12 \
  --note 'Matched the completed request to the provider usage record'
uv run merlin budget TASK_ID --max-calls 150 --max-cost-usd 10 \
  --max-sandbox-seconds 7200 --note 'Approved additional work after inspecting the task'
uv run merlin resume TASK_ID
```

Replace the example cost and limits with inspected values. Do not set usage to
zero simply to free budget. Budget limits are shared across the root and children.

Compose bounds CPU, memory, and process counts for every service. Defaults are
API 1 CPU/768 MiB, worker 2 CPU/1536 MiB, PostgreSQL 2 CPU/1 GiB; telemetry services
have separate limits. Override `MERLIN_<SERVICE>_CPUS`,
`MERLIN_<SERVICE>_MEMORY`, and `MERLIN_<SERVICE>_PIDS` in the Compose environment
(`API`, `WORKER`, `POSTGRES`, `MIGRATE`, `EDGE`, `OTEL`, `JAEGER`, `PROMETHEUS`,
or `GRAFANA`). These cap trusted control services; task repository resources run
in E2B. Tune limits using queue latency, process RSS, and checkpoint sizes, and
reserve host memory for the database and observability stack. App containers
receive a 30-second graceful-stop window and use an init process to reap children.

Connect the terminal client from a trusted machine:

```sh
uv run merlin login --url https://YOUR_HOSTNAME
uv run merlin doctor
uv run merlin github status
uv run merlin github repos
uv run merlin run 'Explain the test setup' --repo OWNER/REPOSITORY --detach
uv run merlin tasks
uv run merlin inspect TASK_ID
uv run merlin attach TASK_ID
```

`login` prompts for the owner token and saves a mode-0600 client configuration.
Use `MERLIN_CLIENT_CONFIG` to select another client configuration file. Remote
connections require HTTPS. `MERLIN_API_URL` and `MERLIN_OWNER_TOKEN` override saved
connection settings; a server `.env` is not a terminal client's credential store.

`merlin chat` starts a conversation without a repository. Gemini is required;
E2B and GitHub configuration are needed only for repository work. Add
`--repo OWNER/REPOSITORY` to enable coding tools in a new session. `attach`
streams durable events; Ctrl-C detaches while the hosted task continues. Reattach
with `--after EVENT_SEQUENCE` to resume from a saved cursor. Use `merlin cancel
TASK_ID` to request cancellation. Inspect approval details with `merlin inspect
TASK_ID`, then `merlin approve APPROVAL_ID` or `merlin approve APPROVAL_ID --reject`.
Prefer approval after reviewing the concrete operation to granting permanent task
publishing permission with `run --permission`.

To return corrections on a pending publishing proposal:

```sh
uv run merlin request-changes APPROVAL_ID --note 'Disclose the failed typecheck in the PR body'
uv run merlin attach TASK_ID
```

This rejects the unexecuted call, records the owner's feedback as its tool result,
and requeues the same task. Native signed model requests remain unchanged. The
revised proposal requires a fresh approval; previously published task branches are
reused. Repeated feedback on a decided approval, stale content, and operations
that already have execution intents are rejected. Plain `approve --reject` pauses
the task without requesting a revision. Normal approval output is concise;
`approve --json` includes the full reviewed payload.

The default client shows concise workspace/tool progress and streams each model
answer once. Internal reservations, checkpoints, and successful tool payloads
remain in persistent events; `merlin attach TASK_ID --json` exports full events.
The provider adapter removes exact duplicate `data.output` fields from Gemini's
input without changing the stored tool results, native signatures, or metadata.

Cost figures use the operator's configured estimates, rather than provider billing.
For `gemini-3.5-flash-lite`, Google's published standard paid rates on 2026-10-28
are $0.30 input and $2.50 output per million tokens. Set
`MERLIN_INPUT_COST_PER_MILLION=0.30` and
`MERLIN_OUTPUT_COST_PER_MILLION=2.50` for those estimates; free-tier usage and
cached-token discounts may differ. Verify the chosen model's
[current pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.5-flash-lite)
when changing models. Restart the worker after changing its configuration.

A paused uncertain operation must be investigated before acknowledging it:

```sh
uv run merlin resolve TASK_ID CALL_ID --note 'Inspected the external effect and recorded the result'
```

Resolution requeues the task, records its acknowledgement, and returns the workspace to its
last completed checkpoint. It does not undo external effects or assert that the
operation succeeded. Verify GitHub state and any external commands first.

## Diagnose workspace startup

If a task fails before its first model call, run from the repository root:

```sh
uv run python scripts/diagnose_workspace.py --repo OWNER/REPOSITORY
```

The diagnostic reads `.env`, creates a temporary E2B workspace with a three-minute
expiry, obtains GitHub read access, clones the selected repository, captures a
snapshot, downloads it, and validates the archive. It prints the failing stage
and redacted exception details without a traceback. It makes no model requests
or GitHub publishing operations, and removes the sandbox on completion. If
cleanup fails, it prints the sandbox ID for manual removal in the E2B dashboard.
This consumes E2B resources and requires working provider network access.

Checkpoint capture/download retries connection interruptions up to three times
with short backoff. Each attempt uses a fresh archive path. Invalid archive data
and cancellation are not retried. Exhausted connection attempts pause the task;
inspect it before using `merlin resume TASK_ID`. An unresolved mutation still
requires the acknowledgement flow above and is never replayed automatically.
New local edit intents are prepared after their pre-edit checkpoint succeeds.
Existing failed tasks keep their state; a newly submitted task in the same session
can start from its latest successful predecessor's saved checkpoint.

Approving a discussed plan (for example, “yes please implement this”) asks Merlin
to execute it in the selected repository. The earlier conversation remains in
the bounded model context after the workspace starts. Greetings and general
conversation still work without starting a workspace.

Gemini requests retry explicit HTTP 408, 429, 500, 502, 503, and 504 errors up to
three attempts with backoff before any response chunk arrives. Interrupted
connections and failures after streaming begins pause the task without an
automatic replay. Retryable provider failures retain task history and saved
checkpoints. Use `/resume` in the terminal UI, or `merlin resume TASK_ID` from
another terminal; plain chat does not interpret `/resume`. Provider error bodies
are excluded from public errors and retry logs. Authentication and request
errors fail with a safe HTTP status and configuration guidance.

## Back up and restore

The `database` volume stores tasks, leases, events, approvals, tool results, and
checkpoint references. The entire `state` volume mounted at `/data` is equally
necessary: it includes `/data/artifacts` and `/data/github-state`. Backing up only
PostgreSQL or only checkpoint files loses recovery and duplicate-operation state.

Use the standard-library maintenance CLI from this checkout. It invokes trusted
maintenance code in the app image; it does not execute repository workloads. The
image must already be built, PostgreSQL must be running, and no migration may be
running. Do not run multiple maintenance commands concurrently or start writers
from another terminal while maintenance is in progress. With a custom Compose
project, pass the same `--project-name` used for deployment.

```sh
mkdir -m 700 backups
python3 scripts/merlin_admin.py backup backups/2026-10-28
python3 scripts/merlin_admin.py verify backups/2026-10-28
```

Backup stops the previously running edge, API, and worker containers, creates a
custom-format PostgreSQL dump and a complete state tar archive, then restarts only
those previously running services. It refuses an existing destination. Backups
have directory mode 0700 and file mode 0600; SHA-256 hashes, sizes, format version,
and creation time are recorded in `manifest.json`. A failed snapshot is removed
and the prior writers are restarted. Inspect Compose status after any failure.
Interrupted tools recover under the same conservative rules as a worker restart.

Copy complete backup directories off-host with encryption and access controls,
and test restoration regularly. Backups contain repository content and operational
state; `.env`, the App private key, Caddy certificates, and observability volumes
are separate. Preserve secret configuration securely with the backup's deployment
image/revision. Do not delete checkpoint artifacts just because they are old:
the configured retention-days setting does not schedule automatic pruning.
With edge/API/workers stopped, preview retention through
`docker compose --env-file .env -f deploy/compose.yaml run --rm --no-deps api merlin prune-artifacts`.
Append `--apply` to remove the listed expired artifacts. Active task artifacts,
child base snapshots, and the latest successful snapshot in each session are
protected even past the default 30 days. GitHub journals are never pruned.

Restore only a trusted backup. Checksums detect corruption; they do not establish
who produced a dump, and PostgreSQL restoration executes the dump's SQL. Restore
with a matching application version and PostgreSQL 17 first, then upgrade.

```sh
python3 scripts/merlin_admin.py verify backups/2026-10-28
python3 scripts/merlin_admin.py restore backups/2026-10-28 --replace-existing
docker compose --env-file .env -f deploy/compose.yaml run --rm --no-deps migrate
docker compose --env-file .env -f deploy/compose.yaml up -d api worker edge
uv run merlin doctor
uv run merlin tasks
```

Restore validates the dump/header, hashes, archive paths, and file types before
contacting Docker. It requires explicit replacement, stops writers, and saves a
complete `before-restore-TIMESTAMP` backup beside the supplied backup before
clearing `/data` and the database's public schema. The database import runs in one
transaction. Application services remain stopped after success or failure so the
operator can apply migrations and review recovery. The automatic rollback backup
can be restored with the same command. If restore fails, keep writers stopped and
inspect `backups/before-restore-*`; do not resume a partly restored deployment.

On a fresh recovery host, first configure the same secrets, build the matching app
image, and start `postgres` alone. The restore command initializes the state volume
through its maintenance container. Preserve the automatic rollback snapshot until
the deployment has been checked. GitHub writes and E2B processes that happened
after a backup are not rewound by restoring it; inspect uncertain external effects
before resuming recovered work.

## Upgrade and observe

Back up before changing the image or applying migrations. Stop edge/API/workers,
build the new image, run the one-shot migration, then start the services:

```sh
docker compose --env-file .env -f deploy/compose.yaml stop edge api worker
docker compose --env-file .env -f deploy/compose.yaml build
docker compose --env-file .env -f deploy/compose.yaml run --rm --no-deps migrate
docker compose --env-file .env -f deploy/compose.yaml up -d api worker edge
docker compose --env-file .env -f deploy/compose.yaml run --rm --no-deps migrate alembic current
curl --fail https://YOUR_HOSTNAME/readyz
```

This release requires migration `0002`, which adds dispatch/accounting fields,
context checkpoints, worker presence, and query indexes. Migration of old
unsettled usage marks it `unknown`; it never assumes the provider did no work.
Take a complete database/state backup, stop all writers, and apply `alembic
upgrade head` once before starting the new API and worker. Do not overlap old and
new worker images during this schema transition. Check `alembic current` against
`alembic heads` from the same image and confirm `/readyz` is 200 after workers
start. Schema creation through `merlin init-db` remains for SQLite development;
use Alembic for PostgreSQL. A successful migration and readiness response still
need task/recovery/provider smoke checks.

If a migration fails, keep writers stopped. Restore the previous image and its
matching full backup when a schema rollback is required. `docker compose down`
preserves named volumes; `down -v` deletes them and is not an upgrade step.

Prometheus scrapes API port 8000 and worker port 8001 internally. The edge blocks
`/metrics`. OTLP traces flow through the collector to Jaeger. Grafana listens only
on host loopback port 3000 and Jaeger on 16686; use a trusted SSH tunnel to view
them. Change Grafana's initial administrator password at first login. Dashboard
provisioning is in `deploy/grafana/`. Logs rotate at 10 MB with three files for app
services. Monitor failed/paused tasks, task transitions, errors, tokens, database
and state disk use, and backups. Protect logs and keep prompts and credentials out
of operational annotations.

## Verification gates

Publishing source now starts with `github_prepare_publish(paths, message)`. It
returns compact `publish_arguments` containing the owned checkpoint ID, its
SHA-256, base commit, selected paths, and message. Merlin submits those exact
arguments to `github_publish_branch`; the broker reads file bytes and executable
modes from that immutable checkpoint. Approval previews use the same snapshot.
Legacy proposals without snapshot metadata must be prepared again before they
can execute. Prepared and approved snapshots are protected during retention.

For scripts retrying an ambiguous submission, reuse
`merlin run 'PROMPT' --repo OWNER/REPO --idempotency-key YOUR_STABLE_KEY --detach`.
Keep the message, repository, options, and key unchanged for the retry; the CLI
derives a stable session when `--session` is omitted. Different work requires a
different key.

Use `merlin reservations TASK_ID --json` to inspect outstanding model usage.
After checking provider billing, use
`merlin resolve-usage TASK_ID RESERVATION_ID --actual-cost-usd AMOUNT --note 'Evidence'`.
To raise a paused root task's limits, use
`merlin budget TASK_ID --max-calls 200 --max-cost-usd 10 --max-sandbox-seconds 7200 --note 'Reason'`,
then explicitly run `merlin resume TASK_ID`. These actions change neither tool
authorization nor the outcome of an uncertain external operation.

Before configuring cloud credentials, run `python3 scripts/runtime_smoke.py` from
a Docker-enabled terminal. It creates an isolated PostgreSQL/API/fixture-worker
stack, exercises migrations and crash recovery, and removes its test containers
and volumes. See [runtime smoke details](runtime-smoke.md) for its exact scope;
the model and sandbox are synthetic fixtures, not live provider integrations.

Default tests replace external providers and use SQLite. CI requires a committed
`uv.lock`, Python 3.13, deterministic tests, Ruff lint/format, mypy, distribution
build, and Compose syntax validation. These checks do not prove that a Docker
image starts, a migration works against live PostgreSQL, or provider accounts are
configured. Before accepting production traffic, exercise the deployment against
dedicated test repositories: migration/restart, an E2B edit and checkpoint restore,
GitHub approval and duplicate-operation handling, cancellation, and a full backup
restore. Live checks need explicitly supplied credentials and must be reported
separately from default tests.

Provider smoke tests are in `tests/test_live_services.py`. Configure the service
credentials and `MERLIN_LIVE_REPOSITORY` for a dedicated App-connected test repo,
then explicitly run
`MERLIN_LIVE_PROVIDER_TESTS=1 uv run pytest -m live tests/test_live_services.py`.
They make real Gemini calls, create and destroy an E2B scratch workspace, exercise
Chromium/checkpoint restoration, and perform read-only GitHub requests.

For database concurrency and migration roundtrips, configure
`MERLIN_TEST_POSTGRES_URL` for a dedicated database whose name starts with
`merlin_test`, then run
`MERLIN_POSTGRES_TESTS=1 uv run pytest -m postgres tests/test_postgres.py`.
Each test creates and drops its own uniquely named schema. These checks are
excluded from the default suite. The production-readiness implementation was
verified against an isolated PostgreSQL 17.11 UTF8 database inside E2B; nine
concurrency, fencing, persistence, and migration tests passed. This does not
replace the separate Docker image/startup/restart gate for the deployed image.
