# Merlin evaluations

The version-one catalogue in `evals/scenarios.v1.json` defines 29 scenarios for
edits, navigation, browser behavior, context, delegation, recovery, and GitHub.
Run the evaluator from a development checkout: its catalogue and test fixtures
are intentionally kept alongside source rather than bundled into the CLI wheel.

## Offline contract checks

```sh
uv run python -m merlin.evaluation --offline --output .merlin/reports/offline.json
```

Each scenario names concrete unittest methods that replace external providers.
The JSON report records assertion evidence, duration, and a pass/fail/inconclusive
status. Missing tests and skipped tests cannot earn a pass. This measures service
contracts and regressions; it does not measure Gemini's coding success rate.
Select individual cases with repeated `--scenario SCENARIO_ID` arguments.

Runtime worker-presence and smoke-driver isolation scenarios use trusted
metadata/command-runner fixtures. They do not establish that Docker or
PostgreSQL started. CI runs this catalogue and uploads its report alongside
distribution and synthetic performance artifacts; missing evidence remains a
separate failure/inconclusive result.

## Reproducible runtime performance

```sh
uv run python scripts/performance_bench.py --iterations 10 \
  --output .merlin/reports/performance.json
```

Run repository verification inside E2B, as required by `AGENTS.md`. The benchmark
uses generated histories, an in-memory database, and controlled fake I/O to
measure context compaction, bounded event/session reads, and sequential versus
bounded parallel tool reads. It records sample counts, p50/p95 durations, and
machine/runtime metadata. Keep the raw JSON with an immutable source revision
or source archive; compare like fixtures on the same environment. One timing
sample is a smoke result, not a latency percentile or service SLO. Query-count,
page-size, cache-invalidation, and concurrency-limit regressions are tested
deterministically in the normal suite.

Use separate baseline/candidate runs before enabling cost-sensitive provider
caches, model routing, workspace reuse, or incremental snapshots. Evaluate
latency and tokens alongside successful hidden behavior checks, changed-file
preservation, recovery, and authorization. A faster run that drops constraints
or bypasses a recovery boundary is a failed change.

## Explicit live fixture trials

Create a dedicated GitHub repository containing the entire contents of
`evals/fixtures/`, including `.merlin-eval-fixture.json`. Do not use a normal work
repository. Install Merlin's GitHub App on it and configure an authenticated
Merlin deployment, an E2B template, and a Gemini model available to your account.
Preserve the fixture repository's default branch during a trial. The runner
verifies its immutable Git tree and fixture marker before submitting any tasks.

Configure these environment variables in your own protected environment:

- `MERLIN_EVAL_LIVE=1` explicitly enables remote work and provider usage.
- `MERLIN_EVAL_API_URL` identifies the dedicated deployment, using HTTPS except loopback.
- `MERLIN_EVAL_REPOSITORY` and `MERLIN_EVAL_FIXTURE_ACK` must both name its fixture repository.
- `MERLIN_EVAL_OWNER_TOKEN` authenticates to Merlin.
- `MERLIN_EVAL_GITHUB_TOKEN` provides read access for independent fixture verification.

```sh
uv run python -m merlin.evaluation --live --scenario edit-exact-replacement \
  --output .merlin/reports/live.json
```

Use an ID from the catalogue if selecting a different scenario. Every submitted
task has publishing permissions disabled. The runner never approves requests,
creates issues, or pushes branches itself. Tasks waiting for approval fail that
trial and are cancelled. Each task is limited to 40 model calls, $2 estimated
model usage, and 900 sandbox seconds; a full run can consume multiple task budgets.

Live grades verify checksum-protected checkpoint content, protected-file Git
hashes, concrete persisted tool results, and screenshot signatures. Final model
prose or a `succeeded` task state alone cannot earn a pass. Usage is included when
available. The runner never executes repository code on the evaluator host.
Missing evidence and unsupported crash/approval scenarios are inconclusive.
Use dedicated fault-injection and reviewed GitHub workflow trials for those cases.

The current fixture objectives include exact file content and persisted tool
evidence. They do not supply an independent general-purpose hidden test runner.
For a coding-quality release study, provision a separate evaluator-owned E2B
workspace, restore only the candidate's verified checkpoint, and apply hidden
tests unavailable to the task workspace. Execute those tests inside E2B with
fixed time/resource limits and a network policy; record the test-suite hash,
checkpoint hash, exit code, timeout state, changed-file checks and token/cost
usage. Keep held-out tasks separate from development fixtures and repeat trials
to show variance. This remains a release gate until that independent runner has
been implemented and exercised; neither final model prose nor an offline
contract percentage closes it.

## Release interpretation

Keep offline and live reports separate. The release target remains all
deterministic security/recovery checks passing and at least 80% of the functional
live evaluation set passing. The current repository has no live result supporting
that target; an offline pass percentage is not a substitute. Provider SDK/network
behavior, PostgreSQL locking, and a real backup/restore must also be checked on
the deployed test environment.
