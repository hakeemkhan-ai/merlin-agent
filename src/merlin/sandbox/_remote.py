"""Standard-library helper shipped to E2B; never executed by the control service."""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_OUTPUT = 64000
TEMPLATE_TOOL_BIN = "/opt/merlin-tools/bin"
MANIFEST = "__MERLIN_MANIFEST__.json"
CACHE_NAMES = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
SECRET_NAMES = {
    ".ssh",
    ".aws",
    ".kube",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "credentials",
    "secrets",
}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}


def excluded(path: str, caches: bool = True) -> bool:
    for part in PurePosixPath(path).parts:
        part = part.casefold()
        if part in SECRET_NAMES or (caches and part in CACHE_NAMES):
            return True
        if PurePosixPath(part).suffix in SECRET_SUFFIXES:
            return True
        if (part == ".env" or part.startswith(".env.")) and part not in ENV_EXAMPLES:
            return True
    return False


def relative_name(value: str, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid workspace-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in value):
        raise ValueError("Path must remain within the workspace")
    normalized = path.as_posix()
    if normalized == "." and not allow_dot:
        raise ValueError("A file path is required")
    if len(normalized) > 1000:
        raise ValueError("Path is too long")
    return normalized


def safe_path(root: Path, value: str, allow_dot: bool = False) -> Path:
    name = relative_name(value, allow_dot)
    if excluded(name, caches=False):
        raise ValueError("Secret paths cannot be accessed by file tools")
    root = root.resolve()
    candidate = root / name
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Symlink escapes workspace")
    # Reject all symlinks for deterministic replacement and reads.
    current = root
    for part in PurePosixPath(name).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlink paths are unsupported")
    return candidate


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Expected a regular file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("File exceeds size limit")
    return path.read_bytes()


def source_files(root: Path, path: Path) -> Iterator[str]:
    if path.is_file():
        if not path.is_symlink():
            yield path.relative_to(root).as_posix()
        return
    for folder, directories, names in os.walk(path, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name != ".git"
            and not (Path(folder) / name).is_symlink()
            and not excluded((Path(folder) / name).relative_to(root).as_posix())
        )
        for name in sorted(names):
            entry = Path(folder) / name
            relative = entry.relative_to(root).as_posix()
            if not excluded(relative) and not entry.is_symlink() and entry.is_file():
                yield relative


def _write(path: Path, content: bytes) -> None:
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("File exceeds size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, name = tempfile.mkstemp(prefix=".merlin-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_archive(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("Checkpoint exceeds archive size limit")
    seen: set[str] = set()
    total = 0
    hashes: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise ValueError("Checkpoint has too many members")
        manifest: dict[str, Any] | None = None
        for member in members:
            name = relative_name(member.name)
            if name != member.name or name in seen or (name != MANIFEST and excluded(name)):
                raise ValueError("Duplicate or excluded checkpoint path")
            seen.add(name)
            if not member.isfile() or member.size < 0 or member.size > MAX_FILE_BYTES:
                raise ValueError("Checkpoint supports bounded regular files only")
            total += member.size
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("Checkpoint exceeds expanded size limit")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Missing checkpoint member")
            value = source.read(MAX_FILE_BYTES + 1)
            if len(value) != member.size:
                raise ValueError("Truncated checkpoint member")
            if name == MANIFEST:
                manifest = json.loads(value)
            else:
                hashes[name] = digest(value)
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != 1
            or manifest.get("files") != hashes
        ):
            raise ValueError("Checkpoint manifest or member checksum mismatch")
        # A file cannot be an ancestor of another file.
        for name in hashes:
            if any(parent.as_posix() in hashes for parent in PurePosixPath(name).parents):
                raise ValueError("Checkpoint file/directory conflict")
        return manifest


def capture(root: Path, target: Path) -> dict[str, Any]:
    files: dict[str, str] = {}
    total = 0
    with tarfile.open(target, "w") as archive:
        for folder, directories, names in os.walk(root, followlinks=False):
            directories[:] = sorted(
                d
                for d in directories
                if not excluded((Path(folder) / d).relative_to(root).as_posix())
            )
            for directory in directories:
                if (Path(folder) / directory).is_symlink():
                    raise ValueError("Checkpoint cannot contain symlinks")
            for name in sorted(names):
                path = Path(folder) / name
                relative = path.relative_to(root).as_posix()
                if excluded(relative):
                    continue
                if relative == MANIFEST:
                    raise ValueError("Reserved checkpoint filename")
                value = _read(path)
                total += len(value)
                if total > MAX_ARCHIVE_BYTES - 1024 * 1024 or len(files) >= MAX_MEMBERS - 1:
                    raise ValueError("Checkpoint exceeds size/member limit")
                info = tarfile.TarInfo(relative)
                info.size = len(value)
                info.mode = path.stat().st_mode & 0o777
                archive.addfile(info, io.BytesIO(value))
                files[relative] = digest(value)
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            base_sha = result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            base_sha = ""
        manifest = {"version": 1, "files": files, "base_sha": base_sha}
        value = json.dumps(manifest, sort_keys=True).encode()
        info = tarfile.TarInfo(MANIFEST)
        info.size = len(value)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(value))
    if target.stat().st_size > MAX_ARCHIVE_BYTES:
        target.unlink(missing_ok=True)
        raise ValueError("Checkpoint archive exceeds size limit")
    return {"path": str(target), "base_sha": base_sha, "files": len(files)}


