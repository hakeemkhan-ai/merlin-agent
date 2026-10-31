# Merlin build status

For the subsequent private beta hardening implementation, migration `0002`,
current measurements and remaining gates, see [runtime upgrades](production-upgrades.md).
The milestones and local environment limitations below are historical records
from 2026-10-29 rather than the latest verification result.

Updated 2026-10-29. The local prototype is implemented and integrated; the live
release acceptance gates remain open. No provider publishing, deployed services,
or production repository changes were performed during local verification.

## Build board

The lead integrated exclusive component scopes from implementation specialists
and independent reviewers in the shared workspace. No usable Git repository was
available for commits or branch merging.

| Wave | Ownership | Delivered | Verification |
|---|---|---|---|
| 0 | Lead + reviewers | Python scaffold, shared protocols, trust/recovery rules | Accepted locally |
| 1 | Persistence/runtime/sandbox | SQL queue, leases, budgets, tool intents, atomic checkpoints, safe tools | Contract and integration tests pass |
| 2 | API/CLI/model/GitHub | Owner API, SSE/chat, Gemini loop, reviewed publishing and OAuth | HTTP/provider fakes and native SDK serialization pass |
| 3 | Context/browser/LSP/delegation | Instructions, compaction, browser policy, navigation, isolated children and source integration | Context, policy, merge and cancellation regressions pass |
| 4 | Operations/evaluations/review | Compose, telemetry, backups, retention, 27-case catalogue, independent audit fixes | Local tests and configuration validation pass |
| 5 | Lead | Distribution packaging, CLI installation, verification/reporting | Local gate passed; live gate pending |
| 6 | Terminal interface | Textual conversations, sessions, references, approvals, reconnect, offline demo | Headless UI, API, SQLite and CLI checks pass; live UI/provider check pending |

## Terminal interface milestone

The eight terminal-interface milestones are implemented. `merlin` opens the
interface in an interactive terminal; `merlin ui --demo` runs entirely offline.
Existing scripting/JSON commands and `chat --plain` remain available.
See the [beginner guide](terminal-ui.md).

- Connection/login, repository selection, paginated sessions and conversation history.
- Live Markdown, bounded tool cards, keyboard navigation, and compact 80×24 layouts.
- Reconnect using applied event cursors and stable submission IDs after lost responses.
- Source previews from validated immutable checkpoints and `@path` completion.
- Exact publishing review, baseline diffs, reject/request-changes and explicit approval.
- Usage estimates, themes, in-memory drafts, cancellation, paused/uncertain-work handling.
- Cancellation/reclaim fencing fix and a Markdown-render/shutdown race regression.

The new API reads project compact task/session data without native history or
publication file payloads. Source previews are task-bound and bounded; control
service commands and provider permission rules are unchanged. No schema migration
is needed. Restart an idle API and worker to load the updated implementation.

UI tests use actual Textual widgets and synthetic providers, including a 10,000-event
replay, interrupted connections, lost POST responses, approval decisions, themes,
narrow layouts, resizing and terminal shutdown. Captures and verification evidence
are under ignored `.merlin/reports/tui/`. No live provider calls, new GitHub writes,
service restarts or deployments were performed for this milestone.

## Verified locally

- `uv.lock` resolves 91 packages, including five added runtime distributions
  for Textual. The updated lock passes `uv lock --check --offline`; the local
  development environment was synced using cached packages.
- Python 3.13: **248 pytest tests and 56 subtests passed; 11 opt-in live checks
  were deselected**, using the repaired `.venv/bin/pytest -q` environment.
- Mypy passed across all 37 application source files in the locked environment.
- **27/27 offline contract evaluation scenarios passed**, with 52 concrete test
  evidence entries. Report: `.merlin/reports/offline.json` (not tracked).
- Ruff lint and formatting, Python compilation, and Compose sample syntax passed.
- Wheel and source distribution built with `uv build --no-build-isolation --offline`.
  The wheel was installed without dependencies into the validation environment;
  `uv run --offline --no-sync merlin --help` passed.
- Distribution inspection confirmed packaged remote helpers and source archive
  deployment files, migrations, documentation, and the dedicated fixture marker.
  The rebuilt source archive also includes the runtime smoke driver, Compose
  configuration, trusted worker fixture, PostgreSQL tests, and dependency lock.
- Backup/restore tests use mocked Docker execution plus real local archive/hash
  validation. They do not establish a successful deployed restoration.

Pytest, mypy, lint, formatting, and offline evaluations now run against the
resolved verification environment rather than the earlier cached-import setup.
API contract tests inject synchronous I/O dispatch; actual production thread
offload remains separate. A standalone offload probe in this restricted
interpreter previously timed out after its first completion.

