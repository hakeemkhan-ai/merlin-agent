from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from pyqwest import ReadError as RustReadError

from merlin.config import Settings
from merlin.contracts import (
    Checkpoint,
    CheckpointUnavailable,
    Task,
    TaskSpec,
    ToolCall,
    Workspace,
    WorkspaceSetupError,
)
from merlin.sandbox import E2BSandboxClient, network_configuration
from merlin.sandbox._remote import MANIFEST, capture, digest, validate_archive


class FakeFiles:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        folder.mkdir()

    def local(self, name: str) -> Path:
        return self.folder / Path(name).name

    async def write(self, name: str, content: bytes | str) -> None:
        self.local(name).write_bytes(content.encode() if isinstance(content, str) else content)

    async def read(self, name: str, format: str = "text") -> bytes | str:
        data = self.local(name).read_bytes()
        return data if format == "bytes" else data.decode()

    async def remove(self, name: str) -> None:
        self.local(name).unlink(missing_ok=True)


class FakeCommands:
    def __init__(self, files: FakeFiles, root: Path) -> None:
        self.files = files
        self.root = root
        self.calls: list[tuple[str, dict]] = []
        self.fail = False
        self.run_state = {"state": "completed", "exit_code": 0, "output": "hello"}

    async def run(self, command: str, **kwargs: object) -> SimpleNamespace:
        self.calls.append((command, dict(kwargs)))
        if self.fail:
            raise TimeoutError("response lost")
        if "git -c credential.helper" in command:
            return SimpleNamespace(stdout="a" * 40, exit_code=0)
        parts = shlex.split(command)
        request = json.loads(self.files.local(parts[-1]).read_text())
        request["root"] = str(self.root)
        operation = request["operation"]
        if operation == "run":
            if kwargs.get("background"):
                return SimpleNamespace(pid=123)
            value = self.run_state
        elif operation == "lsp_reset":
            value = {"reset": False, "output": "No active language-server sessions"}
        elif operation in {"poll_run", "terminate_run"}:
            value = self.run_state
        else:
            if operation == "capture":
                request["arguments"]["target"] = str(
                    self.files.local(request["arguments"]["target"])
                )
            if operation == "restore":
                request["arguments"]["source"] = str(
                    self.files.local(request["arguments"]["source"])
                )
            from merlin.sandbox._remote import operate

            try:
                with patch(
                    "merlin.sandbox._remote.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout="a" * 40),
                ):
                    value = operate(request)
            except Exception as error:
                return SimpleNamespace(
                    stdout=json.dumps({"ok": False, "error": str(error)}), exit_code=0
                )
        return SimpleNamespace(stdout=json.dumps({"ok": True, "data": value}), exit_code=0)


class FakeSDK:
    def __init__(self, folder: Path) -> None:
        self.files = FakeFiles(folder / "remote")
        self.commands = FakeCommands(self.files, folder / "repo")
        self.sandbox_id = "fake-sandbox"
        self.creation: dict = {}
        self.killed = False

    async def create(self, **kwargs: object) -> FakeSDK:
        self.creation = dict(kwargs)
        return self

    async def connect(self, sandbox_id: str, **kwargs: object) -> FakeSDK:
        return self

    async def kill(self) -> None:
        self.killed = True


class SandboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.sdk = FakeSDK(self.folder)
        self.settings = Settings(
            e2b_api_key="test-placeholder", artifact_dir=self.folder / "artifacts"
        )
        self.client = E2BSandboxClient(self.settings, sdk=self.sdk)
        self.task = Task(
            spec=TaskSpec(prompt="test", repository="owner/repo", allowed_domains=["pypi.org"])
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_create_restricts_egress_outside_workspace(self) -> None:
        workspace = await self.client.create(self.task)
        self.assertEqual(workspace.id, self.sdk.sandbox_id)
        self.assertEqual(self.sdk.creation["network"]["allow_out"], ["github.com", "pypi.org"])
        self.assertIn("0.0.0.0/0", self.sdk.creation["network"]["deny_out"])
        self.assertNotIn("envs", self.sdk.creation)

    async def test_tool_transport_failure_is_uncertain_and_not_retried(self) -> None:
        workspace = await self.client.create(self.task)
        count = len(self.sdk.commands.calls)
        self.sdk.commands.fail = True
        result = await self.client.execute(
            workspace, ToolCall(name="bash", arguments={"command": "echo test"})
        )
        self.assertTrue(result.uncertain)
        self.assertFalse(result.ok)
        self.assertEqual(len(self.sdk.commands.calls), count + 1)

    async def test_timeout_outcome_is_uncertain(self) -> None:
        workspace = await self.client.create(self.task)
        self.sdk.commands.run_state = {"state": "timed_out", "exit_code": None}
        result = await self.client.execute(
            workspace, ToolCall(name="bash", arguments={"command": "slow"})
        )
        self.assertTrue(result.uncertain)
        self.assertFalse(result.ok)

    async def test_background_process_survives_adapter_reconnect(self) -> None:
        workspace = await self.client.create(self.task)
        started = await self.client.execute(
            workspace, ToolCall(name="bash", arguments={"command": "server", "background": True})
        )
        self.assertEqual(started.data["pid"], 123)
        second = E2BSandboxClient(self.settings, sdk=self.sdk)
        polled = await second.execute(
            workspace, ToolCall(name="poll_process", arguments={"pid": 123})
        )
        self.assertTrue(polled.ok)
        self.assertEqual(polled.data["state"], "completed")

    async def test_unknown_or_invalid_tool_does_not_dispatch(self) -> None:
        workspace = await self.client.create(self.task)
        count = len(self.sdk.commands.calls)
        result = await self.client.execute(
            workspace, ToolCall(name="bash", arguments={"command": "x", "extra": "bad"})
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.uncertain)
        result = await self.client.execute(workspace, ToolCall(name="unknown"))
        self.assertFalse(result.ok)
        self.assertEqual(len(self.sdk.commands.calls), count)

    async def test_checkpoint_restores_source_and_git_data(self) -> None:
        workspace = await self.client.create(self.task)
        root = self.sdk.commands.root
        (root / "binary").write_bytes(b"\x00\xff")
        (root / ".env").write_text("secret=excluded")
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main")
        checkpoint = await self.client.checkpoint(workspace, self.task.id)
        self.assertTrue((self.settings.artifact_dir / checkpoint.artifact).exists())
        (root / "binary").write_bytes(b"wrong")
        (root / "extra").write_text("remove me")
        await self.client.restore(workspace, checkpoint)
        self.assertEqual((root / "binary").read_bytes(), b"\x00\xff")
        self.assertTrue((root / ".git" / "HEAD").exists())
        self.assertFalse((root / ".env").exists())
        self.assertFalse((root / "extra").exists())

    async def test_checkpoint_and_reads_work_when_e2b_denies_overwriting_helpers(self) -> None:
        original_write = self.sdk.files.write

        async def refuse_helper_overwrite(name, content):
            if Path(name).name.startswith("merlin-helper-") and self.sdk.files.local(name).exists():
                raise PermissionError("E2B denied opening an existing helper file")
            await original_write(name, content)

        with patch.object(self.sdk.files, "write", side_effect=refuse_helper_overwrite):
            workspace = await self.client.create(self.task)
            (self.sdk.commands.root / "README.md").write_text("Fixture repository")
            checkpoint = await self.client.checkpoint(workspace, self.task.id)
            self.assertTrue((self.settings.artifact_dir / checkpoint.artifact).is_file())
            for _ in range(2):
                result = await self.client.execute(
                    workspace, ToolCall(name="read_file", arguments={"path": "README.md"})
                )
                self.assertTrue(result.ok, result.output)
                self.assertIn("Fixture repository", result.output)

    async def test_checkpoint_rejects_unsafe_task_id(self) -> None:
        workspace = await self.client.create(self.task)
        with self.assertRaises(ValueError):
            await self.client.checkpoint(workspace, "../../escape")

    async def test_checkpoint_recovers_from_one_read_failure_using_a_fresh_archive(self) -> None:
        workspace = await self.client.create(self.task)
        original_read = self.sdk.files.read
        targets = []

        async def read(name, **options):
            targets.append(name)
            if len(targets) == 1:
                raise httpx.ReadError("fixture-sensitive-provider-detail")
            return await original_read(name, **options)

        with (
            patch.object(self.sdk.files, "read", side_effect=read),
            patch("merlin.sandbox.asyncio.sleep", new_callable=AsyncMock) as sleep,
            self.assertLogs("merlin.sandbox", level="WARNING") as logs,
        ):
            checkpoint = await self.client.checkpoint(workspace, self.task.id)
        self.assertEqual(len(set(targets)), 2)
        self.assertEqual(sleep.await_count, 1)
        self.assertTrue((self.settings.artifact_dir / checkpoint.artifact).is_file())
        self.assertTrue(all(not self.sdk.files.local(name).exists() for name in targets))
        self.assertNotIn("fixture-sensitive-provider-detail", " ".join(logs.output))

    async def test_checkpoint_transport_retries_are_bounded_for_both_sdk_transports(self) -> None:
        workspace = await self.client.create(self.task)
        for error_type in (httpx.ReadError, RustReadError):
            with self.subTest(transport=error_type.__module__):
                with (
                    patch.object(
                        self.sdk.files, "read", side_effect=error_type("fixture-private-detail")
                    ) as read,
                    patch("merlin.sandbox.asyncio.sleep", new_callable=AsyncMock) as sleep,
                    self.assertLogs("merlin.sandbox", level="WARNING"),
                    self.assertRaises(CheckpointUnavailable) as caught,
                ):
                    await self.client.checkpoint(workspace, self.task.id)
                self.assertEqual(read.await_count, 3)
                self.assertEqual(sleep.await_count, 2)
                self.assertNotIn("fixture-private-detail", str(caught.exception))
                self.assertFalse(list(self.sdk.files.folder.glob("*.tar")))

    async def test_invalid_checkpoint_archive_is_not_retried(self) -> None:
        workspace = await self.client.create(self.task)
        with (
            patch.object(self.sdk.files, "read", return_value=b"not an archive") as read,
            patch("merlin.sandbox.asyncio.sleep", new_callable=AsyncMock) as sleep,
            self.assertRaisesRegex(WorkspaceSetupError, "archive validation failed") as caught,
        ):
            await self.client.checkpoint(workspace, self.task.id)
        self.assertEqual(read.await_count, 1)
        self.assertIsInstance(caught.exception.__cause__, tarfile.ReadError)
        sleep.assert_not_awaited()

    async def test_cancelled_checkpoint_is_not_retried(self) -> None:
        workspace = await self.client.create(self.task)
        with (
            patch.object(self.client, "_helper", side_effect=asyncio.CancelledError) as helper,
            patch("merlin.sandbox.asyncio.sleep", new_callable=AsyncMock) as sleep,
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.client.checkpoint(workspace, self.task.id)
        self.assertEqual(helper.await_count, 1)
        sleep.assert_not_awaited()

    async def test_restore_rejects_tampered_artifact(self) -> None:
        workspace = await self.client.create(self.task)
        checkpoint = await self.client.checkpoint(workspace, self.task.id)
        (self.settings.artifact_dir / checkpoint.artifact).write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "checksum"):
            await self.client.restore(workspace, checkpoint)

    async def test_restore_rejects_artifact_traversal(self) -> None:
        workspace = await self.client.create(self.task)
        with self.assertRaises(ValueError):
            await self.client.restore(
                workspace, Checkpoint(task_id=self.task.id, artifact="../escape.tar", sha256="")
            )

    async def test_bootstrap_keeps_token_out_of_url_and_command(self) -> None:
        workspace = await self.client.create(self.task)
        await self.client.bootstrap(workspace, "owner/repo", "private-read-token")
        command, options = self.sdk.commands.calls[-1]
        self.assertNotIn("private-read-token", command)
        self.assertIn("https://github.com/owner/repo.git", command)
        self.assertEqual(options["envs"]["MERLIN_GIT_READ_TOKEN"], "private-read-token")
        self.assertEqual(workspace.base_sha, "a" * 40)

    async def test_bootstrap_works_when_template_starts_inside_empty_repository(self) -> None:
        # Execute only trusted fixture code with a stub Git executable. No real
        # repository or network is used. A removed cwd makes the stub fail too.
        root = self.folder / "clone-target"
        root.mkdir()
        binary = self.folder / "bin"
        binary.mkdir()
        git = binary / "git"
        git.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "os.getcwd()\n"
            "if 'clone' in sys.argv:\n"
            "    Path(sys.argv[-1]).mkdir()\n"
            "else:\n"
            "    print('a' * 40)\n"
        )
        git.chmod(0o700)
        workspace = Workspace(id=self.sdk.sandbox_id, root=str(root))
        self.client._instances[workspace.id] = self.sdk

        async def run(command, **options):
            result = subprocess.run(
                ["sh", "-c", command],
                cwd=options.get("cwd", root),
                env={
                    **os.environ,
                    **options["envs"],
                    "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
                },
                capture_output=True,
                text=True,
                check=True,
            )
            return SimpleNamespace(stdout=result.stdout, exit_code=0)

        with patch.object(self.sdk.commands, "run", side_effect=run):
            await self.client.bootstrap(workspace, "owner/repo", "fixture-token")
        self.assertTrue(root.is_dir())
        self.assertEqual(workspace.base_sha, "a" * 40)

    async def test_clone_failure_reports_stage_without_provider_output(self) -> None:
        workspace = await self.client.create(self.task)
        error = RuntimeError("stderr contains private-read-token")
        error.exit_code = 128
        with patch.object(self.sdk.commands, "run", side_effect=error):
            with self.assertRaisesRegex(
                WorkspaceSetupError, "Repository clone failed.*128"
            ) as caught:
                await self.client.bootstrap(workspace, "owner/repo", "private-read-token")
        self.assertNotIn("private-read-token", str(caught.exception))

    async def test_initialization_failure_closes_workspace_and_reports_stage(self) -> None:
        self.sdk.commands.fail = True
        with self.assertRaisesRegex(WorkspaceSetupError, "Workspace initialization failed"):
            await self.client.create(self.task)
        self.assertTrue(self.sdk.killed)
        self.assertFalse(self.client._instances)

    async def test_close_kills_sandbox(self) -> None:
        workspace = await self.client.create(self.task)
        await self.client.close(workspace)
        self.assertTrue(self.sdk.killed)


