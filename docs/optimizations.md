# First live task: optimization evidence

The operator completed an explanation task against a dedicated test repository
using Gemini 3.5 Flash-Lite, a real E2B workspace,
GitHub App read access, file tools, and initial/final checkpoints. This was a
read-only explanation task, without dependency installation or test execution.

## Observed baseline

The task made five model calls and four tool calls: list files, read two README
sections, and read `package.json`. Provider-reported usage summed to 42,732 input
and 712 output tokens. Input grew from 2,345 to 15,281 tokens as the conversation
accumulated. Tool text appeared both in `output` and `data.output`.

Normal terminal rendering also repeated successful tool payloads, printed
internal bookkeeping, and displayed the final answer as streamed text, a raw
`model.reply` event, and a final task result.

## Implemented changes

- The shared provider projection removes `data.output` only when it exactly equals the
  same result's `output`. It preserves checksums, line counts, errors, artifacts,
  function IDs, and signed native model parts. It uses the same reduced input for
  token counting and generation. Durable history and complete event payloads
  remain available for recovery and debugging.
- The default terminal renderer shows concise progress and each answer once.
  It supports streamed replies, final-result fallback, and attaching without
  earlier deltas. Failures and approval review remain visible. `--json` retains
  complete machine-readable events and the final snapshot.
- Agent instructions encourage targeted sections and grouped independent reads.
  The worker already supports multiple tool calls in one model turn. This
  guidance has not yet been measured in a fresh live task.

## Offline replay measurements

Using the saved completed task, without new provider calls:

| Representation | Before | After | Reduction |
|---|---:|---:|---:|
| Serialized conversation, excluding internal outer fields | 50,132 characters | 27,932 characters | 44.3% |
| Default terminal event/result output | 57,257 characters | 2,679 characters | 95.3% |

These are character counts. They do not establish equivalent token, cost, or
latency reductions. The next live comparison should record calls, input/output
tokens, provider latency, and answer correctness on the same repository revision.

## Context budgeting after a large publication

An inspected live publishing task completed its branch
upload and PR creation, then failed its final context build. The estimator counted
both native function arguments and Merlin's duplicate `_merlin_calls` bookkeeping,
as well as repeated tool text that the Gemini adapter would omit. Session metadata
then pushed the stored representation past the context limit.

Context budgeting now uses the same pure provider projection as token counting
and generation. This preserves durable history and complete native signed parts;
only internal outer fields and exact `output`/`data.output` duplication are omitted
from the size calculation. Distinct data and genuinely oversized interactions
still count toward the unchanged limit.

An offline replay of that real saved history reproduces the old error and builds
successfully with the fix: the stored history is 147,463 characters, and the
projected context **including session metadata** is 90,027 characters against the
150,000-character budget. These figures measure different representations; no
percentage or token/cost saving is inferred. The original task and its failed
state were left unchanged, and no GitHub action or paid model call was repeated.
Report: `.merlin/reviews/context-budget/replay.json`.

New tests cover large signed publication with predecessor context, exact versus
distinct tool output, actual oversize rejection, and a worker completing a
publication once with real SQLite persistence and fake external services. The
full default suite passes 211 tests and 56 subtests; lint, formatting and mypy pass.
Restart the existing worker when idle to load this fix before live validation.

## Further work

Prioritize better file/section selection and measure grouped reads before adding
more orchestration. Repository-instruction caching needs invalidation for file
mutations, restored workspaces, and background processes. Provider context caching
needs model-specific accounting and TTL/storage-cost evaluation. Retain current
checkpoint, authorization, and uncertain-operation safeguards while optimizing.

Configured dollar figures are estimates. Google's published standard Flash-Lite
rates differ from Merlin's generic defaults; see
[operating guidance](operations.md) for configuration and the pricing source.