For the terminal milestone, the wheel and source archive were rebuilt with a
separate cached build interpreter and `--no-build-isolation --offline`. A wheel
installation outside the source tree passed packaged-style, compact-layout,
streamed-demo, command-palette and detach checks. Its 79 runtime dependencies
were copied from the locked development environment: a fresh uv dependency install
still hit the restricted filesystem's `Not a directory` error. This does not
establish a fresh network installation. Physical-terminal input and a live
Gemini/E2B task through the new interface remain manual acceptance checks.

## Open release gates

1. **Isolated build:** dependency locking, installation, pytest, and mypy are
   complete. The full verifier still needs an uninterrupted run from the
   network-enabled terminal: an executor retry failed installing build dependencies
   with `Not a directory` in uv's temporary build environment. The distributions
   passed a separate build with `--no-build-isolation`.
2. **Runtime deployment:** build/start the Docker images, validate migrations and
   concurrent claims against PostgreSQL, and exercise the real API/worker path.
   Compose syntax alone is insufficient.
   The latest retry could not access `/var/run/docker.sock` (permission denied).
   The isolated [runtime smoke driver](runtime-smoke.md) is ready, including eight
   PostgreSQL-specific tests, real HTTP probes, and SIGKILL/recovery checks. Its
   default preflight correctly exits without starting resources here; real
   PostgreSQL/container results remain pending.
3. **Provider integration:** the read-only, documentation PR and small coding PR
   workflows have now been exercised with real Gemini, E2B and GitHub App access.
   An operator-reviewed coding PR in a dedicated test repository was merged
   after its CI passed. Private repository URLs, commit identifiers, workflow
   run IDs, and application details are intentionally omitted from the
   publishable documentation. The original evidence remains in ignored local
   operational records. This historical trial does not establish deployment
   acceptance for the current Merlin image.

   Earlier live evidence includes repository listing/cloning, a read-only task,
   session checkpoint restoration across edits, and a reviewed documentation PR.
   Publishing approval and request-changes revision were exercised. Git cloning
   runs from `/tmp`; unique helper upload paths fix repeated sticky-directory
   upload failures. Checkpoint transports have three bounded capture attempts,
   distinct invalid-archive diagnostics, and pre/post-edit replay protections.
   Their broader live fault-injection acceptance remains open.

   The coding milestone included regression tests and an operator-prepared
   publication from validated checkpoint bytes after a file exceeded the model
   reply budget. This was one assisted coding task, not completion of the broader
   live model evaluation target. Evidence and guarded operator helpers remain
   in ignored local records.

   An inspected publication task recorded both GitHub writes
   successfully, then failed during context assembly. The local fix now budgets
   the same provider projection used by Gemini counting/generation, omitting
   internal bookkeeping and only exact duplicate tool output. Stored history,
   native signatures, authorization and the 150,000-character limit are preserved.
   Offline replay of that actual history now builds a 90,027-character provider
   context with session metadata; the original failed task record is unchanged.
   New regressions cover completing a large publication exactly once, keeping
   distinct output, and rejecting genuinely oversized signed interactions.
   Full local verification passes 211 tests and 56 subtests, Ruff and mypy.
   The running worker must be restarted to load this code; the replay did not
   make paid model calls or repeat GitHub writes. Evidence:
   `.merlin/reviews/context-budget/replay.json`.

   A separate cancellation/reclaim defect remains: a cancellation-requested task
   may be reclaimed, fail its initial usage reservation and remain running.
   The affected diagnostic attempt was finalized through an acquired lease and
   the normal fenced store API. This needs a focused fix and regression test
   before the next real worker-interruption/recovery milestone. Browser/LSP,
   child orchestration, private permissions/revocation and broader opt-in provider
   smoke cases still require their own live acceptance.
4. **Recovery operations:** restore a full database/state backup into a fresh
   deployment and inject worker/sandbox failures against real services.
5. **Model evaluation:** run live fixture trials and achieve the planned functional
   target. Offline contract passes are not evidence of Gemini coding performance.

Start with [operations](operations.md), [evaluations](evaluations.md), and
[the security review](security-review.md). These remaining gates require a
network-enabled environment and explicitly configured test accounts/infrastructure.

`python3 scripts/verify.py` prepares the next clean verification run in a separate
environment. It does not bypass executor restrictions: uncached package downloads
need PyPI access, and real container validation needs Docker daemon access.
Its three focused tests pass: failed resolution stops the pipeline, checks use a
separate environment, and dry-run mode executes nothing. No clean dependency or
live-service gate was completed by that addition alone. Subsequent clean
pytest/mypy verification fixed repository script imports, explicit model schema
types, and variables reused across unrelated operations.

The runtime fixtures also exposed duplicate completion events. A tool now emits
one `tool.finished` event with its full result in the same transaction that saves
the invocation and checkpoint, rather than a second event from the worker.

The first live task also provided an optimization baseline. Exact duplicate tool
text is removed from provider input, and default terminal output shows concise
progress and each answer once. Offline replay reduced serialized history by
44.3% and terminal output by 95.3%; actual token/latency savings remain unmeasured.
See [optimization evidence](optimizations.md) for scope and further work.
