"""Cross-boundary reproductions from the 2026-10-29 ECC audit."""

import asyncio
import json
import shlex
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from urllib.error import URLError
from uuid import uuid4

import pytest
from sqlalchemy import update

from merlin.agent import AgentToolExecutor
from merlin.browser._remote import BrowserSession
from merlin.contracts import Checkpoint, ModelReply, TaskSpec, TaskState, ToolCall, ToolResult
from merlin.integrations.github import GitHubService, PublishFile
from merlin.lsp import validate_arguments
from merlin.persistence import SQLTaskStore
from merlin.persistence.models import TaskRow
from merlin.sandbox import E2BSandboxClient, network_configuration, output_preview
from merlin.sandbox._remote import MAX_OUTPUT
from merlin.worker import Worker
from tests.test_integration import MemorySandbox, ScriptedModel
from tests.test_sandbox import FakeSDK


@pytest.fixture
def store():
    value = SQLTaskStore("sqlite:///:memory:")
    value.create_schema()
    yield value
    value.close()


def uncertain_task(store):
    task = store.create_task(TaskSpec(prompt="inspect", repository="owner/repo"))
    lease = store.claim("original")
    call = ToolCall(name="bash", arguments={"command": "change source"})
    result = ToolResult(call_id=call.id, ok=False, uncertain=True, output="Response lost")
    store.begin_tool(lease, call)
    store.finish_tool(lease, result)
    store.update_task(
        lease,
        state=TaskState.PAUSED,
        history=[
            {"role": "model", "parts": [], "_merlin_calls": [call.model_dump()]},
            {
                "role": "user",
                "parts": [{"function_response": {"id": call.id, "response": result.model_dump()}}],
            },
        ],
    )
    return task, call


def test_resume_requires_resolving_a_persisted_uncertain_result(store):
    task, call = uncertain_task(store)
    with pytest.raises(ValueError, match="[Uu]ncertain|resolve"):
        store.resume(task.id)
    assert store.get_task(task.id).state == TaskState.PAUSED
    resolved = store.resolve_tool(task.id, call.id, "Inspected external effects")
    assert resolved.state == TaskState.QUEUED
    assert not store.invocations(task.id)[0].result.uncertain


async def test_worker_refuses_uncertainty_even_if_a_task_was_already_requeued(store):
    task, _ = uncertain_task(store)
    with store.engine.begin() as connection:
        connection.execute(update(TaskRow).where(TaskRow.id == task.id).values(state="queued"))
    sandbox = MemorySandbox()
    model = ScriptedModel([ModelReply(text="Must not run")])
    await Worker(store, model, sandbox, AgentToolExecutor(sandbox)).run_once()
    assert store.get_task(task.id).state == TaskState.PAUSED
    assert model.calls == 0
    assert sandbox.checkpoints == []


async def test_model_cannot_dispatch_internal_sandbox_delta_operations(store):
    task = store.create_task(TaskSpec(prompt="inspect", repository="owner/repo"))
    sandbox = MemorySandbox()
    call = ToolCall(name="apply_delta", arguments={"changes": []})
    model = ScriptedModel([ModelReply(calls=[call]), ModelReply(text="Finished")])
    await Worker(store, model, sandbox, AgentToolExecutor(sandbox)).run_once()
    assert sandbox.calls == []
    invocation = store.invocations(task.id)[0]
    assert invocation.call == call and invocation.result.ok is False


@pytest.mark.parametrize("path", ["credentials/token.py", ".env.local/settings.ts", ".aws/key.py"])
def test_lsp_cannot_bypass_file_tool_secret_policy(path):
    with pytest.raises(ValueError, match="[Ss]ecret|[Pp]ath"):
        validate_arguments("lsp_hover", {"path": path})


@pytest.mark.parametrize("path", [".env", ".aws/credentials", "nested/id_rsa", "secrets/key.pem"])
def test_publishing_rejects_secret_files_before_obtaining_write_credentials(path):
    with pytest.raises(ValueError):
        GitHubService._files([PublishFile(path=path, content="private material")])
    assert GitHubService._files([PublishFile(path=".env.example", content="EXAMPLE=")])


