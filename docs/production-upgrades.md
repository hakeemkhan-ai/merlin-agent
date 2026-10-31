# Private beta runtime upgrades

This implementation follows the reliability, repeated-work, and long-task
quality priorities from the repository audit. The original 112 opportunities
are a backlog, not 112 independent switches that can all be enabled safely.
The implemented changes and the remaining measurement gates are recorded here.
This is a private beta hardening pass, not certification of a deployed service.

## Delivered behavior

| Area | Changes | Main evidence |
| --- | --- | --- |
| Model completion | Require a terminal `STOP`; reject truncated, blocked, empty, malformed, partial, and reused-ID responses before dispatching tools. Preserve native signed parts. | `test_model_runtime.py`, `test_gemini_failures.py` |
| Model latency | First-chunk, idle, and total deadlines; first-text and elapsed-time stream flushing; async event persistence. | Model runtime regressions |
| Provider cost | Exact bounded token-count LRU, input-based preflight reservations, cached-input/thinking usage, attempts and response IDs. Missing usage and uncertain requests retain reservations. | Model and production-worker regressions |
| Recovery | Release only proven undispatched requests; operator budget increases and billing reconciliation; legacy unknown reservations survive migration. | `test_production_store.py` |
| Worker | Setup/checkpoint/tool/cleanup deadlines, bounded cancelled-task draining, independent consumer recovery, SIGTERM cleanup and worker presence. Preserve lease fencing and atomic tool-result/checkpoint writes. | `test_production_worker.py`, existing recovery tests |
| Context | Incremental structured summaries, exact token fitting with a reserve, instruction caching/batched reads, pre-mutation instruction revalidation, scoped memory and history retrieval, bounded repository maps. | `test_context_runtime.py` |
| Context integrity | Keep the objective and recent complete interactions; validate summary source prefixes; retain historical evidence references; remove exact duplicate output only from provider projections. | Context and native-part regressions |
| Read concurrency | Bounded explicitly whitelisted reads with mutation barriers and declaration-order response persistence. Grouped cancellation and reused-ID fencing are covered. | Production-worker and integration tests |
| Sandbox | Upload helpers once per workspace/family; verify the bytes executed on every call; remove request files; evict caches on close/setup failure; directory fsync after checkpoint publication. | Sandbox tests and review |
| Large output | File/search pagination, glob filtering before the file cap, byte-bounded listing cursors, process-log offsets and tail reads, explicit clipping metadata. | `test_sandbox_runtime.py` |
| Language servers | Per-workspace Python/TypeScript daemons, changed-document versions, refresh of other open buffers, stale-diagnostic handling, bounded open documents, reset/config invalidation and idle cleanup. | `test_lsp_runtime.py`, real-template smoke |
| Database | Metadata-only control/claim/presence reads, targeted children/predecessors/latest-session checkpoint, constant-query session summaries, keyset task pages and bounded event pages. | Production-store tests and benchmark |
| API | Request byte/read-time limits, stream admission, bounded SSE backlog draining, streamed artifact downloads, owned lifespan cleanup, readiness and checksum-verified manifest caching. | API/store/view tests |
| Publishing | Prepare compact proposals from owned immutable checkpoints; bind approval arguments to checkpoint ID/SHA/base/paths; materialize actual bytes/modes server-side and show the same source in previews. Protect prepared snapshots during retention. | `test_github_runtime.py`, retention tests |
| GitHub efficiency | Bounded scoped read-token cache, expiry/grant/signer invalidation, coalesced token minting and rate-limit-aware retries for safe reads and explicit token rejections. Mutation transport failures remain uncertain. | GitHub runtime tests |
| Terminal | Avoid unchanged tool-card updates; stable explicit CLI submission keys; budget/reservation recovery commands; completion events distinguish execution evidence from final prose. | CLI/TUI and worker tests |
| Operations | Migration `0002`, readiness distinct from liveness, per-process worker health, container resource bounds, graceful shutdown, evaluation/performance CI artifacts and updated runbooks. | Deployment, migration and runtime-smoke tests |

`task.completion` records changed paths, the final checkpoint, and concrete command
exit codes after the last source edit. It does not infer test coverage or correctness
from a command name or the model's final prose. Earlier checks are cleared after
a subsequent edit. API task success means the agent turn completed; operators
must still assess its recorded verification scope.

## Measurements

The repeatable synthetic harness is `scripts/performance_bench.py`; results are
under ignored `.merlin/reports/production-runtime/`. Ten samples on the E2B test
machine produced these results:

