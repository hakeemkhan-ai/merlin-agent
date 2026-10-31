#!/usr/bin/env python3
"""Resolve and verify Merlin in a separate development environment.

This checks local release prerequisites. It does not deploy services, run live
provider tests, or prove that the Docker daemon is available.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Check committed dependency lock", ("uv", "lock", "--check")),
    ("Install locked dependencies", ("uv", "sync", "--locked", "--dev")),
    ("Run default tests", ("uv", "run", "--no-sync", "pytest")),
    ("Check lint", ("uv", "run", "--no-sync", "ruff", "check", ".")),
    ("Check formatting", ("uv", "run", "--no-sync", "ruff", "format", "--check", ".")),
    ("Check types", ("uv", "run", "--no-sync", "mypy", "src")),
    (
        "Run offline contract evaluations",
        (
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "merlin.evaluation",
            "--offline",
            "--output",
            ".merlin/reports/offline.json",
        ),
    ),
    ("Build distributions", ("uv", "build", "--no-sources")),
    (
        "Check Compose syntax",
        (
            "docker",
            "compose",
            "--env-file",
            ".env.example",
            "-f",
            "deploy/compose.yaml",
            "config",
            "--no-env-resolution",
            "--quiet",
        ),
    ),
)


def verify(
    *,
    dry_run: bool = False,
    root: Path = ROOT,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    environment = dict(os.environ)
    # Keep the former cached validation environment out of clean verification.
    environment["UV_PROJECT_ENVIRONMENT"] = str(root / ".merlin" / "verification-venv")
    for label, command in STEPS:
        print(f"{label}: {shlex.join(command)}", flush=True)
        if dry_run:
            continue
        step_environment = dict(environment)
        if command[:2] == ("docker", "compose"):
            # env_file paths resolve against deploy/compose.yaml. Validate the
            # checked-in example without requiring the operator's real .env.
            step_environment["MERLIN_ENV_FILE"] = "../.env.example"
        try:
            runner(command, cwd=root, env=step_environment, check=True)
        except FileNotFoundError:
            print(f"Required executable is unavailable: {command[0]}", file=sys.stderr)
            return 2
        except subprocess.CalledProcessError as error:
            print(f"Verification stopped at: {label} (exit {error.returncode})", file=sys.stderr)
            return error.returncode if 0 < error.returncode < 126 else 1
    if not dry_run:
        print("Local verification passed. Live deployment/provider gates remain separate.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show steps without executing them")
    return verify(dry_run=parser.parse_args(argv).dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
