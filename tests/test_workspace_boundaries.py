"""Workspace symlink and context-import boundary regressions."""
import atexit
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory(prefix="forge-boundaries-test-")
atexit.register(_tmp.cleanup)
os.environ["FORGE_DATA_DIR"] = _tmp.name
os.environ.pop("FORGE_PROFILE", None)

from engine import context_files, memory, repomap, tools  # noqa: E402
from engine.agent import Agent, _agents_md_block  # noqa: E402
from engine.session import SessionStore  # noqa: E402


class WorkspaceBoundaries(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="boundary-case-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.ws = self.base / "workspace"
        self.ws.mkdir()
        self.outside = self.base / "outside.py"
        self.outside.write_text("def PrivateMarkerFunction():\n    return 'PRIVATE_MARKER'\n", encoding="utf-8")
        (self.ws / "safe.py").write_text("def SafeMarkerFunction():\n    return 'SAFE_MARKER'\n", encoding="utf-8")
        try:
            (self.ws / "link.py").symlink_to(self.outside)
            (self.ws / "inside-link.py").symlink_to(self.ws / "safe.py")
            (self.ws / "external-dir").symlink_to(self.base, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    async def test_search_does_not_follow_symlink_files(self):
        self.assertTrue(tools._search_files(self.ws, {"query": "PRIVATE_MARKER"}).startswith("No matches"))
        self.assertIn("safe.py", tools._search_files(self.ws, {"query": "SAFE_MARKER"}))
        self.assertNotIn("inside-link.py", tools._search_files(self.ws, {"query": "SAFE_MARKER"}))

    async def test_repomap_skips_symlinks(self):
        result = repomap.build(self.ws)["map"]
        self.assertNotIn("PrivateMarkerFunction", result)
        self.assertNotIn("link.py", result)
        self.assertIn("SafeMarkerFunction", result)

    async def test_project_imports_stay_in_workspace(self):
        (self.ws / "inside.md").write_text("SAFE_CONTEXT_MARKER", encoding="utf-8")
        (self.ws / "AGENTS.md").write_text(
            f"@inside.md\n@{self.outside}\n@../outside.py\n@link.py\n", encoding="utf-8")
        text = _agents_md_block(self.ws)
        self.assertIn("SAFE_CONTEXT_MARKER", text)
        self.assertNotIn("PRIVATE_MARKER", text)
        self.assertNotIn("PrivateMarkerFunction", text)

    async def test_nested_import_may_reference_parent_inside_workspace(self):
        (self.ws / "docs").mkdir()
        (self.ws / "shared.md").write_text("SHARED_OK", encoding="utf-8")
        (self.ws / "docs" / "child.md").write_text("@../shared.md", encoding="utf-8")
        (self.ws / "AGENTS.md").write_text("@docs/child.md", encoding="utf-8")
        self.assertIn("SHARED_OK", _agents_md_block(self.ws))

    async def test_root_context_file_symlink_cannot_escape(self):
        (self.ws / "AGENTS.md").symlink_to(self.outside)
        self.assertNotIn("PRIVATE_MARKER", _agents_md_block(self.ws))
        self.assertNotIn("PRIVATE_MARKER", context_files.load_with_imports(self.ws / "AGENTS.md"))

    async def test_workspace_memory_cannot_import_outside(self):
        store = SessionStore()
        session = store.create()
        agent = Agent(store, session.id)
        agent.workspace = self.ws
        (self.ws / "MEMORY.md").write_text(f"@{self.outside}\n", encoding="utf-8")
        self.assertNotIn("PRIVATE_MARKER", agent._memory_block())

    async def test_memory_write_and_overview_do_not_follow_external_symlink(self):
        (self.ws / "MEMORY.md").symlink_to(self.outside)
        before = self.outside.read_text(encoding="utf-8")
        result = memory.append_entry(self.ws, "new entry")
        self.assertTrue(result.startswith("Error"), result)
        self.assertEqual(self.outside.read_text(encoding="utf-8"), before)
        self.assertNotIn("PRIVATE_MARKER", memory.memory_overview(self.ws, data_dir=self.ws))
        result = memory.clear_memory(self.ws)
        self.assertNotIn("PRIVATE_MARKER", (self.ws / "MEMORY.md.bak").read_text(encoding="utf-8")
                         if (self.ws / "MEMORY.md.bak").exists() else "")
        self.assertEqual(self.outside.read_text(encoding="utf-8"), before)

    async def test_memory_backup_symlink_is_not_overwritten(self):
        (self.ws / "MEMORY.md").write_text("normal memory", encoding="utf-8")
        (self.ws / "MEMORY.md.bak").symlink_to(self.outside)
        before = self.outside.read_text(encoding="utf-8")
        memory.clear_memory(self.ws)
        self.assertEqual(self.outside.read_text(encoding="utf-8"), before)
        self.assertTrue((self.ws / "MEMORY.md").exists())

    async def test_export_and_tree_skip_symlinks(self):
        from server.main import export_workspace, workspace_tree
        with patch("server.main._ws", return_value=self.ws):
            tree = await workspace_tree("sid", user=None)
            response = await export_workspace("sid", user=None)
            data = b"".join([chunk async for chunk in response.body_iterator])
        self.assertEqual([row["name"] for row in tree["tree"]], ["safe.py"])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertEqual(z.namelist(), ["safe.py"])
            self.assertNotIn(b"PRIVATE_MARKER", z.read("safe.py"))


if __name__ == "__main__":
    unittest.main()
