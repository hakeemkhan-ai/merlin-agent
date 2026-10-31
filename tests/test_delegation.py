import base64
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from merlin.contracts import Checkpoint, TaskSpec, TaskState, ToolCall, ToolResult, Workspace
from merlin.delegation import DelegatingExecutor
from merlin.persistence import SQLTaskStore


class Delegate:
    def definitions(self):
        return []

    async def execute(self, task, workspace, call):
        return ToolResult(call_id=call.id, output="delegated")


class Sandbox:
    def __init__(self):
        self.calls = []

    async def execute(self, workspace, call):
        self.calls.append(call)
        return ToolResult(call_id=call.id, output="Applied")


class DelegationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = SQLTaskStore("sqlite:///:memory:")
        self.store.create_schema()
        self.parent = self.store.create_task(TaskSpec(prompt="fix", repository="owner/repo"))
        self.lease = self.store.claim("parent")
        self.checkpoint = self.snapshot(self.parent.id, {"main.py": b"old", ".git/config": b"git"})
        self.store.save_checkpoint(self.lease, self.checkpoint)
        self.parent = self.store.get_task(self.parent.id)
        self.sandbox = Sandbox()
        self.executor = DelegatingExecutor(Delegate(), self.sandbox, self.store, self.path)
        self.workspace = Workspace(id="parent")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def snapshot(self, task_id, files):
        manifest = {
            "version": 1,
            "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
        }
        values = dict(files)
        values["__MERLIN_MANIFEST__.json"] = json.dumps(manifest).encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, value in values.items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
        data = buffer.getvalue()
        checkpoint = Checkpoint(
            task_id=task_id,
            artifact=f"{hashlib.sha256(data).hexdigest()}.tar",
            sha256=hashlib.sha256(data).hexdigest(),
        )
        (self.path / checkpoint.artifact).write_bytes(data)
        return checkpoint

    async def test_spawn_is_idempotent_scoped_and_least_privileged(self):
        call = ToolCall(name="spawn_child", arguments={"prompt": "fix one function"})
        first = await self.executor.execute(self.parent, self.workspace, call)
        # Recovery may have produced another parent checkpoint before retrying the tool.
        advanced = self.snapshot(self.parent.id, {"main.py": b"parent advanced"})
        self.store.save_checkpoint(self.lease, advanced)
        second = await self.executor.execute(self.parent, self.workspace, call)
        self.assertEqual(first.data, second.data)
        child = self.store.get_task(first.data["child_id"])
        self.assertEqual(child.root_id, self.parent.id)
        self.assertEqual(child.spec.permissions, [])
        self.assertEqual(child.spec.base_checkpoint_id, self.checkpoint.id)
        blocked = await self.executor.execute(child, self.workspace, call)
        self.assertFalse(blocked.ok)

    async def test_merge_contains_only_source_changes_and_stale_hashes(self):
        spawned = await self.executor.execute(
            self.parent, self.workspace, ToolCall(name="spawn_child", arguments={"prompt": "edit"})
        )
        child_id = spawned.data["child_id"]
        child_lease = self.store.claim("child")
        checkpoint = self.snapshot(child_id, {"main.py": b"new", ".git/config": b"other"})
        self.store.save_checkpoint(child_lease, checkpoint)
        self.store.update_task(child_lease, state=TaskState.SUCCEEDED, result="edited")
        result = await self.executor.execute(
            self.parent,
            self.workspace,
            ToolCall(name="integrate_child", arguments={"child_id": child_id}),
        )
        self.assertTrue(result.ok)
        changes = self.sandbox.calls[0].arguments["changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["path"], "main.py")
        self.assertEqual(changes[0]["base_sha256"], hashlib.sha256(b"old").hexdigest())
        self.assertEqual(base64.b64decode(changes[0]["content_base64"]), b"new")

    async def test_stale_parent_cannot_spawn_after_cancel(self):
        self.store.cancel(self.parent.id)
        with self.assertRaises(BaseException) as error:
            await self.executor.execute(
                self.parent,
                self.workspace,
                ToolCall(name="spawn_child", arguments={"prompt": "edit"}),
            )
        self.assertEqual(type(error.exception).__name__, "CancelledError")
