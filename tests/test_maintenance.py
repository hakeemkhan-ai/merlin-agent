import hashlib
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from merlin.config import Settings
from merlin.contracts import Checkpoint, TaskSpec, TaskState, ToolCall, ToolResult, now
from merlin.maintenance import prune_artifacts
from merlin.persistence import SQLTaskStore


class RetentionTests(unittest.TestCase):
    def test_prepared_snapshot_survives_later_edits_and_missing_references(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, artifact_dir=Path(directory))
            store = SQLTaskStore("sqlite:///:memory:")
            store.create_schema()
            self.addCleanup(store.close)
            task = store.create_task(TaskSpec(prompt="edit", repository="owner/repo"))
            lease = store.claim("worker")
            prepared = Checkpoint(task_id=task.id, artifact="prepared.tar", sha256="0" * 64)
            store.save_checkpoint(lease, prepared)
            call = ToolCall(name="github_prepare_publish", arguments={"paths": ["a.py"]})
            store.begin_tool(lease, call)
            store.finish_tool(
                lease,
                ToolResult(
                    call_id=call.id, data={"publish_arguments": {"checkpoint_id": prepared.id}}
                ),
            )
            store.save_checkpoint(
                lease, Checkpoint(task_id=task.id, artifact="latest.tar", sha256="0" * 64)
            )
            invalid = ToolCall(name="github_publish_branch", arguments={"checkpoint_id": "missing"})
            store.begin_tool(lease, invalid)
            store.finish_tool(lease, ToolResult(call_id=invalid.id, ok=False))
            old = (now() - timedelta(days=40)).timestamp()
            for name in ("prepared.tar", "latest.tar", "orphan.tar"):
                path = settings.artifact_dir / name
                path.write_bytes(b"fixture")
                os.utime(path, (old, old))
            assert prune_artifacts(store, settings)["expired_artifacts"] == ["orphan.tar"]

    def test_expired_orphans_are_removed_but_latest_session_and_active_files_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(artifact_dir=Path(directory) / "artifacts")
            settings.artifact_dir.mkdir()
            store = SQLTaskStore("sqlite:///:memory:")
            store.create_schema()
            task = store.create_task(TaskSpec(prompt="edit", repository="owner/repo"))
            lease = store.claim("worker")
            store.save_checkpoint(
                lease,
                Checkpoint(
                    task_id=task.id,
                    artifact="saved.tar",
                    sha256=hashlib.sha256(b"checkpoint").hexdigest(),
                ),
            )
            store.update_task(lease, state=TaskState.SUCCEEDED)
            # An intervening chat turn must not make the coding snapshot eligible
            # for deletion; the next coding request still resumes that snapshot.
            store.create_task(
                TaskSpec(
                    prompt="Good morning", repository="owner/repo", session_id=task.spec.session_id
                )
            )
            conversation_lease = store.claim("worker")
            store.update_task(conversation_lease, state=TaskState.SUCCEEDED, result="Good morning!")
            old = (now() - timedelta(days=40)).timestamp()
            for name in ("saved.tar", "orphan.tar"):
                path = settings.artifact_dir / name
                path.write_bytes(b"checkpoint")
                os.utime(path, (old, old))
            journal = Path(directory) / "github-state" / "operation.json"
            journal.parent.mkdir()
            journal.write_text("preserve duplicate-operation state")
            preview = prune_artifacts(store, settings)
            self.assertEqual(preview["expired_artifacts"], ["orphan.tar"])
            self.assertTrue((settings.artifact_dir / "orphan.tar").exists())
            prune_artifacts(store, settings, apply=True)
            self.assertFalse((settings.artifact_dir / "orphan.tar").exists())
            self.assertTrue((settings.artifact_dir / "saved.tar").exists())
            self.assertTrue(journal.exists())
            store.close()
