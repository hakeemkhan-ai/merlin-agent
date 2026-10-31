"""Terminal client; leaving a client session never cancels hosted work."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
from typing import Annotated, Any
from uuid import uuid4

import httpx
import typer
from rich.console import Console

from merlin.client import (
    ClientError,
    EventDecoder,
    validate_connection,
)
from merlin.client import (
    config_path as config_path,
)
from merlin.client import (
    load_config as _load_config,
)
from merlin.client import (
    save_config as save_config,
)
from merlin.client import (
    terminal_text as terminal_text,
)

app = typer.Typer(help="Merlin: a durable terminal coding agent.", no_args_is_help=False)
github_app = typer.Typer(help="Connect and inspect the GitHub App installation.")
app.add_typer(github_app, name="github")
console = Console()


def interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


@app.callback(invoke_without_command=True)
def main(context: typer.Context) -> None:
    """Open the terminal interface, or use a command for scripting."""
    if context.invoked_subcommand is None:
        if interactive_terminal():
            from merlin.tui import launch

            launch()
        else:
            typer.echo(context.get_help())
            raise typer.Exit(2)


@app.command("ui")
def ui(
    repo: str | None = typer.Option(None, "--repo"),
    session: str | None = typer.Option(None, "--session"),
    task: str | None = typer.Option(None, "--task"),
    demo: bool = typer.Option(
        False, "--demo", help="Try the interface offline with synthetic data"
    ),
) -> None:
    """Open Merlin's full-screen terminal interface."""
    if task and (repo or session):
        raise typer.BadParameter("Use --task on its own; its repository/session are inferred")
    if not interactive_terminal():
        raise typer.BadParameter(
            "The interface requires a terminal; use run/attach --json for scripts"
        )
    from merlin.tui import launch

    launch(repo=repo, session=session, task=task, demo=demo)