class ArchiveTests(unittest.TestCase):
    @staticmethod
    def archive(
        entries: list[tuple[str, bytes | None]], hashes: dict[str, str] | None = None
    ) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name, value in entries:
                member = tarfile.TarInfo(name)
                if value is None:
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/outside"
                    archive.addfile(member)
                else:
                    member.size = len(value)
                    archive.addfile(member, io.BytesIO(value))
            manifest = json.dumps({"version": 1, "files": hashes or {}}).encode()
            member = tarfile.TarInfo(MANIFEST)
            member.size = len(manifest)
            archive.addfile(member, io.BytesIO(manifest))
        return output.getvalue()

    def test_manifest_checksums_verified(self) -> None:
        with self.assertRaisesRegex(ValueError, "checksum"):
            validate_archive(self.archive([("file", b"actual")], {"file": digest(b"wrong")}))

    def test_traversal_links_duplicates_and_secrets_rejected(self) -> None:
        for entries in [
            [("../escape", b"x")],
            [("/absolute", b"x")],
            [("link", None)],
            [("duplicate", b"x"), ("duplicate", b"x")],
            [(".env", b"secret")],
            [("node_modules/dependency", b"cache")],
        ]:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                validate_archive(self.archive(entries))

    def test_directory_file_conflict_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflict"):
            validate_archive(
                self.archive(
                    [("parent", b"a"), ("parent/child", b"b")],
                    {"parent": digest(b"a"), "parent/child": digest(b"b")},
                )
            )

    def test_snapshot_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            (root / "link").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                capture(root, Path(temporary) / "checkpoint.tar")


class NetworkTests(unittest.TestCase):
    def test_invalid_or_private_destinations_rejected(self) -> None:
        for domain in [
            "*",
            "*.github.com",
            "127.0.0.1",
            "localhost",
            "metadata.internal",
            "a.local",
            "x..com",
            "github.com/path",
        ]:
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                network_configuration([domain])

    def test_normalizes_and_deduplicates_domains(self) -> None:
        self.assertEqual(
            network_configuration(["Example.COM", "example.com."])["allow_out"], ["example.com"]
        )
