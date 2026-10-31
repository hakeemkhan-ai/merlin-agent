#!/usr/bin/env python3
"""Validate real PostgreSQL/HTTP/restarts in a disposable Docker project.

Model and sandbox implementations are trusted fixtures. This is a runtime test,
not an E2B/Gemini/GitHub integration trial. No production .env or host ports are used.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Build locked application and verification images", ("build", "api", "checks")),
    ("Start isolated PostgreSQL", ("up", "-d", "--wait", "--wait-timeout", "90", "postgres")),
    ("Apply production migrations", ("run", "--rm", "--no-deps", "migrate")),
    (
        "Verify PostgreSQL migrations and concurrency",
        (
            "run",
            "--rm",
            "--no-deps",
            "checks",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            "/app/pyproject.toml",
            "-m",
            "postgres",
            "/checks/test_postgres.py",
        ),
    ),
    (
        "Start real API and fixture worker",
        ("up", "-d", "--no-build", "--no-deps", "--wait", "--wait-timeout", "90", "api", "worker"),
    ),
    (
        "Verify database schema and worker readiness",
        ("run", "--rm", "--no-deps", "checks", "python", "/checks/runtime_probe.py", "ready"),
    ),
    (
        "Complete a task through HTTP and the durable worker",
        ("run", "--rm", "--no-deps", "checks", "python", "/checks/runtime_probe.py", "complete"),
    ),
    ("Restart API and worker processes", ("restart", "api", "worker")),
    (
        "Verify persisted task and events",
        (
            "run",
            "--rm",
            "--no-deps",
            "checks",
            "python",
            "/checks/runtime_probe.py",
            "verify-complete",
        ),
    ),
    (
        "Interrupt a task after its mutation intent",
        ("run", "--rm", "--no-deps", "checks", "python", "/checks/runtime_probe.py", "interrupt"),
    ),
    ("Crash the fixture worker", ("kill", "-s", "SIGKILL", "worker")),
    ("Start replacement worker", ("up", "-d", "--no-build", "--no-deps", "worker")),
    (
        "Verify uncertain outcome and acknowledged recovery without replay",
        (
            "run",
            "--rm",
            "--no-deps",
            "checks",
            "python",
            "/checks/runtime_probe.py",
            "verify-paused",
        ),
    ),
)


def run(
    *,
    dry_run: bool = False,
    keep: bool = False,
    root: Path = ROOT,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    suffix = secrets.token_hex(6)
    project = "merlin-smoke-" + suffix
    environment = dict(os.environ)
    environment.update(
        {
            "MERLIN_SMOKE_TAG": suffix,
            "MERLIN_SMOKE_PASSWORD": secrets.token_hex(24),
            "MERLIN_SMOKE_OWNER_TOKEN": secrets.token_urlsafe(48),
        }
    )
    compose = (
        "docker",
        "compose",
        "--env-file",
        os.devnull,
        "--project-name",
        project,
        "-f",
        str(root / "deploy" / "compose.smoke.yaml"),
    )
    print(
        f"Disposable project: {project}; fixture model/sandbox, real PostgreSQL and HTTP.",
        flush=True,
    )
    if dry_run:
        for label, command in STEPS:
            print(f"{label}: {shlex.join((*compose, *command))}")
        if not keep:
            print("Cleanup: " + shlex.join((*compose, "down", "--volumes", "--remove-orphans")))
        return 0
    try:
        runner(
            ("docker", "version", "--format", "{{.Server.Version}}"),
            cwd=root,
            env=environment,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        print(
            "Docker daemon unavailable. Run from a Docker-enabled terminal; no test resources "
            "were started.",
            file=sys.stderr,
        )
        return 2
    status = 0
    try:
        for label, command in STEPS:
            print(label, flush=True)
            runner((*compose, *command), cwd=root, env=environment, check=True)
    except (OSError, subprocess.CalledProcessError):
        status = 1
        print("Runtime smoke check failed. Collecting disposable service logs.", file=sys.stderr)
        try:
            runner(
                (*compose, "logs", "--no-color", "--tail", "80"),
                cwd=root,
                env=environment,
                check=False,
            )
        except OSError:
            pass
    finally:
        if keep:
            print(
                f"Retained test resources for {project}; credentials were ephemeral. "
                "Remove this project's containers/volumes after inspection."
            )
        else:
            try:
                runner(
                    (*compose, "down", "--volumes", "--remove-orphans", "--timeout", "5"),
                    cwd=root,
                    env=environment,
                    check=True,
                )
            except (OSError, subprocess.CalledProcessError):
                print(
                    f"Cleanup failed for {project}; remove its test resources manually.",
                    file=sys.stderr,
                )
                status = 1
    if status == 0:
        print("PostgreSQL/HTTP/restart smoke checks passed. Cloud integration remains separate.")
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--keep", action="store_true", help="Retain disposable resources for inspection"
    )
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run, keep=args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