class TaskOutput:
    """Concise progress and streamed text; durable JSON events stay unchanged."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.partial = ""
        self.last_reply: str | None = None
        self.line_open = False
        self.workspace_ready = False

    def end_line(self) -> None:
        if self.line_open:
            typer.echo()
            self.line_open = False

    def status(self, message: str) -> None:
        self.end_line()
        typer.echo(terminal_text(message).replace("\n", " ").replace("\t", " ")[:300])

    def reply(self, text: str) -> None:
        text = terminal_text(text)
        if text.startswith(self.partial):
            remaining = text[len(self.partial) :]
        else:
            self.end_line()
            remaining = text
        if remaining:
            typer.echo(remaining, nl=False)
            self.line_open = not remaining.endswith("\n")
        self.end_line()
        self.partial = ""
        self.last_reply = text

    def event(self, event: dict[str, Any]) -> None:
        kind, data = event["kind"], event["data"]
        if kind == "model.delta":
            text = terminal_text(data["text"])
            if text:
                typer.echo(text, nl=False)
                self.partial += text
                self.line_open = not text.endswith("\n")
        elif kind == "model.reply":
            self.reply(data.get("text", ""))
        elif kind == "task.completion" and data.get("verification") == "not_recorded":
            self.status("No verification command result was recorded after the last edit.")
        elif kind == "task.created":
            self.status("Task queued.")
        elif kind == "task.claimed":
            self.status("Recovering task..." if data.get("reclaimed") else "Thinking...")
        elif kind == "checkpoint.saved" and not self.workspace_ready:
            self.workspace_ready = True
            self.status("Workspace ready.")
        elif kind == "tool.started":
            call = data["call"]
            name, args = call["name"], call.get("arguments", {})
            if name == "read_file":
                label = f"Reading {args.get('path', 'file')}"
                if args.get("end_line") is not None:
                    label += f" (lines {args.get('start_line', 1)}–{args['end_line']})"
                self.status(label)
            elif name == "list_files":
                self.status("Listing repository files...")
            elif name == "search":
                self.status("Searching repository...")
            else:
                self.status(f"Using {name}...")
        elif kind == "tool.finished" and not data.get("ok", True):
            result = data.get("result", {})
            self.status("Tool failed: " + str(result.get("output", "Inspect task for details")))
        elif kind == "approval.requested":
            self.status(f"Approval required. Review with: merlin inspect {self.task_id}")

    def snapshot(self, task: dict[str, Any]) -> None:
        result = task.get("result")
        if result and terminal_text(result) != self.last_reply:
            self.reply(result)
        self.status(f"Task {task['state']}")
        if task.get("error"):
            self.end_line()
            typer.echo(terminal_text(task["error"]))


def load_config() -> dict[str, str]:
    try:
        return _load_config()
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


def api_client() -> httpx.Client:
    configuration = load_config()
    token = os.environ.get("MERLIN_OWNER_TOKEN", configuration.get("owner_token", ""))
    url = os.environ.get("MERLIN_API_URL", configuration.get("api_url", "http://127.0.0.1:8000"))
    if not token:
        raise typer.BadParameter("Run 'merlin login' or set MERLIN_OWNER_TOKEN")
    try:
        url = validate_connection(url)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    return httpx.Client(
        base_url=url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(30, read=60),
    )


def request(method: str, path: str, **kwargs: Any) -> Any:
    try:
        with api_client() as client:
            response = client.request(method, path, **kwargs)
            if response.is_error:
                try:
                    detail = response.json().get("detail", "Request failed")
                except ValueError:
                    detail = "Request failed"
                typer.echo(terminal_text(f"Merlin API {response.status_code}: {detail}"), err=True)
                raise typer.Exit(1)
            return response.json()
    except httpx.HTTPError as error:
        typer.echo("Cannot reach Merlin. Check the server URL and connection.", err=True)
        raise typer.Exit(1) from error


def show(value: Any, as_json: bool = False) -> None:
    if as_json:
        typer.echo(json.dumps(value, default=str))
    else:
        console.print_json(data=value)


@app.command()
def login(url: str = typer.Option("http://127.0.0.1:8000", "--url")) -> None:
    """Save an owner credential with mode 0600; obtain it from your server operator."""
    token = os.environ.get("MERLIN_OWNER_TOKEN") or typer.prompt("Owner token", hide_input=True)
    if len(token) < 32:
        raise typer.BadParameter("Owner token must contain at least 32 characters")
    try:
        url = validate_connection(url)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    save_config(url, token)
    typer.echo("Saved Merlin connection.")


@app.command("run")
def run_task(
    prompt: str,
    repo: str | None = typer.Option(None, "--repo"),
    session: str | None = typer.Option(None, "--session"),
    permission: Annotated[list[str] | None, typer.Option("--permission")] = None,
    allow_domain: Annotated[list[str] | None, typer.Option("--allow-domain")] = None,
    model: str | None = typer.Option(None, "--model"),
    detach: bool = typer.Option(False, "--detach"),
    as_json: bool = typer.Option(False, "--json"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
) -> None:
    """Submit a task; follow it unless --detach is supplied."""
    if idempotency_key is not None and not 1 <= len(idempotency_key) <= 200:
        raise typer.BadParameter("Idempotency key must contain one to200 characters")
    body: dict[str, Any] = {
        "prompt": prompt,
        "repository": repo,
        "session_id": session
        or (
            hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
            if idempotency_key
            else uuid4().hex
        ),
        "permissions": permission or [],
        "allowed_domains": allow_domain or [],
    }
    if model:
        body["model"] = model
    task = request(
        "POST", "/v1/tasks", json=body, headers={"Idempotency-Key": idempotency_key or uuid4().hex}
    )
    if detach:
        show(task, as_json)
    else:
        if not as_json:
            typer.echo(f"Task {task['id']} — terminal disconnects leave it running.")
        attach(task["id"], after=0, as_json=as_json)


@app.command()
def tasks(
    session: str | None = typer.Option(None, "--session"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List tasks, optionally restricted to one session."""
    show(request("GET", "/v1/tasks", params={"session_id": session} if session else {}), as_json)


