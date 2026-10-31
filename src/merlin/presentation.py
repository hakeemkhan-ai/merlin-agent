"""Small public read models and bounded checkpoint previews. No repository execution."""

from __future__ import annotations

import io
import tarfile
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from pydantic import Field

from merlin.contracts import Budget, Event, Record, TaskState
from merlin.sandbox._remote import MANIFEST, excluded, relative_name, validate_archive


class TaskSummary(Record):
    id: str
    session_id: str
    repository: str | None
    prompt: str
    model: str
    state: TaskState
    parent_id: str | None = None
    root_id: str = ""
    generation: int = 0
    checkpoint_id: str | None = None
    result: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    created_at: datetime
    updated_at: datetime
    budget: Budget = Field(default_factory=Budget)
    allowed_domains: list[str] = Field(default_factory=list)


class SessionSummary(Record):
    session_id: str
    repository: str | None
    title: str
    latest_task_id: str
    state: TaskState
    updated_at: datetime
    task_count: int


class TaskPage(Record):
    items: list[TaskSummary]
    next_cursor: int | None = None
    next_key: str | None = None


class SessionPage(Record):
    items: list[SessionSummary]
    next_cursor: int | None = None


def source_path(path: str) -> str:
    value = relative_name(path)
    if (
        value != path
        or value == MANIFEST
        or ".git" in PurePosixPath(value).parts
        or excluded(value)
    ):
        raise ValueError("Path is not an available source file")
    return value


def text_page(text: str, start_line: int = 1) -> dict[str, Any]:
    lines = text.splitlines()
    selected: list[str] = []
    size = 0
    clipped_line = False
    index = start_line - 1
    while index < len(lines) and len(selected) < 200:
        # Include a bounded slice of an unusually long line without returning
        # an empty page that could trap the viewer in an endless next-page loop.
        line = lines[index]
        allowance = max(0, 65536 - size - (1 if selected else 0))
        clipped = line.encode("utf-8")[:allowance].decode("utf-8", errors="ignore")
        clipped_line = clipped_line or clipped != line
        size += (1 if selected else 0) + len(clipped.encode("utf-8"))
        selected.append(clipped)
        index += 1
        if size >= 65536:
            break
    content = "\n".join(selected)
    complete = start_line == 1 and index >= len(lines) and not clipped_line
    return {
        "content": content,
        "start_line": start_line,
        "total_lines": len(lines),
        "next_line": index + 1 if index < len(lines) else None,
        "truncated": not complete,
        "long_line_clipped": clipped_line,
    }


def checkpoint_files(
    data: bytes, query: str = "", cursor: int = 0, *, manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    manifest = manifest if manifest is not None else validate_archive(data)
    names = []
    for path in sorted(manifest["files"]):
        try:
            source_path(path)
        except ValueError:
            continue
        if query.casefold() in path.casefold():
            names.append(path)
    return {
        "items": names[cursor : cursor + 100],
        "next_cursor": cursor + 100 if cursor + 100 < len(names) else None,
        "total": len(names),
    }


def checkpoint_file(
    data: bytes, path: str, start_line: int = 1, *, manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    path = source_path(path)
    manifest = manifest if manifest is not None else validate_archive(data)
    if path not in manifest["files"]:
        raise KeyError("File not found")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        source = archive.extractfile(path)
        if source is None:
            raise KeyError("File not found")
        raw = source.read()
    try:
        text = raw.decode("utf-8")
        if "\x00" in text:
            raise UnicodeError
    except UnicodeError:
        return {"path": path, "binary": True, "size": len(raw), "sha256": manifest["files"][path]}
    return {
        "path": path,
        "binary": False,
        "sha256": manifest["files"][path],
        **text_page(text, start_line),
    }


def compact_event(event: Event) -> Event:
    """A presentation-only projection. Durable replay and JSON export stay intact."""
    data = dict(event.data)
    if event.kind == "tool.started":
        call = dict(data.get("call", {}))
        arguments = call.get("arguments", {})
        call["arguments"] = {
            key: value[:4000] if isinstance(value, str) else value
            for key, value in arguments.items()
            if key
            in {
                "path",
                "cwd",
                "command",
                "query",
                "prompt",
                "pid",
                "child_id",
                "url",
                "start_line",
                "end_line",
            }
        }
        data["call"] = call
    elif event.kind == "tool.finished":
        result = dict(data.get("result", {}))
        output = str(result.get("output", ""))
        result["output"] = output[:4000]
        result["data"] = {"truncated": len(output) > 4000 or bool(result.get("data"))}
        data["result"] = result
    return event.model_copy(update={"data": data})
