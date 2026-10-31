# Docker and PostgreSQL runtime gate

Run from the repository root in a terminal with Docker daemon access and network
access to pull/build images:

```sh
python3 scripts/runtime_smoke.py
```

The driver requires only Python's standard library and Docker Compose. It builds
the application using `uv.lock`, creates a unique `merlin-smoke-...` project, and
generates disposable database/owner credentials. It does not read production
`.env`, mount App private keys, expose host ports, or call cloud providers.

## What is checked

1. Start PostgreSQL 17 and apply the real Alembic migrations.
2. Run nine real PostgreSQL tests covering claim caps, concurrent root budgets,
   stale-lease fencing, cancellation, restart durability, and migration roundtrips.
   These finish before the fixture worker starts so database-level advisory locks
   cannot interfere with the concurrency assertions.
3. Start the real authenticated API and production worker class with trusted fake
   model/sandbox adapters. Verify `/healthz` liveness and `/readyz` database/schema/
   worker presence; each worker container also needs its own fresh heartbeat.
   Submit a task over HTTP and check durable events,
   checkpoint metadata, and exactly one recorded fixture effect.
4. Restart API and worker containers and verify the completed task and events.
5. Submit another task, kill the worker with SIGKILL after an unfinished mutation,
   and start its replacement. Verify that the task pauses instead of replaying it.
6. Explicitly acknowledge that synthetic uncertain outcome through the owner API,
   restore its last completed checkpoint, and verify recovery without a second effect.

The fake adapters manipulate only fixture data; they never execute repository
commands on the control service. This gate validates service/container wiring and
database behavior. Gemini, E2B, Chromium, LSP, and GitHub still need their separate
live smoke tests and functional trials.

## Isolation and cleanup

Containers and volumes belong to the randomly named test project and are removed
on success or failure. Docker images and build caches remain available for reuse.
Failure output includes the disposable project's last service logs. If Docker
preflight fails, the driver exits before building or starting resources.

`--dry-run` prints the sequence without invoking Docker. `--keep` retains test
resources for manual inspection. To remove a retained project, use the exact
printed project name, replacing the placeholder below:

```sh
MERLIN_SMOKE_TAG=cleanup MERLIN_SMOKE_PASSWORD=unused MERLIN_SMOKE_OWNER_TOKEN=unused \
  docker compose --env-file /dev/null --project-name PRINTED_PROJECT_NAME \
  -f deploy/compose.smoke.yaml down --volumes --remove-orphans
```

Those placeholder credentials are only needed to parse the Compose file during
cleanup; they do not reconnect to or alter a production deployment.

## Evidence status

The driver, fixture, health, and state-machine assertions pass deterministic
checks inside E2B. The nine real PostgreSQL tests, including migration0002,
also passed against PostgreSQL17.11 with an isolated UTF8 database in E2B.
Actual Docker image startup and container crash recovery remain unverified in
this executor because it has no Docker daemon. A successful Docker-enabled run
is required to close that gate. CI includes a separate `runtime-smoke` job for it.
