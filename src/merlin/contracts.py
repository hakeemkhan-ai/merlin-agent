"""Shared version-one contracts. Changes are coordinated by the lead integrator."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from merlin.presentation import SessionPage, TaskPage, TaskSummary


def new_id() -> str:
    return uuid4().hex


def now() -> datetime:
    return datetime.now(UTC)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_CHILDREN = "waiting_for_children"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Budget(Record):
    max_calls: int = Field(default=100, ge=1, le=1000)
    max_cost_usd: float = Field(default=5.0, gt=0, le=1000)
    max_sandbox_seconds: int = Field(default=3600, ge=1, le=86400)


class TaskSpec(Record):
    prompt: str = Field(min_length=1, max_length=100000)
    repository: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    session_id: str = Field(default_factory=new_id, pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    parent_id: str | None = None
    permissions: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    model: str = "gemini-3.8-flash"
    budget: Budget = Field(default_factory=Budget)
    base_checkpoint_id: str | None = None


class Task(Record):
    id: str = Field(default_factory=new_id)
    spec: TaskSpec
    root_id: str = ""
    state: TaskState = TaskState.QUEUED
    generation: int = 0
    worker_id: str | None = None
    lease_until: datetime | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    history: list[dict[str, Any]] = Field(default_factory=list)
    sandbox_id: str | None = None
    checkpoint_id: str | None = None
    error: str | None = None
    result: str | None = None
    cancel_requested: bool = False


class Lease(Record):
    task_id: str
    worker_id: str
    generation: int


class Event(Record):
    task_id: str
    sequence: int
    kind: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)


class ToolCall(Record):
    id: str = Field(default_factory=new_id)
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(Record):
    call_id: str
    ok: bool = True
    output: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    uncertain: bool = False


class Invocation(Record):
    task_id: str
    call: ToolCall
    state: str = "prepared"
    result: ToolResult | None = None


class Checkpoint(Record):
    id: str = Field(default_factory=new_id)
    task_id: str
    artifact: str
    sha256: str
    base_sha: str = ""
    created_at: datetime = Field(default_factory=now)


class Approval(Record):
    id: str = Field(default_factory=new_id)
    task_id: str
    call: ToolCall
    digest: str
    status: str = "pending"
    created_at: datetime = Field(default_factory=now)


class ModelReply(Record):
    cached_input_tokens: int = 0
    thinking_tokens: int = 0
    finish_reason: str | None = None
    provider_response_id: str | None = None
    generation_attempts: int = 1
    usage_complete: bool = True
    text: str = ""
    calls: list[ToolCall] = Field(default_factory=list)
    content: dict[str, Any] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0


class Workspace(Record):
    id: str
    root: str = "/home/user/repo"
    base_sha: str = ""


class StaleLease(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class ModelServiceError(RuntimeError):
    """A public model diagnostic derived from status alone, never a provider body."""

    RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

    def __init__(self, status_code: int | None = None) -> None:
        code = status_code if type(status_code) is int and 400 <= status_code <= 599 else None
        self.retryable = code is None or code in self.RETRYABLE_STATUSES
        if code is None:
            message = "Gemini connection was interrupted."
        elif self.retryable:
            message = f"Gemini is temporarily unavailable (HTTP {code})."
        else:
            message = f"Gemini rejected the request (HTTP {code})."
        if self.retryable:
            message += (
                " Task history is saved; use /resume in the terminal UI "
                "or merlin resume TASK_ID to retry."
            )
        else:
            message += " Check the model and Gemini API access in the worker configuration."
        super().__init__(message)


class CheckpointUnavailable(RuntimeError):
    """Static, credential-free summary for exhausted checkpoint transport retries."""

    def __init__(self, *, deadline: bool = False) -> None:
        super().__init__(
            "Recovery checkpoint deadline exceeded"
            if deadline
            else "Recovery checkpoint connection failed after three attempts"
        )


class WorkspaceSetupError(RuntimeError):
    """Public workspace diagnostic assembled without provider output or credentials."""

    def __init__(
        self,
        stage: Literal["initialization", "repository_clone", "checkpoint_validation"],
        exit_code: int | None = None,
    ) -> None:
        message = {
            "initialization": "Workspace initialization failed",
            "repository_clone": "Repository clone failed",
            "checkpoint_validation": "Recovery checkpoint archive validation failed",
        }[stage]
        if type(exit_code) is int:
            message += f" (exit code {exit_code})"
        super().__init__(message)


class ApprovalRequired(RuntimeError):
    pass


class TaskStore(Protocol):
    def create_task(
        self,
        spec: TaskSpec,
        idempotency_key: str | None = None,
        *,
        parent_lease: Lease | None = None,
    ) -> Task: ...
    def get_task(self, task_id: str) -> Task: ...
    def list_tasks(self, session_id: str | None = None) -> list[Task]: ...
    def task_summary(self, task_id: str) -> TaskSummary: ...
    def session_tasks(
        self, session_id: str, cursor: int = 0, limit: int = 20, *, before: str | None = None
    ) -> TaskPage: ...
    def session_summaries(
        self, repository: str | None = None, cursor: int = 0, limit: int = 50
    ) -> SessionPage: ...
    def get_approval(self, approval_id: str) -> Approval: ...
    def approval_summaries(self, task_id: str) -> list[dict[str, Any]]: ...
    def claim(self, worker_id: str, lease_seconds: int = 60) -> Lease | None: ...
    def heartbeat(self, lease: Lease, lease_seconds: int = 60) -> None: ...
    def update_task(self, lease: Lease, **changes: Any) -> Task: ...
    def append_event(
        self, task_id: str, kind: str, data: dict[str, Any], lease: Lease | None = None
    ) -> Event: ...
    def events(
        self,
        task_id: str,
        after: int = 0,
        *,
        limit: int | None = None,
        max_bytes: int | None = None,
    ) -> list[Event]: ...
    def begin_tool(self, lease: Lease, call: ToolCall) -> Invocation: ...
    def finish_tool(
        self, lease: Lease, result: ToolResult, *, checkpoint: Checkpoint | None = None
    ) -> None: ...
    def invocations(self, task_id: str) -> list[Invocation]: ...
    def save_checkpoint(self, lease: Lease, checkpoint: Checkpoint) -> None: ...
    def latest_checkpoint(self, task_id: str) -> Checkpoint | None: ...
    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint: ...
    def request_approval(self, lease: Lease, call: ToolCall) -> Approval: ...
    def approvals(self, task_id: str) -> list[Approval]: ...
    def decide_approval(self, approval_id: str, approved: bool) -> Approval: ...
    def request_approval_changes(self, approval_id: str, note: str) -> Approval: ...
    def consume_approval(self, lease: Lease, call: ToolCall) -> bool: ...
    def reserve_usage(
        self, lease: Lease, cost_usd: float = 0, calls: int = 0, sandbox_seconds: int = 0
    ) -> str: ...
    def settle_usage(self, lease: Lease, reservation: str, cost_usd: float) -> None: ...
    def usage(self, task_id: str) -> dict[str, Any]: ...
    def cancel(self, task_id: str) -> Task: ...
    def resume(self, task_id: str) -> Task: ...
    def resolve_tool(self, task_id: str, call_id: str, note: str) -> Task: ...
    def close(self) -> None: ...
    def readiness(self, worker_max_age_seconds: int = 30) -> dict[str, Any]: ...
    def reservations(self, task_id: str) -> list[dict[str, Any]]: ...
    def amend_budget(self, task_id: str, budget: Budget, note: str) -> Task: ...
    def reconcile_reservation(
        self, task_id: str, reservation: str, actual_cost_usd: float, note: str
    ) -> None: ...
    def history_page(
        self, task_id: str, cursor: int = 0, limit: int = 20, max_chars: int = 64000
    ) -> dict[str, Any]: ...


class ModelClient(Protocol):
    async def generate(
        self, task: Task, history: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


class SandboxClient(Protocol):
    async def create(self, task: Task) -> Workspace: ...
    async def connect(self, sandbox_id: str) -> Workspace: ...
    async def execute(self, workspace: Workspace, call: ToolCall) -> ToolResult: ...
    async def checkpoint(self, workspace: Workspace, task_id: str) -> Checkpoint: ...
    async def restore(self, workspace: Workspace, checkpoint: Checkpoint) -> None: ...
    async def close(self, workspace: Workspace) -> None: ...


class ToolExecutor(Protocol):
    def definitions(self) -> list[dict[str, Any]]: ...
    async def execute(self, task: Task, workspace: Workspace, call: ToolCall) -> ToolResult: ...


class ContextBuilder(Protocol):
    async def build(self, task: Task, workspace: Workspace | None) -> list[dict[str, Any]]: ...


class GitHubPublisher(Protocol):
    async def execute(self, task: Task, call: ToolCall) -> ToolResult: ...
