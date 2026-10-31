"""Drive real terminal widgets with synthetic service responses and no external services."""

import asyncio
from unittest.mock import patch

import pytest
from textual.widgets import Button, Input, Markdown, OptionList

from merlin.client import ClientError
from merlin.tui.app import Composer, MerlinApp
from merlin.tui.demo import DemoClient
from merlin.tui.screens import ApprovalScreen, InfoScreen, Picker


@pytest.fixture(autouse=True)
async def headless_clock():
    # Markdown uses a real executor. A periodic clock also lets its shutdown
    # completion run on restricted hosts without working socket wakeups. The
    # function-scoped event loop clears these timer handles when it closes.
    loop = asyncio.get_running_loop()

    def tick():
        if not loop.is_closed():
            loop.call_later(0.05, tick)

    tick()
    yield


async def ready(app, pilot):
    for _ in range(50):
        if app.api_ready and app.cards:
            return
        await pilot.pause(0.05)
    raise AssertionError("Terminal app did not become ready")


async def test_submit_conversation_without_selecting_repository():
    client = DemoClient()
    app = MerlinApp(client=client)
    async with app.run_test(size=(110, 36)) as pilot:
        for _ in range(30):
            if app.api_ready:
                break
            await pilot.pause(0.05)
        assert app.api_ready
        composer = app.query_one(Composer)
        composer.load_text("Good Morning !")
        await app.submit(composer.text).wait()
        await pilot.pause(0.6)
        created = next(task for task in client.tasks.values() if task["prompt"] == "Good Morning !")
        assert created["repository"] is None
        assert created["state"] == "succeeded"
        assert composer.text == ""
        assert not isinstance(app.screen, Picker)
        feed = app.feeds[created["id"]]
        assert not feed.tools
        assert app.file_feed() is None
        session = app.session_id
        await app.load_session(session)
        assert app.repository == ""
        assert app.session_id == session


@pytest.mark.parametrize("size", [(80, 24), (110, 30), (160, 48)])
async def test_responsive_layout_streaming_and_keyboard_detach(size):
    client = DemoClient()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=size) as pilot:
        await ready(app, pilot)
        await pilot.pause(0.6)
        composer = app.query_one("#composer", Composer)
        assert composer.region.width > 20 and composer.region.height >= 3
        assert composer.region.bottom <= size[1]
        assert app.query_one("#sidebar").display == (size[0] >= 110)
        assert app.current_feed().cursor > 0
        assert len(app.query(Markdown)) == 1
        assert not any(method == "POST" for method, _ in client.calls)
        await pilot.press("ctrl+q")
    assert client.closed
    assert not any(path.endswith("/cancel") for _, path in client.calls)


async def test_palette_help_themes_and_multiline_paste_do_not_submit():
    client = DemoClient()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(110, 36)) as pilot:
        await ready(app, pilot)
        await pilot.press("ctrl+p")
        assert type(app.screen).__name__ == "CommandPalette"
        await pilot.press("escape")
        app.action_help()
        await pilot.pause()
        assert isinstance(app.screen, InfoScreen)
        await pilot.press("escape")
        for theme in ("textual-light", "merlin-mono", "merlin"):
            app.choose_theme(theme)
            await pilot.pause()
            assert app.theme == theme
        composer = app.query_one(Composer)
        composer.focus()
        composer.load_text("first line\nsecond line")
        await pilot.pause()
        await pilot.press("alt+enter")
        assert "\n" in composer.text
        assert not any(method == "POST" for method, _ in client.calls)
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert not app.query_one("#sidebar").display
        assert app.query_one("#composer-hint").region.height == 1


async def test_submit_queue_followup_and_cancel_selected_task():
    client = DemoClient()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        composer = app.query_one(Composer)
        composer.focus()
        composer.load_text("Add a regression test")
        await pilot.press("enter")
        await pilot.pause(0.2)
        posts = [path for method, path in client.calls if method == "POST"]
        assert posts.count("/v1/tasks") == 1
        task = app.current_feed()
        assert task.task["session_id"] == "demo-session"
        assert composer.text == ""
        app.action_cancel()
        await pilot.pause(0.2)
        assert client.tasks[task.task["id"]]["state"] == "cancelled"
        assert any(path.endswith("/cancel") for _, path in client.calls)


async def test_lost_submit_response_reuses_key_and_duplicate_click_does_not_duplicate_work():
    class LostResponse(DemoClient):
        def __init__(self):
            super().__init__()
            self.submissions = []

        async def request(self, method, path, **kwargs):
            value = await super().request(method, path, **kwargs)
            if method == "POST" and path == "/v1/tasks":
                self.submissions.append(kwargs["headers"]["Idempotency-Key"])
                if len(self.submissions) == 1:
                    raise ClientError("Synthetic lost response")
            return value

    client = LostResponse()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        app.query_one(Composer).load_text("Exactly one task")
        await app.submit("Exactly one task").wait()
        assert app.pending_submission
        await app.submit("Changed message").wait()
        assert len(client.submissions) == 1
        await app.submit("Exactly one task").wait()
        assert len(client.submissions) == 2
        assert client.submissions[0] == client.submissions[1]
        assert len(client.tasks) == 2


