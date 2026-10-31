# Merlin E2B template

Build this Dockerfile as an E2B custom template named `merlin`, or set
`MERLIN_E2B_TEMPLATE` to your resulting template identifier. With E2B CLI 2.23.0,
sign in using `e2b auth login`, then run from the repository root:

```sh
cd deploy/e2b
e2b template create merlin --dockerfile Dockerfile
```

Wait for the successful build message before starting Merlin. The alias `merlin`
matches the default `MERLIN_E2B_TEMPLATE` setting. Builds run on E2B infrastructure;
this command does not need a local Docker daemon. See the
[template build overview](https://e2b.dev/resources/introducing-build-system-2-0).
The older `e2b template build` command is no longer available in this CLI.
The Dockerfile avoids multi-stage `COPY --from`, which E2B's parser rejects.
Template creation is an explicit infrastructure action and requires your E2B
account; application startup does not build or publish templates automatically.

The template includes Bash, Git, ripgrep, Python, Node.js, Chromium/Playwright,
Pyright, and the TypeScript language server. Pin and test template dependencies
before updating the deployed template identifier. App-side code uses E2B's SDK
at runtime and never falls back to local subprocess execution.

The SDK starts commands through a login shell, which can replace the Docker
image's `PATH`. Merlin explicitly prepends `/opt/merlin-tools/bin` after that
boundary for helpers and repository Bash commands. Keep the tool virtual
environment at that path when customizing this template.
The browser daemon also restores `/opt/merlin-browsers` as its browser cache when
that directory exists and no explicit `PLAYWRIGHT_BROWSERS_PATH` was supplied.

Workspace operations run as the sandbox user. Task creation configures egress
outside the sandbox: all destinations denied except `github.com` for repository
cloning and exact task-approved domains. Add package registries and documentation
hosts explicitly to the task allowlist as required. Wildcards, IP literals, and
private/local names are rejected. Domain filters operate on HTTP port 80 and TLS
port 443, rather than providing a guarantee against leakage to permitted hosts.

Checkpoints are uncompressed, checksummed, bounded tar archives, stored outside
E2B under the service's artifact directory. Dependency caches and secret paths
are excluded. Symlinks and special files are rejected explicitly. Restoring a
checkpoint replaces the source tree, including Git metadata, and does not restore
application processes. E2B pause/resume is not used as a substitute for backups.
