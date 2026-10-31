# Merlin

A Python terminal assistant for conversation and coding, with a task service, Gemini, and isolated
E2B workspaces. Tasks continue after the terminal disconnects. Repository commands,
browser automation, and language servers execute in E2B; GitHub publishing passes
through a trusted broker with task-scoped permissions or concrete approvals.

The prototype includes durable task leases and events, checkpoint recovery,
cancellation, exact file edits, code search, public-web reading, local application
browser tests, TypeScript/Python LSP, scoped `MERLIN.md` instructions, context
compaction, and isolated coding subagents with conflict-aware source integration.

The [private beta runtime upgrades](docs/production-upgrades.md) document the
implemented reliability/performance changes, measurements, migration `0002`,
and the remaining release gates.

## Develop

Use Python 3.13 and uv. Dependencies are pinned in `uv.lock`; registry access is
required to download packages that are not already cached.

```sh
uv sync --locked --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv build
```

When changing dependencies, run `uv lock` and commit it with `pyproject.toml`.
Provider and PostgreSQL tests are opt-in; default tests use fake providers and SQLite.
The search-helper tests also require `ripgrep`; the supplied E2B template and CI
workflow install it. Run repository checks inside E2B as described in `AGENTS.md`.
Alternatively, run `python3 scripts/verify.py` to check the lock, install into
`.merlin/verification-venv`, and execute these checks plus offline evaluations and
Compose syntax validation. It stops at the first failed step. Use `--dry-run` to
inspect the sequence. This requires registry access, uv, and the Docker CLI.

## Run

Try the full-screen terminal interface without credentials or provider charges:

```sh
uv run merlin ui --demo
```

For real work, keep the API and worker running, then launch `uv run merlin` or
`uv run merlin ui --repo OWNER/REPOSITORY`. The interface includes live Markdown,
session switching, expandable tools, checkpoint file references, and publishing
reviews. See the [beginner terminal guide](docs/terminal-ui.md) for setup and controls.

Chat needs Gemini and the running API/worker; a repository and E2B are optional until
you ask for repository work. Greetings and general questions receive direct replies.
The terminal connects to the API even when all three processes run on your laptop;
it does not execute commands in your current local directory.

For repository work, configure the E2B template and a selected GitHub App installation
using [the operations guide](docs/operations.md). That guide covers local API
startup, Docker Compose deployment, HTTPS, secrets, and backup/restore.

```sh
uv run merlin login --url https://YOUR_MERLIN_HOST
uv run merlin chat
uv run merlin chat --repo OWNER/REPOSITORY
uv run merlin run 'Explain the test setup' --repo OWNER/REPOSITORY --detach
uv run merlin inspect TASK_ID
uv run merlin attach TASK_ID
```

Detaching leaves remote work running; `merlin cancel TASK_ID` cancels it. Publishing
requires explicit task permission or approval of the prepared action. Subagents
cannot publish or expand the parent's permissions.

Normal terminal output shows short progress messages and displays each answer once.
For complete event payloads, use `merlin attach TASK_ID --json` (or `run --json`).
Use `merlin request-changes APPROVAL_ID --note 'Required correction'` to return
feedback on a pending publishing proposal; the revision requires fresh approval.
See [optimization measurements](docs/optimizations.md) for the first live task's
baseline and the distinction between replay measurements and live performance.

## Verify

```sh
uv run python -m merlin.evaluation --offline --output .merlin/reports/offline.json
MERLIN_ENV_FILE=../.env.example docker compose --env-file .env.example -f deploy/compose.yaml config --no-env-resolution --quiet
```

The next runtime gate needs Docker daemon access but no cloud credentials:

```sh
python3 scripts/runtime_smoke.py
```

It checks real PostgreSQL, API requests, migrations, and worker restart recovery
inside a disposable stack; see [runtime smoke details](docs/runtime-smoke.md).

Read [evaluation guidance](docs/evaluations.md), [architecture](docs/architecture.md),
[security review](docs/security-review.md), and [current build status](docs/build-status.md).
Offline checks establish implementation contracts; live model performance,
provider behavior, container startup, PostgreSQL locking, and deployed restoration
remain separate acceptance gates.
