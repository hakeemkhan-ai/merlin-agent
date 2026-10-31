"""A framed JSON-RPC LSP client executed exclusively inside E2B."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_RPC_BYTES = 1024 * 1024
MAX_DOCUMENTS = 64
IDLE_SECONDS = 120
RPC_TIMEOUT_SECONDS = 55


class LanguageClient:
    def __init__(self, process: Any) -> None:
        self.process = process
        self.buffer = bytearray()
        self.next_id = 0
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}
        self.diagnostic_versions: dict[str, int | None] = {}
        self.documents: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self.document_files: dict[str, tuple[Path, str, tuple[int, int, int, int]]] = {}

    def send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message).encode()
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.process.stdin.flush()

    def receive(self, deadline: float) -> dict[str, Any]:
        while True:
            separator = self.buffer.find(b"\r\n\r\n")
            if separator >= 0:
                header = bytes(self.buffer[:separator]).decode("ascii")
                lengths = [
                    int(line.split(":", 1)[1])
                    for line in header.split("\r\n")
                    if line.lower().startswith("content-length:")
                ]
                if len(lengths) != 1 or not 0 <= lengths[0] <= MAX_FRAME_BYTES:
                    raise ValueError("Invalid language-server frame")
                length = lengths[0]
                if len(self.buffer) >= separator + 4 + length:
                    body = bytes(self.buffer[separator + 4 : separator + 4 + length])
                    del self.buffer[: separator + 4 + length]
                    return dict(json.loads(body))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Language-server request timed out")
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                raise TimeoutError("Language-server request timed out")
            content = os.read(self.process.stdout.fileno(), 65536)
            if not content:
                raise RuntimeError("Language server closed its output")
            self.buffer.extend(content)
            if len(self.buffer) > 5 * 1024 * 1024:
                raise ValueError("Language-server frame exceeds limit")

    def handle(self, message: dict[str, Any]) -> None:
        if message.get("method") == "textDocument/publishDiagnostics":
            params = message.get("params", {})
            uri, version = params.get("uri"), params.get("version")
            # Servers may finish analysis for a superseded document version.
            # Never make that stale result the current file's diagnostics.
            if uri in self.documents and (version is None or version == self.documents[uri][1]):
                self.diagnostics[uri] = params.get("diagnostics", [])
                self.diagnostic_versions[uri] = version
        elif message.get("method") and "id" in message:
            result: Any = None
            if message["method"] == "workspace/configuration":
                result = [None for _ in message.get("params", {}).get("items", [])]
            self.send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def request(self, method: str, params: dict[str, Any], timeout: float = 20) -> Any:
        self.next_id += 1
        request_id = self.next_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        try:
            while True:
                message = self.receive(deadline)
                if message.get("id") == request_id and not message.get("method"):
                    if "error" in message:
                        raise ValueError("Language server could not complete this request")
                    return message.get("result")
                self.handle(message)
        except TimeoutError:
            with contextlib.suppress(OSError):
                self.notify("$/cancelRequest", {"id": request_id})
            raise

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def open_document(self, path: Path, language_id: str) -> tuple[str, int]:
        for attempt in range(2):
            state = self._file_state(path)
            content = path.read_text()
            if state == self._file_state(path):
                break
            if attempt:
                raise ValueError("Source changed while the language server was reading it")
        fingerprint = hashlib.sha256(content.encode()).hexdigest()
        uri = path.as_uri()
        previous = self.documents.get(uri)
        if previous is not None:
            self.documents.move_to_end(uri)
            if previous[0] == fingerprint:
                self.document_files[uri] = (path, language_id, state)
                return uri, previous[1]
        version = previous[1] + 1 if previous else 1
        if previous:
            self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": content}],
                },
            )
        else:
            while len(self.documents) >= MAX_DOCUMENTS:
                oldest, _ = self.documents.popitem(last=False)
                self.notify("textDocument/didClose", {"textDocument": {"uri": oldest}})
                self.diagnostics.pop(oldest, None)
                self.diagnostic_versions.pop(oldest, None)
                self.document_files.pop(oldest, None)
            self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id,
                        "version": version,
                        "text": content,
                    },
                },
            )
        self.documents[uri] = (fingerprint, version)
        self.document_files[uri] = (path, language_id, state)
        self.diagnostics.pop(uri, None)
        self.diagnostic_versions.pop(uri, None)
        return uri, version

    @staticmethod
    def _file_state(path: Path) -> tuple[int, int, int, int]:
        info = path.stat()
        return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino

    def refresh_documents(self, root: Path) -> None:
        # Open documents are owned by the client, so a server may ignore their
        # on-disk changes. Refresh all changed open buffers before querying any
        # file, including definitions in a different, previously opened file.
        for uri, (path, language_id, previous) in list(self.document_files.items()):
            try:
                safe_source(root, path.relative_to(root).as_posix())
                state = self._file_state(path)
            except (OSError, ValueError):
                self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
                self.documents.pop(uri, None)
                self.document_files.pop(uri, None)
                self.diagnostics.pop(uri, None)
                self.diagnostic_versions.pop(uri, None)
                continue
            if state != previous:
                self.open_document(path, language_id)


def safe_source(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise ValueError("Language-server path must remain within workspace")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Language-server source escapes workspace")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlink language-server paths are unsupported")
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Expected a source file of at most 1 MiB")
    return path


def _stop(process: Any) -> None:
    """Language servers can own child processes; terminate the complete session."""
    pid = getattr(process, "pid", None)
    try:
        if isinstance(pid, int) and pid > 1:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if isinstance(pid, int) and pid > 1:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=2)
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def _config_fingerprint(root: Path, path: Path | None) -> str:
    """Check relevant ancestor config files; do not walk the repository on each call."""
    folders = [root]
    if path is not None:
        folders.extend(
            parent for parent in path.parents if parent != root and parent.is_relative_to(root)
        )
    values = []
    for folder in folders:
        for name in (
            "pyrightconfig.json",
            "pyproject.toml",
            "setup.cfg",
            "package.json",
            "tsconfig.json",
            "jsconfig.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "uv.lock",
        ):
            candidate = folder / name
            if candidate.is_symlink():
                raise ValueError("Symlink language-server configuration is unsupported")
            if candidate.is_file():
                info = candidate.stat()
                if info.st_size > 8 * 1024 * 1024:
                    raise ValueError("Language-server configuration exceeds size limit")
                values.append(
                    (
                        str(candidate.relative_to(root)),
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                        info.st_ino,
                    )
                )
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


class LanguageSessions:
    """One lazy client per language, scoped to this daemon's workspace only."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.clients: dict[str, tuple[str, LanguageClient]] = {}

    def close(self) -> None:
        clients, self.clients = self.clients, {}
        for _, client in clients.values():
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                _stop(client.process)

    def _client(self, language: str, fingerprint: str) -> LanguageClient:
        previous = self.clients.get(language)
        if previous is not None:
            if previous[0] == fingerprint and previous[1].process.poll() is None:
                return previous[1]
            self.clients.pop(language)
            _stop(previous[1].process)
        command = (
            ["pyright-langserver", "--stdio"]
            if language == "python"
            else ["typescript-language-server", "--stdio"]
        )
        process = subprocess.Popen(
            command,
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        client = LanguageClient(process)
        try:
            client.request(
                "initialize",
                {
                    "processId": None,
                    "rootUri": self.root.as_uri(),
                    "capabilities": {
                        "workspace": {"configuration": True},
                        "textDocument": {"synchronization": {"dynamicRegistration": False}},
                    },
                    "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                },
            )
            client.notify("initialized", {})
        except BaseException:
            _stop(process)
            raise
        self.clients[language] = (fingerprint, client)
        return client

    def execute(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation == "lsp_reset":
            self.close()
            return {"output": "Language-server sessions reset", "reset": True}
        if operation not in {
            "lsp_hover",
            "lsp_definition",
            "lsp_references",
            "lsp_symbols",
            "lsp_diagnostics",
        }:
            raise ValueError("Unknown language-server operation")
        path = safe_source(self.root, arguments["path"]) if "path" in arguments else None
        if path is not None and path.suffix not in {
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
        }:
            return {
                "output": "No language server configured for this file; use text search.",
                "supported": False,
            }
        language = (
            "python"
            if arguments.get("language") == "python" or (path is not None and path.suffix == ".py")
            else "typescript"
        )
        try:
            client = self._client(language, _config_fingerprint(self.root, path))
        except FileNotFoundError:
            return {
                "output": "Language server is unavailable in this template; use text search.",
                "supported": False,
            }
        try:
            uri, version = "", 0
            client.refresh_documents(self.root)
            if path is not None:
                language_id = (
                    "python"
                    if language == "python"
                    else (
                        "typescriptreact"
                        if path.suffix == ".tsx"
                        else "javascriptreact"
                        if path.suffix == ".jsx"
                        else "javascript"
                        if path.suffix in {".js", ".mjs", ".cjs"}
                        else "typescript"
                    )
                )
                uri, version = client.open_document(path, language_id)
            if operation == "lsp_diagnostics":
                deadline = time.monotonic() + 3
                while uri not in client.diagnostics:
                    try:
                        client.handle(client.receive(deadline))
                    except TimeoutError:
                        break
                result: Any = client.diagnostics.get(uri, [])
                return {
                    "output": json.dumps(result)[:30000],
                    "diagnostics": result,
                    "pending": uri not in client.diagnostics,
                    "supported": True,
                    "document_version": version,
                    "versioned": client.diagnostic_versions.get(uri) is not None,
                }
            params: dict[str, Any]
            if operation == "lsp_symbols":
                method, params = "workspace/symbol", {"query": arguments["query"]}
            else:
                if path is None:
                    raise ValueError("This language-server operation requires a source path")
                method = {
                    "lsp_definition": "textDocument/definition",
                    "lsp_references": "textDocument/references",
                    "lsp_hover": "textDocument/hover",
                }[operation]
                params = {
                    "textDocument": {"uri": uri},
                    "position": {
                        "line": arguments.get("line", 1) - 1,
                        "character": arguments.get("column", 1) - 1,
                    },
                }
                if operation == "lsp_references":
                    params["context"] = {"includeDeclaration": True}
            result = client.request(method, params)
            return {
                "output": json.dumps(result)[:30000],
                "result": result,
                "supported": True,
                "document_version": version,
            }
        except (OSError, RuntimeError, TimeoutError, ValueError):
            # A failed server is discarded; the next read starts a clean client.
            # Do not replay even read operations invisibly within this request.
            self.clients.pop(language, None)
            _stop(client.process)
            raise


def _directory(root: Path) -> Path:
    name = hashlib.sha256(str(root).encode()).hexdigest()[:24]
    directory = Path("/tmp") / ("merlin-lsp-" + name)
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Unsafe language-server state directory")
    return directory


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + RPC_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Language-server daemon is busy") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _receive_line(stream: Any) -> dict[str, Any]:
    line = stream.readline(MAX_RPC_BYTES + 1)
    if len(line) > MAX_RPC_BYTES or not line.endswith(b"\n"):
        raise ValueError("Language-server daemon message exceeds limit")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Invalid language-server daemon message")
    return value


def _request(
    directory: Path, configuration: dict[str, Any], operation: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    payload = (
        json.dumps(
            {
                "token": configuration["token"],
                "root": configuration["root"],
                "operation": operation,
                "arguments": arguments,
            }
        ).encode()
        + b"\n"
    )
    if len(payload) > MAX_RPC_BYTES:
        raise ValueError("Language-server request exceeds limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(RPC_TIMEOUT_SECONDS)
        connection.connect(str(directory / "server.sock"))
        connection.sendall(payload)
        with connection.makefile("rb") as stream:
            response = _receive_line(stream)
    if not response.get("ok"):
        raise ValueError(response.get("error", "Language-server request failed"))
    return dict(response["data"])


def serve(configuration_file: str) -> None:
    configuration = json.loads(Path(configuration_file).read_text())
    root = Path(configuration["root"]).resolve()
    directory = _directory(root)
    sessions = LanguageSessions(root)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(directory / "server.sock"))
        (directory / "server.sock").chmod(0o600)
        server.listen(8)
        server.settimeout(1)
        last_active = time.monotonic()
        try:
            while time.monotonic() - last_active < IDLE_SECONDS:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(5)
                    try:
                        with connection.makefile("rb") as stream:
                            request = _receive_line(stream)
                        if request.get("token") != configuration["token"] or request.get(
                            "root"
                        ) != str(root):
                            raise ValueError("Language-server daemon authorization failed")
                        if request["operation"] == "ping":
                            result = {"root": str(root), "protocol": 1}
                        else:
                            result = sessions.execute(
                                request["operation"], request.get("arguments", {})
                            )
                        response = {"ok": True, "data": result}
                    except Exception:
                        # Server diagnostics may contain source/configuration data.
                        response = {
                            "ok": False,
                            "error": (
                                "Language-server request failed; retry the read or use text search"
                            ),
                        }
                    encoded = json.dumps(response).encode() + b"\n"
                    if len(encoded) > MAX_RPC_BYTES:
                        encoded = b'{"ok":false,"error":"Language-server response exceeds limit"}\n'
                    with contextlib.suppress(OSError):
                        connection.sendall(encoded)
                    last_active = time.monotonic()
        finally:
            sessions.close()
            (directory / "server.sock").unlink(missing_ok=True)


def operate(request: dict[str, Any]) -> dict[str, Any]:
    root = Path(request["root"]).resolve()
    operation = request["operation"]
    arguments = request.get("arguments", {})
    if operation != "lsp_reset":
        if "path" in arguments:
            path = safe_source(root, arguments["path"])
            if path.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
                return {
                    "output": "No language server configured for this file; use text search.",
                    "supported": False,
                }
    directory = _directory(root)
    with _locked(directory):
        configuration = None
        path = directory / "configuration.json"
        if path.exists():
            if path.is_symlink() or path.stat().st_size > 4096:
                raise ValueError("Invalid language-server configuration")
            candidate = json.loads(path.read_text())
            if candidate.get("root") != str(root):
                raise ValueError("Language-server workspace differs from daemon workspace")
            try:
                _request(directory, candidate, "ping", {})
                configuration = candidate
            except (ConnectionError, FileNotFoundError):
                pass
        if configuration is None:
            if operation == "lsp_reset":
                return {"output": "No active language-server sessions", "reset": False}
            configuration = {"root": str(root), "token": uuid4().hex}
            temporary = directory / (uuid4().hex + ".json")
            temporary.write_text(json.dumps(configuration))
            temporary.chmod(0o600)
            temporary.replace(path)
            (directory / "server.sock").unlink(missing_ok=True)
            # Never inherit an output pipe held open by this short-lived helper.
            process = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).resolve()), "--serve", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + 5
            while True:
                try:
                    _request(directory, configuration, "ping", {})
                    break
                except (ConnectionError, FileNotFoundError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        process.terminate()
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            process.wait(timeout=2)
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=2)
                        raise RuntimeError("Language-server daemon did not become ready") from None
                    time.sleep(0.05)
        return _request(directory, configuration, operation, arguments)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serve(sys.argv[2])
    else:
        try:
            print(
                json.dumps({"ok": True, "data": operate(json.loads(Path(sys.argv[1]).read_text()))})
            )
        except Exception as error:
            print(json.dumps({"ok": False, "error": str(error)}))
