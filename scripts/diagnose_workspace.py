#!/usr/bin/env python3
"""Diagnose E2B clone/checkpoint setup without model calls or GitHub publishing."""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import re
import unicodedata
from collections.abc import Sequence
from urllib.parse import quote
from uuid import uuid4

from pydantic import SecretStr

from merlin.config import Settings
from merlin.contracts import Budget, Task, TaskSpec
from merlin.integrations.github import GitHubService
from merlin.sandbox import E2BSandboxClient
from merlin.sandbox._remote import validate_archive


class Redactor:
    def __init__(self) -> None:
        self.values: set[str] = set()

    def remember(self, value: str | None) -> None:
        if value:
            self.values.update(
                (value, quote(value, safe=""), base64.b64encode(value.encode()).decode())
            )

    def clean(self, value: str) -> str:
        # Match before truncation, including multiline PEMs and URL-escaped tokens.
        for secret in sorted(self.values, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
            "[REDACTED KEY]",
            value,
            flags=re.DOTALL,
        )
        value = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", value)
        value = re.sub(
            r"\b(?:e2b_[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9_]+|"
            r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b",
            "[REDACTED]",
            value,
        )
        value = "".join(
            character
            for character in value
            if character in "\n\t" or unicodedata.category(character) != "Cc"
        )
        return value[:3000]


async def diagnose(repository: str) -> int:
    from e2b import AsyncSandbox

    settings = Settings()
    redactor = Redactor()
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr):
            redactor.remember(value.get_secret_value())
    if settings.github_private_key_file and settings.github_private_key_file.is_file():
        redactor.remember(settings.github_private_key_file.read_text())

    class DiagnosticSDK:
        @staticmethod
        async def create(**options):
            instance = await AsyncSandbox.create(**options)
            redactor.remember(instance._envd_access_token)
            redactor.remember(instance.traffic_access_token)
            return instance

    class DiagnosticGitHub(GitHubService):
        def _app_token(self) -> str:
            token = super()._app_token()
            redactor.remember(token)
            return token

    client = E2BSandboxClient(settings, sdk=DiagnosticSDK)
    github = DiagnosticGitHub(settings)
    workspace = None
    stage = "create and initialize workspace"
    status = 0
    print("Temporary E2B diagnostic: clone and snapshot only; no Gemini calls.", flush=True)
    try:
        # The sandbox expires after three minutes even if the diagnostic is interrupted.
        async with asyncio.timeout(150):
            task = Task(
                spec=TaskSpec(
                    prompt="Diagnose workspace startup",
                    repository=repository,
                    budget=Budget(max_sandbox_seconds=180),
                )
            )
            print("Checking: " + stage, flush=True)
            workspace = await client.create(task)
            stage = "obtain GitHub read access"
            print("Checking: " + stage, flush=True)
            token = await github.read_token(repository)
            redactor.remember(token)
            stage = "clone repository"
            print("Checking: " + stage, flush=True)
            await client.bootstrap(workspace, repository, token)
            stage = "capture recovery snapshot inside E2B"
            print("Checking: " + stage, flush=True)
            remote = f"/tmp/merlin-diagnostic-{uuid4().hex}.tar"
            await client._helper(workspace, "capture", {"target": remote}, timeout=60)
            stage = "download recovery snapshot"
            print("Checking: " + stage, flush=True)
            instance = await client._instance(workspace)
            data = bytes(await instance.files.read(remote, format="bytes", request_timeout=30))
            stage = "validate recovery snapshot"
            print("Checking: " + stage, flush=True)
            validate_archive(data)
            print("All workspace diagnostic checks passed.", flush=True)
    except Exception as error:
        status = 1
        print("Failed at: " + stage, flush=True)
        # Inspect the cause too: startup adapters intentionally expose only a
        # static summary to normal task events. No traceback or request headers.
        seen: set[int] = set()
        cause: BaseException | None = error
        while cause is not None and id(cause) not in seen and len(seen) < 4:
            seen.add(id(cause))
            print(redactor.clean(f"{type(cause).__name__}: {cause}"), flush=True)
            cause = cause.__cause__
    finally:
        if workspace is not None:
            try:
                await asyncio.wait_for(client.close(workspace), timeout=15)
                print("Temporary sandbox removed.", flush=True)
            except Exception as error:
                status = 1
                print(redactor.clean(f"Sandbox cleanup failed: {error}"), flush=True)
                print("Remove this diagnostic sandbox in the E2B dashboard: " + workspace.id)
        await github.aclose()
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repository as OWNER/REPOSITORY")
    args = parser.parse_args(argv)
    # Only the diagnostic's explicitly redacted output is emitted.
    logging.disable(logging.CRITICAL)
    return asyncio.run(diagnose(args.repo))


if __name__ == "__main__":
    raise SystemExit(main())
