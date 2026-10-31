"""Exercise maintenance ordering and backup validation without a Docker daemon."""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import BinaryIO

from scripts.merlin_admin import (
    RESTORE_STATE,
    SNAPSHOT_STATE,
    AdminError,
    Compose,
    digest,
    main,
    validate_backup,
)


def state_archive(*, unsafe: str | None = None, symlink: bool = False) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        root = tarfile.TarInfo("data")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, content in (
            ("data/artifacts/checkpoints/task/snapshot.tar", b"checkpoint"),
            ("data/github-state/operations/operation.json", b'{"status":"requested"}'),
        ):
            member = tarfile.TarInfo(unsafe or name)
            member.size = len(content)
            if symlink:
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                member.size = 0
            archive.addfile(member, None if symlink else io.BytesIO(content))
            if unsafe or symlink:
                break
    return stream.getvalue()


class FakeCompose(Compose):
    def __init__(self) -> None:
        super().__init__(Path(".env.example"))
        self.calls: list[tuple[str, ...]] = []
        self.fail_dump = False
        self.fail_restore = False
        self.running = b"postgres\napi\nworker\nedge\n"

    def run(
        self, *arguments: str, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None
    ) -> bytes:
        self.calls.append(arguments)
        if arguments[0] == "ps":
            return self.running
        if "pg_dump" in arguments:
            if self.fail_dump:
                raise AdminError("simulated dump failure")
            assert stdout is not None
            stdout.write(b"PGDMPtest-database")
        if SNAPSHOT_STATE in arguments:
            assert stdout is not None
            stdout.write(state_archive())
        if RESTORE_STATE in arguments:
            assert stdin is not None
            if self.fail_restore:
                raise AdminError("simulated restore failure")
            self.restored_state = stdin.read()
        return b""


class OperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.compose = FakeCompose()

    def snapshot(self) -> Path:
        target = self.root / "backup"
        self.compose.backup(target)
        self.compose.calls.clear()
        return target

    @staticmethod
    def update_manifest(backup: Path) -> None:
        path = backup / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["files"]["state.tar"] = {
            "bytes": (backup / "state.tar").stat().st_size,
            "sha256": digest(backup / "state.tar"),
        }
        path.write_text(json.dumps(manifest))

    def test_backup_stops_writers_captures_entire_state_and_restarts_prior_services(self) -> None:
        target = self.root / "backup"
        self.compose.backup(target)
        validate_backup(target)
        self.assertEqual(
            self.compose.calls[1], ("stop", "--timeout", "90", "edge", "api", "worker")
        )
        self.assertEqual(self.compose.calls[-1], ("start", "worker", "api", "edge"))
        with tarfile.open(target / "state.tar") as archive:
            self.assertIn("data/github-state/operations/operation.json", archive.getnames())
            self.assertIn("data/artifacts/checkpoints/task/snapshot.tar", archive.getnames())
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertEqual((target / "database.dump").stat().st_mode & 0o777, 0o600)

    def test_backup_failure_removes_partial_snapshot_and_resumes_writers(self) -> None:
        self.compose.fail_dump = True
        target = self.root / "backup"
        with self.assertRaises(AdminError):
            self.compose.backup(target)
        self.assertFalse(target.exists())
        self.assertEqual(self.compose.calls[-1], ("start", "worker", "api", "edge"))

    def test_backup_does_not_start_previously_stopped_services(self) -> None:
        self.compose.running = b"postgres\napi\n"
        self.compose.backup(self.root / "backup")
        self.assertEqual(self.compose.calls[-1], ("start", "api"))

    def test_maintenance_refuses_active_migration(self) -> None:
        self.compose.running = b"postgres\nmigrate\n"
        with self.assertRaises(AdminError):
            self.compose.backup(self.root / "backup")
        self.assertEqual(len(self.compose.calls), 1)

    def test_existing_backup_is_never_overwritten(self) -> None:
        backup = self.snapshot()
        with self.assertRaises(AdminError):
            self.compose.backup(backup)
        self.assertEqual(self.compose.calls, [])

    def test_restore_requires_explicit_replacement_before_contacting_docker(self) -> None:
        with self.assertRaises(AdminError):
            self.compose.restore(self.snapshot())
        self.assertEqual(self.compose.calls, [])

    def test_tampered_backup_is_rejected_before_services_change(self) -> None:
        backup = self.snapshot()
        (backup / "database.dump").write_bytes(b"PGDMPchanged")
        with self.assertRaises(AdminError):
            self.compose.restore(backup, replace_existing=True)
        self.assertEqual(self.compose.calls, [])

    def test_traversal_and_links_are_rejected_even_with_matching_checksums(self) -> None:
        backup = self.snapshot()
        for options in ({"unsafe": "data/../../etc/passwd"}, {"symlink": True}):
            with self.subTest(options=options):
                (backup / "state.tar").write_bytes(state_archive(**options))
                self.update_manifest(backup)
                with self.assertRaises(AdminError):
                    self.compose.restore(backup, replace_existing=True)
                self.assertEqual(self.compose.calls, [])

    def test_restore_preserves_rollback_before_replacement_and_leaves_services_stopped(
        self,
    ) -> None:
        backup = self.snapshot()
        rollback = self.compose.restore(backup, replace_existing=True)
        validate_backup(rollback)
        snapshot_index = next(
            index for index, call in enumerate(self.compose.calls) if SNAPSHOT_STATE in call
        )
        restore_index = next(
            index for index, call in enumerate(self.compose.calls) if RESTORE_STATE in call
        )
        self.assertLess(snapshot_index, restore_index)
        self.assertEqual(self.compose.restored_state, (backup / "state.tar").read_bytes())
        self.assertFalse(any(call[0] == "start" for call in self.compose.calls))
        self.assertTrue(any("--single-transaction" in call for call in self.compose.calls))

    def test_restore_failure_retains_rollback_and_does_not_restart(self) -> None:
        backup = self.snapshot()
        self.compose.fail_restore = True
        with self.assertRaises(AdminError):
            self.compose.restore(backup, replace_existing=True)
        rollback = next(self.root.glob("before-restore-*"))
        validate_backup(rollback)
        self.assertFalse(any(call[0] == "start" for call in self.compose.calls))

    def test_verify_needs_no_docker_or_environment_file(self) -> None:
        self.assertEqual(main(["--env-file", "/nonexistent", "verify", str(self.snapshot())]), 0)


if __name__ == "__main__":
    unittest.main()
