"""Bounded event projection, independent of widgets and network transports."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Entry:
    key: str
    kind: str
    text: str
    title: str = ""
    status: str = ""
    started_at: str = ""
    finished_at: str = ""


@dataclass
class TaskFeed:
    task: dict[str, Any]
    cursor: int = 0
    entries: deque[Entry] = field(default_factory=deque)
    partial: Entry | None = None
    tools: dict[str, Entry] = field(default_factory=dict)
    reply_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0
    progress: str = "Queued"
    revision: int = 0
    fallback: Entry | None = None

    def add(self, entry: Entry) -> Entry:
        self.entries.append(entry)
        while len(self.entries) > 200:
            old = self.entries.popleft()
            self.tools.pop(old.key, None)
        return entry

    def apply(self, event: dict[str, Any]) -> bool:
        sequence = event.get("sequence", 0)
        if sequence <= self.cursor:
            return False
        if event.get("task_id", self.task["id"]) != self.task["id"]:
            return False
        if self.fallback is not None:
            self.entries.remove(self.fallback)
            self.fallback = None
        self.cursor = sequence
        kind, data = event.get("kind", ""), event.get("data", {})
        timestamp = event.get("created_at", "")
        entry: Entry | None
        if kind == "model.delta":
            if self.partial is None:
                self.partial = self.add(
                    Entry(f"reply-{sequence}", "assistant", "", status="streaming")
                )
            self.partial.text += str(data.get("text", ""))
            self.partial.text = self.partial.text[-128000:]
            self.progress = "Responding"
        elif kind == "model.reply":
            text = str(data.get("text", ""))
            if self.partial is not None:
                self.partial.text, self.partial.status = text, "complete"
                self.partial = None
            elif text:
                self.add(Entry(f"reply-{sequence}", "assistant", text, status="complete"))
            self.reply_count += 1
            self.input_tokens += int(data.get("input_tokens", 0))
            self.output_tokens += int(data.get("output_tokens", 0))
            self.cost += float(data.get("cost_usd", 0))
        elif kind == "task.claimed":
            self.task["state"] = "running"
            if self.partial is not None:
                self.partial.status = "interrupted"
                self.partial = None
            self.progress = "Recovering task" if data.get("reclaimed") else "Thinking"
        elif kind == "checkpoint.saved":
            self.task["checkpoint_id"] = data.get("checkpoint_id")
            self.progress = "Workspace checkpoint saved"
        elif kind == "tool.started":
            call = data.get("call", {})
            arguments = call.get("arguments", {})
            name = call.get("name", "tool")
            detail = next(
                (
                    str(arguments[key])[:4000]
                    for key in ("path", "command", "query", "prompt")
                    if key in arguments
                ),
                "",
            )
            key = call.get("id", f"tool-{sequence}")
            entry = self.add(
                Entry(key, "tool", detail, f"{name}  {detail[:100]}", "running", timestamp)
            )
            self.tools[key] = entry
            self.progress = f"Using {name}"
        elif kind == "tool.finished":
            result = data.get("result", {})
            key = data.get("call_id", "")
            entry = self.tools.get(key)
            if entry is None:
                entry = self.add(Entry(key or f"tool-{sequence}", "tool", "", "Tool result"))
                self.tools[key] = entry
            entry.text = str(result.get("output", ""))[:64000]
            entry.status = (
                "uncertain"
                if data.get("uncertain", result.get("uncertain"))
                else ("passed" if data.get("ok", result.get("ok", True)) else "failed")
            )
            entry.finished_at = timestamp
        elif kind == "approval.requested":
            self.task["state"] = "waiting_for_approval"
            self.progress = "Publishing proposal needs review"
        elif kind == "task.cancel_requested":
            self.task["cancel_requested"] = True
            if data.get("state"):
                self.task["state"] = data["state"]
            self.progress = "Cancellation requested"
        elif kind == "task.updated" and data.get("state"):
            self.task["state"] = data["state"]
        elif kind == "task.completion":
            commands = data.get("commands_after_last_edit", [])
            label = (
                f"Execution evidence: {len(commands)} command result(s) after the last edit."
                if commands
                else "Execution evidence: no verification command recorded."
                if data.get("verification") == "not_recorded"
                else ""
            )
            if label:
                self.add(Entry(f"completion-{sequence}", "notice", label))
        elif kind.startswith("child."):
            self.progress = kind.replace(".", " ").replace("_", " ")
        self.revision += 1
        return True

    def snapshot(self, task: dict[str, Any]) -> None:
        self.task.update(task)
        if task.get("state") in {"failed", "cancelled", "paused"} and self.partial is not None:
            self.partial.status = "interrupted"
            self.partial = None
        result = task.get("result")
        last = next((entry for entry in reversed(self.entries) if entry.kind == "assistant"), None)
        if result and (last is None or last.text != result):
            if self.partial is not None:
                self.partial.text, self.partial.status = result, "complete"
                self.partial = None
            elif self.fallback:
                self.fallback.text = result
            else:
                self.fallback = self.add(Entry("final", "assistant", result, status="complete"))
        self.revision += 1
