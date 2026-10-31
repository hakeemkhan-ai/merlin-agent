"""A wholly in-memory terminal demonstration. No credentials, network, or subprocesses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from merlin.client import ClientError

STAMP = "2026-10-28T12:00:00+00:00"
ANSWER = """Merlin keeps your work on the service, so closing the terminal only detaches you.

1. The API records your task and progress events.
2. A worker checks out the repository inside an isolated **E2B workspace**.
3. Checkpoints preserve edits; reconnecting replays the recorded progress.

```sh
merlin ui --session SESSION_ID
```

The documentation update is prepared. Open **Review** to inspect the synthetic proposal.
"""
SOURCE = """# Merlin workspace

Tasks keep running after the terminal disconnects.

## Workflow

Submit, inspect progress, review changes, and reconnect.
"""


class DemoClient:
    url = "offline://demo"

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.approval_status = "pending"
        self.closed = False
        self.create(
            "demo-task",
            "Explain how Merlin keeps work running.",
            "demo-session",
            "waiting_for_approval",
        )

    async def close(self) -> None:
        self.closed = True

    def create(
        self, identifier: str, prompt: str, session: str, state: str = "queued"
    ) -> dict[str, Any]:
        self.tasks[identifier] = {
            "id": identifier,
            "session_id": session,
            "repository": "demo/merlin",
            "prompt": prompt,
            "model": "demo-model",
            "state": state,
            "root_id": identifier,
            "parent_id": None,
            "created_at": STAMP,
            "updated_at": STAMP,
            "cancel_requested": False,
            "checkpoint_id": "demo-checkpoint" if state != "queued" else None,
            "budget": {"max_calls": 100, "max_cost_usd": 5, "max_sandbox_seconds": 3600},
            "result": None,
            "error": None,
            "allowed_domains": [],
        }
        return self.tasks[identifier]

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        params = kwargs.get("params") or {}
        if path == "/v1/client-info":
            return {
                "api_version": "1",
                "default_model": "demo-model",
                "features": ["session_views", "approval_previews", "checkpoint_files"],
            }
        if path == "/v1/github/status":
            return {"configured": True, "mode": "offline_demo"}
        if path == "/v1/github/repositories":
            return [{"full_name": "demo/merlin", "default_branch": "main"}]
        if path == "/v1/sessions":
            groups: dict[str, list[dict[str, Any]]] = {}
            for task in self.tasks.values():
                groups.setdefault(task["session_id"], []).append(task)
            return {
                "items": [
                    {
                        "session_id": key,
                        "repository": tasks[0]["repository"],
                        "title": tasks[0]["prompt"],
                        "latest_task_id": tasks[-1]["id"],
                        "state": tasks[-1]["state"],
                        "updated_at": STAMP,
                        "task_count": len(tasks),
                    }
                    for key, tasks in reversed(list(groups.items()))
                ],
                "next_cursor": None,
            }
        if path.startswith("/v1/sessions/"):
            session = path.split("/")[3]
            return {
                "items": list(
                    reversed(
                        [
                            dict(task)
                            for task in self.tasks.values()
                            if task["session_id"] == session
                        ]
                    )
                ),
                "next_cursor": None,
            }
        if path == "/v1/tasks" and method == "POST":
            key = kwargs.get("headers", {}).get("Idempotency-Key", "")
            if key not in self.keys:
                body = kwargs["json"]
                identifier = uuid4().hex
                task = self.create(identifier, body["prompt"], body["session_id"])
                task["repository"] = body.get("repository")
                task["model"] = body.get("model", "demo-model")
                self.keys[key] = identifier
            return dict(self.tasks[self.keys[key]])
        if path.startswith("/v1/tasks/"):
            identifier = path.split("/")[3]
            if identifier not in self.tasks:
                raise ClientError("Task unavailable in the offline demo.", 404)
            task = self.tasks[identifier]
            if path.endswith("/summary"):
                return dict(task)
            if path.endswith("/approval-summaries"):
                return (
                    [
                        {
                            "id": "demo-approval",
                            "task_id": identifier,
                            "status": self.approval_status,
                            "digest": "d" * 64,
                            "name": "github_publish_branch",
                        }
                    ]
                    if identifier == "demo-task"
                    else []
                )
            if path.endswith("/usage"):
                return {
                    "root_id": identifier,
                    "calls": 0,
                    "sandbox_seconds": 0,
                    "cost_usd": 0.0,
                    "reserved_cost_usd": 0.0,
                }
            if path.endswith("/cancel"):
                task.update(state="cancelled", cancel_requested=True)
                return dict(task)
            if path.endswith("/resume"):
                if task["state"] != "paused":
                    raise ClientError("Only paused tasks can be resumed.", 409)
                task["state"] = "queued"
                return dict(task)
            if path.endswith("/workspace/files"):
                names = ["README.md", "src/merlin/worker/__init__.py", "tests/test_worker.py"]
                return {
                    "items": [
                        name
                        for name in names
                        if params.get("query", "").casefold() in name.casefold()
                    ],
                    "checkpoint_id": "demo-checkpoint",
                    "created_at": STAMP,
                    "next_cursor": None,
                }
            if path.endswith("/workspace/file"):
                return {
                    "path": params["path"],
                    "content": SOURCE,
                    "start_line": 1,
                    "total_lines": 7,
                    "next_line": None,
                    "checkpoint_id": "demo-checkpoint",
                    "created_at": STAMP,
                    "binary": False,
                }
        if path == "/v1/approvals/demo-approval/preview":
            return {
                "approval_id": "demo-approval",
                "task_id": "demo-task",
                "repository": "demo/merlin",
                "status": self.approval_status,
                "operation": "github_publish_branch",
                "branch": "merlin/task-demo-task",
                "base_sha": "a" * 40,
                "digest": "d" * 64,
                "message": "docs: explain durable sessions (offline example)",
                "files": ["docs/sessions.md"],
                "path": "docs/sessions.md",
                "preview_kind": "diff",
                "content": "--- /dev/null\n+++ b/docs/sessions.md\n@@ -0,0 +1,3 @@\n"
                "+# Durable sessions\n+\n+Closing the terminal leaves remote work running.\n",
                "total_lines": 6,
                "next_line": None,
                "truncated": False,
            }
        if path.startswith("/v1/approvals/demo-approval") and method == "POST":
            self.approval_status = (
                "changes_requested"
                if path.endswith("/changes")
                else ("approved" if kwargs["json"].get("approved") else "rejected")
            )
            self.tasks["demo-task"].update(state="succeeded", result=ANSWER)
            return {"status": self.approval_status}
        raise ClientError("This action is not part of the offline demonstration.", 404)

    async def events(self, task_id: str, after: int) -> AsyncIterator[dict[str, Any]]:
        task = self.tasks[task_id]
        answer = (
            ANSWER
            if task_id == "demo-task"
            else "This is an offline demonstration. No model, sandbox, or GitHub operation "
            "was executed.\n\nTry `/files`, `/usage`, `/sessions`, or `/help`."
        )
        events: list[tuple[str, dict[str, Any]]] = [
            ("task.created", {"state": "queued"}),
            ("task.claimed", {"reclaimed": False}),
            ("checkpoint.saved", {"checkpoint_id": "demo-checkpoint"}),
            (
                "tool.started",
                {
                    "call": {
                        "id": "demo-read",
                        "name": "read_file",
                        "arguments": {"path": "README.md"},
                    }
                },
            ),
            ("tool.finished", {"call_id": "demo-read", "ok": True, "result": {"output": SOURCE}}),
        ]
        if task["repository"] is None:
            events = events[:2]
        for offset in range(0, len(answer), 80):
            events.append(("model.delta", {"text": answer[offset : offset + 80]}))
        events.append(
            ("model.reply", {"text": answer, "cost_usd": 0, "input_tokens": 0, "output_tokens": 0})
        )
        if task_id == "demo-task" and self.approval_status == "pending":
            events.append(("approval.requested", {"approval_id": "demo-approval"}))
        else:
            events.append(("task.updated", {"state": "succeeded"}))
        for sequence, (kind, data) in enumerate(events, 1):
            if sequence <= after:
                continue
            await asyncio.sleep(0.025)
            if task["state"] == "cancelled":
                return
            yield {
                "task_id": task_id,
                "sequence": sequence,
                "kind": kind,
                "data": data,
                "created_at": STAMP,
            }
        if task_id != "demo-task" and task["state"] != "cancelled":
            task.update(state="succeeded", result=answer)