@app.command()
def inspect(task_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Show task state, usage, and outstanding approvals."""
    show(
        {
            "task": request("GET", f"/v1/tasks/{task_id}"),
            "usage": request("GET", f"/v1/tasks/{task_id}/usage"),
            "approvals": request("GET", f"/v1/tasks/{task_id}/approvals"),
        },
        as_json,
    )


@app.command()
def attach(
    task_id: str,
    after: int = typer.Option(0, "--after", min=0),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Follow persisted task events; Ctrl-C detaches without cancelling."""
    cursor = after
    failures = 0
    output = TaskOutput(task_id)
    try:
        while True:
            try:
                with (
                    api_client() as client,
                    client.stream(
                        "GET",
                        f"/v1/tasks/{task_id}/events",
                        headers={"Last-Event-ID": str(cursor)},
                    ) as response,
                ):
                    response.raise_for_status()
                    decoder = EventDecoder()
                    for line in response.iter_lines():
                        if value := decoder.feed(line):
                            if as_json:
                                typer.echo(json.dumps(value))
                            else:
                                output.event(value)
                            cursor = value["sequence"]
                task = request("GET", f"/v1/tasks/{task_id}")
                if as_json:
                    typer.echo(json.dumps({"kind": "task.snapshot", "task": task}))
                else:
                    output.snapshot(task)
                if task["state"] in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "paused",
                    "waiting_for_approval",
                }:
                    if task["state"] in {"failed", "cancelled"}:
                        raise typer.Exit(1)
                    return
                failures = 0
            except (httpx.HTTPError, ValueError, ClientError) as error:
                failures += 1
                if failures >= 3:
                    typer.echo(
                        f"Disconnected. Reattach with: merlin attach {task_id} --after {cursor}",
                        err=True,
                    )
                    raise typer.Exit(1) from error
                time.sleep(0.5 * failures)
    except KeyboardInterrupt:
        output.end_line()
        typer.echo(
            f"Detached. Task continues; reattach with: merlin attach {task_id} --after {cursor}"
        )


