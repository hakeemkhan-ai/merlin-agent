"""Real SQLite terminal projections and immutable previews, without provider credentials."""

import hashlib
import io
import json
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from merlin.api import create_app
from merlin.config import Settings
from merlin.contracts import Checkpoint, TaskSpec
from merlin.persistence import SQLTaskStore
from merlin.presentation import checkpoint_file, text_page
from tests.test_api import AUTH, TOKEN, ASGIClient, direct_io


def archive(files):
    contents = dict(files)
    contents["__MERLIN_MANIFEST__.json"] = json.dumps(
        {
            "version": 1,
            "base_sha": "a" * 40,
            "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
        }
    ).encode()
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w") as output:
        for name, value in contents.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            output.addfile(member, io.BytesIO(value))
    return target.getvalue()


class ReadGitHub:
    async def read_file_at_commit(self, repository, path, sha):
        assert repository == "owner/repo" and sha == "a" * 40
        return b"old\n" if path != "new.md" else None


class TerminalViewTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = SQLTaskStore("sqlite:///:memory:")
        self.store.create_schema()
        self.addCleanup(self.store.close)
        self.client = ASGIClient(
            create_app(
                Settings(owner_token=TOKEN, artifact_dir=self.root),
                self.store,
                github=ReadGitHub(),
                run_sync=direct_io,
            )
        )

    def create(self, session="session", repository="owner/repo"):
        return self.store.create_task(
            TaskSpec(prompt="First prompt", repository=repository, session_id=session)
        )

    def snapshot(self, task):
        lease = self.store.claim("worker")
        data = archive({"README.md": b"hello\nworld\n", ".git/config": b"not source"})
        (self.root / "snapshot.tar").write_bytes(data)
        checkpoint = Checkpoint(
            task_id=task.id, artifact="snapshot.tar", sha256=hashlib.sha256(data).hexdigest()
        )
        self.store.save_checkpoint(lease, checkpoint)
        return lease, checkpoint

    def test_new_routes_require_owner_and_info_excludes_secrets(self):
        for path in (
            "/v1/client-info",
            "/v1/sessions",
            "/v1/sessions/x/tasks",
            "/v1/tasks/x/summary",
            "/v1/tasks/x/workspace/files",
            "/v1/tasks/x/workspace/file?path=a&checkpoint_id=b",
            "/v1/approvals/x/preview",
        ):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        response = self.client.get("/v1/client-info", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertIn("session_views", response.json()["features"])
        self.assertNotIn(TOKEN, response.text)

    def test_session_pages_exclude_native_history_and_support_repository_filter(self):
        first = self.create()
        lease = self.store.claim("worker")
        self.store.update_task(lease, history=[{"private": "large-native-payload"}])
        self.create("another", "other/repo")
        response = self.client.get("/v1/sessions?limit=1", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["items"]), 1)
        self.assertEqual(response.json()["next_cursor"], 1)
        filtered = self.client.get("/v1/sessions?repository=owner/repo", headers=AUTH).json()
        self.assertEqual(filtered["items"][0]["session_id"], "session")
        page = self.client.get("/v1/sessions/session/tasks", headers=AUTH)
        self.assertEqual(page.json()["items"][0]["id"], first.id)
        self.assertNotIn("large-native-payload", page.text)
        self.assertNotIn("history", page.text)
        self.assertEqual(self.client.get("/v1/sessions?limit=101", headers=AUTH).status_code, 422)

    def test_checkpoint_previews_are_numbered_and_bound_to_task_and_hash(self):
        task = self.create()
        _, checkpoint = self.snapshot(task)
        page = self.client.get(f"/v1/tasks/{task.id}/workspace/files", headers=AUTH).json()
        self.assertEqual(page["items"], ["README.md"])
        self.assertEqual(page["checkpoint_id"], checkpoint.id)
        route = f"/v1/tasks/{task.id}/workspace/file"
        preview = self.client.get(
            route,
            headers=AUTH,
            params={"checkpoint_id": checkpoint.id, "path": "README.md", "start_line": 2},
        )
        self.assertEqual(preview.json()["content"], "world")
        self.assertEqual(preview.json()["start_line"], 2)
        for path in ("../README.md", ".git/config", "/etc/passwd", ".env"):
            self.assertEqual(
                self.client.get(
                    route, headers=AUTH, params={"checkpoint_id": checkpoint.id, "path": path}
                ).status_code,
                404,
            )
        other = self.create("other")
        self.assertEqual(
            self.client.get(
                f"/v1/tasks/{other.id}/workspace/files?checkpoint_id={checkpoint.id}", headers=AUTH
            ).status_code,
            404,
        )
        (self.root / "snapshot.tar").write_bytes(b"changed")
        self.assertEqual(
            self.client.get(f"/v1/tasks/{task.id}/workspace/files", headers=AUTH).status_code, 409
        )

    def test_checkpoint_rejects_missing_snapshot_and_external_artifact(self):
        task = self.create()
        self.assertEqual(
            self.client.get(f"/v1/tasks/{task.id}/workspace/files", headers=AUTH).status_code, 409
        )
        lease = self.store.claim("worker")
        self.store.save_checkpoint(
            lease, Checkpoint(task_id=task.id, artifact="../outside", sha256="b" * 64)
        )
        self.assertEqual(
            self.client.get(f"/v1/tasks/{task.id}/workspace/files", headers=AUTH).status_code, 409
        )

    def test_approval_preview_uses_immutable_base_and_decision_stays_explicit(self):
        from merlin.contracts import ToolCall

        task = self.create()
        lease, _ = self.snapshot(task)
        approval = self.store.request_approval(
            lease,
            ToolCall(
                name="github_publish_branch",
                arguments={
                    "base_sha": "a" * 40,
                    "message": "docs: edit",
                    "files": [
                        {"path": "README.md", "content": "new\n"},
                        {"path": "new.md", "content": "brand new\n"},
                    ],
                },
            ),
        )
        route = f"/v1/approvals/{approval.id}/preview"
        response = self.client.get(route, headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["digest"], approval.digest)
        self.assertEqual(response.json()["preview_kind"], "diff")
        self.assertIn("-old", response.json()["content"])
        self.assertIn("+new", response.json()["content"])
        self.assertNotIn("brand new", response.json()["content"])
        self.assertEqual(self.store.get_approval(approval.id).status, "pending")
        created = self.client.get(route, headers=AUTH, params={"path": "new.md"}).json()
        self.assertIn("/dev/null", created["content"])
        self.assertEqual(
            self.client.get(route, headers=AUTH, params={"path": "other.md"}).status_code, 404
        )

    def test_text_previews_bound_large_lines_and_binary_files(self):
        text = text_page("x" * 100000)
        self.assertLessEqual(len(text["content"].encode()), 65536)
        self.assertTrue(text["long_line_clipped"])
        self.assertTrue(text["truncated"])
        self.assertIsNone(text["next_line"])
        data = archive({"binary.dat": b"\x00\xff"})
        self.assertTrue(checkpoint_file(data, "binary.dat")["binary"])
