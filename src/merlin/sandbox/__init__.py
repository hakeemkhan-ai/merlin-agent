"""E2B-only workspace execution and independent durable checkpoints."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import tarfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from merlin.config import Settings
from merlin.contracts import (
    Checkpoint,
    CheckpointUnavailable,
    Task,
    ToolCall,
    ToolResult,
    Workspace,
    WorkspaceSetupError,
)
from merlin.network import validate_domains
from merlin.sandbox._remote import (
    MAX_ARCHIVE_BYTES,
    MAX_OUTPUT,
    TEMPLATE_TOOL_BIN,
    validate_archive,
)


def checkpoint_connection_error(error: Exception) -> bool:
    """Recognize network interruptions from E2B's HTTP and optional Rust transports."""
    import httpx
    from e2b.exceptions import TimeoutException

    if isinstance(error, (httpx.TransportError, ConnectionError, TimeoutError, TimeoutException)):
        return True
    try:
        from pyqwest import ReadError, StreamError, WriteError
    except ImportError:
        return False
    return isinstance(error, (ReadError, StreamError, WriteError))


def output_preview(output: str) -> str:
    """Bound the same JSON representation used to measure provider context."""
    if len(json.dumps(output)) <= MAX_OUTPUT:
        return output
    low, high = 0, min(len(output), MAX_OUTPUT)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(output[:middle])) <= MAX_OUTPUT:
            low = middle
        else:
            high = middle - 1
    return output[:low]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BashArguments(Arguments):
    command: str = Field(min_length=1, max_length=100000)
    cwd: str = "."
    timeout: int = Field(default=120, ge=1, le=600)
    background: bool = False


class ProcessArguments(Arguments):
    pid: int = Field(ge=1)
    stdout_offset: int = Field(default=0, ge=0, le=2**53)
    stderr_offset: int = Field(default=0, ge=0, le=2**53)
    tail: bool = False


class ReadArguments(Arguments):
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class WriteArguments(Arguments):
    path: str
    content: str
    expected_sha256: str | None = None
    create_only: bool = False


class ReplaceArguments(Arguments):
    path: str
    old: str = Field(min_length=1)
    new: str
    expected_count: int = Field(default=1, ge=1)
    expected_sha256: str | None = None


class SearchArguments(Arguments):
    query: str
    path: str = "."
    glob: str | None = None
    cursor: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=2000, ge=1, le=2000)


class ListArguments(Arguments):
    path: str = "."
    cursor: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=2000, ge=1, le=2000)


FILE_ARGUMENTS: dict[str, type[Arguments]] = {
    "read_file": ReadArguments,
    "write_file": WriteArguments,
    "replace_text": ReplaceArguments,
    "search": SearchArguments,
    "list_files": ListArguments,
}


def network_configuration(domains: list[str]) -> dict[str, Any]:
    """Fail closed: allow exact public hostnames only, never arbitrary CIDRs."""
    return {
        "deny_out": ["0.0.0.0/0"],
        # The API accepts fifty domains; GitHub cloning adds one service host.
        "allow_out": validate_domains(domains, limit=51),
    }