async def test_approval_requires_explicit_button_and_rejects_stale_decision():
    client = DemoClient()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        app.open_approval("demo-approval")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ApprovalScreen)
        assert not app.screen.query_one("#proposal-approve", Button).disabled
        assert client.approval_status == "pending"
        await pilot.press("y")
        assert client.approval_status == "pending"
        client.approval_status = "rejected"
        await pilot.click("#proposal-approve")
        await pilot.pause(0.2)
        assert not any(
            method == "POST" and path.startswith("/v1/approvals/") for method, path in client.calls
        )
        assert app.screen.query_one("#proposal-approve", Button).disabled


async def test_approval_controls_and_scrollable_proposal_fit_narrow_terminal():
    app = MerlinApp(demo=True)
    async with app.run_test(size=(80, 24)) as pilot:
        await ready(app, pilot)
        app.open_approval("demo-approval")
        await pilot.pause(0.2)
        for action in ("approve", "reject", "changes", "close"):
            button = app.screen.query_one(f"#proposal-{action}", Button)
            assert button.region.right <= 80 and button.region.bottom <= 24
        assert app.screen.query_one("#proposal-scroll").region.height >= 3


@pytest.mark.parametrize("decision", ["approve", "reject", "changes"])
async def test_approval_decision_and_feedback_actions(decision):
    client = DemoClient()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        app.open_approval("demo-approval")
        await pilot.pause(0.2)
        await pilot.click(f"#proposal-{decision}")
        if decision == "changes":
            await pilot.pause()
            app.screen.query_one(Input).value = "Add the missing test evidence."
            await pilot.click("#submit-note")
        await pilot.pause(0.3)
        expected = {"approve": "approved", "reject": "rejected", "changes": "changes_requested"}[
            decision
        ]
        assert client.approval_status == expected
        assert (
            len(
                [
                    path
                    for method, path in client.calls
                    if method == "POST" and path.startswith("/v1/approvals/")
                ]
            )
            == 1
        )


async def test_file_reference_completion_and_session_drafts():
    client = DemoClient()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        await pilot.pause(0.5)
        composer = app.query_one(Composer)
        composer.focus()
        composer.load_text("Explain @READ")
        await pilot.pause(0.3)
        assert app.query_one("#references", OptionList).display
        await pilot.press("tab")
        assert composer.text == "Explain @README.md "
        await app.action_new_session().wait()
        assert composer.text == ""
        assert app.drafts["demo-session"] == "Explain @README.md "
        await app.open_session("demo-session").wait()
        assert composer.text == "Explain @README.md "
        app.action_files()
        await pilot.pause(0.2)
        assert isinstance(app.screen, Picker)


async def test_older_server_is_actionable_and_never_queues_work():
    class OldServer(DemoClient):
        async def request(self, method, path, **kwargs):
            if path == "/v1/client-info":
                raise ClientError("Unsupported", 404)
            return await super().request(method, path, **kwargs)

    client = OldServer()
    app = MerlinApp(client=client)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        assert "update server" in app.connection_status
        assert isinstance(app.screen, InfoScreen)
        assert not app.api_ready
        assert not any(method == "POST" for method, _ in client.calls)


async def test_model_links_never_open_automatically_or_accept_invalid_schemes():
    app = MerlinApp(demo=True)
    async with app.run_test() as pilot:
        await ready(app, pilot)
        with patch("merlin.tui.app.webbrowser.open") as opened:
            for url in (
                "javascript:alert(1)",
                "file:///etc/passwd",
                "https://[invalid",
                "https://user:secret@example.com",
            ):
                app.link_clicked(Markdown.LinkClicked(Markdown(), url))
            assert not opened.called


async def test_reconnect_replays_from_applied_cursor_without_new_task():
    class InterruptedStream(DemoClient):
        def __init__(self):
            super().__init__()
            self.cursors = []

        async def events(self, task_id, after):
            self.cursors.append(after)
            async for record in super().events(task_id, after):
                yield record
                if len(self.cursors) == 1 and record["sequence"] == 5:
                    raise ClientError("Synthetic disconnect")

    client = InterruptedStream()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        for _ in range(60):
            if len(client.cursors) >= 2:
                break
            await pilot.pause(0.05)
        assert client.cursors[:2] == [0, 5]
        assert not any(method == "POST" for method, _ in client.calls)
        await pilot.pause(0.6)
        feed = app.current_feed()
        assert len([entry for entry in feed.entries if entry.kind == "assistant"]) == 1


async def test_large_event_replay_keeps_widget_count_bounded_and_accepts_typing():
    class LargeHistory(DemoClient):
        async def events(self, task_id, after):
            for sequence in range(after + 1, 10001):
                yield {
                    "task_id": task_id,
                    "sequence": sequence,
                    "kind": "model.reply",
                    "data": {"text": f"Recorded update {sequence}"},
                }
            self.tasks[task_id]["state"] = "succeeded"

    client = LargeHistory()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        app.query_one(Composer).focus()
        await pilot.press("h", "i")
        assert app.query_one(Composer).text == "hi"
        for _ in range(50):
            if app.current_feed().cursor == 10000:
                break
            await pilot.pause(0.05)
        assert app.current_feed().cursor == 10000
        assert len(app.current_feed().entries) <= 200
        assert len(app.cards) <= 200


