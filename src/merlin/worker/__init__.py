"""Durable worker with explicit side-effect boundaries and conservative recovery."""

from __future__ import annotations

import asyncio
import contextlib
import math
from typing import Any

from merlin.agent import (
    MUTATING_TOOLS,
    PUBLISHING_PERMISSIONS,
    SAFE_REPLAY_TOOLS,
    requires_approval,
)
from merlin.contracts import (
    ApprovalRequired,
    BudgetExceeded,
    CheckpointUnavailable,
    ContextBuilder,
    Lease,
    ModelClient,
    ModelServiceError,
    SandboxClient,
    StaleLease,
    Task,
    TaskState,
    TaskStore,
    ToolCall,
    ToolExecutor,
    ToolResult,
    Workspace,
    WorkspaceSetupError,
    new_id,
    now,
)
from merlin.telemetry import (
    ERRORS,
    MODEL_COST,
    OPERATIONS,
    QUEUE_SECONDS,
    RECOVERIES,
    SANDBOX_SECONDS,
    TASK_STATES,
    TASKS,
    span,
)


class TaskCancelled(RuntimeError):
    """A service cancellation, distinct from worker process shutdown."""


class WorkspaceDeadline(RuntimeError):
    """Workspace setup expired without dispatching a repository operation."""


PARALLEL_READ_TOOLS = frozenset(
    {
        "read_file",
        "search",
        "list_files",
        "history_lookup",
        "repository_map",
        "lsp_definition",
        "lsp_references",
        "lsp_hover",
        "lsp_symbols",
        "lsp_diagnostics",
    }
)


