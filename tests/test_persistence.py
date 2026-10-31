"""Credential-free persistence checks; PostgreSQL locking is tested separately."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from sqlalchemy.orm import Session

from merlin.contracts import (
    Budget,
    BudgetExceeded,
    Checkpoint,
    Lease,
    StaleLease,
    TaskSpec,
    TaskState,
    ToolCall,
    ToolResult,
    now,
)
from merlin.persistence import SQLTaskStore
from merlin.persistence.models import ApprovalRow, TaskRow


class PersistenceTests(TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.url = f"sqlite+pysqlite:///{Path(self.directory.name) / 'tasks.db'}"
        self.store = SQLTaskStore(self.url, max_running_roots=10)
        self.store.create_schema()

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def create(self, **changes: object):  # type: ignore[no-untyped-def]
        return self.store.create_task(
            TaskSpec(prompt="Fix the tests", repository="owner/repo", **changes)
        )

    def claim(self, **changes: object) -> Lease:
        self.create(**changes)
        lease = self.store.claim("worker")
        assert lease is not None
        return lease

    def expire(self, task_id: str) -> None:
        with Session(self.store.engine) as session, session.begin():
            row = session.get(TaskRow, task_id)
            assert row is not None
            row.lease_until = now() - timedelta(seconds=1)

    def test_idempotency_returns_one_task_and_rejects_changed_content(self) -> None:
        spec = TaskSpec(prompt="Fix", repository="owner/repo")
        original = self.store.create_task(spec, "request-1")
        repeated = self.store.create_task(spec, "request-1")
        self.assertEqual(original.id, repeated.id)
        self.assertEqual(len(self.store.list_tasks()), 1)
        with self.assertRaises(ValueError):
            self.store.create_task(spec.model_copy(update={"prompt": "Different"}), "request-1")

    def test_idempotency_serializes_concurrent_requests(self) -> None:
        spec = TaskSpec(prompt="Fix", repository="owner/repo")

        def submit(_: int) -> str:
            alternate = SQLTaskStore(self.url)
            try:
                return alternate.create_task(spec, "same-request").id
            finally:
                alternate.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(submit, range(8)))
        self.assertEqual(len(set(ids)), 1)

    def test_session_tasks_are_serial_and_other_sessions_progress(self) -> None:
        first = self.create(session_id="one")
        second = self.create(session_id="one")
        independent = self.create(session_id="two")
        first_lease = self.store.claim("a")
        assert first_lease is not None
        self.assertEqual(first_lease.task_id, first.id)
        second_lease = self.store.claim("b")
        assert second_lease is not None
        self.assertEqual(second_lease.task_id, independent.id)
        self.assertIsNone(self.store.claim("c"))
        self.store.update_task(first_lease, state=TaskState.SUCCEEDED)
        next_lease = self.store.claim("c")
        assert next_lease is not None
        self.assertEqual(next_lease.task_id, second.id)

    def test_paused_session_task_blocks_followups(self) -> None:
        lease = self.claim(session_id="one")
        self.create(session_id="one")
        self.store.update_task(lease, state=TaskState.PAUSED, error="Review required")
        self.assertIsNone(self.store.claim("other"))
        self.store.resume(lease.task_id)
        resumed = self.store.claim("other")
        assert resumed is not None
        self.assertEqual(resumed.task_id, lease.task_id)

    def test_expired_lease_is_reclaimed_and_fenced(self) -> None:
        lease = self.claim()
        self.expire(lease.task_id)
        for operation in (
            lambda: self.store.heartbeat(lease),
            lambda: self.store.update_task(lease, result="late result"),
            lambda: self.store.reserve_usage(lease, calls=1),
            lambda: self.store.begin_tool(lease, ToolCall(name="bash")),
        ):
            with self.assertRaises(StaleLease):
                operation()
        replacement = self.store.claim("replacement")
        assert replacement is not None
        self.assertEqual(replacement.task_id, lease.task_id)
        self.assertEqual(replacement.generation, lease.generation + 1)
        with self.assertRaises(StaleLease):
            self.store.update_task(lease, result="late result")
        self.store.heartbeat(replacement)

    def test_events_are_ordered_and_replay_after_cursor(self) -> None:
        lease = self.claim()
        self.store.append_event(lease.task_id, "custom", {"value": 1}, lease)
        events = self.store.events(lease.task_id)
        self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(self.store.events(lease.task_id, after=2)[0].kind, "custom")

    def test_wrong_task_event_lease_is_rejected(self) -> None:
        lease = self.claim()
        other = self.create()
        with self.assertRaises(StaleLease):
            self.store.append_event(other.id, "bad", {}, lease)

    def test_invocation_survives_restart_and_result_is_immutable(self) -> None:
        lease = self.claim()
        call = ToolCall(name="bash", arguments={"command": "pytest"})
        self.store.begin_tool(lease, call)
        reopened = SQLTaskStore(self.url)
        try:
            pending = reopened.invocations(lease.task_id)
            self.assertEqual(len(pending), 1)
            self.assertIsNone(pending[0].result)
        finally:
            reopened.close()
        result = ToolResult(call_id=call.id, output="passed")
        self.store.finish_tool(lease, result)
        self.store.finish_tool(lease, result)
        self.assertEqual(self.store.begin_tool(lease, call).result, result)
        with self.assertRaises(ValueError):
            self.store.finish_tool(lease, result.model_copy(update={"output": "changed"}))
        with self.assertRaises(ValueError):
            self.store.begin_tool(lease, call.model_copy(update={"arguments": {"command": "rm"}}))

    def test_uncertain_result_is_not_success(self) -> None:
        lease = self.claim()
        call = ToolCall(name="bash")
        self.store.begin_tool(lease, call)
        self.store.finish_tool(lease, ToolResult(call_id=call.id, ok=False, uncertain=True))
        self.assertEqual(self.store.invocations(lease.task_id)[0].state, "uncertain")

    def test_operator_resolution_never_claims_success_or_replays_unknown_effect(self) -> None:
        lease = self.claim()
        call = ToolCall(name="bash")
        self.store.begin_tool(lease, call)
        result = ToolResult(call_id=call.id, ok=False, uncertain=True)
        self.store.finish_tool(lease, result)
        self.store.update_task(
            lease,
            state=TaskState.PAUSED,
            sandbox_id="uncertain-sandbox",
            history=[
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "id": call.id,
                                "name": call.name,
                                "response": result.model_dump(),
                            }
                        }
                    ],
                }
            ],
        )
        resolved = self.store.resolve_tool(lease.task_id, call.id, "Verified no remote update")
        self.assertEqual(resolved.state, TaskState.QUEUED)
        self.assertIsNone(resolved.sandbox_id)
        saved = self.store.invocations(lease.task_id)[0].result
        assert saved is not None
        self.assertFalse(saved.ok)
        self.assertFalse(saved.uncertain)
        self.assertIn("External effects are not rolled back", saved.output)
        self.assertEqual(
            resolved.history[0]["parts"][0]["function_response"]["response"], saved.model_dump()
        )
        replacement = self.store.claim("replacement")
        assert replacement is not None
        self.assertEqual(self.store.begin_tool(replacement, call).result, saved)

    def test_resolution_rejects_successful_tool_and_requires_paused_task(self) -> None:
        lease = self.claim()
        call = ToolCall(name="bash")
        self.store.begin_tool(lease, call)
        with self.assertRaises(ValueError):
            self.store.resolve_tool(lease.task_id, call.id, "Inspect")
        self.store.finish_tool(lease, ToolResult(call_id=call.id, output="Done"))
        self.store.update_task(lease, state=TaskState.PAUSED)
        with self.assertRaises(ValueError):
            self.store.resolve_tool(lease.task_id, call.id, "Cannot rewrite success")

    def test_tool_result_and_checkpoint_commit_atomically(self) -> None:
        lease = self.claim()
        call = ToolCall(name="write_file")
        self.store.begin_tool(lease, call)
        checkpoint = Checkpoint(task_id=lease.task_id, artifact="snapshot", sha256="a" * 64)
        result = ToolResult(call_id=call.id, output="changed")
        with self.assertRaises(ValueError):
            self.store.finish_tool(
                lease,
                result,
                checkpoint=checkpoint.model_copy(update={"sha256": "invalid"}),
            )
        self.assertIsNone(self.store.invocations(lease.task_id)[0].result)
        self.assertIsNone(self.store.latest_checkpoint(lease.task_id))
        self.store.finish_tool(lease, result, checkpoint=checkpoint)
        self.assertEqual(self.store.invocations(lease.task_id)[0].result, result)
        self.assertEqual(self.store.latest_checkpoint(lease.task_id), checkpoint)

    def test_approval_is_single_use_and_prepares_operation_atomically(self) -> None:
        lease = self.claim()
        call = ToolCall(name="github.create_issue", arguments={"title": "A fix"})
        approval = self.store.request_approval(lease, call)
        self.assertFalse(self.store.consume_approval(lease, call))
        self.store.update_task(lease, state=TaskState.WAITING_FOR_APPROVAL)
        self.store.decide_approval(approval.id, True)
        self.assertEqual(self.store.get_task(lease.task_id).state, TaskState.QUEUED)
        resumed = self.store.claim("replacement")
        assert resumed is not None
        changed = call.model_copy(update={"arguments": {"title": "Unreviewed"}})
        self.assertFalse(self.store.consume_approval(resumed, changed))
        self.assertTrue(self.store.consume_approval(resumed, call))
        self.assertFalse(self.store.consume_approval(resumed, call))
        self.assertEqual(self.store.invocations(lease.task_id)[0].call, call)

    def test_approval_invalidated_by_checkpoint_change(self) -> None:
        lease = self.claim()
        call = ToolCall(name="github.publish")
        approval = self.store.request_approval(lease, call)
        checkpoint = Checkpoint(task_id=lease.task_id, artifact="artifact", sha256="a" * 64)
        self.store.save_checkpoint(lease, checkpoint)
        with self.assertRaises(ValueError):
            self.store.decide_approval(approval.id, True)

    def test_expired_approval_cannot_be_decided(self) -> None:
        lease = self.claim()
        approval = self.store.request_approval(lease, ToolCall(name="github.publish"))
        with Session(self.store.engine) as session, session.begin():
            row = session.get(ApprovalRow, approval.id)
            assert row is not None
            row.expires_at = now() - timedelta(seconds=1)
        with self.assertRaises(ValueError):
            self.store.decide_approval(approval.id, True)

    def test_rejected_approval_pauses_task(self) -> None:
        lease = self.claim()
        approval = self.store.request_approval(lease, ToolCall(name="github.publish"))
        self.store.update_task(lease, state=TaskState.WAITING_FOR_APPROVAL)
        self.store.decide_approval(approval.id, False)
        self.assertEqual(self.store.get_task(lease.task_id).state, TaskState.PAUSED)

    def pending_proposal(self, *, prepare=False):
        lease = self.claim()
        call = ToolCall(name="github_create_pr", arguments={"title": "Docs", "body": "Summary"})
        native = {
            "role": "model",
            "parts": [
                {
                    "function_call": {"id": call.id, "name": call.name, "args": call.arguments},
                    "thought_signature": "b3BhcXVl",
                }
            ],
            "_merlin_calls": [call.model_dump(mode="json")],
        }
        self.store.update_task(lease, history=[native])
        approval = self.store.request_approval(lease, call)
        if prepare:
            self.store.begin_tool(lease, call)
        self.store.update_task(lease, state=TaskState.WAITING_FOR_APPROVAL)
        return lease, call, approval, native

    def test_review_feedback_preserves_signed_history_rejects_old_call_and_requeues(self):
        lease, call, approval, native = self.pending_proposal()
        result = self.store.request_approval_changes(approval.id, "Disclose the failed typecheck")
        self.assertEqual(result.status, "rejected")
        task = self.store.get_task(lease.task_id)
        self.assertEqual(task.state, TaskState.QUEUED)
        self.assertEqual(task.history[0], native)
        response = task.history[-1]["parts"][0]["function_response"]
        self.assertEqual(response["id"], call.id)
        self.assertFalse(response["response"]["ok"])
        self.assertFalse(response["response"]["uncertain"])
        self.assertIn("Disclose the failed typecheck", response["response"]["output"])
        self.assertEqual(task.spec.permissions, [])
        resumed = self.store.claim("replacement")
        self.assertIsNotNone(resumed)
        self.assertFalse(self.store.consume_approval(resumed, call))
        self.assertFalse(self.store.invocations(task.id)[0].result.ok)
        with self.assertRaises(ValueError):
            self.store.decide_approval(approval.id, True)
        with self.assertRaises(ValueError):
            self.store.request_approval_changes(approval.id, "Repeat")

    def test_review_feedback_cannot_revise_an_operation_with_an_execution_intent(self):
        lease, _, approval, native = self.pending_proposal(prepare=True)
        with self.assertRaisesRegex(ValueError, "execution intent"):
            self.store.request_approval_changes(approval.id, "Revise")
        self.assertEqual(self.store.get_task(lease.task_id).history, [native])
        self.assertEqual(self.store.approvals(lease.task_id)[0].status, "pending")

    def test_review_feedback_rejects_blank_notes_and_unmatched_history(self):
        lease = self.claim()
        approval = self.store.request_approval(lease, ToolCall(name="github_create_pr"))
        self.store.update_task(lease, state=TaskState.WAITING_FOR_APPROVAL)
        with self.assertRaisesRegex(ValueError, "1 to 4000"):
            self.store.request_approval_changes(approval.id, "   ")
        with self.assertRaisesRegex(ValueError, "unanswered model tool call"):
            self.store.request_approval_changes(approval.id, "Revise")
        self.assertEqual(self.store.approvals(lease.task_id)[0].status, "pending")

    def test_budget_reservations_and_settlement(self) -> None:
        lease = self.claim(budget=Budget(max_calls=2, max_cost_usd=1, max_sandbox_seconds=100))
        reservation = self.store.reserve_usage(lease, cost_usd=0.7, calls=1, sandbox_seconds=50)
        with self.assertRaises(BudgetExceeded):
            self.store.reserve_usage(lease, cost_usd=0.4)
        self.store.settle_usage(lease, reservation, cost_usd=0.2)
        self.store.settle_usage(lease, reservation, cost_usd=0.2)
        self.store.reserve_usage(lease, cost_usd=0.8, calls=1, sandbox_seconds=50)
        self.assertEqual(self.store.usage(lease.task_id)["calls"], 2)
        self.assertAlmostEqual(self.store.usage(lease.task_id)["cost_usd"], 0.2)
        with self.assertRaises(BudgetExceeded):
            self.store.reserve_usage(lease, calls=1)
        with self.assertRaises(BudgetExceeded):
            self.store.reserve_usage(lease, sandbox_seconds=1)
        with self.assertRaises(ValueError):
            self.store.reserve_usage(lease, cost_usd=float("nan"))
        with self.assertRaises(ValueError):
            self.store.settle_usage(lease, reservation, cost_usd=0.3)

    def test_children_share_root_budget_and_cannot_expand_permissions(self) -> None:
        parent_lease = self.claim(permissions=["github.issue"], budget=Budget(max_calls=1))
        child = self.create(parent_id=parent_lease.task_id, permissions=["github.issue"])
        with self.assertRaises(ValueError):
            self.create(parent_id=parent_lease.task_id, permissions=["github.push"])
        child_lease = self.store.claim("child")
        assert child_lease is not None
        self.assertEqual(child_lease.task_id, child.id)
        self.store.reserve_usage(child_lease, calls=1)
        self.assertEqual(self.store.usage(parent_lease.task_id)["calls"], 1)
        with self.assertRaises(BudgetExceeded):
            self.store.reserve_usage(parent_lease, calls=1)

    def test_concurrent_reservations_do_not_overspend(self) -> None:
        parent = self.claim(budget=Budget(max_calls=2))
        children = [self.create(parent_id=parent.task_id) for _ in range(2)]
        leases = [self.store.claim(f"child-{index}") for index, _ in enumerate(children)]

        def reserve(lease: Lease | None) -> bool:
            assert lease is not None
            alternate = SQLTaskStore(self.url)
            try:
                alternate.reserve_usage(lease, calls=2)
                return True
            except BudgetExceeded:
                return False
            finally:
                alternate.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, leases))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.store.usage(parent.task_id)["calls"], 2)

    def test_cancel_keeps_running_lease_for_cleanup_and_stops_new_work(self) -> None:
        lease = self.claim()
        child = self.create(parent_id=lease.task_id)
        self.store.cancel(lease.task_id)
        task = self.store.get_task(lease.task_id)
        self.assertTrue(task.cancel_requested)
        self.assertEqual(task.state, TaskState.RUNNING)
        self.assertEqual(self.store.get_task(child.id).state, TaskState.CANCELLED)
        with self.assertRaises(StaleLease):
            self.store.begin_tool(lease, ToolCall(name="bash"))
        self.store.heartbeat(lease)
        self.store.update_task(lease, state=TaskState.CANCELLED)
        with self.assertRaises(ValueError):
            self.store.resume(lease.task_id)

    def test_queued_task_cancels_immediately(self) -> None:
        task = self.create()
        self.assertEqual(self.store.cancel(task.id).state, TaskState.CANCELLED)
        self.assertIsNone(self.store.claim("worker"))

    def test_checkpoint_is_immutable_and_restores_reference(self) -> None:
        lease = self.claim()
        checkpoint = Checkpoint(task_id=lease.task_id, artifact="one", sha256="a" * 64)
        self.store.save_checkpoint(lease, checkpoint)
        self.assertEqual(self.store.latest_checkpoint(lease.task_id), checkpoint)
        self.assertEqual(self.store.get_checkpoint(checkpoint.id), checkpoint)
        with self.assertRaises(ValueError):
            self.store.save_checkpoint(lease, checkpoint.model_copy(update={"artifact": "two"}))
        with self.assertRaises(ValueError):
            self.store.save_checkpoint(lease, checkpoint.model_copy(update={"sha256": "invalid"}))

    def test_checkpoint_can_seed_child_and_same_session_followup(self) -> None:
        lease = self.claim(session_id="shared")
        checkpoint = Checkpoint(task_id=lease.task_id, artifact="one", sha256="a" * 64)
        self.store.save_checkpoint(lease, checkpoint)
        child = self.create(parent_id=lease.task_id, base_checkpoint_id=checkpoint.id)
        self.assertEqual(self.store.latest_checkpoint(child.id), checkpoint)
        self.store.update_task(lease, state=TaskState.SUCCEEDED)
        followup = self.create(session_id="shared", base_checkpoint_id=checkpoint.id)
        self.assertEqual(self.store.latest_checkpoint(followup.id), checkpoint)
        with self.assertRaises(ValueError):
            self.create(session_id="unrelated", base_checkpoint_id=checkpoint.id)

    def test_task_identity_cannot_be_changed_by_worker(self) -> None:
        lease = self.claim()
        with self.assertRaises(ValueError):
            self.store.update_task(lease, generation=100)
        with self.assertRaises(ValueError):
            self.store.update_task(lease, spec={"prompt": "escape"})

    def test_delegation_creation_rejects_stale_lease_even_for_idempotent_retry(self) -> None:
        lease = self.claim()
        spec = TaskSpec(prompt="Child", repository="owner/repo", parent_id=lease.task_id)
        self.store.create_task(spec, "child-create", parent_lease=lease)
        self.expire(lease.task_id)
        with self.assertRaises(StaleLease):
            self.store.create_task(spec, "child-create", parent_lease=lease)

    def test_schema_records_have_no_naive_timestamps_at_contract_boundary(self) -> None:
        lease = self.claim()
        task = self.store.get_task(lease.task_id)
        self.assertIsNotNone(task.created_at.tzinfo)
        assert task.lease_until is not None
        self.assertIsNotNone(task.lease_until.tzinfo)
        self.assertTrue(all(event.created_at.tzinfo for event in self.store.events(task.id)))

    def test_unknown_ids_raise_key_errors(self) -> None:
        with self.assertRaises(KeyError):
            self.store.get_task("missing")
        with self.assertRaises(KeyError):
            self.store.events("missing")
        with self.assertRaises(KeyError):
            self.store.decide_approval("missing", True)
        with self.assertRaises(KeyError):
            self.store.get_checkpoint("missing")

    def test_default_global_root_and_child_concurrency_limits(self) -> None:
        limited = SQLTaskStore(self.url)
        try:
            first = self.create()
            self.create()
            lease = limited.claim("root")
            assert lease is not None
            self.assertEqual(lease.task_id, first.id)
            self.assertIsNone(limited.claim("second-root"))
            for _ in range(3):
                self.create(parent_id=first.id)
            self.assertIsNotNone(limited.claim("child-1"))
            self.assertIsNotNone(limited.claim("child-2"))
            self.assertIsNone(limited.claim("child-3"))
        finally:
            limited.close()