async def test_exit_during_pending_markdown_update_does_not_touch_unmounted_widgets():
    from merlin.tui.app import MessageCard

    app = MerlinApp(demo=True)
    entered = asyncio.Event()
    original = MessageCard.update_entry

    async def slow_update(card, entry):
        if entry.kind == "assistant":
            entered.set()
            await asyncio.sleep(0.1)
        if card.is_mounted:
            await original(card, entry)

    with patch.object(MessageCard, "update_entry", slow_update):
        async with app.run_test(size=(120, 40)):
            await asyncio.wait_for(entered.wait(), 3)
        assert not app.is_running


async def test_slow_submit_preserves_text_typed_while_the_response_is_pending():
    started, release = asyncio.Event(), asyncio.Event()

    class SlowResponse(DemoClient):
        async def request(self, method, path, **kwargs):
            if method == "POST" and path == "/v1/tasks":
                started.set()
                await release.wait()
            return await super().request(method, path, **kwargs)

    client = SlowResponse()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        composer = app.query_one(Composer)
        composer.load_text("Original task")
        worker = app.submit(composer.text)
        await asyncio.wait_for(started.wait(), 3)
        composer.load_text("The next draft must survive")
        release.set()
        await worker.wait()
        assert composer.text == "The next draft must survive"
        assert len([path for method, path in client.calls if method == "POST"]) == 1


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
async def test_ambiguous_http_failure_reuses_submission_key(status):
    class FailedAfterCommit(DemoClient):
        def __init__(self):
            super().__init__()
            self.submission_keys = []

        async def request(self, method, path, **kwargs):
            value = await super().request(method, path, **kwargs)
            if method == "POST" and path == "/v1/tasks":
                self.submission_keys.append(kwargs["headers"]["Idempotency-Key"])
                if len(self.submission_keys) == 1:
                    raise ClientError("Synthetic upstream failure after commit", status)
            return value

    client = FailedAfterCommit()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        app.query_one(Composer).load_text("Do this once")
        await app.submit("Do this once").wait()
        assert app.pending_submission is not None
        await app.submit("Do this once").wait()
        assert client.submission_keys[0] == client.submission_keys[1]
        assert len(client.tasks) == 2  # Initial demo task plus one new task.


async def test_changing_server_invalidates_old_refresh_and_private_drafts():
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowRefresh(DemoClient):
        block = False

        async def request(self, method, path, **kwargs):
            if self.block and path == "/v1/sessions":
                entered.set()
                await release.wait()
            return await super().request(method, path, **kwargs)

    old = SlowRefresh()
    app = MerlinApp(client=old, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        app.drafts["old-private-session"] = "old private draft"
        old.block = True
        refresh = app.refresh_data()
        await asyncio.wait_for(entered.wait(), 3)
        generation = app.view_generation
        new = DemoClient()
        await app.connected(new).wait()
        release.set()
        await pilot.pause(0.2)
        assert app.view_generation > generation
        assert refresh.is_cancelled
        assert app.client is new and old.closed
        assert app.drafts == {}
        assert app.selected_task is None


async def test_composer_event_after_shutdown_is_ignored():
    from types import SimpleNamespace

    app = MerlinApp(demo=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        composer = app.query_one(Composer)
    app.composer_changed(SimpleNamespace(text_area=composer))


async def test_ctrl_j_inserts_a_portable_newline_without_submitting():
    client = DemoClient()
    app = MerlinApp(client=client, repo="demo/merlin", session="demo-session")
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(app, pilot)
        composer = app.query_one(Composer)
        composer.focus()
        composer.load_text("first")
        composer.move_cursor(composer.document.end)
        await pilot.press("ctrl+j")
        assert composer.text == "first\n"
        assert not any(method == "POST" for method, _ in client.calls)


async def test_followers_remain_bounded_when_a_queued_task_starts(monkeypatch):
    from merlin.tui.state import TaskFeed

    app = MerlinApp(client=DemoClient())
    active = set()

    async def blocked(feed, generation):
        identifier = feed.task["id"]
        active.add(identifier)
        try:
            await asyncio.Event().wait()
        finally:
            active.remove(identifier)

    monkeypatch.setattr(app, "follow", blocked)
    app.feeds = {
        str(index): TaskFeed({"id": str(index), "state": "queued", "created_at": str(index)})
        for index in range(4)
    }
    try:
        app.sync_followers()
        await asyncio.sleep(0)
        assert active == {"0", "1", "2"}
        app.feeds["3"].task["state"] = "running"
        app.sync_followers()
        await asyncio.sleep(0)
        assert set(app.followers) == active == {"3", "0", "1"}
    finally:
        await app.stop_followers()
        await app.client.close()
