import importlib.util
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import e2b_verify

spec = importlib.util.spec_from_file_location(
    "merlin_verification", Path(__file__).resolve().parents[1] / "scripts" / "verify.py"
)
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


class VerificationTests(unittest.TestCase):
    def test_source_snapshot_excludes_credentials_and_special_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_paths = [
                ".ssh/id_rsa",
                ".kube/config",
                ".netrc",
                ".git-credentials",
                "nested/credentials/token.json",
                "nested/SECRETS/token.json",
                "nested/ID_ED25519",
                "nested/certificate.p12",
                "nested/certificate.PFX",
                "nested/.env.production",
            ]
            for name in private_paths:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic-credential-must-not-transfer")
            (root / "source.py").write_text("pass\n")
            (root / ".env.example").write_text("EXAMPLE=placeholder\n")
            (root / "link").symlink_to(root / ".netrc")
            os.mkfifo(root / "pipe")
            with patch.object(e2b_verify, "ROOT", root):
                data = e2b_verify.snapshot()
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                members = archive.getmembers()
                self.assertEqual(
                    {member.name for member in members if member.isfile()},
                    {"source.py", ".env.example"},
                )
                self.assertTrue(all(member.isdir() or member.isfile() for member in members))

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