class E2BSandboxClient:
    """No host subprocess fallback. The SDK factory is injectable for tests only."""

    def __init__(self, settings: Settings, sdk: Any = None) -> None:
        self.settings = settings
        self._sdk = sdk
        self._instances: dict[str, Any] = {}
        self._processes: dict[tuple[str, int], str] = {}
        self._domains: dict[str, list[str]] = {}
        self._source = Path(__file__).with_name("_remote.py").read_text()
        self._sources = {
            "files": self._source,
            "browser": Path(__file__).parent.parent.joinpath("browser", "_remote.py").read_text(),
            "lsp": Path(__file__).parent.parent.joinpath("lsp", "_remote.py").read_text(),
        }
        self._helper_paths: dict[tuple[str, str], str] = {}
        self._helper_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._lsp_dirty: set[str] = set()
        self._lsp_locks: dict[str, asyncio.Lock] = {}

    def _factory(self) -> Any:
        if self._sdk is None:
            try:
                from e2b import AsyncSandbox
            except ImportError as error:
                raise RuntimeError("Install Merlin's locked E2B SDK dependency") from error
            self._sdk = AsyncSandbox
        if not self.settings.e2b_api_key.get_secret_value():
            raise ValueError("MERLIN_E2B_API_KEY is required")
        return self._sdk

    def _forget(self, workspace_id: str) -> None:
        self._instances.pop(workspace_id, None)
        self._domains.pop(workspace_id, None)
        self._lsp_dirty.discard(workspace_id)
        self._lsp_locks.pop(workspace_id, None)
        for key in [key for key in self._helper_paths if key[0] == workspace_id]:
            self._helper_paths.pop(key, None)
            self._helper_locks.pop(key, None)
        self._processes = {
            key: value for key, value in self._processes.items() if key[0] != workspace_id
        }

    async def _instance(self, workspace: Workspace) -> Any:
        if workspace.id not in self._instances:
            await self.connect(workspace.id)
        return self._instances[workspace.id]

    async def _command(
        self, workspace: Workspace, operation: str, arguments: dict[str, Any]
    ) -> str:
        instance = await self._instance(workspace)
        family = (
            "browser"
            if operation.startswith("browser_")
            else "lsp"
            if operation.startswith("lsp_")
            else "files"
        )
        source = self._sources[family]
        fingerprint = hashlib.sha256(source.encode()).hexdigest()
        invocation = uuid4().hex
        key = (workspace.id, family)
        async with self._helper_locks.setdefault(key, asyncio.Lock()):
            if key not in self._helper_paths:
                script = f"/tmp/merlin-helper-{fingerprint[:16]}-{invocation}.py"
                await instance.files.write(script, source)
                self._helper_paths[key] = script
        script = self._helper_paths[key]
        request = f"/tmp/merlin-request-{invocation}.json"
        await instance.files.write(
            request,
            json.dumps({"root": workspace.root, "operation": operation, "arguments": arguments}),
        )
        # E2B invokes commands via bash -l, which can reset Docker's PATH. Set
        # the template tools path inside that shell so Python resolves to the
        # environment containing Playwright/Pyright. Custom templates without
        # this directory retain their usual python3 fallback.
        # Read once, verify, then execute the verified bytes. A repository process
        # cannot replace a cached helper between the check and execution.
        loader = (
            "import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "r=pathlib.Path(sys.argv[2]); "
            "b=p.read_bytes(); "
            f"assert hashlib.sha256(b).hexdigest()=={fingerprint!r}, 'Helper checksum mismatch'; "
            "sys.argv=sys.argv[1:]\n"
            "try:\n"
            " exec(compile(b,str(p),'exec'),{'__name__':'__main__','__file__':str(p)})\n"
            "finally:\n"
            " r.unlink(missing_ok=True)"
        )
        return (
            f"PATH={shlex.quote(TEMPLATE_TOOL_BIN)}:$PATH python3 -I -c {shlex.quote(loader)} "
            + shlex.quote(script)
            + " "
            + shlex.quote(request)
        )

    async def _helper(
        self, workspace: Workspace, operation: str, arguments: dict[str, Any], timeout: int = 60
    ) -> dict[str, Any]:
        instance = await self._instance(workspace)
        result = await instance.commands.run(
            await self._command(workspace, operation, arguments),
            timeout=timeout,
            request_timeout=timeout + 10,
        )
        try:
            response = json.loads(result.stdout)
        except (ValueError, AttributeError) as error:
            raise RuntimeError("Invalid response from sandbox helper") from error
        if not response.get("ok"):
            raise ValueError(response.get("error", "Sandbox operation failed"))
        return dict(response["data"])

    async def create(self, task: Task) -> Workspace:
        network = network_configuration(["github.com", *task.spec.allowed_domains])
        instance = await self._factory().create(
            template=self.settings.e2b_template,
            api_key=self.settings.e2b_api_key.get_secret_value(),
            timeout=min(task.spec.budget.max_sandbox_seconds, 3600),
            metadata={"merlin_task_id": task.id, "merlin_generation": str(task.generation)},
            network=network,
            lifecycle={"on_timeout": "kill"},
        )
        workspace = Workspace(id=instance.sandbox_id)
        self._instances[workspace.id] = instance
        try:
            await self._helper(workspace, "initialize", {})
            self._domains[workspace.id] = network["allow_out"]
            await instance.files.write(
                "/tmp/merlin-network-policy.json", json.dumps(network["allow_out"])
            )
        except BaseException as error:
            with contextlib.suppress(Exception):
                await instance.kill()
            self._forget(workspace.id)
            if isinstance(error, Exception):
                raise WorkspaceSetupError(
                    "initialization", getattr(error, "exit_code", None)
                ) from error
            raise
        return workspace

    async def connect(self, sandbox_id: str) -> Workspace:
        instance = await self._factory().connect(
            sandbox_id, api_key=self.settings.e2b_api_key.get_secret_value()
        )
        self._instances[sandbox_id] = instance
        try:
            policy = await instance.files.read("/tmp/merlin-network-policy.json")
            self._domains[sandbox_id] = network_configuration(json.loads(policy))["allow_out"]
        except Exception:
            self._domains[sandbox_id] = []
        return Workspace(id=sandbox_id)

    async def bootstrap(
        self, workspace: Workspace, repository: str, read_token: str = ""
    ) -> Workspace:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid GitHub repository")
        instance = await self._instance(workspace)
        root = shlex.quote(workspace.root)
        # Token is passed only as this bootstrap command's environment, never in the URL or config.
        helper = (
            '!f() { printf "username=x-access-token\\npassword=%s\\n" '
            '"$MERLIN_GIT_READ_TOKEN"; }; f'
        )
        command = (
            f"rmdir -- {root} && git -c credential.helper={shlex.quote(helper)} "
            f"clone -- https://github.com/{repository}.git {root} "
            f"&& git -C {root} rev-parse HEAD"
        )
        try:
            result = await instance.commands.run(
                command,
                # Templates may default to workspace.root. Removing that directory
                # would invalidate the shell's cwd before Git gets a chance to clone.
                cwd="/tmp",
                envs={"MERLIN_GIT_READ_TOKEN": read_token, "GIT_TERMINAL_PROMPT": "0"},
                timeout=120,
                request_timeout=130,
            )
        except Exception as error:
            raise WorkspaceSetupError(
                "repository_clone", getattr(error, "exit_code", None)
            ) from error
        workspace.base_sha = result.stdout.strip().splitlines()[-1]
        return workspace

    async def execute(self, workspace: Workspace, call: ToolCall) -> ToolResult:
        try:
            if call.name in FILE_ARGUMENTS:
                arguments = (
                    FILE_ARGUMENTS[call.name]
                    .model_validate(call.arguments)
                    .model_dump(exclude_none=True)
                )
                data = await self._helper(workspace, call.name, arguments)
            elif call.name.startswith("browser_"):
                from merlin.browser import validate_arguments

                await self._instance(workspace)
                arguments = validate_arguments(call.name, call.arguments)
                arguments["allowed_domains"] = self._domains.get(workspace.id, [])
                data = await self._helper(workspace, call.name, arguments)
            elif call.name.startswith("lsp_"):
                from merlin.lsp import validate_arguments as validate_lsp_arguments

                arguments = validate_lsp_arguments(call.name, call.arguments)
                async with self._lsp_locks.setdefault(workspace.id, asyncio.Lock()):
                    if workspace.id in self._lsp_dirty:
                        await self._helper(workspace, "lsp_reset", {})
                        self._lsp_dirty.discard(workspace.id)
                    data = await self._helper(workspace, call.name, arguments)
            elif call.name == "apply_delta":
                if set(call.arguments) != {"changes"}:
                    raise ValueError("Invalid delta arguments")
                data = await self._helper(workspace, "apply_delta", call.arguments)
            elif call.name == "bash":
                bash_arguments = BashArguments.model_validate(call.arguments)
                run_id = uuid4().hex
                values = bash_arguments.model_dump(exclude={"background"}) | {"run_id": run_id}
                if bash_arguments.background:
                    await self._helper(workspace, "validate_cwd", {"cwd": bash_arguments.cwd})
                    instance = await self._instance(workspace)
                    handle = await instance.commands.run(
                        await self._command(workspace, "run", values),
                        background=True,
                        timeout=bash_arguments.timeout + 5,
                        request_timeout=30,
                    )
                    pid = int(handle.pid)
                    self._processes[(workspace.id, pid)] = run_id
                    await instance.files.write(
                        f"/tmp/merlin-process-{pid}.json", json.dumps({"run_id": run_id})
                    )
                    data = {"pid": pid, "state": "running", "output": "Background process started"}
                else:
                    data = await self._helper(
                        workspace, "run", values, timeout=bash_arguments.timeout + 5
                    )
            elif call.name in {"poll_process", "terminate_process"}:
                process_arguments = ProcessArguments.model_validate(call.arguments)
                instance = await self._instance(workspace)
                run = self._processes.get((workspace.id, process_arguments.pid))
                if run is None:
                    marker = await instance.files.read(
                        f"/tmp/merlin-process-{process_arguments.pid}.json"
                    )
                    run = str(json.loads(marker)["run_id"])
                operation = "poll_run" if call.name == "poll_process" else "terminate_run"
                data = await self._helper(
                    workspace,
                    operation,
                    {
                        **process_arguments.model_dump(exclude={"pid"}),
                        "run_id": run,
                    },
                )
                data["pid"] = process_arguments.pid
            else:
                return ToolResult(call_id=call.id, ok=False, output="Unknown sandbox tool")
            changed_path = str(call.arguments.get("path", ""))
            if call.name in {"bash", "apply_delta"} or (
                call.name in {"write_file", "replace_text"}
                and (
                    Path(changed_path).name
                    in {"pyproject.toml", "pyrightconfig.json", "package.json", "package-lock.json"}
                    or Path(changed_path).name.startswith("tsconfig")
                )
            ):
                self._lsp_dirty.add(workspace.id)
            output = str(data.get("output", ""))
            uncertain = data.get("state") == "timed_out"
            ok = data.get("exit_code", 0) in {None, 0} and not uncertain
            artifacts: list[str] = []
            if "screenshot_path" in data:
                instance = await self._instance(workspace)
                remote_image = data.pop("screenshot_path")
                if not re.fullmatch(r"/tmp/merlin-screenshot-[a-f0-9]{32}\.png", remote_image):
                    raise ValueError("Invalid screenshot artifact path")
                image = bytes(await instance.files.read(remote_image, format="bytes"))
                if len(image) > 16 * 1024 * 1024:
                    raise ValueError("Screenshot exceeds artifact size limit")
                relative_image = Path("screenshots") / (uuid4().hex + ".png")
                image_target = self.settings.artifact_dir / relative_image
                image_target.parent.mkdir(parents=True, exist_ok=True)
                image_target.write_bytes(image)
                artifacts.append(relative_image.as_posix())
                await instance.files.remove(remote_image)
            preview = output_preview(output)
            if preview != output:
                relative = Path("outputs") / uuid4().hex / "output.txt"
                target = self.settings.artifact_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(output)
                artifacts.append(relative.as_posix())
                data["truncated"] = True
            # Output already has a dedicated field. Keeping its original value
            # in data defeats truncation and provider-context deduplication.
            data.pop("output", None)
            if call.name in {"bash", "poll_process", "terminate_process"}:
                # Both streams were displayed with boundaries in output; retaining
                # them here duplicates the page and can hide its continuation keys.
                data.pop("stdout", None)
                data.pop("stderr", None)
            serialized = json.dumps(data)
            if len(serialized.encode("utf-8")) > MAX_OUTPUT:
                relative_data = Path("outputs") / uuid4().hex / "result.json"
                data_target = self.settings.artifact_dir / relative_data
                data_target.parent.mkdir(parents=True, exist_ok=True)
                data_target.write_text(serialized)
                artifacts.append(relative_data.as_posix())
                data = {
                    key: data[key]
                    for key in (
                        "state",
                        "exit_code",
                        "pid",
                        "supported",
                        "pending",
                        "stdout_offset",
                        "stdout_next_offset",
                        "stdout_bytes",
                        "stdout_truncated",
                        "stderr_offset",
                        "stderr_next_offset",
                        "stderr_bytes",
                        "stderr_truncated",
                        "next_cursor",
                        "files_searched",
                        "output_truncated",
                    )
                    if key in data
                    and (
                        isinstance(data[key], (bool, int, type(None)))
                        or key == "state"
                        and isinstance(data[key], str)
                        and data[key] in {"running", "completed", "timed_out"}
                    )
                }
                data["truncated"] = True
            return ToolResult(
                call_id=call.id,
                ok=ok,
                output=preview,
                data=data,
                uncertain=uncertain,
                artifacts=artifacts,
            )
        except asyncio.CancelledError:
            raise
        except (ValueError, ValidationError) as error:
            return ToolResult(call_id=call.id, ok=False, output=str(error))
        except Exception:
            # The remote operation may have completed before its response was lost.
            return ToolResult(
                call_id=call.id,
                ok=False,
                output=(
                    "Sandbox transport failed; operation outcome is uncertain. "
                    "Inspect the workspace before continuing."
                ),
                uncertain=True,
            )

    async def checkpoint(self, workspace: Workspace, task_id: str) -> Checkpoint:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            raise ValueError("Invalid checkpoint task identifier")
        for attempt in range(3):
            try:
                return await self._checkpoint_once(workspace, task_id)
            except Exception as error:
                if not checkpoint_connection_error(error):
                    raise
                if attempt == 2:
                    raise CheckpointUnavailable from error
                # Retrying captures repository state only. Each attempt owns new
                # remote paths so an earlier in-flight capture cannot corrupt it.
                # Mutating tools and GitHub operations are never retried here.
                logging.getLogger(__name__).warning(
                    "Checkpoint connection interrupted; retrying snapshot (attempt %d/3)",
                    attempt + 2,
                )
                await asyncio.sleep(0.5 * 2**attempt)
        raise AssertionError("Checkpoint retry loop exhausted unexpectedly")

    async def _checkpoint_once(self, workspace: Workspace, task_id: str) -> Checkpoint:
        checkpoint_id = uuid4().hex
        remote = f"/tmp/merlin-checkpoint-{checkpoint_id}.tar"
        instance = await self._instance(workspace)
        try:
            metadata = await self._helper(workspace, "capture", {"target": remote}, timeout=120)
            data = bytes(await instance.files.read(remote, format="bytes"))
            try:
                await asyncio.to_thread(validate_archive, data)
            except (ValueError, tarfile.TarError) as error:
                # tarfile.ReadError and the network transports' ReadError share
                # a name. Preserve a safe diagnostic that distinguishes them.
                raise WorkspaceSetupError("checkpoint_validation") from error
            relative = Path("checkpoints") / task_id / f"{checkpoint_id}.tar"
            target = self.settings.artifact_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            with temporary.open("wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return Checkpoint(
                id=checkpoint_id,
                task_id=task_id,
                artifact=relative.as_posix(),
                sha256=hashlib.sha256(data).hexdigest(),
                base_sha=str(metadata.get("base_sha", "")),
            )
        finally:
            try:
                await instance.files.remove(remote)
            except Exception:
                pass

    async def restore(self, workspace: Workspace, checkpoint: Checkpoint) -> None:
        artifact_root = self.settings.artifact_dir.resolve()
        from merlin.sandbox._remote import relative_name

        relative = relative_name(checkpoint.artifact)
        raw_artifact = artifact_root / relative
        current = artifact_root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Checkpoint artifact contains a symlink")
        artifact = raw_artifact.resolve()
        if not artifact.is_relative_to(artifact_root) or artifact.is_symlink():
            raise ValueError("Checkpoint artifact escapes artifact directory")
        if artifact.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Checkpoint exceeds size limit")
        data = artifact.read_bytes()
        if hashlib.sha256(data).hexdigest() != checkpoint.sha256:
            raise ValueError("Checkpoint checksum mismatch")
        await asyncio.to_thread(validate_archive, data)
        remote = f"/tmp/merlin-restore-{uuid4().hex}.tar"
        instance = await self._instance(workspace)
        try:
            await instance.files.write(remote, data)
            await self._helper(workspace, "restore", {"source": remote}, timeout=120)
            self._lsp_dirty.add(workspace.id)
            workspace.base_sha = checkpoint.base_sha
        finally:
            try:
                await instance.files.remove(remote)
            except Exception:
                pass

    async def close(self, workspace: Workspace) -> None:
        try:
            instance = await self._instance(workspace)
            await instance.kill()
        finally:
            self._forget(workspace.id)