def test_browser_websockets_are_intercepted_before_pages_can_connect():
    playwright = Mock()
    context = playwright.chromium.launch.return_value.new_context.return_value
    BrowserSession(playwright, {"allowed_domains": ["docs.example.com"]})
    context.route_web_socket.assert_called_once()
    pattern, handler = context.route_web_socket.call_args.args
    assert pattern == "**/*"
    socket = Mock(url="wss://docs.example.com/write")
    handler(socket)
    socket.on_message.assert_called_once()
    socket.on_message.call_args.args[0]("must not be forwarded")
    socket.send.assert_not_called()
    socket.connect_to_server.assert_not_called()


@pytest.mark.parametrize("host", ["localhost.", "LOCALHOST.", "127.0.0.1."])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_public_page_cannot_bypass_origin_checks_with_a_trailing_dot(host, method):
    session = BrowserSession.__new__(BrowserSession)
    session.domains = ["docs.example.com"]
    session.entries = []
    session.page = Mock(url="https://docs.example.com/untrusted")
    request = SimpleNamespace(
        url=f"http://{host}:3000/private",
        method=method,
        is_navigation_request=lambda: False,
        redirected_from=None,
        frame=SimpleNamespace(page=session.page, url=session.page.url, parent_frame=None),
    )
    route = Mock(request=request)
    session.route(route)
    route.abort.assert_called_once()
    route.continue_.assert_not_called()


def test_session_repository_is_enforced_inside_the_store(store):
    store.create_task(TaskSpec(prompt="one", repository="owner/first", session_id="same"))
    with pytest.raises(ValueError, match="[Ss]ession.*repositor"):
        store.create_task(TaskSpec(prompt="two", repository="owner/other", session_id="same"))
    assert len(store.list_tasks("same")) == 1


@pytest.mark.parametrize("failure", [RuntimeError("invalid archive"), asyncio.CancelledError()])
async def test_failed_or_cancelled_restore_closes_the_new_workspace(store, failure):
    task = store.create_task(TaskSpec(prompt="restore", repository="owner/repo"))
    lease = store.claim("previous")
    store.save_checkpoint(lease, Checkpoint(task_id=task.id, artifact="old.tar", sha256="0" * 64))
    store.update_task(lease, state=TaskState.PAUSED)
    store.resume(task.id)
    sandbox = MemorySandbox()
    sandbox.restore = AsyncMock(side_effect=failure)
    model = ScriptedModel([ModelReply(calls=[ToolCall(name="list_files")])])
    worker = Worker(store, model, sandbox, AgentToolExecutor(sandbox))
    if isinstance(failure, RuntimeError):
        await worker.run_once()
    else:
        with pytest.raises(type(failure)):
            await worker.run_once()
    assert sandbox.closed == [task.id]


async def test_large_tool_payload_is_bounded_and_full_output_is_an_artifact(tmp_path):
    from merlin.config import Settings
    from merlin.contracts import Task

    sdk = FakeSDK(tmp_path)
    settings = Settings(e2b_api_key="fixture", artifact_dir=tmp_path / "artifacts")
    client = E2BSandboxClient(settings, sdk=sdk)
    workspace = await client.create(Task(spec=TaskSpec(prompt="run", repository="owner/repo")))
    output = "x" * (MAX_OUTPUT + 1000)
    sdk.commands.run_state = {"output": output, "result": [output], "exit_code": 0}
    result = await client.execute(workspace, ToolCall(name="bash", arguments={"command": "test"}))
    assert result.ok and len(json.dumps(result.output)) <= MAX_OUTPUT
    assert len(json.dumps(result.data).encode()) <= MAX_OUTPUT
    assert result.data["truncated"]
    assert any((settings.artifact_dir / name).read_text() == output for name in result.artifacts)


@pytest.mark.parametrize("output", ["अ" * 20000, "\n" * 40000, "🙂" * 10000])
def test_output_context_bound_includes_unicode_and_control_character_escaping(output):
    preview = output_preview(output)
    assert output.startswith(preview)
    assert len(json.dumps(preview)) <= MAX_OUTPUT
    assert len(json.dumps(preview + output[len(preview)])) > MAX_OUTPUT


