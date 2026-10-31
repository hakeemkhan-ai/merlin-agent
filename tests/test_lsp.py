from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from merlin.lsp import validate_arguments
from merlin.lsp._remote import LanguageClient, LanguageSessions, operate, safe_source


class LanguageServerTests(unittest.TestCase):
    def test_framed_response_parsing(self) -> None:
        content = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": "hello"}}).encode()
        client = LanguageClient(SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO()))
        client.buffer.extend(f"Content-Length: {len(content)}\r\n\r\n".encode() + content)
        self.assertEqual(client.receive(0)["result"], {"value": "hello"})

    def test_configuration_requests_are_answered(self) -> None:
        stream = io.BytesIO()
        client = LanguageClient(SimpleNamespace(stdin=stream, stdout=io.BytesIO()))
        client.handle(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "workspace/configuration",
                "params": {"items": [{"section": "python"}]},
            }
        )
        self.assertIn(b'"result": [null]', stream.getvalue())

    def test_server_receives_current_source_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("updated = 42")
            process = SimpleNamespace(
                stdin=io.BytesIO(),
                stdout=io.BytesIO(),
                terminate=Mock(),
                wait=Mock(),
                kill=Mock(),
                poll=Mock(return_value=None),
            )
            with (
                patch("merlin.lsp._remote.subprocess.Popen", return_value=process),
                patch.object(LanguageClient, "request", return_value={"contents": "int"}),
                patch.object(LanguageClient, "notify") as notify,
            ):
                sessions = LanguageSessions(root)
                result = sessions.execute("lsp_hover", {"path": "app.py", "line": 1, "column": 1})
                process.terminate.assert_not_called()
                sessions.close()
            self.assertTrue(result["supported"])
            calls = [
                call for call in notify.call_args_list if call.args[0] == "textDocument/didOpen"
            ]
            self.assertEqual(calls[0].args[1]["textDocument"]["text"], "updated = 42")
            process.terminate.assert_called_once()

    def test_unsupported_language_has_explicit_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.rs").write_text("fn main() {}")
            result = operate(
                {"root": str(root), "operation": "lsp_hover", "arguments": {"path": "app.rs"}}
            )
            self.assertFalse(result["supported"])
            self.assertIn("text search", result["output"])

    def test_traversal_and_symlink_sources_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "link.py").symlink_to("/etc/passwd")
            for name in ["../escape.py", "/etc/passwd", "link.py"]:
                with self.subTest(path=name), self.assertRaises(ValueError):
                    safe_source(root, name)

    def test_tool_positions_are_one_based(self) -> None:
        self.assertEqual(
            validate_arguments("lsp_definition", {"path": "x.py", "line": 2})["column"], 1
        )
        with self.assertRaises(ValueError):
            validate_arguments("lsp_hover", {"path": "x.py", "line": 0})
