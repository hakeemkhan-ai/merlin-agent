# Independent implementation review

## 2026-11-06 review

The [security, quality, and bug review](audit-2026-11-06.md) records fixes for
credential exclusions during verification uploads, authentication before request
body processing, multi-operation uncertainty resolution, terminal resolution
state, and vulnerable monitoring image pins. It includes regression evidence,
the Python dependency audit, and the remaining deployment verification limits.

## 2026-10-29 audit

The subsequent ECC audit's [prioritized findings and execution evidence](audit-2026-10-29.md)
record fixes for uncertain-result recovery, session repository isolation, secret
paths, browser-origin checks, result bounds, and terminal races.

The broker now rejects credential/environment/key paths before requesting write
credentials. The same secret-path policy applies to direct file and LSP requests,
checkpoint capture/validation, and source previews. `.env.example` remains usable.
Existing archives containing newly excluded key/secret paths fail validation and
need a new clean checkpoint; the validator does not rewrite old artifacts.
Git history is still preserved and requires its own secret hygiene.

Browser WebSocket traffic never connects to a server: routed messages are consumed
locally. This includes localhost WebSockets because Playwright does not provide
the frame ancestry needed to safely distinguish an external iframe's socket.
Local HTTP application interaction remains available. Local hostnames use the
same normalization in URL authorization and origin checks, including trailing dots.

Uncertain tool results require explicit operator resolution before resume. Workers
also enforce that rule for tasks queued before an update, and register workspaces
for cleanup before attempting restoration. Session repository consistency is
enforced by the store transaction, with a PostgreSQL session advisory lock.

## Earlier implementation review

The build used separate implementation owners for persistence, runtime, sandbox
tools, GitHub, API/CLI, evaluations, and operations. Cross-component review found
and corrected these issues before final integration:

- Completed reviewed GitHub operations replay their durable result before asking
  for another approval; uncertain mutations remain blocked from blind replay.
- Child creation reuses its original checkpoint/specification on idempotent retry.
- Children are denied publishing before an approval can block their parent.
- Terminal root tasks cancel both queued and already-running children.
- Reclaimed leases receive fresh sandboxes restored from a completed checkpoint,
  preventing a stale worker's cleanup from killing the replacement workspace.
- Recursive search enumerates allowed source files before applying caller globs;
  globs cannot opt secret paths back into model input.
- External pages, external ancestor frames, and public redirect chains cannot
  proxy browser requests to sandbox-local applications.
- Gemini preflights input tokens within its reserved context allowance, including
  serialized instructions/tool declarations and a framing margin.
- Private GitHub issue/PR reads request their corresponding read permissions.
- Empty optional environment values use defaults, and deployment health checks
  match the actual API route.

Regression evidence is in `tests/test_security_crosscheck.py`, provider tests,
and the real-store worker integration tests. Review does not establish a complete
security guarantee. Egress allowlists permit access to their approved hosts,
arbitrary Bash remains capable within its own sandbox, and secrets present in
repository history require separate repository hygiene.

Live E2B/Chromium/LSP behavior, PostgreSQL races, container startup, GitHub App
authorization/revocation, and full deployment backup restoration have not been
verified in this restricted development environment. No external publishing or
deployment was performed as part of these local checks.