class Worker:
    """Run one leased root or child task; dependencies are injectable for tests."""

    def __init__(
        self,
        store: TaskStore,
        model: ModelClient,
        sandbox: SandboxClient,
        executor: ToolExecutor,
        context: ContextBuilder | None = None,
        *,
        worker_id: str | None = None,
        heartbeat_interval: float = 5.0,
        lease_seconds: int = 60,
        model_reservation_usd: float = 0.25,
        workspace_setup_timeout: float = 180,
        checkpoint_timeout: float = 120,
        cleanup_timeout: float = 15,
        tool_timeout: float = 660,
        parallel_read_tools: int = 4,
    ) -> None:
        if heartbeat_interval <= 0 or lease_seconds <= heartbeat_interval:
            raise ValueError("Lease must exceed the positive heartbeat interval")
        self.store = store
        self.model = model
        self.sandbox = sandbox
        self.executor = executor
        self.context = context
        self.worker_id = worker_id or new_id()
        self.heartbeat_interval = heartbeat_interval
        self.lease_seconds = lease_seconds
        self.model_reservation_usd = model_reservation_usd
        self.workspace_setup_timeout = workspace_setup_timeout
        self.checkpoint_timeout = checkpoint_timeout
        self.cleanup_timeout = cleanup_timeout
        self.tool_timeout = tool_timeout
        if not 1 <= parallel_read_tools <= 16:
            raise ValueError("Read concurrency must be between one and sixteen")
        self.parallel_read_tools = parallel_read_tools
        self._workspaces: dict[str, Workspace] = {}
        self._sandbox_active: set[str] = set()

    async def run_once(self) -> bool:
        lease = await asyncio.to_thread(self.store.claim, self.worker_id, self.lease_seconds)
        if lease is None:
            return False
        task = self.store.get_task(lease.task_id)
        QUEUE_SECONDS.observe(max(0, (now() - task.created_at).total_seconds()))
        if task.generation > 1:
            RECOVERIES.inc()
        execution = asyncio.create_task(self._run_traced_task(lease))
        TASKS.labels("running").inc()
        try:
            # Claim/reclaim can race with a cancellation. Do not start a
            # workspace or reserve more work for a cancelled task.
            self._check_cancel(lease)
            while True:
                done, _ = await asyncio.wait({execution}, timeout=self.heartbeat_interval)
                if execution in done:
                    await execution
                    break
                control = await asyncio.to_thread(self._control, lease.task_id)
                if control["cancel_requested"]:
                    execution.cancel()
                    await self._drain(execution)
                    self.store.update_task(lease, state=TaskState.CANCELLED, error="Cancelled")
                    break
                await asyncio.to_thread(self.store.heartbeat, lease, self.lease_seconds)
                if lease.task_id in self._sandbox_active:
                    self._reserve_sandbox(lease)
        except TaskCancelled:
            execution.cancel()
            await self._drain(execution)
            self._transition(lease, TaskState.CANCELLED, error="Cancelled")
        except StaleLease:
            execution.cancel()
            await self._drain(execution)
            # reserve_usage rejects cancellation with StaleLease too. Finalize
            # only through the original fenced lease; a newer worker owns its
            # own transition even if it also observes cancellation.
            if self.store.get_task(lease.task_id).cancel_requested:
                self._transition(lease, TaskState.CANCELLED, error="Cancelled")
        except BudgetExceeded:
            execution.cancel()
            await self._drain(execution)
            self._transition(
                lease,
                TaskState.PAUSED,
                error="Task budget exhausted or context exceeds the configured input limit",
            )
        except ModelServiceError as error:
            ERRORS.labels("model").inc()
            execution.cancel()
            await self._drain(execution)
            self._transition(
                lease,
                TaskState.PAUSED if error.retryable else TaskState.FAILED,
                error=str(error),
            )
        except CheckpointUnavailable as error:
            ERRORS.labels("checkpoint").inc()
            execution.cancel()
            await self._drain(execution)
            self._transition(
                lease,
                TaskState.PAUSED,
                error=f"{error}. Inspect the task before resuming.",
            )
        except WorkspaceDeadline:
            execution.cancel()
            await self._drain(execution)
            self._transition(
                lease,
                TaskState.PAUSED,
                error="Workspace setup deadline exceeded. Task history is saved; resume to retry.",
            )
        except asyncio.CancelledError:
            execution.cancel()
            await self._drain(execution)
            # A worker shutdown is not a task cancellation. Reclaim will reconcile intents.
            raise
        except Exception as exc:
            ERRORS.labels("worker").inc()
            execution.cancel()
            await self._drain(execution)
            # Only this internally assembled diagnostic is safe for public events.
            # Provider stderr and arbitrary exception strings can include credentials.
            detail = str(exc) if isinstance(exc, WorkspaceSetupError) else "task failed"
            self._transition(lease, TaskState.FAILED, error=f"{type(exc).__name__}: {detail}")
        finally:
            self._sandbox_active.discard(lease.task_id)
            workspace = self._workspaces.pop(lease.task_id, None)
            if workspace is not None:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(self.cleanup_timeout):
                        await self.sandbox.close(workspace)
                release = getattr(self.context, "release", None)
                if release is not None:
                    release(workspace.id)
            # Reporting failures must not prevent sandbox cleanup or kill sibling consumers.
            with contextlib.suppress(Exception):
                final_state = self._control(lease.task_id)["state"]
                TASKS.labels(final_state.value).inc()
                counts = getattr(self.store, "state_counts", None)
                if counts is not None:
                    values = await asyncio.to_thread(counts)
                    for state in TaskState:
                        TASK_STATES.labels(state.value).set(values.get(state.value, 0))
        return True

    async def _run_traced_task(self, lease: Lease) -> None:
        task = self.store.get_task(lease.task_id)
        with (
            OPERATIONS.labels("task").time(),
            span("task.run", task_id=task.id, root_task_id=task.root_id),
        ):
            await self._run_task(lease)

    async def _drain(self, execution: asyncio.Task[None]) -> None:
        def retrieve(finished: asyncio.Task[None]) -> None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                finished.exception()

        # wait_for waits for cancellation acknowledgement and can itself hang.
        # A late adapter response remains fenced by task state and lease checks.
        done, _ = await asyncio.wait({execution}, timeout=self.cleanup_timeout)
        if execution in done:
            retrieve(execution)
        else:
            execution.add_done_callback(retrieve)

    def _transition(self, lease: Lease, state: TaskState, **changes: Any) -> None:
        with contextlib.suppress(StaleLease):
            self.store.update_task(lease, state=state, **changes)

    def _check_cancel(self, lease: Lease) -> None:
        if self._control(lease.task_id)["cancel_requested"]:
            raise TaskCancelled

    def _control(self, task_id: str) -> dict[str, Any]:
        projection = getattr(self.store, "task_control", None)
        if projection is not None:
            return dict(projection(task_id))
        task = self.store.get_task(task_id)
        return {"state": task.state, "cancel_requested": task.cancel_requested}

    def _reserve_sandbox(self, lease: Lease) -> None:
        quantum = max(1, math.ceil(self.heartbeat_interval))
        self.store.reserve_usage(lease, sandbox_seconds=quantum)
        SANDBOX_SECONDS.inc(quantum)

    async def _workspace(self, lease: Lease, task: Task) -> Workspace:
        self._check_cancel(lease)
        self._reserve_sandbox(lease)
        self._sandbox_active.add(task.id)
        checkpoint = self.store.latest_checkpoint(task.id)
        session_checkpoint = getattr(self.store, "latest_session_checkpoint", None)
        if checkpoint is None and task.spec.parent_id is None and session_checkpoint is not None:
            checkpoint = session_checkpoint(task)
            if checkpoint is not None:
                checkpoint = checkpoint.model_copy(update={"id": new_id(), "task_id": task.id})
                self.store.save_checkpoint(lease, checkpoint)
                task = self.store.get_task(task.id)
        # Resolve follow-up snapshots at execution, keeping submitted specs idempotent.
        if checkpoint is None and not task.spec.parent_id and hasattr(self.store, "list_tasks"):
            lookup = getattr(self.store, "predecessors", None)
            candidates = (
                lookup(task) if lookup is not None else self.store.list_tasks(task.spec.session_id)
            )
            predecessors = [
                previous
                for previous in candidates
                if previous.id != task.id
                and previous.state == TaskState.SUCCEEDED
                and previous.spec.parent_id is None
                and previous.spec.repository == task.spec.repository
                and previous.created_at < task.created_at
            ]
            # Conversational turns have no snapshot. Keep the most recent actual
            # workspace even when the intervening messages were ordinary chat.
            for previous in sorted(predecessors, key=lambda item: item.created_at, reverse=True):
                checkpoint = self.store.latest_checkpoint(previous.id)
                if checkpoint:
                    # A cross-task checkpoint remains immutable; create a new owned reference.
                    checkpoint = checkpoint.model_copy(update={"id": new_id(), "task_id": task.id})
                    self.store.save_checkpoint(lease, checkpoint)
                    task = self.store.get_task(task.id)
                    break
        # A reclaimed lease must never share a sandbox with the old worker:
        # database fencing cannot retract commands already sent to that machine.
        if task.sandbox_id and task.generation <= 1:
            try:
                workspace = await self.sandbox.connect(task.sandbox_id)
            except Exception:
                workspace = await self.sandbox.create(task)
                self._workspaces[task.id] = workspace
                if checkpoint is not None:
                    await self.sandbox.restore(workspace, checkpoint)
        else:
            workspace = await self.sandbox.create(task)
            self._workspaces[task.id] = workspace
            if checkpoint is not None:
                await self.sandbox.restore(workspace, checkpoint)
        self._workspaces[task.id] = workspace
        self.store.update_task(lease, sandbox_id=workspace.id)
        return workspace

    async def _checkpoint(self, lease: Lease, workspace: Workspace) -> None:
        self._check_cancel(lease)
        try:
            async with asyncio.timeout(self.checkpoint_timeout):
                checkpoint = await self.sandbox.checkpoint(workspace, lease.task_id)
        except TimeoutError as error:
            raise CheckpointUnavailable(deadline=True) from error
        self.store.save_checkpoint(lease, checkpoint)

    async def _run_task(self, lease: Lease) -> None:
        task = self.store.get_task(lease.task_id)
        self._check_cancel(lease)
        # A prepared intent from another generation may already have changed the world.
        if any(
            item.result is not None
            and item.result.uncertain
            or item.result is None
            and item.call.name not in SAFE_REPLAY_TOOLS
            for item in self.store.invocations(task.id)
        ):
            self.store.update_task(
                lease,
                state=TaskState.PAUSED,
                error="An interrupted tool has an uncertain outcome; inspect before resuming",
            )
            return
        workspace: Workspace | None = None
        if not task.history:
            task = self.store.update_task(
                lease, history=[{"role": "user", "parts": [{"text": task.spec.prompt}]}]
            )
        while True:
            self._check_cancel(lease)
            task = self.store.get_task(task.id)
            pending = self._pending_calls(task.history)
            if pending:
                if task.spec.repository is not None and workspace is None:
                    try:
                        async with asyncio.timeout(self.workspace_setup_timeout):
                            workspace = await self._workspace(lease, task)
                    except TimeoutError as error:
                        raise WorkspaceDeadline from error
                    if self.store.latest_checkpoint(task.id) is None:
                        await self._checkpoint(lease, workspace)
                index = 0
                while index < len(pending):
                    call = pending[index]
                    self._check_cancel(lease)
                    # Only explicitly listed repository reads may overlap.
                    # Mutations and approval-sensitive operations form barriers.
                    batch = []
                    if workspace is not None and self.parallel_read_tools > 1:
                        while index < len(pending) and pending[index].name in PARALLEL_READ_TOOLS:
                            batch.append(pending[index])
                            index += 1
                    if batch:
                        assert workspace is not None
                        if not await self._read_batch(lease, workspace, batch):
                            return
                    else:
                        index += 1
                        if not await self._dispatch(lease, workspace, call):
                            return
                continue
            history = (
                await self.context.build(task, workspace)
                if self.context is not None
                else task.history
            )
            clean_history = [
                {key: value for key, value in entry.items() if not key.startswith("_merlin")}
                for entry in history
            ]
            tools = self.executor.definitions() if task.spec.repository is not None else []
            estimate = getattr(self.model, "estimate_call_cost", None)
            cost = (
                await estimate(task, clean_history, tools)
                if estimate is not None
                else self.model_reservation_usd
            )
            self._check_cancel(lease)
            reservation = self.store.reserve_usage(lease, cost_usd=cost, calls=1)
            marker = getattr(self.store, "mark_usage_dispatched", None)
            if marker is not None:
                marker(lease, reservation)
            try:
                with (
                    OPERATIONS.labels("model").time(),
                    span("model.generate", task_id=task.id, model=task.spec.model),
                ):
                    reply = await self.model.generate(task, clean_history, tools)
            except ModelServiceError as error:
                actual = getattr(error, "cost_usd", None)
                if actual is not None:
                    self.store.settle_usage(lease, reservation, actual)
                elif getattr(error, "request_started", True) is False:
                    release_usage = getattr(self.store, "release_usage", None)
                    if release_usage is not None:
                        release_usage(lease, reservation, undispatched=True)
                raise
            if reply.usage_complete:
                self.store.settle_usage(lease, reservation, reply.cost_usd)
            MODEL_COST.inc(reply.cost_usd)
            from merlin.telemetry import TOKENS

            TOKENS.labels("input").inc(reply.input_tokens)
            TOKENS.labels("output").inc(reply.output_tokens)
            self.store.append_event(
                task.id,
                "model.reply",
                {
                    "text": reply.text,
                    "input_tokens": reply.input_tokens,
                    "output_tokens": reply.output_tokens,
                    "cost_usd": reply.cost_usd,
                    "usage_complete": reply.usage_complete,
                    "cached_input_tokens": reply.cached_input_tokens,
                    "thinking_tokens": reply.thinking_tokens,
                    "finish_reason": reply.finish_reason,
                    "provider_response_id": reply.provider_response_id,
                    "generation_attempts": reply.generation_attempts,
                },
                lease,
            )
            content = (
                dict(reply.content)
                if reply.content
                else {"role": "model", "parts": [{"text": reply.text}]}
            )
            content.setdefault("role", "model")
            if reply.calls:
                content["_merlin_calls"] = [call.model_dump(mode="json") for call in reply.calls]
            task = self.store.update_task(lease, history=[*task.history, content])
            if not reply.calls:
                if workspace is not None:
                    await self._checkpoint(lease, workspace)
                self.store.append_event(
                    task.id, "task.completion", self._completion(task.id), lease
                )
                self.store.update_task(
                    lease, state=TaskState.SUCCEEDED, result=reply.text, error=None
                )
                return

    def _completion(self, task_id: str) -> dict[str, Any]:
        """Separate concrete tool evidence from the model's final prose."""
        task = self.store.get_task(task_id)
        invocations = {item.call.id: item for item in self.store.invocations(task_id)}
        commands: list[dict[str, Any]] = []
        changed: list[str] = []
        failures = 0
        source_changed = False
        for entry in task.history:
            for raw_call in entry.get("_merlin_calls", []):
                invocation = invocations.get(raw_call.get("id"))
                if invocation is None or invocation.result is None:
                    continue
                call, result = invocation.call, invocation.result
                if (
                    call.name == "bash"
                    and not call.arguments.get("background")
                    and not result.uncertain
                ):
                    source_changed = True
                    if type(result.data.get("exit_code")) is int:
                        commands.append(
                            {
                                "call_id": call.id,
                                "command": str(call.arguments.get("command", ""))[:2000],
                                "exit_code": result.data["exit_code"],
                            }
                        )
                if not result.ok or result.uncertain:
                    failures += 1
                    continue
                if call.name in {"write_file", "replace_text", "integrate_child"}:
                    source_changed = True
                    commands.clear()  # Earlier command results do not validate a later edit.
                    path = call.arguments.get("path")
                    if isinstance(path, str):
                        changed.append(path[:1024])
        return {
            "checkpoint_id": task.checkpoint_id,
            "changed_paths": list(dict.fromkeys(changed))[-100:],
            "commands_after_last_edit": commands[-20:],
            "verification": "commands_failed"
            if any(command["exit_code"] != 0 for command in commands)
            else "commands_recorded"
            if commands
            else "not_recorded"
            if source_changed
            else "not_applicable",
            "historical_tool_failures": failures,
            "notice": (
                "Command records are execution evidence; review their scope. "
                "Final model prose alone is not verification."
            ),
        }

    @staticmethod
    def _pending_calls(history: list[dict[str, Any]]) -> list[ToolCall]:
        answered = {
            part["function_response"].get("id")
            for entry in history
            for part in entry.get("parts", [])
            if isinstance(part, dict) and "function_response" in part
        }
        return [
            ToolCall.model_validate(call)
            for entry in history
            for call in entry.get("_merlin_calls", [])
            if call.get("id") not in answered
        ]

    async def _dispatch(self, lease: Lease, workspace: Workspace | None, call: ToolCall) -> bool:
        task = self.store.get_task(lease.task_id)
        execution_task = task
        before_tool = getattr(self.context, "before_tool", None)
        existing = next(
            (item for item in self.store.invocations(task.id) if item.call.id == call.id), None
        )
        if existing is not None and existing.result is not None:
            # A finished reviewed operation must not require another approval after recovery.
            self.store.begin_tool(
                lease, call
            )  # Revalidate the persisted argument digest and lease.
            result = existing.result
        elif task.spec.repository is None:
            # Suppressing declarations alone is not an authorization boundary.
            self.store.begin_tool(lease, call)
            result = ToolResult(
                call_id=call.id,
                ok=False,
                output=(
                    "No repository selected. Answer conversationally, or ask the user "
                    "to select a repository for coding tools."
                ),
            )
            self.store.finish_tool(lease, result)
        elif task.spec.parent_id and call.name in PUBLISHING_PERMISSIONS:
            self.store.begin_tool(lease, call)
            result = ToolResult(call_id=call.id, ok=False, output="Children cannot publish")
            self.store.finish_tool(lease, result)
        elif (
            workspace is not None
            and before_tool is not None
            and (guidance := await before_tool(task, workspace, call)) is not None
        ):
            # Instructions discovered for a new edit target must reach the model
            # before authorization is consumed or any workspace effect is attempted.
            self.store.begin_tool(lease, call)
            result = guidance
            self.store.finish_tool(lease, result)
        else:
            if workspace is None:
                raise ValueError("Workspace tools require an isolated workspace")
            if requires_approval(task, call):
                if not self.store.consume_approval(lease, call):
                    self.store.request_approval(lease, call)
                    self.store.update_task(lease, state=TaskState.WAITING_FOR_APPROVAL)
                    return False
                execution_task = task.model_copy(deep=True)
                execution_task.spec.permissions.append(call.name)
            if call.name in MUTATING_TOOLS:
                await self._checkpoint(lease, workspace)
            # No mutation is dispatched until its recovery checkpoint is durable.
            # Approved publishing may already have an intent from consume_approval;
            # preserve that transaction's conservative reconciliation behavior.
            self.store.begin_tool(lease, call)
            self.store.append_event(task.id, "tool.started", {"call": call.model_dump()}, lease)
            try:
                with (
                    OPERATIONS.labels("tool").time(),
                    span("tool.execute", task_id=task.id, tool=call.name, call_id=call.id),
                ):
                    async with asyncio.timeout(self.tool_timeout):
                        result = await self.executor.execute(execution_task, workspace, call)
            except ApprovalRequired:
                # Executor authorization is a defense in depth, not an approval path.
                result = ToolResult(call_id=call.id, ok=False, output="Authorization denied")
            except asyncio.CancelledError:
                raise
            except Exception:
                # Dispatch may have reached an external system before the exception.
                result = ToolResult(
                    call_id=call.id, ok=False, output="Tool outcome is uncertain", uncertain=True
                )
            if not result.ok:
                ERRORS.labels("tool").inc()
            after_tool = getattr(self.context, "after_tool", None)
            if after_tool is not None:
                after_tool(workspace, call, result)
            if call.name in MUTATING_TOOLS and not result.uncertain:
                # Commit the post-operation filesystem reference and result atomically.
                try:
                    async with asyncio.timeout(self.checkpoint_timeout):
                        checkpoint = await self.sandbox.checkpoint(workspace, lease.task_id)
                except TimeoutError as error:
                    raise CheckpointUnavailable(deadline=True) from error
                self.store.finish_tool(lease, result, checkpoint=checkpoint)
            else:
                self.store.finish_tool(lease, result)
        return self._record_response(lease, call, result)

    async def _read_batch(self, lease: Lease, workspace: Workspace, calls: list[ToolCall]) -> bool:
        task = self.store.get_task(lease.task_id)
        completed = {
            item.call.id: item.result
            for item in self.store.invocations(task.id)
            if item.result is not None
        }
        semaphore = asyncio.Semaphore(self.parallel_read_tools)

        async def execute(call: ToolCall) -> ToolResult:
            previous = completed.get(call.id)
            if previous is not None:
                self.store.begin_tool(lease, call)
                return previous
            async with semaphore:
                self._check_cancel(lease)
                self.store.begin_tool(lease, call)
                self.store.append_event(task.id, "tool.started", {"call": call.model_dump()}, lease)
                try:
                    with OPERATIONS.labels("tool").time():
                        async with asyncio.timeout(self.tool_timeout):
                            return await self.executor.execute(task, workspace, call)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return ToolResult(
                        call_id=call.id,
                        ok=False,
                        output="Read outcome is uncertain",
                        uncertain=True,
                    )

        # TaskGroup drains cancellation before returning. Persist in declaration
        # order so provider-native call blocks retain deterministic response order.
        try:
            async with asyncio.TaskGroup() as group:
                executions = [group.create_task(execute(call)) for call in calls]
        except ExceptionGroup as error:

            def control_errors(group: BaseException) -> bool:
                if isinstance(group, BaseExceptionGroup):
                    return all(control_errors(item) for item in group.exceptions)
                return isinstance(group, (TaskCancelled, StaleLease))

            if control_errors(error):
                self._check_cancel(lease)
                raise StaleLease from None
            raise
        results = [execution.result() for execution in executions]
        for call, result in zip(calls, results, strict=True):
            if call.id not in completed:
                self.store.finish_tool(lease, result)
            self._record_response(lease, call, result, pause_uncertain=False)
        if any(result.uncertain for result in results):
            self.store.update_task(
                lease,
                state=TaskState.PAUSED,
                error="Read outcome is uncertain; inspect before retrying",
            )
            return False
        return True

    def _record_response(
        self, lease: Lease, call: ToolCall, result: ToolResult, *, pause_uncertain: bool = True
    ) -> bool:
        history = self.store.get_task(lease.task_id).history
        response = {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "name": call.name,
                        "id": call.id,
                        "response": result.model_dump(mode="json"),
                    }
                }
            ],
        }
        if (
            history
            and history[-1].get("role") == "user"
            and history[-1].get("parts")
            and all("function_response" in part for part in history[-1]["parts"])
        ):
            # Parallel model calls form one interaction: all corresponding tool
            # responses belong in a single user content block for Gemini.
            combined = {**history[-1], "parts": [*history[-1]["parts"], *response["parts"]]}
            history = [*history[:-1], combined]
        else:
            history = [*history, response]
        self.store.update_task(lease, history=history)
        if result.uncertain and pause_uncertain:
            self.store.update_task(
                lease,
                state=TaskState.PAUSED,
                error="Tool outcome is uncertain; inspect before retrying",
            )
            return False
        return True


def main() -> None:
    """Application wiring is imported only by the process entrypoint."""
    import signal

    from merlin.application import run_worker

    async def serve() -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            await run_worker()
        except asyncio.CancelledError:
            pass
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    asyncio.run(serve())
