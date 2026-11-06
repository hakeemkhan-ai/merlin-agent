#!/usr/bin/env python3
"""Transfer a credential-free source snapshot and run checks only inside E2B.

The local process performs infrastructure calls and file transfer only. It never
imports application code or runs repository tests on the control host. State is
kept under the ignored .merlin directory so validation survives terminal resets.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import re
import shlex
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".merlin" / "e2b-verification.json"
REMOTE = "/home/user/merlin-verify"
UV = "/home/user/.local/bin/uv"
EXCLUDED = {
    ".git",
    ".env",
    ".agents",
    ".codex",
    ".aws",
    ".venv",
    ".merlin",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    "artifacts",
    "backups",
    "secrets",
    "credentials",
    ".ssh",
    ".kube",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
}
EXCLUDED_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".crt",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pyc",
    ".log",
}


def snapshot() -> bytes:
    def allowed(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        path = Path(member.name)
        if not (member.isdir() or member.isfile()):
            return None
        # Infrastructure-only: do not import application code on the control host.
        # Check every component so nested credentials and case variants are excluded.
        for part in path.parts:
            name = part.casefold()
            if (
                name in EXCLUDED
                or Path(name).suffix in EXCLUDED_SUFFIXES
                or name.endswith(".egg-info")
                or name.startswith(".env")
                and name != ".env.example"
            ):
                return None
        return member

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for path in sorted(ROOT.iterdir()):
            archive.add(path, arcname=path.name, filter=allowed)
    return data.getvalue()


def configuration() -> dict[str, str]:
    from dotenv import dotenv_values

    return {
        key: value
        for key, value in {**dotenv_values(ROOT / ".env"), **os.environ}.items()
        if isinstance(value, str)
    }


async def main() -> int:
    from e2b import AsyncSandbox
    from e2b.sandbox.commands.command_handle import CommandExitException

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "sync", "run", "put", "fetch", "kill"])
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args()
    config = configuration()
    key = config.get("MERLIN_E2B_API_KEY") or config.get("E2B_API_KEY")
    if not key:
        print("E2B verification requires a configured API key.")
        return 2
    logging.disable(logging.CRITICAL)

    def clean(value: str) -> str:
        for name, secret in config.items():
            if (
                len(secret) >= 8
                and name.startswith("MERLIN_")
                and name.endswith(("KEY", "TOKEN", "SECRET", "PASSWORD"))
            ):
                value = value.replace(secret, "[REDACTED]")
        return re.sub(
            r"\b(?:e2b_[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]+)\b", "[REDACTED]", value
        )

    async def run(sandbox: AsyncSandbox, command: str, *, cwd: str = REMOTE) -> int:
        try:
            result = await sandbox.commands.run(
                command,
                cwd=cwd,
                timeout=900,
                request_timeout=920,
                on_stdout=lambda value: print(clean(value), end="", flush=True),
                on_stderr=lambda value: print(clean(value), end="", file=sys.stderr, flush=True),
                envs={"UV_CACHE_DIR": "/home/user/.cache/uv", "PYTHONUNBUFFERED": "1"},
            )
            return result.exit_code
        except CommandExitException as error:
            return error.exit_code

    async def upload(sandbox: AsyncSandbox) -> int:
        target = "/tmp/merlin-source-" + uuid4().hex + ".tar.gz"
        await sandbox.files.write(target, snapshot())
        code = await run(
            sandbox, f"mkdir -p {REMOTE} && tar xzf {target} -C {REMOTE}", cwd="/home/user"
        )
        await sandbox.files.remove(target)
        print("Credential-free source snapshot uploaded.")
        return code

    try:
        if args.action == "create":
            async with asyncio.timeout(45):
                sandbox = await AsyncSandbox.create(
                    template=config.get("MERLIN_E2B_TEMPLATE", "merlin"),
                    api_key=key,
                    timeout=3600,
                    request_timeout=30,
                    metadata={"purpose": "merlin-verification"},
                    lifecycle={"on_timeout": "kill"},
                )
            STATE.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump({"sandbox_id": sandbox.sandbox_id}, output)
            print("Disposable E2B verification workspace created.")
            code = await upload(sandbox)
            if code:
                return code
            code = await run(sandbox, "curl -LsSf https://astral.sh/uv/0.12.9/install.sh | sh")
            if code:
                return code
            return await run(sandbox, f"{UV} sync --locked --dev")
        state = json.loads(STATE.read_text())
        sandbox = await AsyncSandbox.connect(state["sandbox_id"], api_key=key)
        if args.action == "kill":
            await sandbox.kill()
            STATE.unlink(missing_ok=True)
            print("Verification sandbox removed.")
            return 0
        if args.action == "sync":
            return await upload(sandbox)
        if args.action == "run" and len(args.arguments) == 1:
            return await run(sandbox, args.arguments[0])
        if args.action == "put" and len(args.arguments) == 2:
            temporary = "/tmp/merlin-transfer-" + uuid4().hex
            await sandbox.files.write(temporary, Path(args.arguments[0]).read_bytes())
            # Fresh uploads avoid E2B's existing-file ownership/cache behavior;
            # rename publishes the complete file in one filesystem operation.
            return await run(
                sandbox, f"mv -- {shlex.quote(temporary)} {shlex.quote(args.arguments[1])}"
            )
        if args.action == "fetch" and len(args.arguments) == 2:
            path = Path(args.arguments[1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(await sandbox.files.read(args.arguments[0], format="bytes")))
            return 0
        parser.error("run requires a command; put/fetch require a source and destination")
    except Exception as error:
        # SDK exceptions can embed headers and signed URLs. Public output must
        # not include exception bodies or traceback locals.
        print(f"Verification transport failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
