"""Responsive remote-agent terminal application."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import re
import webbrowser
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import Screen
from textual.theme import Theme
from textual.widget import MountError
from textual.widgets import Button, Collapsible, Footer, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from merlin.client import ClientError, MerlinClient, connection, terminal_text
from merlin.tui.preferences import load_preferences, save_preferences
from merlin.tui.screens import (
    ApprovalScreen,
    InfoScreen,
    LoginScreen,
    NoteScreen,
    Picker,
    SourceScreen,
)
from merlin.tui.state import Entry, TaskFeed

TERMINAL = {"succeeded", "failed", "cancelled"}
WAITING = {"paused", "waiting_for_approval"}

HELP = """GETTING STARTED
Chat with Merlin, or choose a repository for coding. Repository tools run in E2B.
Messages sent during a task are queued follow-ups, not instructions injected into that task.

KEYBOARD
Enter send · Ctrl+J / Alt+Enter newline · Shift+Enter newline where supported
Ctrl+P commands · Ctrl+N new session · Ctrl+B sidebar
Ctrl+Q or Ctrl+C detach · Escape dismiss · Tab move focus / accept a file reference
Paste multiple lines without submitting them. No special terminal font is required.

COMMANDS
/new /sessions /repo /model /details /usage /approvals /files
/cancel /resume /resolve /reconnect /connect /theme /help /quit

SAFETY AND RECOVERY
/cancel requests cancellation of the selected task and its children.
/resume resumes a paused task. /resolve requires inspection and an operator note.
Publishing needs a specific approval unless the task already has a server-side permission.
API connected does not establish that a worker or provider is ready.

REFERENCES
Type @path to refer to a workspace file. Tab accepts a suggestion after a checkpoint exists.
References are paths; the agent reads their contents through its normal tools.
Previews show the checkpoint timestamp, not a claim about a live workspace.

MODELS
The server supplies the default. Enter another model ID with /model for future tasks.
Availability and billing are not inferred from the name.

