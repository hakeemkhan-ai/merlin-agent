"""Keyboard-accessible overlays. All untrusted text is rendered without Rich markup."""

from __future__ import annotations

from typing import Any

from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from merlin.client import ClientError, MerlinClient, save_config, terminal_text


class Picker(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, title: str, items: list[tuple[str, str]], *, custom: bool = False) -> None:
        super().__init__()
        self.heading, self.items, self.custom = title, items, custom
        self.values: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog picker"):
            yield Label(Text(self.heading), classes="dialog-title")
            yield Input(placeholder="Type to search…", id="picker-search")
            yield OptionList(id="picker-options")
            if self.custom:
                yield Button("Use entered model ID", id="picker-custom")
            yield Static("↑ ↓ select · Enter choose · Escape close", classes="hint", markup=False)

    def on_mount(self) -> None:
        self.filter("")
        self.query_one(Input).focus()

    def filter(self, query: str) -> None:
        selected = [
            (label, value)
            for label, value in self.items
            if all(word in label.casefold() for word in query.casefold().split())
        ]
        self.values = [value for _, value in selected]
        options = self.query_one(OptionList)
        options.clear_options().add_options(
            [Option(Text(terminal_text(label))) for label, _ in selected]
        )
        if selected:
            options.highlighted = 0

    @on(Input.Changed)
    def search(self, event: Input.Changed) -> None:
        self.filter(event.value)

    @on(Input.Submitted)
    def choose_first(self) -> None:
        options = self.query_one(OptionList)
        if self.values:
            self.dismiss(self.values[options.highlighted or 0])
        elif self.custom and self.query_one(Input).value.strip():
            self.dismiss(self.query_one(Input).value.strip())

    @on(OptionList.OptionSelected)
    def choose(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.values[event.option_index])

    @on(Button.Pressed, "#picker-custom")
    def custom_value(self) -> None:
        if value := self.query_one(Input).value.strip():
            self.dismiss(value)

    async def on_key(self, event: Any) -> None:
        if event.key in {"up", "down"} and self.query_one(Input).has_focus:
            event.stop()
            event.prevent_default()
            options = self.query_one(OptionList)
            if event.key == "down":
                options.action_cursor_down()
            else:
                options.action_cursor_up()


class NoteScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, heading: str, explanation: str) -> None:
        super().__init__()
        self.heading, self.explanation = heading, explanation

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.heading, classes="dialog-title")
            yield Static(self.explanation, markup=False)
            yield Input(placeholder="Required operator note", max_length=4000, id="note")
            yield Button("Submit note", id="submit-note", variant="primary")

    @on(Button.Pressed, "#submit-note")
    @on(Input.Submitted)
    def submit(self) -> None:
        if value := self.query_one(Input).value.strip():
            self.dismiss(value)


class InfoScreen(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, heading: str, text: str) -> None:
        super().__init__()
        self.heading, self.text = heading, text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog large-dialog"):
            yield Label(Text(self.heading), classes="dialog-title")
            with VerticalScroll():
                yield Static(Text(terminal_text(self.text)))
            yield Button("Close", id="close-info")

    @on(Button.Pressed)
    def close(self) -> None:
        self.dismiss()


class LoginScreen(ModalScreen[MerlinClient | None]):
    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, url: str = "http://127.0.0.1:8000") -> None:
        super().__init__()
        self.url = url

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Connect to Merlin", classes="dialog-title")
            yield Static(
                "Use your server address and owner token. Provider keys stay on the server.",
                markup=False,
            )
            yield Input(value=self.url, placeholder="Server URL", id="login-url")
            yield Input(password=True, placeholder="Owner token", id="login-token")
            yield Static("", id="login-error", classes="error", markup=False)
            yield Button("Connect and save", variant="primary", id="connect-save")

    @on(Button.Pressed, "#connect-save")
    @on(Input.Submitted)
    def connect(self) -> None:
        self.connect_worker()

    @work(exclusive=True)
    async def connect_worker(self) -> None:
        button = self.query_one(Button)
        button.disabled = True
        client = None
        try:
            token = self.query_one("#login-token", Input).value
            if len(token) < 32:
                raise ValueError("Owner token must contain at least 32 characters")
            client = MerlinClient(self.query_one("#login-url", Input).value.strip(), token)
            # Unlike public health, this verifies the owner credential on old servers too.
            await client.request("GET", "/v1/github/status")
            save_config(client.url, token)
            self.query_one("#login-token", Input).value = ""
            self.dismiss(client)
            client = None
        except (ValueError, ClientError, OSError) as error:
            message = (
                str(error)
                if isinstance(error, (ClientError, ValueError))
                else "Could not save the connection configuration."
            )
            self.query_one("#login-error", Static).update(Text(message))
        finally:
            if client is not None:
                await client.close()
            if self.is_mounted:
                button.disabled = False


class ApprovalScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "dismiss(False)", "Close")]

    def __init__(self, client: Any, approval_id: str) -> None:
        super().__init__()
        self.client, self.approval_id = client, approval_id
        self.preview: dict[str, Any] = {}
        self.path: str | None = None
        self.start_line = 1
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog large-dialog"):
            yield Label("Review publishing proposal", classes="dialog-title")
            with Horizontal(classes="controls"):
                yield Button("Choose file", id="proposal-file")
                yield Button("Previous", id="proposal-prev")
                yield Button("Next", id="proposal-next")
                yield Button("Refresh", id="proposal-refresh")
                yield Button("Details", id="proposal-details")
            with VerticalScroll(id="proposal-scroll"):
                yield Static("Loading exact proposal…", id="proposal-meta", markup=False)
                yield Static("", id="proposal-content", markup=False)
            yield Static("", id="proposal-error", classes="error", markup=False)
            with Horizontal(classes="controls"):
                yield Button(
                    "Approve this operation",
                    id="proposal-approve",
                    variant="primary",
                    disabled=True,
                )
                yield Button("Reject", id="proposal-reject", disabled=True)
                yield Button("Request changes", id="proposal-changes", disabled=True)
                yield Button("Close", id="proposal-close")

    def on_mount(self) -> None:
        self.load_preview()

    @work(exclusive=True, group="preview")
    async def load_preview(self) -> None:
        try:
            params: dict[str, Any] = {"start_line": self.start_line}
            if self.path:
                params["path"] = self.path
            self.preview = await self.client.request(
                "GET", f"/v1/approvals/{self.approval_id}/preview", params=params
            )
            data = self.preview
            self.path = data.get("path")
            meta = [
                f"{data['repository']} · {data['operation']} · {data['status']}",
                f"Branch: {data['branch']}"
                + (f" · base {data['base_sha'][:12]}" if data.get("base_sha") else ""),
                data.get("message") or data.get("title", ""),
                f"Proposal {data['digest'][:16]} · {len(data.get('files', []))} file(s)",
            ]
            if self.path:
                meta.append(f"File: {self.path} · {data.get('preview_kind', 'content')}")
            meta.append(
                f"Lines {self.start_line}–"
                f"{min(self.start_line + 199, data.get('total_lines', 0))} "
                f"of {data.get('total_lines', 0)}. "
                + (
                    "Partial preview; more content may remain."
                    if data.get("truncated")
                    else "Complete preview of this item."
                )
            )
            if data.get("notice"):
                meta.append(data["notice"])
            if data.get("long_line_clipped"):
                meta.append("A long line was clipped at the preview size limit.")
            self.query_one("#proposal-meta", Static).update(Text(terminal_text("\n".join(meta))))
            content = terminal_text(data.get("content", ""))
            self.query_one("#proposal-content", Static).update(
                Syntax(content, "diff", background_color="default", word_wrap=True)
                if data.get("preview_kind") == "diff"
                else Text(content)
            )
            self.query_one("#proposal-prev", Button).disabled = self.start_line == 1
            self.query_one("#proposal-next", Button).disabled = not data.get("next_line")
            self.query_one("#proposal-file", Button).disabled = not data.get("files")
            for identifier in ("approve", "reject", "changes"):
                self.query_one(f"#proposal-{identifier}", Button).disabled = (
                    data["status"] != "pending" or self.busy
                )
        except ClientError as error:
            self.query_one("#proposal-error", Static).update(Text(str(error)))

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        identifier = event.button.id
        if identifier == "proposal-close":
            self.dismiss(False)
        elif identifier == "proposal-details":
            details = [
                f"Repository: {self.preview.get('repository', '')}",
                f"Operation: {self.preview.get('operation', '')}",
                f"Branch: {self.preview.get('branch', '')}",
                f"PR base: {self.preview.get('base', '')}",
                f"Base commit: {self.preview.get('base_sha', '')}",
                f"Digest: {self.preview.get('digest', '')}",
                f"Mode: {self.preview.get('mode', '')}",
                f"SHA256: {self.preview.get('sha256', '')}",
                "Commit message / title:\n"
                f"{self.preview.get('message') or self.preview.get('title', '')}",
                "Files:\n" + "\n".join(self.preview.get("files", [])),
            ]
            self.app.push_screen(InfoScreen("Exact proposal details", "\n\n".join(details)))
        elif identifier == "proposal-file":
            self.app.push_screen(
                Picker("Proposed files", [(path, path) for path in self.preview.get("files", [])]),
                self.choose_file,
            )
        elif identifier in {"proposal-prev", "proposal-next", "proposal-refresh"}:
            if identifier == "proposal-prev":
                self.start_line = max(1, self.start_line - 200)
            elif identifier == "proposal-next":
                self.start_line = self.preview.get("next_line") or self.start_line
            self.load_preview()
        elif identifier == "proposal-changes":
            self.app.push_screen(
                NoteScreen(
                    "Request changes", "The revised proposal will require a fresh approval."
                ),
                self.changes,
            )
        elif identifier in {"proposal-approve", "proposal-reject"}:
            self.decide(identifier == "proposal-approve")

    def choose_file(self, path: str | None) -> None:
        if path:
            self.path, self.start_line = path, 1
            self.load_preview()

    def changes(self, note: str | None) -> None:
        if note:
            self.decide(False, note)

    @work(exclusive=True, group="decision")
    async def decide(self, approved: bool, note: str | None = None) -> None:
        if self.busy or not self.preview:
            return
        self.busy = True
        for identifier in ("approve", "reject", "changes"):
            self.query_one(f"#proposal-{identifier}", Button).disabled = True
        try:
            current = await self.client.request("GET", f"/v1/approvals/{self.approval_id}/preview")
            if current["digest"] != self.preview["digest"] or current["status"] != "pending":
                raise ClientError(
                    "This proposal is no longer pending. Refresh to see its current state."
                )
            if note is not None:
                await self.client.request(
                    "POST", f"/v1/approvals/{self.approval_id}/changes", json={"note": note}
                )
            else:
                await self.client.request(
                    "POST", f"/v1/approvals/{self.approval_id}", json={"approved": approved}
                )
            self.dismiss(True)
        except ClientError as error:
            self.query_one("#proposal-error", Static).update(
                Text(str(error) + " Refresh before retrying; no decision was automatically resent.")
            )
        finally:
            self.busy = False
            if self.is_mounted:
                self.load_preview()


class SourceScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, client: Any, task_id: str, checkpoint_id: str, path: str) -> None:
        super().__init__()
        self.client, self.task_id, self.checkpoint_id, self.path = (
            client,
            task_id,
            checkpoint_id,
            path,
        )
        self.start_line = 1
        self.page: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog large-dialog"):
            yield Label("Workspace source preview", classes="dialog-title")
            yield Static(Text(terminal_text(self.path)), id="source-meta")
            with VerticalScroll():
                yield Static("Loading…", id="source-content", markup=False)
            with Horizontal(classes="controls"):
                yield Button("Previous", id="source-prev")
                yield Button("Next", id="source-next")
                yield Button("Add @reference", id="source-reference", variant="primary")
                yield Button("Close", id="source-close")

    def on_mount(self) -> None:
        self.load_file()

    @work(exclusive=True)
    async def load_file(self) -> None:
        try:
            self.page = await self.client.request(
                "GET",
                f"/v1/tasks/{self.task_id}/workspace/file",
                params={
                    "checkpoint_id": self.checkpoint_id,
                    "path": self.path,
                    "start_line": self.start_line,
                },
            )
            data = self.page
            end_line = min(self.start_line + 199, data.get("total_lines", 0))
            notice = (
                "Clipped long line: 64 KiB preview limit.\n"
                if data.get("long_line_clipped")
                else ""
            )
            self.query_one("#source-meta", Static).update(
                Text(
                    terminal_text(
                        f"{notice}{self.path}\nCheckpoint {self.checkpoint_id[:12]} · "
                        f"{data.get('created_at', '')}\n"
                        f"Lines {self.start_line}–{end_line} of {data.get('total_lines', 0)}"
                    )
                )
            )
            if data.get("binary"):
                content: Any = Text("Binary file; text preview unavailable.")
            else:
                content = Syntax(
                    terminal_text(data.get("content", "")),
                    Syntax.guess_lexer(self.path, data.get("content", "")),
                    line_numbers=True,
                    start_line=self.start_line,
                    background_color="default",
                    word_wrap=True,
                )
            self.query_one("#source-content", Static).update(content)
            self.query_one("#source-prev", Button).disabled = self.start_line == 1
            self.query_one("#source-next", Button).disabled = not data.get("next_line")
        except ClientError as error:
            self.query_one("#source-content", Static).update(Text(str(error)))

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "source-reference":
            self.dismiss(self.path)
        elif event.button.id == "source-close":
            self.dismiss(None)
        elif event.button.id in {"source-prev", "source-next"}:
            self.start_line = (
                max(1, self.start_line - 200)
                if event.button.id == "source-prev"
                else self.page.get("next_line") or self.start_line
            )
            self.load_file()
