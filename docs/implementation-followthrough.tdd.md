# Implementation follow-through regression — 2026-10-29

An approved implementation plan was repeatedly answered with another plan.
The model instructions discouraged resuming earlier work without recognizing
explicit approval, and full prior conversation was removed as soon as a
workspace started. Provider failures also surfaced as `ServerError: task failed`.
The screenshots do not establish the original provider HTTP status.

The fix makes implementation requests actionable, retains bounded prior
conversation during tool use, and handles provider failures with safe errors
and recoverable task state. The work followed ECC's defect workflow: trace the
failure, reproduce it, implement the fix, and obtain code and security reviews.
The user authorized the fix; no commit or service restart was performed.

## Plan and guarantees

| Task | Regression target | Evidence |
| --- | --- | --- |
| Retain the approved proposal through workspace activation | `tests/test_chat.py::test_approved_plan_stays_in_context_through_repository_execution` | Initially failed after the first read; now preserves a complete proposal longer than the old 2,000-character excerpt through reads and writes. |
| Execute an implementation approval while keeping chat conversational | Live Gemini, real file and command adapter inside E2B | A planning-only request used no tools. “Yes please implement this” read and changed the fixture and ran Node tests; four independent assertions passed. A following greeting used zero tools and zero sandbox seconds. |
| Retry explicit transient provider rejections before the first response chunk | `tests/test_gemini_failures.py` | Six initial HTTP regressions failed. HTTP and early stream failures now recover within three attempts; request/authentication errors are not retried. |
| Preserve cancellation and avoid replaying partial or uncertain responses | `tests/test_gemini_failures.py` | Cancellation propagates. Interrupted connections and partial streams are not automatically retried. |
| Resume saved edits without repeating them | `tests/test_chat.py::test_model_outage_pauses_and_resumes_without_repeating_completed_edit` | A model outage pauses the task after an edit; resume succeeds with one edit invocation in total. |
| Keep provider bodies private | `tests/test_gemini_failures.py::test_sdk_retry_logging_does_not_expose_provider_body` | Review reproduced a payload leak in SDK retry logging. Adapter-owned retries and suppression of the `google_genai` logger resolve it; public errors use status alone. |

## Verification

All execution was inside a disposable E2B workspace. Local operations were
limited to source inspection, editing, and artifact transfer.

- `uv run pytest -q --tb=short`: 326 passed, 56 subtests passed, 15 opt-in tests deselected.
- Final focused suite: 54 passed and 4 subtests passed after stream cleanup's type correction.
- Coverage of context, contracts, Gemini, and worker: 604 of 652 statements, 93% combined; individual modules 89–100%.
- `uv run ruff check .` and `uv run ruff format --check .`: passed.
- `uv run mypy src`: 38 source files passed.
- `uv build`: source distribution and wheel built.
- `uv run python -m merlin.evaluation --offline`: all 27 contract scenarios passed.
- ECC code and security review findings were corrected and rechecked.

Coverage was measured with `uv run --with coverage coverage run` over
`merlin.integrations.gemini,merlin.context,merlin.worker,merlin.contracts`
and the five focused test files. This is statement coverage for those modules,
not a coverage claim for the entire application. No dependency or lockfile
changes were needed.

The first live harness used an overly specific Node reporter/count assertion;
it was corrected to run four independent behavior checks because Merlin can
add tests and Node's reporter format varies. Earlier live attempts did execute
and test edits, but only the corrected complete run is recorded as passing.
The live fixture made no GitHub operations and did not modify a user repository.
PostgreSQL and live GitHub suites were not rerun for this change. Long session
history remains bounded and older material can be omitted when context is full.

Generated diff, coverage, evaluation, and live results are under
`.merlin/reports/followthrough-2026-10-29/`. No usable Git metadata was present
for checkpoint commits; the diff and this report preserve the red/green evidence.

See [operations](operations.md) for retry and resume behavior. Provider retry
status guidance was checked against Google's
[Gemini troubleshooting guide](https://ai.google.dev/gemini-api/docs/troubleshooting)
and the installed `google-genai` 2.33.0 SDK source. Restart the local worker to
load the change, then request implementation in the same repository session.