def restore(root: Path, source: Path) -> dict[str, Any]:
    data = source.read_bytes()
    manifest = validate_archive(data)
    temporary = Path(tempfile.mkdtemp(prefix=".merlin-restore-", dir=root.parent))
    old = root.parent / (".merlin-old-" + uuid4().hex)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive.getmembers():
                if member.name == MANIFEST:
                    continue
                destination = temporary / relative_name(member.name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                reader = archive.extractfile(member)
                if reader is None:
                    raise ValueError("Missing checkpoint file")
                destination.write_bytes(reader.read())
                destination.chmod(member.mode & 0o777)
        if root.exists():
            root.rename(old)
        try:
            temporary.rename(root)
        except BaseException:
            if old.exists():
                old.rename(root)
            raise
        return {"base_sha": manifest.get("base_sha", ""), "files": len(manifest["files"])}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        if old.exists():
            shutil.rmtree(old)


def operate(arguments: dict[str, Any]) -> dict[str, Any]:
    root = Path(arguments["root"]).resolve()
    operation = arguments["operation"]
    args = arguments.get("arguments", {})
    if operation == "initialize":
        root.mkdir(parents=True, exist_ok=True)
        return {"root": str(root)}
    if not root.is_dir():
        raise ValueError("Workspace does not exist")
    if operation in {"poll_run", "terminate_run"}:
        run_id = args["run_id"]
        if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
            raise ValueError("Invalid process handle")
        folder = Path("/tmp/merlin-runs") / run_id
        state = json.loads((folder / "state.json").read_text())
        if operation == "terminate_run" and state.get("state") == "running":
            try:
                os.killpg(state["child_pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass

        def bounded(name: str) -> str:
            with (folder / name).open("rb") as source:
                size = os.fstat(source.fileno()).st_size
                requested = max(0, int(args.get(name + "_offset", 0)))
                offset = max(0, size - MAX_OUTPUT) if args.get("tail") else min(requested, size)
                source.seek(offset)
                content = source.read(MAX_OUTPUT)
                tail = bool(args.get("tail"))
                budget = MAX_OUTPUT // 2 - 2048
                low, high = 0, len(content)
                while low < high:
                    middle = (low + high + 1) // 2
                    selected = content[-middle:] if tail else content[:middle]
                    if len(json.dumps(selected.decode("utf-8", errors="replace"))) <= budget:
                        low = middle
                    else:
                        high = middle - 1
                if tail and low < len(content):
                    offset += len(content) - low
                content = content[-low:] if tail and low else content[:low]
                state[name + "_offset"] = offset
                state[name + "_next_offset"] = offset + len(content)
                state[name + "_bytes"] = size
                state[name + "_truncated"] = offset > 0 or offset + len(content) < size
                return content.decode("utf-8", errors="replace")

        state["stdout"] = bounded("stdout")
        state["stderr"] = bounded("stderr")
        state["output"] = state["stdout"] + (
            "\n[stderr]\n" + state["stderr"] if state["stderr"] else ""
        )
        return state
    if operation == "run":
        cwd = safe_path(root, args.get("cwd", "."), allow_dot=True)
        if not cwd.is_dir():
            raise ValueError("Working directory does not exist")
        folder = Path("/tmp/merlin-runs") / args["run_id"]
        folder.mkdir(parents=True, mode=0o700, exist_ok=False)

        def state_write(state: dict[str, Any]) -> None:
            temporary = folder / "state.tmp"
            temporary.write_text(json.dumps(state))
            temporary.replace(folder / "state.json")

        with (folder / "stdout").open("wb") as stdout, (folder / "stderr").open("wb") as stderr:
            process = subprocess.Popen(
                [
                    "bash",
                    "-lc",
                    f"export PATH={shlex.quote(TEMPLATE_TOOL_BIN)}:$PATH\n{args['command']}",
                ],
                cwd=cwd,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            state = {"state": "running", "child_pid": process.pid}
            state_write(state)
            try:
                code = process.wait(timeout=args.get("timeout", 120))
                state.update(state="completed", exit_code=code)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                state.update(state="timed_out", exit_code=None)
            state_write(state)
        return operate(
            {"root": str(root), "operation": "poll_run", "arguments": {"run_id": args["run_id"]}}
        )
    if operation == "apply_delta":
        changes = args.get("changes", [])
        if not isinstance(changes, list) or len(changes) > 2000:
            raise ValueError("Invalid file delta")
        planned = []
        delta_names: set[str] = set()
        total = 0
        for change in changes:
            if not isinstance(change, dict) or set(change) - {
                "path",
                "base_sha256",
                "content_base64",
            }:
                raise ValueError("Invalid delta member")
            name = relative_name(change["path"])
            if name in delta_names or ".git" in PurePosixPath(name).parts:
                raise ValueError("Duplicate or protected delta path")
            delta_names.add(name)
            path = safe_path(root, name)
            original = _read(path) if path.exists() else None
            expected = change.get("base_sha256")
            if (digest(original) if original is not None else None) != expected:
                raise ValueError(f"Integration conflict: {name}")
            encoded = change.get("content_base64")
            value = base64.b64decode(encoded, validate=True) if encoded is not None else None
            if value is not None:
                total += len(value)
                if len(value) > MAX_FILE_BYTES or total > MAX_ARCHIVE_BYTES:
                    raise ValueError("Delta exceeds size limit")
            planned.append((name, path, original, value))
        for name in delta_names:
            if any(parent.as_posix() in delta_names for parent in PurePosixPath(name).parents):
                raise ValueError("Conflicting delta paths")
        staging = Path(tempfile.mkdtemp(prefix=".merlin-delta-", dir=root.parent))
        committed = []
        try:
            for index, (_, path, original, value) in enumerate(planned):
                if value is not None:
                    candidate = staging / f"new-{index}"
                    candidate.write_bytes(value)
                    candidate.chmod(path.stat().st_mode & 0o777 if original is not None else 0o644)
            try:
                for index, (_, path, original, value) in enumerate(planned):
                    backup = staging / f"old-{index}"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if original is not None:
                        path.replace(backup)
                    committed.append((path, backup))
                    if value is not None:
                        (staging / f"new-{index}").replace(path)
            except BaseException:
                for path, backup in reversed(committed):
                    path.unlink(missing_ok=True)
                    if backup.exists():
                        backup.replace(path)
                raise
            return {"output": "File delta applied", "files": len(planned)}
        finally:
            shutil.rmtree(staging)
    if operation == "validate_cwd":
        path = safe_path(root, args.get("cwd", "."), allow_dot=True)
        if not path.is_dir():
            raise ValueError("Working directory does not exist")
        return {"cwd": str(path)}
    if operation == "read_file":
        path = safe_path(root, args["path"])
        value = _read(path)
        lines = value.decode("utf-8").splitlines()
        start = int(args.get("start_line", 1))
        end = int(args.get("end_line", start + 199))
        if start < 1 or end < start or end - start > 1999:
            raise ValueError("Invalid line range (maximum 2000 lines)")
        output = "\n".join(
            f"{number}: {line}" for number, line in enumerate(lines[start - 1 : end], start)
        )
        return {
            "output": output[:MAX_OUTPUT],
            "sha256": digest(value),
            "total_lines": len(lines),
            "truncated": len(output) > MAX_OUTPUT,
        }
    if operation in {"write_file", "replace_text"}:
        path = safe_path(root, args["path"])
        exists = path.exists()
        original = _read(path) if exists else b""
        expected = args.get("expected_sha256")
        if expected is not None and expected != digest(original):
            raise ValueError("Stale file content: sha256 does not match")
        if operation == "write_file":
            if args.get("create_only", False) and exists:
                raise ValueError("File already exists")
            value = args["content"].encode("utf-8")
        else:
            if not exists:
                raise ValueError("File does not exist")
            old, new = args["old"], args["new"]
            count = int(args.get("expected_count", 1))
            if not old or count < 1:
                raise ValueError("Replacement requires nonempty old text and positive match count")
            text = original.decode("utf-8")
            if text.count(old) != count:
                raise ValueError("Replacement match count differs from expected_count")
            value = text.replace(old, new).encode("utf-8")
        _write(path, value)
        return {"output": "File updated", "sha256": digest(value), "bytes": len(value)}
    if operation == "search":
        path = safe_path(root, args.get("path", "."), allow_dot=True)
        # Enumerate through the same policy as file reads before applying any
        # caller glob; an inclusion glob must never re-enable secret paths.
        cursor, limit = int(args.get("cursor", 0)), int(args.get("limit", 2000))
        pattern = args.get("glob")
        files = []
        matched = 0
        for scanned, name in enumerate(source_files(root, path)):
            if scanned >= 100000:
                raise ValueError("Repository enumeration exceeds limit; narrow the search path")
            if pattern:
                negative = pattern.startswith("!")
                include = fnmatch.fnmatchcase(name, pattern.lstrip("!")) or Path(name).match(
                    pattern.lstrip("!")
                )
                if include == negative:
                    continue
            if matched >= cursor:
                files.append(name)
                if len(files) > limit:
                    break
            matched += 1
        more = len(files) > limit
        files = files[:limit]
        output = ""
        consumed = 0
        for offset in range(0, len(files), 50):
            command = [
                "rg",
                "--line-number",
                "--with-filename",
                "--color",
                "never",
                "--max-count",
                "100",
                "--max-columns",
                "2000",
                "--max-columns-preview",
            ]
            command += ["--", args["query"], *["./" + name for name in files[offset : offset + 50]]]
            result = subprocess.run(
                command, cwd=root, capture_output=True, text=True, timeout=30, check=False
            )
            if result.returncode not in {0, 1}:
                raise ValueError(result.stderr[:MAX_OUTPUT])
            output += result.stdout
            consumed = min(len(files), offset + 50)
            if len(output) > MAX_OUTPUT:
                break
        return {
            "output": output[:MAX_OUTPUT],
            "truncated": len(output) > MAX_OUTPUT or more,
            "output_truncated": len(output) > MAX_OUTPUT,
            "files_searched": consumed,
            "next_cursor": cursor + consumed if more or consumed < len(files) else None,
            "notice": (
                "Narrow the query or path when output_truncated is true; "
                "file pagination cannot recover clipped matching lines."
            ),
        }
    if operation == "list_files":
        path = safe_path(root, args.get("path", "."), allow_dot=True)
        cursor, limit = int(args.get("cursor", 0)), int(args.get("limit", 2000))
        files = []
        output_bytes = 0
        more = False
        for index, relative in enumerate(source_files(root, path)):
            if index >= 100000:
                raise ValueError("Repository enumeration exceeds limit; narrow the listing path")
            if index >= cursor:
                # Measure the JSON escaping used by the adapter. A cursor must
                # never advance beyond paths clipped from the returned page.
                length = len(json.dumps(relative)) + (2 if files else 0)
                if len(files) >= limit or output_bytes + length > MAX_OUTPUT - 1024:
                    more = True
                    break
                files.append(relative)
                output_bytes += length
        return {
            "output": "\n".join(files),
            "truncated": more,
            "next_cursor": cursor + len(files) if more else None,
        }
    if operation == "capture":
        return capture(root, Path(args["target"]))
    if operation == "restore":
        return restore(root, Path(args["source"]))
    raise ValueError("Unknown workspace operation")


if __name__ == "__main__":
    try:
        request = json.loads(
            Path(sys.argv[1]).read_text()
            if sys.argv[1].startswith("/")
            else base64.b64decode(sys.argv[1])
        )
        print(json.dumps({"ok": True, "data": operate(request)}))
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}))
