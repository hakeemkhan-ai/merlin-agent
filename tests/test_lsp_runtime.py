"""Session reuse, fresh source state and bounded sandbox-local LSP transport."""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from merlin.lsp._remote import (
    MAX_DOCUMENTS,
    MAX_RPC_BYTES,
    LanguageClient,
    LanguageSessions,
    _directory,
    _receive_line,
    _request,
    operate,
    serve,
)


def fake_process() -> SimpleNamespace:
    return SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=io.BytesIO(),
        terminate=Mock(),
        wait=Mock(),
        kill=Mock(),
        poll=Mock(return_value=None),
    )


def test_reuses_server_and_only_sends_changed_source(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 1")
    process = fake_process()
    with (
        patch("merlin.lsp._remote.subprocess.Popen", return_value=process) as start,
        patch.object(LanguageClient, "request", return_value={"contents": "int"}) as request,
        patch.object(LanguageClient, "notify") as notify,
    ):
        sessions = LanguageSessions(tmp_path)
        try:
            assert sessions.execute("lsp_hover", {"path": "app.py"})["document_version"] == 1
            assert sessions.execute("lsp_hover", {"path": "app.py"})["document_version"] == 1
            source.write_text("value = 2")
            assert sessions.execute("lsp_hover", {"path": "app.py"})["document_version"] == 2
            start.assert_called_once()
            assert [call.args[0] for call in request.call_args_list].count("initialize") == 1
            assert [call.args[0] for call in notify.call_args_list].count(
                "textDocument/didOpen"
            ) == 1
            changes = [
                call for call in notify.call_args_list if call.args[0] == "textDocument/didChange"
            ]
            assert changes[0].args[1]["contentChanges"] == [{"text": "value = 2"}]
            process.terminate.assert_not_called()
        finally:
            sessions.close()
        process.terminate.assert_called_once()


def test_configuration_change_restarts_server(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1")
    first, second = fake_process(), fake_process()
    with (
        patch("merlin.lsp._remote.subprocess.Popen", side_effect=[first, second]) as start,
        patch.object(LanguageClient, "request", return_value=None),
    ):
        sessions = LanguageSessions(tmp_path)
        try:
            sessions.execute("lsp_hover", {"path": "app.py"})
            (tmp_path / "pyrightconfig.json").write_text('{"typeCheckingMode":"strict"}')
            sessions.execute("lsp_hover", {"path": "app.py"})
            assert start.call_count == 2
            first.terminate.assert_called_once()
        finally:
            sessions.close()


def test_other_open_files_are_refreshed_before_querying_an_unchanged_file(tmp_path: Path) -> None:
    first, second = tmp_path / "first.py", tmp_path / "second.py"
    first.write_text("from second import value")
    second.write_text("value = 1")
    process = fake_process()
    with (
        patch("merlin.lsp._remote.subprocess.Popen", return_value=process) as start,
        patch.object(LanguageClient, "request", return_value=None),
        patch.object(LanguageClient, "notify") as notify,
    ):
        sessions = LanguageSessions(tmp_path)
        try:
            sessions.execute("lsp_hover", {"path": "first.py"})
            sessions.execute("lsp_hover", {"path": "second.py"})
            notify.reset_mock()
            second.write_text("value = 'changed'")
            sessions.execute("lsp_hover", {"path": "first.py"})
            changes = [
                call for call in notify.call_args_list if call.args[0] == "textDocument/didChange"
            ]
            assert len(changes) == 1
            assert changes[0].args[1]["textDocument"] == {"uri": second.as_uri(), "version": 2}
            assert changes[0].args[1]["contentChanges"] == [{"text": "value = 'changed'"}]
            start.assert_called_once()
        finally:
            sessions.close()


def test_deleted_and_symlink_replaced_open_files_are_closed(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 1")
    client = LanguageClient(fake_process())
    uri, _ = client.open_document(source, "python")
    source.unlink()
    with patch.object(client, "notify") as notify:
        client.refresh_documents(tmp_path)
        notify.assert_called_once_with("textDocument/didClose", {"textDocument": {"uri": uri}})
    assert not client.documents and not client.document_files
    source.write_text("value = 2")
    client.open_document(source, "python")
    source.unlink()
    source.symlink_to(tmp_path / "other.py")
    client.refresh_documents(tmp_path)
    assert not client.documents


def test_dead_server_restarts_on_next_read(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1")
    first, second = fake_process(), fake_process()
    with (
        patch("merlin.lsp._remote.subprocess.Popen", side_effect=[first, second]) as start,
        patch.object(LanguageClient, "request", return_value=None),
    ):
        sessions = LanguageSessions(tmp_path)
        try:
            sessions.execute("lsp_hover", {"path": "app.py"})
            first.poll.return_value = 1
            sessions.execute("lsp_hover", {"path": "app.py"})
            assert start.call_count == 2
        finally:
            sessions.close()


def test_timeout_cancels_request_and_discards_server(tmp_path: Path) -> None:
    process = fake_process()
    client = LanguageClient(process)
    with (
        patch.object(client, "receive", side_effect=TimeoutError),
        patch.object(client, "notify") as notify,
    ):
        with pytest.raises(TimeoutError):
            client.request("textDocument/hover", {})
        notify.assert_called_once_with("$/cancelRequest", {"id": 1})
    (tmp_path / "app.py").write_text("value = 1")
    with (
        patch("merlin.lsp._remote.subprocess.Popen", return_value=process),
        patch.object(LanguageClient, "request", side_effect=[{}, TimeoutError]),
    ):
        sessions = LanguageSessions(tmp_path)
        with pytest.raises(TimeoutError):
            sessions.execute("lsp_hover", {"path": "app.py"})
        assert not sessions.clients
        process.terminate.assert_called_once()


def test_document_updates_invalidate_stale_diagnostics(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 1")
    client = LanguageClient(fake_process())
    uri, _ = client.open_document(source, "python")
    client.handle(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "version": 1, "diagnostics": [{"message": "old"}]},
        }
    )
    assert client.diagnostics[uri]
    source.write_text("value = 2")
    client.open_document(source, "python")
    assert uri not in client.diagnostics
    client.handle(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "version": 1, "diagnostics": [{"message": "stale"}]},
        }
    )
    assert uri not in client.diagnostics
    client.handle(
        {
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "version": 2, "diagnostics": []},
        }
    )
    assert client.diagnostics[uri] == []