SCRIPTS
merlin run / merlin attach --json keep their original behavior.
merlin chat --plain uses the original line-based conversation.
"""


class Composer(TextArea):
    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True),
        Binding("alt+enter,shift+enter,ctrl+j", "newline", "New line", show=False, priority=True),
    ]

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def action_submit(self) -> None:
        if self.text.strip():
            self.post_message(self.Submitted(self.text))

    def action_newline(self) -> None:
        self.insert("\n")


class MessageCard(Vertical):
    def __init__(self, entry: Entry) -> None:
        super().__init__(classes=f"message {entry.kind}")
        self.entry = entry
        self.last = entry.text
        self.last_label = ""

    def compose(self) -> ComposeResult:
        yield Static("You" if self.entry.kind == "user" else "Merlin", classes="role", markup=False)
        if self.entry.kind == "assistant":
            yield Markdown(terminal_text(self.entry.text), open_links=False)
        else:
            yield Static(Text(terminal_text(self.entry.text)))

    async def update_entry(self, entry: Entry) -> None:
        self.entry = entry
        label = "You" if entry.kind == "user" else "Merlin"
        if entry.status in {"streaming", "interrupted"}:
            label += f" · {entry.status}"
        if self.last_label != label:
            self.query_one(".role", Static).update(label)
            self.last_label = label
        if self.last != entry.text:
            if entry.kind == "assistant":
                await self.query_one(Markdown).update(terminal_text(entry.text))
            else:
                self.query(Static).last().update(Text(terminal_text(entry.text)))
            self.last = entry.text


class ToolCard(Collapsible):
    def __init__(self, entry: Entry, details: bool) -> None:
        self.entry = entry
        self.last_entry: tuple[str, str, str, str, str] | None = None
        self.output = Static(Text(terminal_text(entry.text)), classes="tool-output")
        super().__init__(
            self.output,
            title=escape(terminal_text(entry.title)),
            collapsed=not details,
            classes="tool-card",
        )

    async def update_entry(self, entry: Entry) -> None:
        signature = (entry.text, entry.title, entry.status, entry.started_at, entry.finished_at)
        if self.last_entry == signature:
            return
        self.last_entry = signature
        duration = ""
        if entry.started_at and entry.finished_at:
            with contextlib.suppress(ValueError):
                seconds = (
                    datetime.fromisoformat(entry.finished_at)
                    - datetime.fromisoformat(entry.started_at)
                ).total_seconds()
                duration = f" · {max(0, seconds):.1f}s"
        self.title = escape(terminal_text(f"{entry.title} · {entry.status}{duration}"))
        self.output.update(Text(terminal_text(entry.text)))
        self.set_class(entry.status in {"failed", "uncertain"}, "tool-error")


class MerlinApp(App[None]):
    TITLE = "Merlin"
    CSS_PATH = "merlin.tcss"
    BINDINGS = [
        Binding("ctrl+q,ctrl+c", "detach", "Exit", priority=True),
        Binding("ctrl+n", "new_session", "New session", priority=True),
        Binding("ctrl+b", "sidebar", "Sidebar", show=False, priority=True),
        Binding("f1", "help", "Help"),
    ]

    def __init__(
        self,
        *,
        repo: str | None = None,
        session: str | None = None,
        task: str | None = None,
        demo: bool = False,
        client: Any = None,
        allow_domains: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.demo = demo
        self.persist = client is None and not demo
        self.preferences = load_preferences() if self.persist else {}
        self.repository = repo or ("demo/merlin" if demo else "")
        self.session_id = session or ("demo-session" if demo else uuid4().hex)
        self.initial_session, self.initial_task = session, task
        self.client = client
        self.default_model = ""
        self.model: str | None = None
        self.allow_domains = allow_domains or []
        self.sessions: list[dict[str, Any]] = []
        self.session_cursor: int | None = None
        self.history_cursor: int | None = None
        self.browsing_history = False
        self.usage_record: dict[str, Any] = {}
        self.feeds: dict[str, TaskFeed] = {}
        self.followers: dict[str, asyncio.Task[None]] = {}
        self.drafts: dict[str, str] = {}
        self.selected_task: str | None = task
        self.details = False
        self.dirty = True
        self.rendering = False
        self.card_keys: list[str] = []
        self.cards: dict[str, Any] = {}
        self.submitting = False
        self.pending_submission: tuple[str, dict[str, Any]] | None = None
        self.connection_status = "Offline demo" if demo else "Connecting"
        self.sidebar_requested = True
        self.reference_paths: list[str] = []
        self.api_features: set[str] = set()
        self.api_ready = False
        self.view_generation = 0

    def trim_feeds(self) -> None:
        """Keep a bounded window. Older remote tasks remain available through history."""
        if len(self.feeds) <= 40:
            return
        keep = list(self.feeds)[-36:]
        keep.extend(
            identifier
            for identifier, feed in self.feeds.items()
            if feed.task["state"] not in TERMINAL | {"queued"}
        )
        if self.selected_task:
            keep.append(self.selected_task)
        selected = set(keep[-40:])
        self.feeds = {
            identifier: feed for identifier, feed in self.feeds.items() if identifier in selected
        }

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static("merlin", id="brand", markup=False)
            yield Static("Chat", id="context", markup=False)
            yield Static(self.connection_status, id="connection", markup=False)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Button("+ New session", id="new-session", variant="primary")
                yield Button("Choose repository", id="choose-repo")
                yield Static("RECENT SESSIONS", classes="section-label", markup=False)
                yield OptionList(id="sessions")
                yield Button("More sessions", id="more-sessions", disabled=True)
            with Vertical(id="main"):
                with Horizontal(id="history-controls"):
                    yield Button("Earlier messages", id="older-history", disabled=True)
                    yield Button("Back to latest", id="latest-history", disabled=True)
                with VerticalScroll(id="timeline"):
                    yield Static(
                        "merlin\n\nYour assistant for conversation and code.\n\n"
                        "Ask a question, say hello, or choose a repository to work on code.\n"
                        "Ctrl+P opens commands. /help shows the basics.",
                        id="welcome",
                        markup=False,
                    )
                yield Button("↓ New activity — jump to latest", id="new-activity")
                with Horizontal(id="task-bar"):
                    yield Static("Ready", id="task-status", markup=False)
                    yield Button("Tasks", id="choose-task")
                    yield Button("Review", id="review-task")
                    yield Button("Cancel", id="cancel-task")
                yield OptionList(id="references")
                yield Composer(id="composer", soft_wrap=True, tab_behavior="focus")
                with Horizontal(id="composer-footer"):
                    yield Static(
                        "Enter send · Ctrl+J newline · @ file · Ctrl+P commands",
                        id="composer-hint",
                        markup=False,
                    )
                    yield Static("Usage: /usage", id="usage-status", markup=False)
                    yield Button("Send", id="send", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(
            Theme(
                name="merlin",
                primary="#a78bfa",
                secondary="#67e8f9",
                accent="#a78bfa",
                foreground="#e5e7eb",
                background="#0b0f14",
                surface="#111827",
                panel="#151d2b",
                dark=True,
            )
        )
        self.register_theme(
            Theme(
                name="merlin-mono",
                primary="#eeeeee",
                secondary="#dddddd",
                accent="#ffffff",
                foreground="#eeeeee",
                background="#101010",
                surface="#181818",
                panel="#222222",
                success="#eeeeee",
                warning="#eeeeee",
                error="#eeeeee",
                dark=True,
            )
        )
        preferred = (
            "merlin-mono" if "NO_COLOR" in os.environ else self.preferences.get("theme", "merlin")
        )
        self.theme = preferred if preferred in self.available_themes else "merlin"
        self.query_one("#references").display = False
        self.query_one("#new-activity").display = False
        self.query_one("#composer", Composer).focus()
        self.resize_layout()
        self.set_interval(0.1, self.render_timeline)
        self.set_interval(5, self.refresh_data)
        self.connect()

    def on_resize(self, event: events.Resize) -> None:
        if self.query("#sidebar"):
            self.resize_layout(event.size.width, event.size.height)

    def resize_layout(self, width: int | None = None, height: int | None = None) -> None:
        width = self.size.width if width is None else width
        height = self.size.height if height is None else height
        self.query_one("#sidebar").display = width >= 110 and self.sidebar_requested
        self.default_screen.set_class(height < 30, "compact")

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        commands: dict[str, tuple[str, Callable[[], Any]]] = {
            "New session": ("Start a conversation", self.action_new_session),
            "Switch session": ("Find recent work", self.action_sessions),
            "Choose repository": ("Select an installed GitHub repository", self.action_repo),
            "Choose model": ("Change the model for future tasks", self.action_model),
            "Review approvals": ("Inspect an exact publishing proposal", self.action_approvals),
            "Workspace files": ("Browse the latest source checkpoint", self.action_files),
            "Task usage": ("View recorded usage and budgets", self.action_usage),
            "Tool details": ("Expand/collapse or load task activity", self.action_details),
            "Cancel task": ("Request cancellation of the task and children", self.action_cancel),
            "Resume task": ("Resume a paused task", self.action_resume),
            "Reconnect": ("Reconnect without submitting another task", self.action_reconnect),
            "Connection": ("Change server address and owner credential", self.action_connect),
            "Theme": ("Dark, light, or monochrome", self.action_theme),
            "Help": ("Keyboard shortcuts and commands", self.action_help),
            "Exit": ("Detach; remote work continues", self.action_detach),
        }
        for title, (help_text, callback) in commands.items():
            yield SystemCommand(title, help_text, callback)

    @work(exclusive=True, group="connect")
    async def connect(self) -> None:
        if self.demo and self.client is None:
            from merlin.tui.demo import DemoClient

            self.client = DemoClient()
        if self.client is None:
            try:
                url, token = connection()
                self.client = MerlinClient(url, token)
            except ValueError as error:
                self.set_connection("Not connected")
                self.notify(str(error), severity="warning")
                self.action_connect()
                return
        try:
            try:
                info = await self.client.request("GET", "/v1/client-info")
            except ClientError as error:
                if error.status == 404:
                    await self.client.request("GET", "/v1/github/status")
                    self.set_connection("API connected · update server")
                    self.show_info(
                        "Server update required",
                        "This server predates the terminal read APIs. Restart the API and worker "
                        "with the updated installation. Existing run, attach, and chat --plain "
                        "commands remain available.",
                    )
                    return
                raise
            self.api_features = set(info.get("features", []))
            self.default_model = info.get("default_model", "")
            self.api_ready = True
            self.set_connection("Offline demo" if self.demo else "API connected")
            if self.initial_task:
                record = await self.client.request("GET", f"/v1/tasks/{self.initial_task}/summary")
                self.repository, self.session_id = record["repository"] or "", record["session_id"]
                self.initial_task = None
                await self.load_session(self.session_id)
                if record["id"] not in self.feeds:
                    feed = TaskFeed(record)
                    feed.snapshot(record)
                    self.feeds = {record["id"]: feed}
                    self.browsing_history = True
                self.selected_task = record["id"]
            elif self.initial_session or self.demo:
                await self.load_session(self.session_id)
                self.initial_session = None
            await self.load_sessions()
            self.sync_followers()
            self.dirty = True
        except ClientError as error:
            self.set_connection("Disconnected")
            self.notify(str(error), severity="error")

    def set_connection(self, text: str) -> None:
        self.connection_status = text
        if self.query("#connection"):
            self.query_one("#connection", Static).update(text)

    async def load_sessions(self, *, more: bool = False) -> None:
        if not self.client or not self.api_ready:
            return
        generation, client = self.view_generation, self.client
        page = await client.request(
            "GET", "/v1/sessions", params={"cursor": self.session_cursor if more else 0}
        )
        if generation != self.view_generation or client is not self.client:
            return
        items = self.sessions + page["items"] if more else page["items"]
        self.sessions = list({row["session_id"]: row for row in items}.values())
        self.session_cursor = page.get("next_cursor")
        options = self.query_one("#sessions", OptionList)
        highlighted = options.highlighted
        options.clear_options().add_options(
            [
                Option(
                    Text(
                        terminal_text(
                            f"{row['title'].replace(chr(10), ' ')}\n"
                            f"{row['repository'] or 'Chat'} · {row['state']}"
                        )
                    )
                )
                for row in self.sessions
            ]
        )
        if self.sessions:
            options.highlighted = min(highlighted or 0, len(self.sessions) - 1)
        self.query_one("#more-sessions", Button).disabled = self.session_cursor is None

    async def stop_followers(self) -> None:
        tasks = list(self.followers.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.followers.clear()

    async def load_session(self, session_id: str) -> None:
        self.drafts[self.session_id] = self.query_one("#composer", Composer).text
        self.view_generation += 1
        generation = self.view_generation
        await self.stop_followers()
        page = await self.client.request("GET", f"/v1/sessions/{session_id}/tasks")
        if generation != self.view_generation:
            return
        if not page["items"]:
            self.notify("This session has no tasks yet.")
            return
        repo = page["items"][0]["repository"] or ""
        if self.repository and session_id == self.session_id and repo != self.repository:
            raise ClientError(
                "This session belongs to a different repository. Open it without --repo."
            )
        self.repository, self.session_id = repo, session_id
        self.history_cursor = page.get("next_cursor")
        self.browsing_history = False
        self.feeds = {}
        for row in reversed(page["items"]):
            feed = TaskFeed(row)
            feed.snapshot(row)
            self.feeds[row["id"]] = feed
        if self.selected_task not in self.feeds:
            self.selected_task = page["items"][0]["id"]
        self.query_one("#composer", Composer).load_text(self.drafts.get(session_id, ""))
        self.pending_submission = None
        if page["items"]:
            self.allow_domains = page["items"][0].get("allowed_domains", self.allow_domains)
        self.sync_followers()
        self.dirty = True
        self.remember()

    def current_feed(self) -> TaskFeed | None:
        if self.selected_task in self.feeds:
            return self.feeds[self.selected_task]  # type: ignore[index]
        return next(reversed(self.feeds.values()), None) if self.feeds else None

    def sync_followers(self) -> None:
        self.trim_feeds()
        for identifier in list(self.followers):
            if self.followers[identifier].done():
                self.followers.pop(identifier)
            elif identifier not in self.feeds:
                self.followers.pop(identifier).cancel()
        candidates = [feed for feed in self.feeds.values() if feed.task["state"] not in TERMINAL]
        candidates.sort(key=lambda feed: (feed.task["state"] != "running", feed.task["created_at"]))
        candidates = candidates[:3]
        selected = {feed.task["id"] for feed in candidates}
        for identifier in list(self.followers):
            if identifier not in selected:
                self.followers.pop(identifier).cancel()
        for feed in candidates:
            identifier = feed.task["id"]
            if identifier not in self.followers or self.followers[identifier].done():
                self.followers[identifier] = asyncio.create_task(
                    self.follow(feed, self.view_generation)
                )

    async def follow(self, feed: TaskFeed, generation: int) -> None:
        failures = 0
        applied = 0
        try:
            while generation == self.view_generation:
                try:
                    async for event in self.client.events(feed.task["id"], feed.cursor):
                        if generation != self.view_generation:
                            return
                        if feed.apply(event):
                            self.dirty = True
                        applied += 1
                        if applied % 50 == 0:
                            await asyncio.sleep(0)
                    snapshot = await self.client.request(
                        "GET", f"/v1/tasks/{feed.task['id']}/summary"
                    )
                    feed.snapshot(snapshot)
                    self.dirty = True
                    failures = 0
                    if snapshot["state"] in TERMINAL:
                        return
                    if snapshot["state"] in WAITING:
                        await asyncio.sleep(2)
                    else:
                        await asyncio.sleep(0.5)
                except ClientError as error:
                    if error.status in {401, 403, 404}:
                        self.set_connection("Authentication/resource unavailable")
                        return
                    failures += 1
                    self.set_connection(f"Reconnecting {failures}/3")
                    if failures >= 3:
                        self.set_connection("Disconnected · /reconnect")
                        return
                    await asyncio.sleep(2 ** (failures - 1))
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            self.set_connection("Invalid event data · /reconnect")

    @work(exclusive=True, group="refresh")
    async def refresh_data(self) -> None:
        if (
            not self.api_ready
            or not self.client
            or self.connection_status.startswith("Disconnected")
        ):
            return
        try:
            generation = self.view_generation
            await self.load_sessions()
            if self.browsing_history:
                return
            page = await self.client.request("GET", f"/v1/sessions/{self.session_id}/tasks")
            if generation != self.view_generation:
                return
            for record in reversed(page["items"]):
                if record["id"] in self.feeds:
                    self.feeds[record["id"]].snapshot(record)
                else:
                    feed = TaskFeed(record)
                    feed.snapshot(record)
                    self.feeds[record["id"]] = feed
            self.sync_followers()
            selected = self.current_feed()
            if selected:
                self.usage_record = await self.client.request(
                    "GET", f"/v1/tasks/{selected.task['id']}/usage"
                )
            self.dirty = True
        except ClientError as error:
            self.set_connection("Disconnected · /reconnect")
            self.notify(str(error), severity="warning")

    async def render_timeline(self) -> None:
        if not self.is_running or not self.dirty or self.rendering:
            return
        generation = self.view_generation
        self.rendering = True
        self.dirty = False
        try:
            timeline = self.query_one("#timeline", VerticalScroll)
            at_bottom = timeline.is_vertical_scroll_end
            rows: list[tuple[str, Entry]] = []
            for identifier, feed in sorted(
                self.feeds.items(), key=lambda item: item[1].task["created_at"]
            ):
                rows.append((f"{identifier}/prompt", Entry("prompt", "user", feed.task["prompt"])))
                rows.extend((f"{identifier}/{entry.key}", entry) for entry in feed.entries)
                if feed.task.get("error"):
                    rows.append(
                        (f"{identifier}/error", Entry("error", "notice", feed.task["error"]))
                    )
            rows = rows[-200:]
            desired = [key for key, _ in rows]
            surviving = [key for key in self.card_keys if key in desired]
            if surviving != desired[: len(surviving)]:
                for card in self.cards.values():
                    await card.remove()
                    if not self.is_running:
                        return
                self.cards.clear()
                self.card_keys.clear()
            for key in list(self.cards):
                if key not in desired:
                    await self.cards.pop(key).remove()
                    if not self.is_running:
                        return
            for key, entry in rows:
                if key not in self.cards:
                    card = (
                        ToolCard(entry, self.details)
                        if entry.kind == "tool"
                        else MessageCard(entry)
                    )
                    self.cards[key] = card
                    await timeline.mount(card)
                    if not self.is_running:
                        return
                await self.cards[key].update_entry(entry)
                if not self.is_running:
                    return
                if generation != self.view_generation:
                    self.dirty = True
                    return
            self.card_keys = desired
            self.query_one("#welcome").display = not rows
            if at_bottom:
                timeline.scroll_end(animate=False)
            elif rows:
                self.query_one("#new-activity").display = True
            selected_feed = self.current_feed()
            status = "Ready"
            if selected_feed:
                raw_state = selected_feed.task["state"]
                state = raw_state.replace("_", " ")
                status = (
                    f"{state} · {selected_feed.progress}" if raw_state not in TERMINAL else state
                )
                if selected_feed.task.get("cancel_requested") and raw_state not in TERMINAL:
                    status = "Cancellation requested"
            self.query_one("#task-status", Static).update(Text(terminal_text(status)))
            self.query_one("#cancel-task", Button).disabled = (
                selected_feed is None or selected_feed.task["state"] in TERMINAL
            )
            self.query_one("#review-task", Button).disabled = selected_feed is None
            self.query_one("#older-history", Button).disabled = self.history_cursor is None
            self.query_one("#latest-history", Button).disabled = not self.browsing_history
            self.query_one("#history-controls").display = (
                self.history_cursor is not None or self.browsing_history
            )
            usage = self.usage_record
            if selected_feed:
                if usage.get("root_id") == selected_feed.task.get("root_id"):
                    label = f"≈${usage['cost_usd']:.4f} · {usage['calls']} calls"
                else:
                    label = f"≈${selected_feed.cost:.4f} · {selected_feed.reply_count} replies"
                self.query_one("#usage-status", Static).update(label)
            model = self.model or self.default_model or "server default"
            if selected_feed and selected_feed.task["state"] not in TERMINAL:
                current_model = selected_feed.task.get("model", model)
                model = current_model + (
                    f" · next {self.model}" if self.model and self.model != current_model else ""
                )
            self.query_one("#context", Static).update(
                Text(terminal_text(f"{self.repository or 'Chat'} · {model}"))
            )
            active = any(item.task["state"] not in TERMINAL for item in self.feeds.values())
            self.query_one("#send", Button).label = "Queue next" if active else "Send"
        except (NoMatches, MountError):
            if self.is_running:
                raise
        finally:
            self.rendering = False

    @on(Composer.Submitted)
    def compose_submitted(self, event: Composer.Submitted) -> None:
        self.submit(event.text)

    @on(Button.Pressed)
    def button(self, event: Button.Pressed) -> None:
        actions: dict[str, Callable[[], Any]] = {
            "new-session": self.action_new_session,
            "choose-repo": self.action_repo,
            "review-task": self.action_approvals,
            "cancel-task": self.action_cancel,
        }
        if event.button.id in actions:
            actions[event.button.id]()
        elif event.button.id == "send":
            self.submit(self.query_one("#composer", Composer).text)
        elif event.button.id == "new-activity":
            self.query_one("#timeline", VerticalScroll).scroll_end(animate=False)
            self.query_one("#new-activity").display = False
        elif event.button.id == "more-sessions":
            self.more_sessions()
        elif event.button.id == "older-history":
            self.older_history()
        elif event.button.id == "latest-history":
            self.open_session(self.session_id)
        elif event.button.id == "choose-task":
            self.choose_task()

    @on(OptionList.OptionSelected, "#sessions")
    def session_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_index < len(self.sessions):
            self.open_session(self.sessions[event.option_index]["session_id"])

    @work(exclusive=True, group="session")
    async def open_session(self, session_id: str) -> None:
        try:
            await self.load_session(session_id)
        except ClientError as error:
            self.notify(str(error), severity="error")

    @work(exclusive=True, group="more-sessions")
    async def more_sessions(self) -> None:
        try:
            await self.load_sessions(more=True)
        except ClientError as error:
            self.notify(str(error), severity="error")

    @work(exclusive=True, group="session")
    async def older_history(self) -> None:
        if self.history_cursor is None:
            return
        try:
            self.view_generation += 1
            await self.stop_followers()
            page = await self.client.request(
                "GET",
                f"/v1/sessions/{self.session_id}/tasks",
                params={"cursor": self.history_cursor},
            )
            if not page["items"]:
                self.history_cursor = None
                self.dirty = True
                return
            self.feeds = {}
            for row in reversed(page["items"]):
                feed = TaskFeed(row)
                feed.snapshot(row)
                self.feeds[row["id"]] = feed
            self.selected_task = page["items"][0]["id"]
            self.history_cursor = page.get("next_cursor")
            self.browsing_history = True
            self.dirty = True
        except ClientError as error:
            self.notify(str(error), severity="error")

    def choose_task(self) -> None:
        self.push_screen(
            Picker(
                "Select task for review, usage, or cancellation",
                [
                    (
                        f"{feed.task['state']} · {feed.task['prompt'][:80]} · {identifier[:8]}",
                        identifier,
                    )
                    for identifier, feed in reversed(list(self.feeds.items()))
                ],
            ),
            self.task_chosen,
        )

    def task_chosen(self, value: str | None) -> None:
        if value in self.feeds:
            self.selected_task = value
            self.dirty = True

    @work(group="submit")
    async def submit(self, text: str) -> None:
        submitted_text = text
        text = text.strip()
        if not text or self.submitting:
            return
        if text.startswith("/"):
            command = text[1:].strip()
            callback = (
                getattr(self, f"action_{command.replace('-', '_')}", None)
                if re.fullmatch(r"[a-z-]+", command)
                else None
            )
            if callback is None or command.startswith("_"):
                self.notify("Unknown command. Use Ctrl+P or /help.", severity="warning")
                return
            allowed = {
                "help",
                "new",
                "sessions",
                "repo",
                "model",
                "models",
                "details",
                "usage",
                "approvals",
                "files",
                "cancel",
                "resume",
                "resolve",
                "reconnect",
                "connect",
                "theme",
                "quit",
                "exit",
            }
            if command not in allowed:
                self.notify("Unknown command. Use /help.")
                return
            self.query_one("#composer", Composer).load_text("")
            result = callback()
            if inspect.isawaitable(result):
                await result
            return
        if not self.client or not self.api_ready:
            self.notify("Connect to an updated Merlin API before submitting.", severity="warning")
            return
        if len(text) > 100000:
            self.notify("Messages are limited to 100,000 characters.", severity="error")
            return
        body: dict[str, Any] = {
            "prompt": text,
            "repository": self.repository or None,
            "session_id": self.session_id,
            "permissions": [],
            "allowed_domains": self.allow_domains,
        }
        if self.model:
            body["model"] = self.model
        if self.pending_submission and self.pending_submission[1] != body:
            self.notify(
                "The previous submission has an unknown outcome. Retry the unchanged message "
                "or reconnect to inspect it first.",
                severity="warning",
            )
            return
        if self.pending_submission is None:
            self.pending_submission = (uuid4().hex, body)
        key, body = self.pending_submission
        self.submitting = True
        self.query_one("#send", Button).disabled = True
        generation = self.view_generation
        try:
            task = await self.client.request(
                "POST", "/v1/tasks", json=body, headers={"Idempotency-Key": key}
            )
            self.pending_submission = None
            if self.drafts.get(body["session_id"]) == submitted_text:
                self.drafts[body["session_id"]] = ""
            if generation != self.view_generation:
                self.notify("Task submitted in the previous session; it continues there.")
                return
            # POST has already acknowledged the task. Project that response
            # locally: a later failed status read must never trigger a fresh POST.
            task_summary = dict(task)
            if "spec" in task:
                spec = task["spec"]
                task_summary.update(
                    {
                        key: spec[key]
                        for key in (
                            "prompt",
                            "repository",
                            "session_id",
                            "model",
                            "budget",
                            "allowed_domains",
                        )
                    }
                )
                task_summary.pop("spec", None)
                task_summary.pop("history", None)
            feed = TaskFeed(task_summary)
            feed.snapshot(task_summary)
            self.feeds[task["id"]] = feed
            self.selected_task = task["id"]
            composer = self.query_one("#composer", Composer)
            if composer.text == submitted_text:
                composer.load_text("")
            self.sync_followers()
            self.dirty = True
            self.remember()
            try:
                await self.load_sessions()
            except ClientError:
                self.notify("Task submitted. Recent sessions could not refresh; use /reconnect.")
        except ClientError as error:
            # A gateway/server failure or request timeout may follow a commit.
            # Preserve the original submission identity until its outcome is known.
            if 400 <= error.status < 500 and error.status not in {408, 429}:
                self.pending_submission = None
            self.notify(
                str(error)
                + (
                    " Retry the unchanged message to reuse its submission ID."
                    if self.pending_submission is not None
                    else ""
                ),
                severity="error",
                timeout=8,
            )
        finally:
            self.submitting = False
            self.query_one("#send", Button).disabled = False

    def remember(self) -> None:
        if self.persist:
            self.preferences.update(
                {"theme": self.theme, "repository": self.repository, "session_id": self.session_id}
            )
            with contextlib.suppress(OSError):
                save_preferences(self.preferences)

    def show_info(self, title: str, text: str) -> None:
        self.push_screen(InfoScreen(title, text))

    def action_help(self) -> None:
        self.show_info("Merlin help", HELP)

    def action_detach(self) -> None:
        self.exit()

    async def action_quit(self) -> None:
        self.action_detach()

    action_exit = action_detach

    def action_sidebar(self) -> None:
        if self.size.width < 110:
            self.action_sessions()
        else:
            self.sidebar_requested = not self.sidebar_requested
            self.resize_layout()

    @work(exclusive=True, group="session")
    async def action_new_session(self) -> None:
        self.drafts[self.session_id] = self.query_one("#composer", Composer).text
        self.view_generation += 1
        await self.stop_followers()
        self.session_id = uuid4().hex
        self.feeds.clear()
        self.selected_task = None
        self.history_cursor = None
        self.browsing_history = False
        self.query_one("#composer", Composer).load_text("")
        self.pending_submission = None
        self.dirty = True
        self.query_one("#composer", Composer).focus()

    action_new = action_new_session

    def action_sessions(self) -> None:
        self.push_screen(
            Picker(
                "Switch session",
                [
                    (
                        f"{row['title']} · {row['repository'] or 'Chat'} · {row['state']}",
                        row["session_id"],
                    )
                    for row in self.sessions
                ],
            ),
            self.session_chosen,
        )

    def session_chosen(self, value: str | None) -> None:
        if value:
            self.open_session(value)

    @work(exclusive=True, group="repos")
    async def action_repo(self) -> None:
        if self.client is None:
            self.action_connect()
            return
        try:
            rows = await self.client.request("GET", "/v1/github/repositories")
            if not rows:
                self.show_info(
                    "No repositories available",
                    "Install the configured GitHub App on a repository, then reconnect. "
                    "Your server operator configures the App; provider keys are not entered here.",
                )
                return
            self.push_screen(
                Picker("Choose repository", [(row["full_name"], row["full_name"]) for row in rows]),
                self.choose_repository,
            )
        except ClientError as error:
            self.show_info(
                "GitHub connection",
                str(error) + "\nCheck merlin doctor and the server's GitHub App configuration.",
            )

    def choose_repository(self, value: str | None) -> None:
        if value and value != self.repository:
            self.repository = value
            self.action_new_session()

    def action_model(self) -> None:
        models = list(dict.fromkeys([self.default_model, *self.preferences.get("models", [])]))
        self.push_screen(
            Picker(
                "Model for future tasks · availability unverified",
                [
                    (
                        model
                        + (
                            " · server default"
                            if model == self.default_model
                            else " · previously entered"
                        ),
                        model,
                    )
                    for model in models
                    if model
                ],
                custom=True,
            ),
            self.choose_model,
        )

    action_models = action_model

    def choose_model(self, value: str | None) -> None:
        if value:
            self.model = value
            self.preferences["models"] = list(
                dict.fromkeys([value, *self.preferences.get("models", [])])
            )[:10]
            self.remember()
            self.dirty = True

    def action_theme(self) -> None:
        self.push_screen(
            Picker(
                "Theme",
                [
                    ("Merlin dark", "merlin"),
                    ("Light", "textual-light"),
                    ("Monochrome", "merlin-mono"),
                ],
            ),
            self.choose_theme,
        )

    def choose_theme(self, value: str | None) -> None:
        if value:
            self.theme = value
            self.remember()

    @work(exclusive=True, group="details")
    async def action_details(self) -> None:
        self.details = not self.details
        for card in self.cards.values():
            if isinstance(card, ToolCard):
                card.collapsed = not self.details
        feed = self.current_feed()
        if feed and feed.cursor == 0:
            await self.follow(feed, self.view_generation)

    @work(exclusive=True, group="usage")
    async def action_usage(self) -> None:
        feed = self.current_feed()
        if not feed:
            self.notify("Submit a task to view recorded usage.")
            return
        try:
            usage = await self.client.request("GET", f"/v1/tasks/{feed.task['id']}/usage")
            budget = feed.task.get("budget", {})
            self.show_info(
                "Task usage",
                f"Task {feed.task['id']}\nRoot {usage['root_id']} (shared with children)\n\n"
                f"Estimated model cost: ${usage['cost_usd']:.6f}\n"
                f"Reserved estimate: ${usage['reserved_cost_usd']:.6f}\n"
                f"Model calls: {usage['calls']} / {budget.get('max_calls', '?')}\n"
                f"Sandbox seconds: {usage['sandbox_seconds']} / "
                f"{budget.get('max_sandbox_seconds', '?')}\n"
                f"Estimated cost limit: ${budget.get('max_cost_usd', '?')}\n\n"
                "Provider billing may differ; this is recorded service accounting.",
            )
        except ClientError as error:
            self.notify(str(error), severity="error")

    @work(exclusive=True, group="approvals")
    async def action_approvals(self) -> None:
        feeds = list(self.feeds.values())
        if not feeds:
            self.notify("No task selected.")
            return
        try:
            approvals: list[dict[str, Any]] = []
            for feed in feeds[-20:]:
                rows = await self.client.request(
                    "GET", f"/v1/tasks/{feed.task['id']}/approval-summaries"
                )
                approvals.extend(row for row in rows if row["status"] == "pending")
            if not approvals:
                self.notify("No pending approvals in this conversation.")
            elif len(approvals) == 1:
                self.open_approval(approvals[0]["id"])
            else:
                self.push_screen(
                    Picker(
                        "Pending approvals",
                        [(f"{row['name']} · {row['id'][:12]}", row["id"]) for row in approvals],
                    ),
                    self.open_approval,
                )
        except ClientError as error:
            self.notify(str(error), severity="error")

    def open_approval(self, value: str | None) -> None:
        if value:
            self.push_screen(ApprovalScreen(self.client, value), self.approval_closed)

    def approval_closed(self, value: bool | None) -> None:
        self.refresh_data()

    @work(exclusive=True, group="task-action")
    async def task_action(self, action: str) -> None:
        feed = self.current_feed()
        if not feed:
            self.notify("No task selected.")
            return
        try:
            await self.client.request("POST", f"/v1/tasks/{feed.task['id']}/{action}")
            summary = await self.client.request("GET", f"/v1/tasks/{feed.task['id']}/summary")
            feed.snapshot(summary)
            self.dirty = True
            self.sync_followers()
        except ClientError as error:
            self.notify(str(error), severity="error")

    def action_cancel(self) -> None:
        self.task_action("cancel")

    def action_resume(self) -> None:
        self.task_action("resume")

    @work(exclusive=True, group="resolve")
    async def action_resolve(self) -> None:
        feed = self.current_feed()
        if not feed or feed.task["state"] != "paused":
            self.notify("Select a paused task before resolving an uncertain operation.")
            return
        # Only reveal call identifiers already present in this task's persisted events.
        calls = [
            entry for entry in feed.entries if entry.kind == "tool" and entry.status == "uncertain"
        ]
        if not calls:
            self.show_info(
                "Inspect uncertain work",
                "Load /details, inspect any external effects, then use "
                f"merlin inspect {feed.task['id']}. "
                "Acknowledgment does not undo effects or replay the operation.",
            )
            return
        self.push_screen(
            Picker("Uncertain operations", [(entry.title, entry.key) for entry in calls]),
            self.resolve_selected,
        )

    def resolve_selected(self, call_id: str | None) -> None:
        feed = self.current_feed()
        if call_id and feed:
            task_id = feed.task["id"]

            def noted(note: str | None) -> None:
                if note:
                    self.resolve_note(task_id, call_id, note)

            self.push_screen(
                NoteScreen(
                    "Acknowledge uncertain outcome",
                    "Inspect external effects first. This restores the last completed checkpoint; "
                    "it does not undo effects, mark success, or replay the operation.",
                ),
                noted,
            )

    @work(exclusive=True, group="resolve-note")
    async def resolve_note(self, task_id: str, call_id: str, note: str) -> None:
        try:
            await self.client.request(
                "POST", f"/v1/tasks/{task_id}/tools/{call_id}/resolve", json={"note": note}
            )
            self.refresh_data()
        except ClientError as error:
            self.notify(str(error), severity="error")

    def action_reconnect(self) -> None:
        self.connect()

    def action_connect(self) -> None:
        if self.demo:
            self.notify("Offline demo: no credentials or network are used.")
            return
        self.push_screen(LoginScreen(), self.connection_chosen)

    def connection_chosen(self, value: MerlinClient | None) -> None:
        if value:
            self.connected(value)

    @work(exclusive=True, group="connection-change")
    async def connected(self, value: MerlinClient | None) -> None:
        if value:
            self.view_generation += 1
            self.api_ready = False
            for worker in list(self.workers):
                if worker.node is self and worker.group != "connection-change":
                    worker.cancel()
            await self.stop_followers()
            if self.client:
                await self.client.close()
            while len(self.screen_stack) > 1:
                await self.pop_screen()
            self.client = value
            self.pending_submission = None
            self.submitting = False
            self.feeds.clear()
            self.sessions.clear()
            self.drafts.clear()
            self.repository = ""
            self.session_id = uuid4().hex
            self.initial_session = self.initial_task = self.selected_task = None
            self.history_cursor = self.session_cursor = None
            self.browsing_history = False
            self.usage_record = {}
            self.default_model = ""
            self.model = None
            self.allow_domains = []
            self.reference_paths.clear()
            self.query_one("#references").display = False
            self.query_one("#composer", Composer).load_text("")
            self.dirty = True
            self.connect()

    @work(exclusive=True, group="files")
    async def action_files(self) -> None:
        feed = self.file_feed()
        if not feed:
            self.notify("File suggestions are available after the first workspace checkpoint.")
            return
        try:
            paths: list[str] = []
            cursor = 0
            checkpoint_id = None
            while len(paths) < 2000:
                params: dict[str, Any] = {"cursor": cursor}
                if checkpoint_id:
                    params["checkpoint_id"] = checkpoint_id
                page = await self.client.request(
                    "GET", f"/v1/tasks/{feed.task['id']}/workspace/files", params=params
                )
                checkpoint_id = page["checkpoint_id"]
                paths.extend(page["items"])
                if page.get("next_cursor") is None:
                    break
                cursor = page["next_cursor"]
            task_id = feed.task["id"]

            def picked(path: str | None) -> None:
                if path:
                    self.push_screen(
                        SourceScreen(self.client, task_id, str(checkpoint_id), path),
                        self.insert_reference,
                    )

            self.push_screen(
                Picker(
                    "Checkpoint source files · first 2,000 paths", [(path, path) for path in paths]
                ),
                picked,
            )
        except ClientError as error:
            self.notify(str(error), severity="warning")

    def file_feed(self) -> TaskFeed | None:
        if not self.repository:
            return None
        return next(
            (feed for feed in reversed(self.feeds.values()) if feed.task.get("checkpoint_id")), None
        )

    @on(TextArea.Changed, "#composer")
    def composer_changed(self, event: TextArea.Changed) -> None:
        if not self.is_running or not event.text_area.is_mounted or not self.query("#references"):
            return
        match = re.search(r"@([^\s@]*)$", event.text_area.text)
        if match and self.file_feed():
            self.suggest_files(match[1], self.view_generation)
        else:
            self.query_one("#references").display = False

    @work(exclusive=True, group="suggestions")
    async def suggest_files(self, query: str, generation: int) -> None:
        await asyncio.sleep(0.2)
        feed = self.file_feed()
        if not feed:
            return
        try:
            page = await self.client.request(
                "GET", f"/v1/tasks/{feed.task['id']}/workspace/files", params={"query": query}
            )
            if generation != self.view_generation:
                return
            self.reference_paths = page["items"][:6]
            options = self.query_one("#references", OptionList)
            options.clear_options().add_options(
                [Option(Text(path)) for path in self.reference_paths]
            )
            options.display = bool(self.reference_paths)
            if self.reference_paths:
                options.highlighted = 0
        except ClientError:
            self.query_one("#references").display = False

    @on(OptionList.OptionSelected, "#references")
    def reference_selected(self, event: OptionList.OptionSelected) -> None:
        self.insert_reference(self.reference_paths[event.option_index])

    def insert_reference(self, path: str | None) -> None:
        if path:
            composer = self.query_one("#composer", Composer)
            text = composer.text
            reference = f"@{path}" if not any(char.isspace() for char in path) else f'@"{path}"'
            match = re.search(r"@([^\s@]*)$", text)
            composer.load_text(
                text[: match.start()] + reference + " "
                if match
                else text + (" " if text else "") + reference + " "
            )
            composer.move_cursor(composer.document.end)
            self.query_one("#references").display = False
            composer.focus()

    def on_key(self, event: events.Key) -> None:
        if not self.is_running or not self.query("#references"):
            return
        options = self.query_one("#references", OptionList)
        if options.display and self.query_one("#composer", Composer).has_focus:
            if event.key == "tab" and self.reference_paths:
                event.stop()
                event.prevent_default()
                self.insert_reference(self.reference_paths[options.highlighted or 0])
            elif event.key == "escape":
                options.display = False
                event.stop()
            elif event.key in {"up", "down"}:
                event.stop()
                event.prevent_default()
                options.action_cursor_down() if event.key == "down" else options.action_cursor_up()

    @on(Markdown.LinkClicked)
    def link_clicked(self, event: Markdown.LinkClicked) -> None:
        url = event.href
        try:
            parsed = urlsplit(url)
        except ValueError:
            self.notify("This link has an invalid address.", severity="warning")
            return
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or terminal_text(url) != url
        ):
            self.notify("Only explicit HTTP/HTTPS links can be opened.", severity="warning")
            return
        self.push_screen(
            Picker("Open external link", [(url, url), ("Cancel", "")]),
            self.open_external,
        )

    def open_external(self, value: str | None) -> None:
        if value:
            webbrowser.open(value)

    async def on_unmount(self) -> None:
        await self.stop_followers()
        if self.client:
            await self.client.close()
