"""SSE framing, credential safety, and bounded, replay-safe presentation state."""

import json

import httpx
import pytest

from merlin.client import ClientError, MerlinClient, sse_events, terminal_text, validate_connection
from merlin.tui.state import TaskFeed


def feed():
    return TaskFeed({"id": "task", "state": "queued", "prompt": "fix", "created_at": "2026-10-28"})


def event(sequence, kind, data):
    return {"task_id": "task", "sequence": sequence, "kind": kind, "data": data}


def test_feed_replays_once_and_final_snapshot_does_not_repeat_answer():
    state = feed()
    delta = event(1, "model.delta", {"text": "Hello "})
    assert state.apply(delta)
    assert not state.apply(delta)
    state.apply(event(2, "model.delta", {"text": "world"}))
    state.apply(
        event(3, "model.reply", {"text": "Hello world", "input_tokens": 20, "output_tokens": 2})
    )
    state.snapshot({"state": "succeeded", "result": "Hello world"})
    assert [entry.text for entry in state.entries] == ["Hello world"]
    assert state.input_tokens == 20 and state.output_tokens == 2
    assert state.cursor == 3


def test_recovered_generation_keeps_interrupted_reply_separate():
    state = feed()
    state.apply(event(1, "model.delta", {"text": "An unfinished "}))
    state.apply(event(2, "task.claimed", {"reclaimed": True}))
    state.apply(event(3, "model.delta", {"text": "Recovered answer"}))
    state.apply(event(4, "model.reply", {"text": "Recovered answer"}))
    assert [entry.text for entry in state.entries] == ["An unfinished ", "Recovered answer"]
    assert state.entries[0].status == "interrupted"
    for stopped in ("failed", "cancelled", "paused"):
        interrupted = feed()
        interrupted.apply(event(1, "model.delta", {"text": "Unfinished response"}))
        interrupted.snapshot({"state": stopped, "result": None})
        assert interrupted.partial is None
        assert interrupted.entries[0].status == "interrupted"


def test_loading_completed_activity_replaces_summary_fallback_and_bounds_10000_events():
    state = feed()
    state.snapshot({"state": "succeeded", "result": "Finished"})
    state.apply(event(1, "task.created", {}))
    state.apply(event(2, "model.reply", {"text": "Finished"}))
    assert [entry.text for entry in state.entries] == ["Finished"]
    for sequence in range(3, 10003):
        call_id = f"call-{sequence}"
        state.apply(
            event(
                sequence,
                "tool.started",
                {"call": {"id": call_id, "name": "read_file", "arguments": {"path": "README.md"}}},
            )
        )
    assert state.cursor == 10002
    assert len(state.entries) == len(state.tools) == 200


def test_controls_and_direction_overrides_cannot_escape_rendering():
    assert terminal_text("\x1b]52;c;secret\x07\r\u202eevil") == "]52;c;secretevil"
    assert terminal_text("తెలుగు हिन्दी ✓\n") == "తెలుగు हिन्दी ✓\n"
    for url in (
        "http://remote.example",
        "https://user:secret@example.com",
        "https://example.com?token=secret",
        "file:///tmp/server",
    ):
        with pytest.raises(ValueError):
            validate_connection(url)
    assert validate_connection("http://localhost:8000/") == "http://localhost:8000"


def test_operator_resolution_clears_uncertain_tool_without_marking_success():
    state = feed()
    state.apply(event(1, "tool.started", {"call": {"id": "call", "name": "bash"}}))
    state.apply(
        event(
            2,
            "tool.finished",
            {"call_id": "call", "ok": False, "uncertain": True, "result": {"output": "Lost reply"}},
        )
    )
    assert state.tools["call"].status == "uncertain"
    resolution = event(
        3, "tool.operator_resolved", {"call_id": "call", "note": "Inspected external effects"}
    )
    assert state.apply(resolution)
    assert state.tools["call"].status == "resolved"
    assert "Inspected external effects" in state.tools["call"].text
    assert not state.apply(resolution)


def test_ui_preferences_validate_types_and_never_store_tokens_or_drafts(tmp_path, monkeypatch):
    from merlin.tui.preferences import load_preferences, save_preferences

    path = tmp_path / "ui.json"
    monkeypatch.setenv("MERLIN_UI_CONFIG", str(path))
    save_preferences(
        {
            "theme": "merlin",
            "models": ["model-id"],
            "owner_token": "secret",
            "draft": "private prompt",
        }
    )
    assert "secret" not in path.read_text() and "private prompt" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text('{"theme": {}, "models": "bad", "repository": false}')
    assert load_preferences() == {}


@pytest.mark.asyncio
async def test_sse_uses_complete_frames_not_id_lines_or_incomplete_data():
    body = (
        ': keepalive\nid: 3\ndata: {"sequence": 3,\n'
        'data: "kind": "model.delta", "data": {"text": "hello"}}\n\n'
        'id: 4\ndata: {"sequence": 4}'
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    ) as http:
        async with http.stream("GET", "http://test") as response:
            records = [record async for record in sse_events(response)]
    assert [record["sequence"] for record in records] == [3]


@pytest.mark.asyncio
async def test_transport_never_retries_post_or_echoes_arbitrary_error_bodies():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(409, json={"detail": "PRIVATE-CREDENTIAL-in-upstream-error"})

    client = MerlinClient(
        "http://localhost:8000", "owner-secret", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ClientError) as failure:
            await client.request(
                "POST",
                "/v1/tasks",
                json={"prompt": "test"},
                headers={"Idempotency-Key": "stable-key"},
            )
        assert failure.value.status == 409
        assert "PRIVATE" not in str(failure.value)
        assert "owner-secret" not in str(failure.value)
        assert len(requests) == 1
        assert requests[0].headers["idempotency-key"] == "stable-key"
        assert json.loads(requests[0].content) == {"prompt": "test"}
    finally:
        await client.close()
