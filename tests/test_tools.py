import tempfile
import unittest
from pathlib import Path

from merlin.sandbox._remote import digest, operate
from merlin.tools import sandbox_tool_definitions


class FileToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def call(self, name: str, **arguments: object) -> dict:
        return operate({"root": str(self.root), "operation": name, "arguments": arguments})

    def test_numbered_read_returns_checksum(self) -> None:
        value = "one\ntwo\nthree\n"
        (self.root / "file.py").write_text(value)
        result = self.call("read_file", path="file.py", start_line=2, end_line=3)
        self.assertEqual(result["output"], "2: two\n3: three")
        self.assertEqual(result["sha256"], digest(value.encode()))

    def test_exact_replacement_rejects_ambiguous_count(self) -> None:
        (self.root / "file").write_text("same same")
        with self.assertRaisesRegex(ValueError, "match count"):
            self.call("replace_text", path="file", old="same", new="new")
        self.assertEqual((self.root / "file").read_text(), "same same")
        self.call("replace_text", path="file", old="same", new="new", expected_count=2)
        self.assertEqual((self.root / "file").read_text(), "new new")

    def test_stale_replacement_and_write_rejected(self) -> None:
        (self.root / "file").write_text("changed")
        for operation, arguments in [
            ("write_file", {"content": "new"}),
            ("replace_text", {"old": "changed", "new": "new"}),
        ]:
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "Stale"):
                self.call(operation, path="file", expected_sha256=digest(b"old"), **arguments)
        self.assertEqual((self.root / "file").read_text(), "changed")

    def test_create_only_and_parent_directories(self) -> None:
        self.call("write_file", path="nested/new.py", content="hello", create_only=True)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.call("write_file", path="nested/new.py", content="overwrite", create_only=True)

    def test_parent_absolute_secret_and_symlink_paths_rejected(self) -> None:
        (self.root / "link").symlink_to("/etc/passwd")
        for name in ["../outside", "/etc/passwd", ".env", ".aws/credentials", "link"]:
            with self.subTest(path=name), self.assertRaises(ValueError):
                self.call("read_file", path=name)

    def test_lists_source_and_excludes_cache_and_secrets(self) -> None:
        for name in ["file.py", ".env", ".env.example", "node_modules/x", ".git/HEAD"]:
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("data")
        result = self.call("list_files")
        self.assertEqual(set(result["output"].splitlines()), {"file.py", ".env.example"})

    def test_line_range_limits(self) -> None:
        (self.root / "file").write_text("test")
        for start, end in [(0, 1), (2, 1), (1, 3000)]:
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.call("read_file", path="file", start_line=start, end_line=end)

    def test_tool_definitions_are_unique_and_closed(self) -> None:
        definitions = sandbox_tool_definitions()
        names = [tool["name"] for tool in definitions]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("replace_text", names)
        for definition in definitions:
            self.assertFalse(definition["parameters"]["additionalProperties"])


class DeltaTests(unittest.TestCase):
    def test_conflict_aborts_all_changes(self) -> None:
        import base64

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "first").write_text("a")
            (root / "second").write_text("b")
            changes = [
                {
                    "path": "first",
                    "base_sha256": digest(b"a"),
                    "content_base64": base64.b64encode(b"new").decode(),
                },
                {"path": "second", "base_sha256": digest(b"stale"), "content_base64": None},
            ]
            with self.assertRaisesRegex(ValueError, "conflict"):
                operate(
                    {
                        "root": str(root),
                        "operation": "apply_delta",
                        "arguments": {"changes": changes},
                    }
                )
            self.assertEqual((root / "first").read_text(), "a")
            self.assertEqual((root / "second").read_text(), "b")

    def test_binary_create_update_delete(self) -> None:
        import base64

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "old").write_text("delete")
            changes = [
                {
                    "path": "new/file",
                    "base_sha256": None,
                    "content_base64": base64.b64encode(b"\x00\xff").decode(),
                },
                {"path": "old", "base_sha256": digest(b"delete"), "content_base64": None},
            ]
            operate(
                {"root": str(root), "operation": "apply_delta", "arguments": {"changes": changes}}
            )
            self.assertEqual((root / "new/file").read_bytes(), b"\x00\xff")
            self.assertFalse((root / "old").exists())
