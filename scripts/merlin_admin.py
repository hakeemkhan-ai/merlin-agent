#!/usr/bin/env python3
"""Quiesced Compose backups and explicit, rollback-protected restores.

Only trusted maintenance code executes in the app container. Repository workloads
continue to run exclusively in E2B. This script needs only the Python standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

ROOT = Path(__file__).resolve().parents[1]
APP_SERVICES = ("edge", "api", "worker")
BACKUP_FILES = ("database.dump", "state.tar")
SNAPSHOT_STATE = """
import sys, tarfile
def safe(info):
    if not (info.isfile() or info.isdir()):
        raise ValueError('State contains a link or special file')
    return info
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
    archive.add('/data', arcname='data', filter=safe)
"""
RESTORE_STATE = """
import os, pathlib, shutil, sys, tarfile
root = pathlib.Path('/data')
for child in root.iterdir():
    if child.is_dir() and not child.is_symlink():
        shutil.rmtree(child)
    else:
        child.unlink()
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    archive.extractall(path='/', filter='data')
for directory, dirs, files in os.walk(root):
    os.chown(directory, 10001, 10001)
    for name in files:
        os.chown(os.path.join(directory, name), 10001, 10001)
"""


class AdminError(RuntimeError):
    """An operator-facing maintenance failure without credential-bearing output."""


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def validate_backup(source: Path) -> None:
    """Reject incomplete, changed, or unsafe snapshots before contacting Docker."""
    manifest_path = source / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise AdminError("Backup has no regular manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text())
        if manifest["format"] != 1 or manifest["postgres_major"] != 17:
            raise ValueError("Unsupported backup version")
        if set(manifest["files"]) != set(BACKUP_FILES):
            raise ValueError("Incomplete backup manifest")
        for name in BACKUP_FILES:
            path = source / name
            record = manifest["files"][name]
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size != record["bytes"]
                or digest(path) != record["sha256"]
            ):
                raise ValueError("Backup checksum mismatch")
        with (source / "database.dump").open("rb") as stream:
            if stream.read(5) != b"PGDMP":
                raise ValueError("Not a PostgreSQL custom-format dump")
        seen: set[str] = set()
        with tarfile.open(source / "state.tar", "r:") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or path.parts[0] != "data"
                    or "\\" in member.name
                    or not (member.isfile() or member.isdir())
                    or path.as_posix() in seen
                ):
                    raise ValueError("Unsafe state archive member")
                seen.add(path.as_posix())
        if "data" not in seen:
            raise ValueError("State archive has no data root")
    except (OSError, KeyError, TypeError, ValueError, tarfile.TarError) as error:
        raise AdminError("Backup validation failed; no service was changed") from error


class Compose:
    def __init__(self, env_file: Path, project_name: str | None = None) -> None:
        self.env_file = env_file.resolve()
        self.command = [
            "docker",
            "compose",
            "--env-file",
            str(self.env_file),
            "-f",
            str(ROOT / "deploy" / "compose.yaml"),
        ]
        if project_name:
            self.command += ["--project-name", project_name]

    def run(
        self, *arguments: str, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None
    ) -> bytes:
        environment = os.environ.copy()
        environment["MERLIN_ENV_FILE"] = str(self.env_file)
        try:
            result = subprocess.run(
                [*self.command, *arguments],
                env=environment,
                stdin=stdin,
                stdout=stdout if stdout is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            # Compose output may include expanded configuration; never print it here.
            raise AdminError("Docker Compose maintenance command failed") from error
        return result.stdout or b""

    @contextmanager
    def quiesce(self, *, resume: bool) -> Iterator[None]:
        running = self.run("ps", "--status", "running", "--services").decode().splitlines()
        if "migrate" in running:
            raise AdminError("Wait for the migration service to finish before maintenance")
        if "postgres" not in running:
            raise AdminError("Start the PostgreSQL service before maintenance")
        active = [service for service in APP_SERVICES if service in running]
        if active:
            self.run("stop", "--timeout", "90", *active)
        try:
            yield
        finally:
            if resume and active:
                self.run("start", *reversed(active))

    @staticmethod
    def _output(path: Path) -> BinaryIO:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return os.fdopen(descriptor, "wb")

    def snapshot(self, destination: Path) -> None:
        """Write a new directory. Caller must have stopped application writers."""
        destination.mkdir(mode=0o700, parents=False)
        try:
            with self._output(destination / "database.dump") as stream:
                self.run(
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "merlin",
                    "-d",
                    "merlin",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    stdout=stream,
                )
            with self._output(destination / "state.tar") as stream:
                self.run(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "--user",
                    "0",
                    "--entrypoint",
                    "python",
                    "api",
                    "-c",
                    SNAPSHOT_STATE,
                    stdout=stream,
                )
            manifest = {
                "format": 1,
                "postgres_major": 17,
                "created_at": datetime.now(UTC).isoformat(),
                "files": {
                    name: {
                        "bytes": (destination / name).stat().st_size,
                        "sha256": digest(destination / name),
                    }
                    for name in BACKUP_FILES
                },
            }
            with self._output(destination / "manifest.json") as stream:
                stream.write((json.dumps(manifest, indent=2) + "\n").encode())
            validate_backup(destination)
        except BaseException:
            shutil.rmtree(destination)
            raise

    def backup(self, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise AdminError("Backup destination already exists; choose a new directory")
        with self.quiesce(resume=True):
            self.snapshot(destination)

    def restore(self, source: Path, *, replace_existing: bool = False) -> Path:
        validate_backup(source)
        if not replace_existing:
            raise AdminError("Restore replaces all Merlin data; pass --replace-existing explicitly")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        rollback = source.parent / f"before-restore-{stamp}"
        with self.quiesce(resume=False):
            # Preserve the entire current state before any destructive operation.
            self.snapshot(rollback)
            with (source / "state.tar").open("rb") as stream:
                self.run(
                    "run",
                    "--rm",
                    "--no-deps",
                    "-T",
                    "--user",
                    "0",
                    "--entrypoint",
                    "python",
                    "api",
                    "-c",
                    RESTORE_STATE,
                    stdin=stream,
                )
            self.run(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "merlin",
                "-d",
                "merlin",
                "--set",
                "ON_ERROR_STOP=1",
                "-c",
                "DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION merlin;",
            )
            with (source / "database.dump").open("rb") as stream:
                self.run(
                    "exec",
                    "-T",
                    "postgres",
                    "pg_restore",
                    "-U",
                    "merlin",
                    "-d",
                    "merlin",
                    "--single-transaction",
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                    stdin=stream,
                )
        return rollback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--project-name", help="Compose project override used during deployment")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser(
        "backup", help="Stop writers, snapshot DB and all /data, resume writers"
    )
    backup.add_argument("destination", type=Path, help="New directory under an existing parent")
    restore = commands.add_parser(
        "restore", help="Verify snapshot, save rollback, replace DB and /data"
    )
    restore.add_argument("source", type=Path)
    restore.add_argument("--replace-existing", action="store_true")
    verify = commands.add_parser(
        "verify", help="Check hashes and archive paths without contacting Docker"
    )
    verify.add_argument("source", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            validate_backup(args.source)
            print("Backup validated.")
            return 0
        compose = Compose(args.env_file, args.project_name)
        if args.command == "backup":
            compose.backup(args.destination)
            print(f"Backup complete: {args.destination}")
        else:
            rollback = compose.restore(args.source, replace_existing=args.replace_existing)
            print(f"Restore complete. Rollback backup: {rollback}")
            print(
                "Application services remain stopped. "
                "Apply migrations, then start api, worker, edge."
            )
    except (AdminError, OSError) as error:
        print(f"Maintenance failed: {error}", file=sys.stderr)
        if args.command == "restore":
            print(
                "Keep services stopped; inspect the before-restore backup before recovery.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
