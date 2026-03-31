"""Tests for engine.work.repo_bootstrap module."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.work.repo_bootstrap import ensure_repo_structure, _REPO_ROOT_SYMLINKS


class EnsureRepoStructureTest(unittest.TestCase):
    """Tests for ensure_repo_structure()."""

    def test_creates_directory_and_files_from_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skills_dir = root / "skills"
            catalog_path = skills_dir / "catalog.json"
            manifest_path = skills_dir / "manifest.json"
            sources_path = skills_dir / "sources.json"
            debug_dir = root / "debug"
            debug_issues_dir = debug_dir / "issues"
            debug_tracker_path = debug_dir / "tracker.json"
            inputs_dir = root / "inputs"

            # Create the targets that repo-root symlinks point to
            (root / "engine" / "work").mkdir(parents=True)
            (root / "engine" / "ORCHESTRATION.md").write_text("# Orchestration\n")
            (root / "engine" / "automator.py").write_text("# launcher\n")

            with patch("engine.work.repo_bootstrap.SKILLS_DIR", skills_dir), \
                 patch("engine.work.repo_bootstrap.SKILLS_CATALOG_PATH", catalog_path), \
                 patch("engine.work.repo_bootstrap.SKILLS_MANIFEST_PATH", manifest_path), \
                 patch("engine.work.repo_bootstrap.SKILLS_SOURCES_PATH", sources_path), \
                 patch("engine.work.repo_bootstrap.DEBUG_DIR", debug_dir), \
                 patch("engine.work.repo_bootstrap.DEBUG_ISSUES_DIR", debug_issues_dir), \
                 patch("engine.work.repo_bootstrap.DEBUG_TRACKER_PATH", debug_tracker_path), \
                 patch("engine.work.repo_bootstrap.INPUTS_DIR", inputs_dir), \
                 patch("engine.work.repo_bootstrap.REPO_ROOT", root):
                ensure_repo_structure()

            self.assertTrue(skills_dir.is_dir())
            self.assertTrue(catalog_path.exists())
            self.assertTrue(manifest_path.exists())
            self.assertTrue(sources_path.exists())
            self.assertTrue(debug_dir.is_dir())
            self.assertTrue(debug_issues_dir.is_dir())
            self.assertTrue(debug_tracker_path.exists())
            self.assertTrue(inputs_dir.is_dir())

            # Check JSON is valid
            catalog = json.loads(catalog_path.read_text())
            self.assertEqual(catalog["version"], 1)

            sources = json.loads(sources_path.read_text())
            self.assertGreater(len(sources["repos"]), 0)

            # Check repo-root symlinks
            for link_name in _REPO_ROOT_SYMLINKS:
                link = root / link_name
                self.assertTrue(link.is_symlink(), f"Missing repo-root symlink: {link}")

            # Check .gitignore was created
            gitignore = root / ".gitignore"
            self.assertTrue(gitignore.exists())
            gi_content = gitignore.read_text()
            self.assertIn("__pycache__", gi_content)
            self.assertIn("CLAUDE.md", gi_content)
            self.assertIn("debug/", gi_content)

    def test_does_not_overwrite_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skills_dir = root / "skills"
            skills_dir.mkdir()
            sources_path = skills_dir / "sources.json"
            catalog_path = skills_dir / "catalog.json"
            manifest_path = skills_dir / "manifest.json"
            debug_dir = root / "debug"
            debug_issues_dir = debug_dir / "issues"
            debug_tracker_path = debug_dir / "tracker.json"
            inputs_dir = root / "inputs"

            # Write custom sources
            custom = {"version": 1, "repos": [{"id": "custom", "url": "https://example.com", "skills_path": "s"}]}
            sources_path.write_text(json.dumps(custom))

            with patch("engine.work.repo_bootstrap.SKILLS_DIR", skills_dir), \
                 patch("engine.work.repo_bootstrap.SKILLS_CATALOG_PATH", catalog_path), \
                 patch("engine.work.repo_bootstrap.SKILLS_MANIFEST_PATH", manifest_path), \
                 patch("engine.work.repo_bootstrap.SKILLS_SOURCES_PATH", sources_path), \
                 patch("engine.work.repo_bootstrap.DEBUG_DIR", debug_dir), \
                 patch("engine.work.repo_bootstrap.DEBUG_ISSUES_DIR", debug_issues_dir), \
                 patch("engine.work.repo_bootstrap.DEBUG_TRACKER_PATH", debug_tracker_path), \
                 patch("engine.work.repo_bootstrap.INPUTS_DIR", inputs_dir), \
                 patch("engine.work.repo_bootstrap.REPO_ROOT", root):
                ensure_repo_structure()

            # Custom sources should not be overwritten
            data = json.loads(sources_path.read_text())
            self.assertEqual(data["repos"][0]["id"], "custom")

            # Custom .gitignore should not be overwritten
            gitignore = root / ".gitignore"
            gitignore.write_text("# custom\n")
            with patch("engine.work.repo_bootstrap.SKILLS_DIR", skills_dir), \
                 patch("engine.work.repo_bootstrap.SKILLS_CATALOG_PATH", catalog_path), \
                 patch("engine.work.repo_bootstrap.SKILLS_MANIFEST_PATH", manifest_path), \
                 patch("engine.work.repo_bootstrap.SKILLS_SOURCES_PATH", sources_path), \
                 patch("engine.work.repo_bootstrap.DEBUG_DIR", debug_dir), \
                 patch("engine.work.repo_bootstrap.DEBUG_ISSUES_DIR", debug_issues_dir), \
                 patch("engine.work.repo_bootstrap.DEBUG_TRACKER_PATH", debug_tracker_path), \
                 patch("engine.work.repo_bootstrap.INPUTS_DIR", inputs_dir), \
                 patch("engine.work.repo_bootstrap.REPO_ROOT", root):
                ensure_repo_structure()
            self.assertEqual(gitignore.read_text(), "# custom\n")


if __name__ == "__main__":
    unittest.main()