| Measurement | Result | Limit |
| --- | --- | --- |
| Repeated instruction builds | 30 cold instruction RPCs, zero warm RPCs; p50 2.83ms cold and 0.48ms warm | Synthetic 2ms reads; mutation boundaries deliberately revalidate |
| Exact output projection | 48,472 to 24,876 fixture bytes, 48.7% fewer | Serialized fixture bytes, not billed tokens or a live cost saving |
| Session lists | Three SELECTs at both 10 and 100 sessions, without selecting native history | SQLite generated fixtures with 64KB histories |
| Four-read task | p50 121.9ms sequential, 89.0ms at concurrency four | Entire fake-provider task including SQL/setup; no real network SLO |
| Python LSP | 5.624s cold, 0.0018s warm | One real E2B-template smoke sample |
| TypeScript LSP | 2.1953s cold, 0.0628s warm | One real E2B-template smoke sample |

The LSP smoke also verified changed document versions and session reset. It made
no model or GitHub calls. These measurements support eliminating repeated work;
they do not establish a real-model coding success rate or hosted-service p95.

## Upgrade and verification

Stop writers, take a complete database/state backup, run `alembic upgrade head`,
then start the matching API and worker. Do not overlap old workers with the new
schema. Existing unsettled reservations become `unknown` and need evidence-based
reconciliation rather than an automatic refund. See [operations](operations.md)
for commands and rollback details.

All repository commands for this implementation ran inside disposable E2B
workspaces. Local operations were source inspection, edits, infrastructure calls,
and artifact transfer. The deterministic suite uses fake external providers;
the PostgreSQL and LSP checks are separately identified. No deployment, real
GitHub write, or user-repository publication was performed.

Verification completed on 2026-10-31:

- Default suite: **449 tests and 56 subtests passed**, 15 opt-in cases deselected.
- Final verification-runner follow-up: three tests passed after fixing example-only
  Compose validation to avoid requiring the operator's real `.env`.
- Ruff lint and formatting passed for 113 Python files; mypy passed 42 source files.
- All 29 offline contract scenarios passed.
- Nine real PostgreSQL 17.11 UTF8 concurrency, fencing, persistence and migration
  tests passed in the earlier component E2B workspace; these are separate from
  the default-suite count.
- Real-template Python and TypeScript LSP smoke passed, including edits/reset.
- The committed dependency lock passed validation. Wheel and source distributions
  built; the installed wheel passed CLI/resource checks outside the source tree.
- Compose syntax passed using the official Compose v2.39.4 standalone binary,
  verified against its published release SHA-256, and the example environment.
  This did not start containers or establish a successful deployed restore.

During repository publication, GitHub Actions also passed the actual
container/PostgreSQL/API worker-crash recovery smoke job using fake model and
sandbox providers. This closes that image/stack smoke check, while full deployed
backup restoration and live-provider quality/latency gates remain open.

The combined-suite run caught a stdout formatting compatibility regression; it
was corrected before the final pass. Broker review also added a regression for
a successful GitHub write returning unreadable JSON: it remains uncertain and
reconciles its existing marker without posting again.

## Measurement-gated backlog

The following opportunities remain disabled or unimplemented. They require the
evidence below before they should change the default runtime:

| Opportunity | Required next evidence or design |
| --- | --- |
| Append-only native message storage | Migration/backfill, immutable signed-message semantics, and long-history SQL write/read benchmarks. Canonical history currently remains JSON; paging bounds responses but still loads that JSON internally. |
| Incremental/compressed checkpoint chains | Repeated large-repository capture/restore measurements, chain verification, corruption/failure drills and retention/backup compatibility. Full independent snapshots remain the recovery format. |
| Warm workspace/dependency/clone reuse | Explicit trust, repository identity, credential expiry and isolation keys; a measured clone/setup bottleneck. No cross-task machine sharing is enabled. |
| Explicit provider prompt caches | Supported-model verification, stable-prefix hit measurements, TTL/deletion lifecycle and net storage-cost accounting. Exact local token-count caching is separate. |
| Model routing or learned scheduling | Representative task-quality, cost and p50/p95 comparisons, with regression gates. No silent model substitution is enabled. |
| Suspending parents while children run | Durable continuation scheduling and checkpointing of background work; fault/reclaim tests beyond the current fixed private-beta root/child limits. |
| Multimodal browser feedback | Bounded image transport, token accounting and visual-task quality evaluations. Screenshots are currently artifacts. |
| Automated verification requirements | Task-specific acceptance contracts, hidden test execution in E2B and checks tied to the exact final snapshot. The runtime now records evidence without claiming those contracts exist. |
| Broader production acceptance | A real deployed-image startup/restart and backup/restore drill; dedicated repository Gemini/E2B and reviewed GitHub mutation trials; live long-task quality and latency baselines. |

These gates are part of the remaining roadmap. Passing deterministic tests alone
does not justify claiming all audit opportunities or production acceptance gates
are complete.
