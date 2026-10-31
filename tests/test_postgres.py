"""Real PostgreSQL locking and migrations; never substitute SQLite results."""

import os
import unittest
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

try:
    import pytest
except ImportError:
    pass
else:
    pytestmark = pytest.mark.postgres

from sqlalchemy import create_engine, inspect, text, update
from sqlalchemy.engine import make_url

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
from merlin.persistence.models import Base, TaskRow


@unittest.skipUnless(os.getenv("MERLIN_POSTGRES_TESTS") == "1", "PostgreSQL tests are opt-in")
class PostgreSQLTests(unittest.TestCase):
    def setUp(self) -> None:
        url = make_url(os.environ["MERLIN_TEST_POSTGRES_URL"])
        if url.get_backend_name() != "postgresql" or not (url.database or "").startswith(
            "merlin_test"
        ):
            raise ValueError("Use a dedicated PostgreSQL database named merlin_test...")
        # Bound connection attempts and lock waits, including failure cleanup.
        url = url.set(drivername="postgresql+psycopg").update_query_dict(
            {"connect_timeout": "5", "options": "-cstatement_timeout=10000 -clock_timeout=5000"}
        )
        self.schema = "merlin_test_" + uuid4().hex
        self.admin = create_engine(url)
        self.addCleanup(self.admin.dispose)
        # unittest runs cleanups even when setUp raises; tearDown would not run.
        self.addCleanup(self._drop_schema)
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        scoped = url.update_query_dict(
            {
                "options": (
                    f"-csearch_path={self.schema} -cstatement_timeout=10000 -clock_timeout=5000"
                )
            }
        )
        self.scoped_url = scoped.render_as_string(hide_password=False)
        self.first = self._store()
        self.second = self._store()
        with self.first.engine.connect() as connection:
            self.assertEqual(connection.scalar(text("SELECT current_schema()")), self.schema)
        self.first.create_schema()

    def _drop_schema(self) -> None:
        with self.admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))

    def _store(self, *, max_running_roots: int = 1) -> SQLTaskStore:
        store = SQLTaskStore(self.scoped_url, max_running_roots=max_running_roots)
        self.addCleanup(store.close)
        return store

    def _race[Result](self, operations: Sequence[Callable[[], Result]]) -> list[Result]:
        # Every worker is scheduled before the barrier. Synchronizing inside a
        # database lock would deadlock when an advisory-lock loser returns early.
        start = Barrier(len(operations), timeout=5)

        def run(operation: Callable[[], Result]) -> Result:
            start.wait()
            return operation()

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            futures = [pool.submit(run, operation) for operation in operations]
            return [future.result(timeout=15) for future in futures]

    def test_global_claim_cap_holds_across_independent_connections(self) -> None:
        tasks = [
            self.first.create_task(
                TaskSpec(prompt=f"Task {number}", repository="owner/repo", session_id=f"s{number}")
            )
            for number in range(6)
        ]
        self.assertEqual(len({task.spec.session_id for task in tasks}), 6)
        stores = [self.first, self.second, self._store(), self._store()]
        claims = self._race(
            [
                lambda store=store, index=index: store.claim(f"worker-{index}")
                for index, store in enumerate(stores)
            ]
        )
        leases = [claim for claim in claims if claim is not None]
        self.assertEqual(len(leases), 1)
        running = [task for task in self.second.list_tasks() if task.state == TaskState.RUNNING]
        self.assertEqual([task.id for task in running], [leases[0].task_id])
        blocked = self._race([lambda store=store: store.claim("blocked") for store in stores])
        self.assertEqual(blocked, [None] * len(stores))
        self.second.update_task(leases[0], state=TaskState.SUCCEEDED)
        next_claims = self._race(
            [lambda store=store: store.claim("next-worker") for store in stores]
        )
        next_leases = [claim for claim in next_claims if claim is not None]
        self.assertEqual(len(next_leases), 1)
        self.assertNotEqual(next_leases[0].task_id, leases[0].task_id)

    def test_concurrent_creation_cannot_mix_repositories_in_one_session(self) -> None:
        def submit(store: SQLTaskStore, repository: str) -> str:
            try:
                store.create_task(
                    TaskSpec(prompt="race", repository=repository, session_id="repository-race")
                )
            except ValueError:
                return "rejected"
            return "created"

        outcomes = self._race(
            [
                lambda: submit(self.first, "owner/first"),
                lambda: submit(self.second, "owner/second"),
            ]
        )
        self.assertEqual(sorted(outcomes), ["created", "rejected"])
        self.assertEqual(len(self.first.list_tasks("repository-race")), 1)

    def test_session_gate_serializes_followups_while_other_sessions_progress(self) -> None:
        same_session = [
            self.first.create_task(
                TaskSpec(prompt=f"Followup {index}", repository="owner/repo", session_id="shared")
            )
            for index in range(2)
        ]
        independent = self.first.create_task(
            TaskSpec(prompt="Independent", repository="owner/repo", session_id="independent")
        )
        stores = [self._store(max_running_roots=3) for _ in range(3)]
        leases = []
        # Try-lock losers may need another attempt after the winning transaction
        # commits. Three bounded rounds can fill both eligible session slots.
        for _ in range(3):
            leases.extend(
                claim
                for claim in self._race(
                    [lambda store=store: store.claim("session-worker") for store in stores]
                )
                if claim is not None
            )
        self.assertEqual(len(leases), 2)
        self.assertEqual(len({lease.task_id for lease in leases}), 2)
        self.assertIn(independent.id, {lease.task_id for lease in leases})
        shared_lease = next(lease for lease in leases if lease.task_id != independent.id)
        self.assertIn(shared_lease.task_id, {task.id for task in same_session})
        stores[0].update_task(shared_lease, state=TaskState.SUCCEEDED)
        followup = stores[1].claim("followup-worker")
        assert followup is not None
        self.assertEqual(
            {shared_lease.task_id, followup.task_id}, {task.id for task in same_session}
        )

    def test_child_claim_cap_holds_across_independent_connections(self) -> None:
        parent = self.first.create_task(TaskSpec(prompt="Parent", repository="owner/repo"))
        parent_lease = self.first.claim("parent")
        assert parent_lease is not None
        children = [
            self.first.create_task(
                TaskSpec(prompt=f"Child {index}", repository="owner/repo", parent_id=parent.id),
                parent_lease=parent_lease,
            )
            for index in range(4)
        ]
        stores = [self.first, self.second, self._store(), self._store()]
        leases = []
        for _ in range(3):
            leases.extend(
                claim
                for claim in self._race(
                    [lambda store=store: store.claim("child-worker") for store in stores]
                )
                if claim is not None
            )
        self.assertEqual(len(leases), 2)
        self.assertEqual(len({lease.task_id for lease in leases}), 2)
        self.assertTrue({lease.task_id for lease in leases} <= {child.id for child in children})
        self.assertEqual(self.second.get_task(parent.id).generation, parent_lease.generation)
        self.assertIsNone(self.second.claim("over-child-cap"))

    def test_expired_lease_is_reclaimed_and_stale_completion_is_fenced(self) -> None:
        task = self.first.create_task(TaskSpec(prompt="Recover", repository="owner/repo"))
        original = self.first.claim("original")
        assert original is not None
        call = ToolCall(name="bash", arguments={"command": "pytest"})
        self.first.begin_tool(original, call)
        reservation = self.first.reserve_usage(original, cost_usd=0.5, calls=1)
        checkpoint = Checkpoint(task_id=task.id, artifact="test-snapshot", sha256="a" * 64)
        with self.second.engine.begin() as connection:
            connection.execute(
                update(TaskRow)
                .where(TaskRow.id == task.id)
                .values(lease_until=now() - timedelta(seconds=1))
            )
        with self.assertRaises(StaleLease):
            self.first.heartbeat(original)
        replacement = self.second.claim("replacement")
        assert replacement is not None
        self.assertEqual(replacement.task_id, original.task_id)
        self.assertEqual(replacement.generation, original.generation + 1)
        result = ToolResult(call_id=call.id, output="recovered result")
        before = self.second.events(task.id)
        for operation in (
            lambda: self.first.finish_tool(original, result, checkpoint=checkpoint),
            lambda: self.first.update_task(original, state=TaskState.SUCCEEDED, result="late"),
            lambda: self.first.save_checkpoint(original, checkpoint),
            lambda: self.first.reserve_usage(original, calls=1),
            lambda: self.first.settle_usage(original, reservation, cost_usd=0.2),
        ):
            with self.assertRaises(StaleLease):
                operation()
        self.assertEqual(self.second.events(task.id), before)
        self.assertIsNone(self.second.invocations(task.id)[0].result)
        self.assertIsNone(self.second.latest_checkpoint(task.id))
        self.assertAlmostEqual(self.second.usage(task.id)["reserved_cost_usd"], 0.5)
        self.second.finish_tool(replacement, result, checkpoint=checkpoint)
        self.second.settle_usage(replacement, reservation, cost_usd=0.2)
        self.second.update_task(replacement, state=TaskState.SUCCEEDED, result="recovered")
        self.assertEqual(self.first.latest_checkpoint(task.id), checkpoint)
        self.assertEqual(self.first.invocations(task.id)[0].result, result)
        self.assertEqual(self.first.get_task(task.id).result, "recovered")
        claims = [event for event in self.first.events(task.id) if event.kind == "task.claimed"]
        self.assertEqual([event.data["reclaimed"] for event in claims], [False, True])

    def test_concurrent_children_reserve_one_shared_root_budget_atomically(self) -> None:
        parent = self.first.create_task(
            TaskSpec(
                prompt="Parent",
                repository="owner/repo",
                budget=Budget(max_calls=2, max_cost_usd=1, max_sandbox_seconds=10),
            )
        )
        parent_lease = self.first.claim("parent")
        assert parent_lease is not None
        children = [
            self.first.create_task(
                TaskSpec(prompt=f"Child {index}", repository="owner/repo", parent_id=parent.id),
                parent_lease=parent_lease,
            )
            for index in range(2)
        ]
        first_lease = self.first.claim("child-one")
        second_lease = self.second.claim("child-two")
        assert first_lease is not None and second_lease is not None
        leases = [first_lease, second_lease]
        self.assertEqual({lease.task_id for lease in leases}, {child.id for child in children})

        def reserve(store: SQLTaskStore, lease: Lease) -> str | None:
            try:
                return store.reserve_usage(lease, calls=2, cost_usd=0.75, sandbox_seconds=10)
            except BudgetExceeded:
                return None

        outcomes = self._race(
            [
                lambda store=store, lease=lease: reserve(store, lease)
                for store, lease in zip((self.first, self.second), leases, strict=True)
            ]
        )
        self.assertEqual(sum(outcome is not None for outcome in outcomes), 1)
        usage = self.second.usage(parent.id)
        self.assertEqual(usage["calls"], 2)
        self.assertEqual(usage["sandbox_seconds"], 10)
        self.assertAlmostEqual(usage["reserved_cost_usd"], 0.75)
        self.assertEqual(usage["cost_usd"], 0)
        for child in children:
            self.assertEqual(self.first.usage(child.id), usage)
        self.assertEqual(
            sum(
                event.kind == "usage.reserved"
                for child in children
                for event in self.second.events(child.id)
            ),
            1,
        )
        winner = next(index for index, outcome in enumerate(outcomes) if outcome is not None)
        reservation = outcomes[winner]
        assert reservation is not None
        self.second.settle_usage(leases[winner], reservation, cost_usd=0.25)
        self.first.settle_usage(leases[winner], reservation, cost_usd=0.25)
        settled = self.first.usage(parent.id)
        self.assertAlmostEqual(settled["reserved_cost_usd"], 0)
        self.assertAlmostEqual(settled["cost_usd"], 0.25)

    def test_parent_cancel_flags_running_child_and_keeps_cleanup_lease(self) -> None:
        parent = self.first.create_task(TaskSpec(prompt="Parent", repository="owner/repo"))
        parent_lease = self.first.claim("parent")
        assert parent_lease is not None
        children = [
            self.first.create_task(
                TaskSpec(prompt=f"Child {index}", repository="owner/repo", parent_id=parent.id),
                parent_lease=parent_lease,
            )
            for index in range(2)
        ]
        child_lease = self.second.claim("child")
        assert child_lease is not None
        call = ToolCall(name="bash")
        self.second.begin_tool(child_lease, call)
        self.first.cancel(parent.id)
        running = self.second.get_task(child_lease.task_id)
        self.assertTrue(running.cancel_requested)
        self.assertEqual(running.state, TaskState.RUNNING)
        self.assertEqual(running.worker_id, child_lease.worker_id)
        self.assertEqual(running.generation, child_lease.generation)
        queued_id = next(child.id for child in children if child.id != child_lease.task_id)
        queued = self.second.get_task(queued_id)
        self.assertTrue(queued.cancel_requested)
        self.assertEqual(queued.state, TaskState.CANCELLED)
        for operation in (
            lambda: self.second.begin_tool(child_lease, ToolCall(name="bash")),
            lambda: self.second.reserve_usage(child_lease, calls=1),
            lambda: self.second.update_task(child_lease, state=TaskState.SUCCEEDED),
        ):
            with self.assertRaises(StaleLease):
                operation()
        self.second.heartbeat(child_lease)
        self.second.finish_tool(child_lease, ToolResult(call_id=call.id, output="cleanup complete"))
        self.second.update_task(child_lease, state=TaskState.CANCELLED)
        self.first.update_task(parent_lease, state=TaskState.CANCELLED)
        self.assertTrue(all(task.state == TaskState.CANCELLED for task in self.first.list_tasks()))
        self.assertIsNone(self.second.claim("late-worker"))

    def test_initial_migration_upgrade_and_downgrade(self) -> None:
        from alembic import command
        from alembic.config import Config
        from alembic.migration import MigrationContext
        from alembic.script import ScriptDirectory

        Base.metadata.drop_all(self.first.engine)
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", self.scoped_url.replace("%", "%%"))
        with patch.dict(os.environ, {"MERLIN_DATABASE_URL": self.scoped_url}):
            command.upgrade(config, "head")
            command.upgrade(config, "head")
            with self.first.engine.connect() as connection:
                self.assertEqual(
                    MigrationContext.configure(connection).get_current_revision(),
                    ScriptDirectory.from_config(config).get_current_head(),
                )
            task = self.first.create_task(TaskSpec(prompt="Migrated", repository="owner/repo"))
            self.assertEqual(self.second.get_task(task.id).spec.prompt, "Migrated")
            command.downgrade(config, "base")
            self.assertFalse(
                set(Base.metadata.tables) & set(inspect(self.first.engine).get_table_names())
            )
            command.upgrade(config, "head")
            self.assertEqual(self.second.list_tasks(), [])

    def test_reopened_store_preserves_checkpoint_intent_and_usage_ledger(self) -> None:
        spec = TaskSpec(prompt="Restart", repository="owner/repo")
        task = self.first.create_task(spec, "restart-request")
        lease = self.first.claim("worker-before-restart")
        assert lease is not None
        call = ToolCall(name="bash", arguments={"command": "pytest"})
        self.first.begin_tool(lease, call)
        checkpoint = Checkpoint(task_id=task.id, artifact="restart-snapshot", sha256="b" * 64)
        self.first.save_checkpoint(lease, checkpoint)
        reservation = self.first.reserve_usage(lease, calls=1, cost_usd=0.4, sandbox_seconds=1)
        self.first.update_task(lease, sandbox_id="test-sandbox", history=[{"role": "user"}])
        self.first.close()
        self.second.close()
        reopened = self._store()
        self.assertEqual(reopened.create_task(spec, "restart-request").id, task.id)
        self.assertEqual(reopened.latest_checkpoint(task.id), checkpoint)
        self.assertEqual(reopened.get_task(task.id).sandbox_id, "test-sandbox")
        self.assertEqual(reopened.get_task(task.id).history, [{"role": "user"}])
        self.assertIsNone(reopened.begin_tool(lease, call).result)
        self.assertEqual(len(reopened.invocations(task.id)), 1)
        self.assertAlmostEqual(reopened.usage(task.id)["reserved_cost_usd"], 0.4)
        result = ToolResult(call_id=call.id, output="finished after restart")
        reopened.finish_tool(lease, result)
        reopened.settle_usage(lease, reservation, cost_usd=0.2)
        reopened.update_task(lease, state=TaskState.SUCCEEDED, result="done")
        reopened.close()
        final = self._store()
        self.assertEqual(final.get_task(task.id).state, TaskState.SUCCEEDED)
        self.assertEqual(final.invocations(task.id)[0].result, result)
        self.assertEqual(final.latest_checkpoint(task.id), checkpoint)
        self.assertEqual(final.usage(task.id)["calls"], 1)
        self.assertAlmostEqual(final.usage(task.id)["reserved_cost_usd"], 0)
        self.assertAlmostEqual(final.usage(task.id)["cost_usd"], 0.2)
        events = final.events(task.id)
        self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
