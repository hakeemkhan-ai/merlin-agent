"""Driver guards: disposable scope, fail-fast preflight, and cleanup on failure."""

import importlib.util
import io
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "runtime_smoke", Path(__file__).resolve().parents[1] / "scripts" / "runtime_smoke.py"
)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class RuntimeSmokeTests(unittest.TestCase):
    def test_unavailable_daemon_never_starts_or_removes_resources(self):
        calls = []

        def unavailable(command, **options):
            calls.append(command)
            raise subprocess.CalledProcessError(1, command)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = driver.run(runner=unavailable)
        self.assertEqual(status, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("docker", "version"))

    def test_failed_checks_remove_only_the_unique_test_project(self):
        calls = []

        def fail_checks(command, **options):
            calls.append((command, options))
            if "/checks/test_postgres.py" in command:
                raise subprocess.CalledProcessError(1, command)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(driver.run(runner=fail_checks), 1)
        cleanup = calls[-1][0]
        self.assertIn("down", cleanup)
        self.assertIn("--volumes", cleanup)
        project = cleanup[cleanup.index("--project-name") + 1]
        self.assertRegex(project, r"^merlin-smoke-[a-f0-9]{12}$")
        self.assertIn("compose.smoke.yaml", cleanup[cleanup.index("-f") + 1])
        for command, _ in calls[1:]:
            self.assertEqual(command[command.index("--project-name") + 1], project)

    def test_full_sequence_crashes_worker_then_verifies_recovery_without_secret_output(self):
        calls = []
        output = io.StringIO()

        def record(command, **options):
            calls.append((command, options))

        with redirect_stdout(output):
            self.assertEqual(driver.run(runner=record), 0)
        commands = [command for command, _ in calls]
        self.assertTrue(any("SIGKILL" in command for command in commands))
        self.assertTrue(any(command[-1] == "verify-paused" for command in commands))
        ready_index = next(i for i, command in enumerate(commands) if command[-1] == "ready")
        complete_index = next(i for i, command in enumerate(commands) if command[-1] == "complete")
        self.assertLess(ready_index, complete_index)
        # Claim advisory locks are database-scoped, so the live fixture worker
        # must not race the independent PostgreSQL locking tests.
        pg_index = next(
            i for i, command in enumerate(commands) if "/checks/test_postgres.py" in command
        )
        start_index = next(
            i
            for i, command in enumerate(commands)
            if command[-2:] == ("api", "worker") and "up" in command
        )
        self.assertLess(pg_index, start_index)
        self.assertEqual(
            commands[-1][-5:], ("down", "--volumes", "--remove-orphans", "--timeout", "5")
        )
        for _, options in calls:
            self.assertTrue(options["check"])
            self.assertNotIn(options["env"]["MERLIN_SMOKE_PASSWORD"], output.getvalue())
            self.assertNotIn(options["env"]["MERLIN_SMOKE_OWNER_TOKEN"], output.getvalue())

    def test_dry_run_executes_nothing(self):
        def forbidden(*args, **kwargs):
            self.fail("Dry run executed Docker")

        with redirect_stdout(io.StringIO()):
            self.assertEqual(driver.run(dry_run=True, runner=forbidden), 0)

    def test_cleanup_failure_prevents_success(self):
        def fail_cleanup(command, **options):
            if "down" in command:
                raise subprocess.CalledProcessError(1, command)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(driver.run(runner=fail_cleanup), 1)