@app.command()
def approve(
    approval_id: str,
    reject: bool = typer.Option(False, "--reject"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Approve or reject a specific reviewed publishing operation."""
    value = request("POST", f"/v1/approvals/{approval_id}", json={"approved": not reject})
    if as_json:
        show(value, True)
    else:
        typer.echo(terminal_text(f"Approval {value['status']}."))
        typer.echo(terminal_text(f"Task: {value['task_id']}"))


@app.command("request-changes")
def request_changes(
    approval_id: str,
    note: str = typer.Option(..., "--note", help="Corrections required before publishing"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Send feedback on a pending proposal; a revised operation needs fresh approval."""
    value = request("POST", f"/v1/approvals/{approval_id}/changes", json={"note": note})
    if as_json:
        show(value, True)
    else:
        typer.echo("Changes requested. Task queued for revision.")
        typer.echo(terminal_text(f"Follow with: merlin attach {value['task_id']}"))


@app.command()
def cancel(task_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Request cancellation of a task and its children."""
    show(request("POST", f"/v1/tasks/{task_id}/cancel"), as_json)


@app.command()
def resume(task_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Resume a paused task from its durable state."""
    show(request("POST", f"/v1/tasks/{task_id}/resume"), as_json)


@app.command("budget")
def amend_budget(
    task_id: str,
    max_calls: int = typer.Option(..., "--max-calls", min=1, max=1000),
    max_cost_usd: float = typer.Option(..., "--max-cost-usd", min=0, max=1000),
    max_sandbox_seconds: int = typer.Option(..., "--max-sandbox-seconds", min=1, max=86400),
    note: str = typer.Option(..., "--note"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Increase a paused root task's budget; resume remains an explicit action."""
    show(
        request(
            "POST",
            f"/v1/tasks/{task_id}/budget",
            json={
                "budget": {
                    "max_calls": max_calls,
                    "max_cost_usd": max_cost_usd,
                    "max_sandbox_seconds": max_sandbox_seconds,
                },
                "note": note,
            },
        ),
        as_json,
    )


@app.command("reservations")
def reservations(task_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Inspect outstanding model usage, including unknown provider outcomes."""
    show(request("GET", f"/v1/tasks/{task_id}/reservations"), as_json)


@app.command("resolve-usage")
def resolve_usage(
    task_id: str,
    reservation_id: str,
    actual_cost_usd: float = typer.Option(..., "--actual-cost-usd", min=0),
    note: str = typer.Option(..., "--note"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Reconcile usage after checking provider billing; never repeats a request."""
    show(
        request(
            "POST",
            f"/v1/tasks/{task_id}/reservations/{reservation_id}/resolve",
            json={"actual_cost_usd": actual_cost_usd, "note": note},
        ),
        as_json,
    )


@app.command()
def resolve(
    task_id: str,
    call_id: str,
    note: str = typer.Option(..., "--note"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Acknowledge an uncertain tool; restore the last completed checkpoint.

    Inspect external effects first: acknowledgement does not undo them and does
    not mark the operation successful or replay it.
    """
    show(
        request(
            "POST",
            f"/v1/tasks/{task_id}/tools/{call_id}/resolve",
            json={"note": note},
        ),
        as_json,
    )


@app.command()
def chat(
    repo: str | None = typer.Option(None, "--repo"),
    session: str | None = typer.Option(None, "--session"),
    allow_domain: Annotated[list[str] | None, typer.Option("--allow-domain")] = None,
    plain: bool = typer.Option(False, "--plain", help="Use the original line-based conversation"),
) -> None:
    """Interactive chat; /quit detaches and /cancel explicitly stops active work."""
    if not plain and interactive_terminal():
        from merlin.tui import launch

        launch(repo=repo, session=session, allow_domains=allow_domain or [])
        return
    from prompt_toolkit import PromptSession

    prompt_session: PromptSession[str] = PromptSession()
    session_id = session or uuid4().hex
    latest: str | None = None
    typer.echo(f"Merlin session {session_id}; /quit to detach, /cancel to cancel the latest task.")
    while True:
        try:
            message = prompt_session.prompt("merlin> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if message in {"/quit", "/exit"}:
            return
        if message == "/cancel":
            if latest:
                cancel(latest, as_json=False)
            continue
        if not message:
            continue
        task = request(
            "POST",
            "/v1/tasks",
            json={
                "prompt": message,
                "repository": repo,
                "session_id": session_id,
                "allowed_domains": allow_domain or [],
            },
            headers={"Idempotency-Key": uuid4().hex},
        )
        latest = str(task["id"])
        try:
            attach(latest, after=0, as_json=False)
        except typer.Exit:
            continue


@github_app.command("connect")
def github_connect() -> None:
    """Print the GitHub authorization URL for this server's App installation."""
    value = request("POST", "/v1/github/connect")
    typer.echo(value["authorization_url"])


@github_app.command("status")
def github_status(as_json: bool = typer.Option(False, "--json")) -> None:
    show(request("GET", "/v1/github/status"), as_json)


@github_app.command("repos")
def github_repos(as_json: bool = typer.Option(False, "--json")) -> None:
    show(request("GET", "/v1/github/repositories"), as_json)


@app.command()
def doctor() -> None:
    """Check the connection and show GitHub configuration without revealing secrets."""
    value = request("GET", "/healthz")
    github = request("GET", "/v1/github/status")
    show({"service": value, "github": github})


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", min=1, max=65535),
) -> None:
    """Run the API. Apply migrations before starting a deployed service."""
    import uvicorn

    uvicorn.run("merlin.api:create_app", host=host, port=port, factory=True)


@app.command("init-db")
def init_db() -> None:
    """Initialize a development database; deployments use Alembic migrations."""
    from merlin.application import make_store
    from merlin.config import Settings

    store = make_store(Settings(), initialize=True)
    store.close()
    typer.echo("Development database initialized.")


@app.command("new-token")
def new_token() -> None:
    """Generate an owner token for secure server provisioning."""
    typer.echo(secrets.token_urlsafe(32))


@app.command("prune-artifacts")
def prune_artifacts_command(apply: bool = typer.Option(False, "--apply")) -> None:
    """Server administration: preview expired artifacts after stopping API/workers."""
    from merlin.application import make_store
    from merlin.config import Settings
    from merlin.maintenance import prune_artifacts

    settings = Settings()
    store = make_store(settings)
    try:
        show(prune_artifacts(store, settings, apply=apply), as_json=True)
    finally:
        store.close()


__all__ = ["app"]
