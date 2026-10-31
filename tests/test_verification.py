import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "merlin_verification", Path(__file__).resolve().parents[1] / "scripts" / "verify.py"
)
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


class VerificationTests(unittest.TestCase):
    def test_failed_resolution_stops_before_install_or_validation(self):
        calls = []

        def fail(command, **options):
            calls.append(command)
            raise subprocess.CalledProcessError(2, command)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = verification.verify(runner=fail)
        self.assertEqual(result, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("uv", "lock", "--check"))

    def test_verification_uses_isolated_environment_without_mutating_callers_environment(self):
        calls = []
        original = os.environ.get("UV_PROJECT_ENVIRONMENT")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def record(command, **options):
                calls.append((command, options))

            with redirect_stdout(io.StringIO()):
                self.assertEqual(verification.verify(root=root, runner=record), 0)
            self.assertTrue(any("--locked" in command for command, _ in calls))
            for _command, options in calls:
                self.assertEqual(options["cwd"], root)
                self.assertTrue(options["check"])
                self.assertEqual(
                    options["env"]["UV_PROJECT_ENVIRONMENT"],
                    str(root / ".merlin" / "verification-venv"),
                )
        self.assertEqual(os.environ.get("UV_PROJECT_ENVIRONMENT"), original)

    def test_dry_run_never_executes_commands(self):
        def forbidden(*args, **kwargs):
            self.fail("Dry run executed a command")

        with redirect_stdout(io.StringIO()):
            self.assertEqual(verification.verify(dry_run=True, runner=forbidden), 0)
