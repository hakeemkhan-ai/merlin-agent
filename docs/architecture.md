# Merlin architecture and implementation contracts

The terminal connects to an authenticated API. PostgreSQL stores tasks, leases,
events, approvals, invocation results, checkpoints, and root-scoped budgets. Workers
call Gemini and execute tools in E2B. GitHub writes pass through a trusted broker.

`merlin.contracts` is the version-one boundary. The lead owns shared contracts and
dependencies. Worker mutations must validate an unexpired lease generation.
Persist complete model responses before dispatching tools. An interrupted invocation
is uncertain until reconciled; arbitrary shell commands are never blindly replayed.

Workspaces and child workspaces are isolated. Checkpoint artifacts are durable,
checksummed, and published before database references. Restore validates archive paths.
Children inherit root budgets and cannot widen permissions or publish to GitHub.

The development test harness uses fake providers and an ephemeral SQLite database.
PostgreSQL concurrency and real external behavior require separately marked tests.

## Runtime boundaries

The CLI sends owner-authenticated `/v1` requests and resumes SSE event streams by
sequence ID. API and worker are separate processes from the same Python package.
The composition root wires provider protocols to the SQL store, Gemini, E2B,
repository context, tool dispatcher, GitHub broker, and child-task dispatcher.

Each model response is stored before its tool calls execute. Invocation identity
and arguments remain stable across recovery. Mutating tools publish their
post-operation checkpoint reference and result in one database transaction.
Completed results can repair missing history without running the operation again.
Parallel model calls receive one complete native tool-response block.

Unfinished mutating invocations pause for operator inspection. Resolution records
a failed, acknowledged outcome and restores the last completed source checkpoint;
it neither asserts success nor reverses external effects. A reclaimed generation
creates its own sandbox so stale cleanup cannot affect the replacement worker.

Each child starts from an immutable parent snapshot and shares its root's budget.
The parent integrates child file deltas serially, requiring unchanged base hashes;
any conflict aborts the entire merge. Parent termination requests child cleanup.
Only one delegation level and two concurrent children are supported initially.
Child integration currently merges file contents; executable-bit-only changes
need explicit parent application and verification before publishing.

The initial source archive limit is 64 MiB, with bounded members and regular files
only. Symlinks and special files are rejected. Dependency caches and secret paths
are excluded, while Git history is preserved. Application processes and browser
sessions are recreated after restoration; snapshots are not VM memory backups.

## Context and visibility

Root and applicable nested `MERLIN.md` files supplement the user's objective.
Recent complete interactions preserve Gemini's native metadata; bounded summaries
reference the original durable history, changes, validation, and unresolved work.
Input-token preflight includes serialized instructions/tool declarations plus a
framing margin inside the reserved allowance. Model selection is persisted per
task, with service configuration supplying the default for new submissions.

Task/model/tool traces use identifiers and operation names, not prompts or
credentials. Prometheus tracks task states, queue duration, recoveries, usage,
estimated spend, reserved sandbox runtime, compactions, and latency. Full task
history and artifacts require owner authentication. Human terminal output filters
control characters while JSON output preserves escaped original data.

The Gemini SDK is constrained below its next major version, following its
[published migration notice](https://pypi.org/project/google-genai/). The E2B adapter
requires the network-policy-capable SDK; its Python API still needs a live smoke
test against the configured account and template.
