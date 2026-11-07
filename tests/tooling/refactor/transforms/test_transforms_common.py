"""Tests for the shared infrastructure: workspace overlay, module index, IO."""

from __future__ import annotations

import unittest

from tests.tooling.refactor.support._helpers import RepoFixture

from scripts.refactor.transforms.common import (
    ModuleIndex,
    ToolError,
    Workspace,
    decode_source,
    normalize_rel,
    sha256_hex,
    unified_diff,
)


class NormalizePathTests(unittest.TestCase):
    def test_rejects_absolute_and_escape(self):
        for bad in ("/etc/passwd", "..", "a/../../b", "./.."):
            with self.assertRaises(ToolError):
                normalize_rel(bad)

    def test_normalizes_plain_path(self):
        self.assertEqual(normalize_rel("a/./b/../c.py"), "a/c.py")


class DecodeTests(unittest.TestCase):
    def test_latin1_cookie_roundtrip(self):
        data = b"# coding: latin-1\nTEXT = 'caf\xe9'\n"
        text, encoding = decode_source(data, "x.py")
        self.assertIn("caf\xe9", text)
        self.assertEqual(text.encode(encoding), data)

    def test_crlf_preserved_in_text(self):
        data = b"a = 1\r\nb = 2\r\n"
        text, _ = decode_source(data, "x.py")
        self.assertEqual(text, "a = 1\r\nb = 2\r\n")

    def test_utf8_bom(self):
        data = "VALUE = 'x'\n".encode("utf-8-sig")
        text, encoding = decode_source(data, "x.py")
        self.assertEqual(text.encode(encoding), data)


class ModuleIndexTests(unittest.TestCase):
    def test_modules_packages_and_namespace(self):
        index = ModuleIndex(
            [
                "src/hyprial/__init__.py",
                "src/hyprial/agents/__init__.py",
                "src/hyprial/agents/registry.py",
                "src/hyprial/nsp/child.py",  # directory without __init__.py
                "tests/helper.py",
            ]
        )
        self.assertEqual(index.module_of("src/hyprial/agents/registry.py"), "hyprial.agents.registry")
        self.assertEqual(index.path_of("hyprial.agents.registry"), "src/hyprial/agents/registry.py")
        self.assertTrue(index.is_package("hyprial.agents"))
        self.assertTrue(index.is_package("hyprial.nsp"))  # namespace package
        self.assertTrue(index.is_module("hyprial.nsp.child"))
        self.assertEqual(index.file_package("tests/helper.py"), "tests")

    def test_relative_resolution_layers(self):
        index = ModuleIndex(["src/hyprial/a/b/c.py", "src/hyprial/a/base.py", "src/hyprial/top.py"])
        self.assertEqual(
            index.resolve_relative("hyprial.a.b", 1, "sibling"), "hyprial.a.b.sibling"
        )
        self.assertEqual(index.resolve_relative("hyprial.a.b", 2, "base"), "hyprial.a.base")
        self.assertEqual(index.resolve_relative("hyprial.a.b", 3, "top"), "hyprial.top")
        self.assertIsNone(index.resolve_relative("hyprial.a.b", 9, "x"))

    def test_relative_form_minimal(self):
        index = ModuleIndex([])
        self.assertEqual(index.relative_form("hyprial.daemon.impl.sub", "hyprial.kernel.c"), (4, "kernel.c"))
        self.assertEqual(index.relative_form("hyprial.kernel", "hyprial.kernel.impl.x"), (1, "impl.x"))
        self.assertEqual(index.relative_form("hyprial", "hyprial.x"), (1, "x"))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = RepoFixture()
        self.fixture.setUp()
        self.root = self.fixture.root
        (self.root / "pkg").mkdir()
        (self.root / "pkg/a.py").write_text("A = 1\n")
        (self.root / "top.py").write_text("T = 1\n")

    def tearDown(self):
        self.fixture.tearDown()

    def _ws(self) -> Workspace:
        return Workspace(self.root, ["pkg/a.py", "top.py"])

    def test_overlay_write_read_and_paths(self):
        ws = self._ws()
        ws.write("pkg/a.py", b"A = 2\n")
        self.assertEqual(ws.read("pkg/a.py"), b"A = 2\n")
        self.assertTrue(ws.is_dirty("pkg/a.py"))
        self.assertEqual(ws.paths(), ["pkg/a.py", "top.py"])

    def test_materialize_preserves_executable_bit(self):
        ws = self._ws()
        (self.root / "pkg/a.py").chmod(0o755)
        ws.write("pkg/a.py", b"A = 2\n")
        ws.materialize()
        mode = (self.root / "pkg/a.py").stat().st_mode
        self.assertTrue(mode & 0o111, f"executable bit lost after materialize: {oct(mode)}")

    def test_move_updates_index_and_listing(self):
        ws = self._ws()
        ws.move("pkg/a.py", "pkg/b.py")
        self.assertFalse(ws.exists("pkg/a.py"))
        self.assertTrue(ws.exists("pkg/b.py"))
        self.assertEqual(ws.index.module_of("pkg/b.py"), "pkg.b")
        self.assertEqual(ws.index.path_of("pkg.b"), "pkg/b.py")
        self.assertEqual(ws.moves()[0].src, "pkg/a.py")

    def test_move_refuses_existing_target(self):
        ws = self._ws()
        ws.write("pkg/b.py", b"x = 1\n")
        with self.assertRaises(ToolError):
            ws.move("pkg/a.py", "pkg/b.py")

    def test_list_dir_merges_overlay(self):
        ws = self._ws()
        ws.write("pkg/new.py", b"")
        ws.delete("pkg/a.py")
        self.assertEqual(ws.list_dir("pkg"), {"new.py"})
        ws2 = self._ws()
        self.assertEqual(ws2.list_dir("pkg"), {"a.py"})


class DiffTests(unittest.TestCase):
    def test_unified_diff_content(self):
        diff = unified_diff("a.py", b"x = 1\n", b"x = 2\n")
        self.assertIn("--- a/a.py", diff)
        self.assertIn("+++ b/a.py", diff)
        self.assertIn("-x = 1", diff)
        self.assertIn("+x = 2", diff)

    def test_sha256_hex(self):
        import hashlib

        self.assertEqual(sha256_hex(b""), hashlib.sha256(b"").hexdigest())


if __name__ == "__main__":
    unittest.main()