async def test_sdk_login_shell_selects_the_template_helper_interpreter(tmp_path, monkeypatch):
    from merlin.config import Settings
    from merlin.contracts import Task

    binary = tmp_path / "template-bin"
    binary.mkdir()
    shim = binary / "python3"
    shim.write_text("#!/bin/sh\nprintf 'template interpreter selected'\n")
    shim.chmod(0o700)
    monkeypatch.setattr("merlin.sandbox.TEMPLATE_TOOL_BIN", str(binary), raising=False)
    sdk = FakeSDK(tmp_path)
    client = E2BSandboxClient(Settings(e2b_api_key="fixture"), sdk=sdk)
    workspace = await client.create(Task(spec=TaskSpec(prompt="init", repository="owner/repo")))
    workspace.root = str(sdk.commands.root)
    command = await client._command(workspace, "initialize", {})
    # The fake uploader stores files under its fixture directory, rather than /tmp.
    for name in (sdk.files.folder).iterdir():
        command = command.replace("/tmp/" + name.name, shlex.quote(str(name)))
    result = subprocess.run(["bash", "-lc", command], capture_output=True, text=True, timeout=10)
    assert result.stdout == "template interpreter selected"


def test_repository_shell_keeps_template_tools_after_loading_login_profile(tmp_path, monkeypatch):
    from merlin.sandbox import _remote

    binary = tmp_path / "template-bin"
    binary.mkdir()
    shim = binary / "merlin-audit-tool"
    shim.write_text("#!/bin/sh\nprintf 'template tool available'\n")
    shim.chmod(0o700)
    monkeypatch.setattr(_remote, "TEMPLATE_TOOL_BIN", str(binary), raising=False)
    result = _remote.operate(
        {
            "root": str(tmp_path),
            "operation": "run",
            "arguments": {"command": "merlin-audit-tool", "run_id": uuid4().hex, "timeout": 5},
        }
    )
    assert result["exit_code"] == 0
    assert result["output"] == "template tool available"


@pytest.mark.parametrize("configured", [None, "", "/custom/browser-cache"])
def test_browser_daemon_restores_template_cache_before_starting_playwright(
    tmp_path, monkeypatch, configured
):
    from merlin.browser import _remote

    cache = tmp_path / "browsers"
    cache.mkdir()
    monkeypatch.setattr(_remote, "TEMPLATE_BROWSER_DIR", str(cache), raising=False)
    if configured:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", configured)
    else:
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    calls = []

    def start():
        import os

        calls.append(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
        return MagicMock()

    package, api = ModuleType("playwright"), ModuleType("playwright.sync_api")
    api.sync_playwright = start
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    monkeypatch.setattr(_remote, "BrowserSession", Mock())
    monkeypatch.setattr(_remote, "HTTPServer", Mock())
    configuration = tmp_path / "browser.json"
    configuration.write_text(
        json.dumps({"root": str(tmp_path), "allowed_domains": [], "token": "fixture"})
    )
    _remote.serve(str(configuration))
    assert calls == [configured or str(cache)]


@pytest.mark.parametrize("ready_after", [7, None])
def test_browser_cold_start_waits_and_failed_start_is_cleaned_up(
    tmp_path, monkeypatch, ready_after
):
    from pathlib import Path

    from merlin.browser import _remote

    clock = [0.0]
    process = Mock()

    def path(value):
        if str(value).startswith("/tmp/merlin-browser"):
            return tmp_path / Path(value).name
        return Path(value)

    def sleep(seconds):
        clock[0] += seconds

    def request(configuration, operation, arguments, timeout=35):
        if ready_after is None or clock[0] < ready_after:
            raise URLError("Fixture daemon still starting")
        return {"output": "ready"}

    monkeypatch.setattr(_remote, "Path", path)
    monkeypatch.setattr(_remote.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_remote.time, "sleep", sleep)
    monkeypatch.setattr(_remote, "_request", request)
    monkeypatch.setattr(_remote.subprocess, "Popen", Mock(return_value=process))
    invocation = {"root": str(tmp_path), "operation": "browser_read", "arguments": {}}
    if ready_after is not None:
        assert _remote.operate(invocation)["output"] == "ready"
        assert 7 <= clock[0] < 20
        process.terminate.assert_not_called()
    else:
        with pytest.raises(RuntimeError, match="did not start"):
            _remote.operate(invocation)
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        assert not (tmp_path / "merlin-browser.json").exists()


@pytest.mark.parametrize("domain", ["service.lan", "service.home", "-bad.example", "bad-.example"])
def test_sandbox_network_policy_cannot_bypass_api_domain_rules(domain):
    with pytest.raises(ValueError):
        network_configuration([domain])
