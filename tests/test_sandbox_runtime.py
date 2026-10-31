"""Large-repository pagination and background output recovery inside E2B."""

from __future__ import annotations

import json
from pathlib import Path

from merlin.sandbox._remote import MAX_OUTPUT, operate


def test_list_files_paginates_without_exposing_excluded_paths(tmp_path: Path):
    for name in ["a.py", "b.py", "c.py", ".env", "private.key"]:
        (tmp_path / name).write_text("needle")
    first = operate({"root": str(tmp_path), "operation": "list_files", "arguments": {"limit": 2}})
    second = operate(
        {
            "root": str(tmp_path),
            "operation": "list_files",
            "arguments": {"limit": 2, "cursor": first["next_cursor"]},
        }
    )
    assert first["truncated"] and not second["truncated"]
    assert first["output"].splitlines() + second["output"].splitlines() == ["a.py", "b.py", "c.py"]


def test_search_applies_glob_before_the_file_page_limit(tmp_path: Path):
    for index in range(2001):
        (tmp_path / f"a{index:04}.txt").write_text("unrelated")
    (tmp_path / "z.py").write_text("find_this = 1\n")
    result = operate(
        {
            "root": str(tmp_path),
            "operation": "search",
            "arguments": {"query": "find_this", "glob": "*.py", "limit": 1},
        }
    )
    assert "z.py:1:find_this = 1" in result["output"]
    assert result["files_searched"] == 1
    assert result["next_cursor"] is None


def test_search_pages_files_and_reports_output_clipping_separately(tmp_path: Path):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("needle\n")
    first = operate(
        {"root": str(tmp_path), "operation": "search", "arguments": {"query": "needle", "limit": 2}}
    )
    assert first["next_cursor"] == 2
    assert not first["output_truncated"]
    second = operate(
        {
            "root": str(tmp_path),
            "operation": "search",
            "arguments": {"query": "needle", "limit": 2, "cursor": 2},
        }
    )
    assert "c.py:1:needle" in second["output"]
    assert second["next_cursor"] is None


def test_listing_long_paths_never_advances_past_clipped_names(tmp_path: Path):
    directory = tmp_path / ("d" * 160) / ("e" * 160)
    directory.mkdir(parents=True)
    expected = []
    for index in range(220):
        path = directory / (f"{index:04}-" + "f" * 160)
        path.write_text("")
        expected.append(path.relative_to(tmp_path).as_posix())
    cursor, actual = 0, []
    while True:
        page = operate(
            {"root": str(tmp_path), "operation": "list_files", "arguments": {"cursor": cursor}}
        )
        assert len(json.dumps(page["output"])) < MAX_OUTPUT
        actual.extend(page["output"].splitlines())
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    assert actual == expected


def test_process_log_offsets_recover_output_after_the_first_page(tmp_path: Path):
    from uuid import uuid4

    run_id = uuid4().hex
    folder = Path("/tmp/merlin-runs") / run_id
    folder.mkdir(parents=True)
    try:
        (folder / "state.json").write_text(json.dumps({"state": "completed", "exit_code": 0}))
        content = "a" * MAX_OUTPUT + "final verification passed\n"
        (folder / "stdout").write_text(content)
        (folder / "stderr").write_text("")
        request = {"root": str(tmp_path), "operation": "poll_run", "arguments": {"run_id": run_id}}
        first = operate(request)
        assert 0 < first["stdout_next_offset"] < MAX_OUTPUT
        request["arguments"]["stdout_offset"] = first["stdout_next_offset"]
        recovered = first["stdout"]
        while request["arguments"]["stdout_offset"] < len(content):
            second = operate(request)
            recovered += second["stdout"]
            request["arguments"]["stdout_offset"] = second["stdout_next_offset"]
        assert recovered == content
        request["arguments"] = {"run_id": run_id, "tail": True}
        assert operate(request)["stdout"].endswith("final verification passed\n")
    finally:
        import shutil

        shutil.rmtree(folder)