def test_open_documents_are_bounded(tmp_path: Path) -> None:
    client = LanguageClient(fake_process())
    with patch.object(client, "notify") as notify:
        for index in range(MAX_DOCUMENTS + 2):
            path = tmp_path / f"source{index}.py"
            path.write_text("value = 1")
            client.open_document(path, "python")
        assert len(client.documents) == MAX_DOCUMENTS
        assert [call.args[0] for call in notify.call_args_list].count("textDocument/didClose") == 2


def test_reset_never_starts_an_absent_daemon(tmp_path: Path) -> None:
    folder = tmp_path / "daemon"
    folder.mkdir()
    with (
        patch("merlin.lsp._remote._directory", return_value=folder),
        patch("merlin.lsp._remote.subprocess.Popen") as start,
    ):
        assert operate({"root": str(tmp_path), "operation": "lsp_reset"})["reset"] is False
        start.assert_not_called()


def test_reset_closes_both_language_sessions(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1")
    (tmp_path / "app.ts").write_text("const value = 1")
    first, second = fake_process(), fake_process()
    with (
        patch("merlin.lsp._remote.subprocess.Popen", side_effect=[first, second]),
        patch.object(LanguageClient, "request", return_value=None),
    ):
        sessions = LanguageSessions(tmp_path)
        sessions.execute("lsp_hover", {"path": "app.py"})
        sessions.execute("lsp_hover", {"path": "app.ts"})
        assert sessions.execute("lsp_reset", {})["reset"] is True
        assert not sessions.clients
        first.terminate.assert_called_once()
        second.terminate.assert_called_once()


def test_daemon_transport_requires_bounded_complete_messages() -> None:
    with pytest.raises(ValueError, match="exceeds limit"):
        _receive_line(io.BytesIO(b"x" * (MAX_RPC_BYTES + 1)))
    with pytest.raises(ValueError, match="exceeds limit"):
        _receive_line(io.BytesIO(b'{"ok":true}'))
    assert _receive_line(io.BytesIO(b'{"ok":true}\n')) == {"ok": True}


def test_daemon_transport_authentication_and_idle_cleanup(tmp_path: Path) -> None:
    folder = tmp_path / "daemon"
    folder.mkdir(mode=0o700)
    configuration = {"root": str(tmp_path), "token": "test-token"}
    path = folder / "configuration.json"
    path.write_text(json.dumps(configuration))
    errors: list[BaseException] = []

    def run() -> None:
        try:
            serve(str(path))
        except BaseException as error:
            errors.append(error)

    with (
        patch("merlin.lsp._remote._directory", return_value=folder),
        patch("merlin.lsp._remote.IDLE_SECONDS", 0.05),
        patch.object(LanguageSessions, "close") as close,
    ):
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 1
        while not (folder / "server.sock").exists():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert _request(folder, configuration, "ping", {}) == {"root": str(tmp_path), "protocol": 1}
        with pytest.raises(ValueError, match="request failed"):
            _request(folder, {**configuration, "token": "wrong"}, "ping", {})
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not errors
        close.assert_called_once()
    assert not (folder / "server.sock").exists()


def test_directory_refuses_symlink_state(tmp_path: Path) -> None:
    name = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:24]
    state = Path("/tmp") / ("merlin-lsp-" + name)
    state.symlink_to(tmp_path, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="Unsafe"):
            _directory(tmp_path)
    finally:
        state.unlink()


def test_request_refuses_oversized_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exceeds limit"):
        _request(
            tmp_path,
            {"root": str(tmp_path), "token": "test"},
            "lsp_symbols",
            {"query": "x" * MAX_RPC_BYTES},
        )


def test_diagnostics_do_not_claim_absent_results_passed(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1")
    process = fake_process()
    with (
        patch("merlin.lsp._remote.subprocess.Popen", return_value=process),
        patch.object(LanguageClient, "request", return_value={}),
        patch.object(LanguageClient, "receive", side_effect=TimeoutError),
    ):
        sessions = LanguageSessions(tmp_path)
        try:
            result = sessions.execute("lsp_diagnostics", {"path": "app.py"})
            assert result["diagnostics"] == [] and result["pending"] is True
            assert result["versioned"] is False
        finally:
            sessions.close()


def test_configuration_symlinks_are_rejected_before_start(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1")
    (tmp_path / "pyrightconfig.json").symlink_to(tmp_path / "outside.json")
    with patch("merlin.lsp._remote.subprocess.Popen") as start:
        with pytest.raises(ValueError, match="Symlink"):
            LanguageSessions(tmp_path).execute("lsp_hover", {"path": "app.py"})
        start.assert_not_called()
