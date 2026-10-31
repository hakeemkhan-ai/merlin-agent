"""Diagnostic staging, credential redaction, and disposable workspace cleanup."""

import io
import json
import tarfile
import unittest
from contextlib import ExitStack, redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

from merlin.config import Settings
from merlin.contracts import Workspace
from merlin.sandbox._remote import MANIFEST
from scripts.diagnose_workspace import Redactor, diagnose


class RedactionTests(unittest.TestCase):
    def test_secrets_are_removed_before_truncation_and_terminal_controls_are_stripped(self):
        redactor = Redactor()
        secret = "fixture token/with+special=characters"
        redactor.remember(secret)
        key = "-----BEGIN RSA PRIVATE KEY-----\nfixture\n-----END RSA PRIVATE KEY-----"
        message = f"{secret} {quote(secret, safe='')} {key} Bearer another-token \x1b\x07"
        output = redactor.clean(message)
        for value in (secret, quote(secret, safe=""), "fixture\n", "another-token", "\x1b", "\x07"):
            self.assertNotIn(value, output)
        self.assertIn("[REDACTED]", output)


class WorkspaceDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def run_diagnostic(self, download_error=None):
        stream = io.BytesIO()
        manifest = json.dumps({"version": 1, "files": {}}).encode()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))
        settings = Settings(
            _env_file=None,
            gemini_api_key="fixture-gemini-secret",
            e2b_api_key="fixture-e2b-secret",
            github_private_key_file=None,
        )
        workspace = Workspace(id="diagnostic-workspace")
        instance = SimpleNamespace(
            files=SimpleNamespace(
                read=AsyncMock(return_value=stream.getvalue(), side_effect=download_error)
            )
        )
        client = SimpleNamespace(
            create=AsyncMock(return_value=workspace),
            bootstrap=AsyncMock(),
            _helper=AsyncMock(),
            _instance=AsyncMock(return_value=instance),
            close=AsyncMock(),
        )
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch("scripts.diagnose_workspace.Settings", return_value=settings))
            stack.enter_context(
                patch("scripts.diagnose_workspace.E2BSandboxClient", return_value=client)
            )
            stack.enter_context(
                patch(
                    "scripts.diagnose_workspace.GitHubService.read_token",
                    new=AsyncMock(return_value="fixture-installation-token"),
                )
            )
            stack.enter_context(redirect_stdout(output))
            status = await diagnose("owner/repo")
        client.close.assert_awaited_once_with(workspace)
        self.assertEqual(client.create.call_args.args[0].spec.budget.max_sandbox_seconds, 180)
        return status, output.getvalue()

    async def test_download_failure_identifies_stage_redacts_credentials_and_cleans_up(self):
        error = RuntimeError("fixture-gemini-secret fixture-e2b-secret fixture-installation-token")
        status, output = await self.run_diagnostic(error)
        self.assertEqual(status, 1)
        self.assertIn("Failed at: download recovery snapshot", output)
        self.assertIn("Temporary sandbox removed", output)
        for secret in ("fixture-gemini-secret", "fixture-e2b-secret", "fixture-installation-token"):
            self.assertNotIn(secret, output)

    async def test_complete_diagnostic_validates_real_archive_and_cleans_up(self):
        status, output = await self.run_diagnostic()
        self.assertEqual(status, 0)
        self.assertIn("All workspace diagnostic checks passed", output)
