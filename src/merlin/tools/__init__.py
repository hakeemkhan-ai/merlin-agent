"""Validated sandbox tool declarations exposed to the model."""

from typing import Any


def sandbox_tool_definitions() -> list[dict[str, Any]]:
    def tool(
        name: str, description: str, properties: dict[str, Any], required: list[str]
    ) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }

    string = {"type": "string"}
    path = {
        "type": "string",
        "description": "Workspace-relative path; no parent traversal or symlinks.",
    }
    checksum = {
        "type": "string",
        "description": "SHA256 returned by read_file; rejects stale content.",
    }
    return [
        tool(
            "bash",
            "Run Bash inside E2B. Timeouts may leave an uncertain outcome; do not blindly retry.",
            {
                "command": string,
                "cwd": path,
                "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
                "background": {"type": "boolean"},
            },
            ["command"],
        ),
        tool(
            "poll_process",
            "Inspect a background process; use returned byte offsets for the next log page.",
            {
                "pid": {"type": "integer", "minimum": 1},
                "stdout_offset": {"type": "integer", "minimum": 0},
                "stderr_offset": {"type": "integer", "minimum": 0},
                "tail": {"type": "boolean"},
            },
            ["pid"],
        ),
        tool(
            "terminate_process",
            "Terminate a background process in this task workspace.",
            {"pid": {"type": "integer", "minimum": 1}},
            ["pid"],
        ),
        tool(
            "read_file",
            "Read a UTF-8 file with line numbers and its current SHA256.",
            {
                "path": path,
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            ["path"],
        ),
        tool(
            "write_file",
            "Create or atomically replace a UTF-8 file, optionally requiring a matching checksum.",
            {
                "path": path,
                "content": string,
                "expected_sha256": checksum,
                "create_only": {"type": "boolean"},
            },
            ["path", "content"],
        ),
        tool(
            "replace_text",
            "Replace exact text only when its occurrence count and optional checksum match.",
            {
                "path": path,
                "old": string,
                "new": string,
                "expected_count": {"type": "integer", "minimum": 1},
                "expected_sha256": checksum,
            },
            ["path", "old", "new"],
        ),
        tool(
            "search",
            "Search source files with ripgrep; returns bounded matching lines.",
            {
                "query": string,
                "path": path,
                "glob": string,
                "cursor": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            ["query"],
        ),
        tool(
            "list_files",
            "List a page of source paths; use next_cursor for more. Excludes secrets and caches.",
            {
                "path": path,
                "cursor": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            [],
        ),
    ]
