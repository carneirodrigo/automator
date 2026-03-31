"""Tests for engine.work.skill_sync module."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.work.skill_loader import (
    _file_hash,
    _write_json,
    load_skills_catalog,
    parse_skill_md,
)
from engine.work.skill_sync import build_catalog, check_freshness, main


class BuildCatalogTest(unittest.TestCase):
    """Tests for build_catalog() with local mock repo."""

    def _setup_mock_repo(self, tmpdir: Path) -> tuple[Path, Path, Path]:
        """Create a fake repo structure simulating a vendor skills repo."""
        cache_dir = tmpdir / ".cache"
        catalog_path = tmpdir / "catalog.json"
        sources_path = tmpdir / "sources.json"
        manifest_path = tmpdir / "manifest.json"

        # Create a fake cached repo
        repo_dir = cache_dir / "test-vendor"
        skills_root = repo_dir / "skills"
        skill_dir = skills_root / "example-skill"
        skill_dir.mkdir(parents=True)
        git_dir = repo_dir / ".git"
        git_dir.mkdir(parents=True)

        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            "---\nname: example-skill\ndescription: An example skill\ntags:\n  - example\n  - testing\nversion: \"1.0\"\n---\n# Example\n\nExample body.\n"
        )

        # Write sources.json
        sources = {
            "repos": [
                {"id": "test-vendor", "url": "https://github.com/test/skills", "skills_path": "skills"}
            ]
        }
        _write_json(sources_path, sources)

        # Write empty catalog
        _write_json(catalog_path, {"version": 1, "skills": []})

        return tmpdir, catalog_path, sources_path

    def test_builds_catalog_from_cached_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir, catalog_path, sources_path = self._setup_mock_repo(Path(tmpdir))

            with patch("engine.work.skill_sync.SKILLS_SOURCES_PATH", sources_path), \
                 patch("engine.work.skill_sync.SKILLS_CATALOG_PATH", catalog_path), \
                 patch("engine.work.skill_sync.SKILLS_CACHE_DIR", skills_dir / ".cache"), \
                 patch("engine.work.skill_sync.load_skills_catalog") as mock_catalog, \
                 patch("engine.work.skill_sync._ensure_repo_cached") as mock_clone:

                mock_catalog.return_value = {"version": 1, "skills": []}
                mock_clone.return_value = skills_dir / ".cache" / "test-vendor"

                stats = build_catalog()

            self.assertEqual(len(stats["added"]), 1)
            self.assertEqual(stats["added"][0]["id"], "test-vendor--example-skill")
            self.assertIn("worker", stats["added"][0]["roles"])

    def test_filters_by_repo_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir, catalog_path, sources_path = self._setup_mock_repo(Path(tmpdir))

            with patch("engine.work.skill_sync.SKILLS_SOURCES_PATH", sources_path), \
                 patch("engine.work.skill_sync.SKILLS_CATALOG_PATH", catalog_path), \
                 patch("engine.work.skill_sync.load_skills_catalog") as mock_catalog, \
                 patch("engine.work.skill_sync._ensure_repo_cached") as mock_clone:

                mock_catalog.return_value = {"version": 1, "skills": []}
                mock_clone.return_value = skills_dir / ".cache" / "test-vendor"

                # Filter to nonexistent repo
                stats = build_catalog(repo_filter="nonexistent")

            self.assertEqual(len(stats["added"]), 0)


class CheckFreshnessTest(unittest.TestCase):
    """Tests for check_freshness()."""

    @patch("engine.work.skill_sync.load_skills_manifest")
    @patch("engine.work.skill_sync.load_skills_catalog")
    def test_detects_stale_skills(self, mock_catalog, mock_manifest) -> None:
        mock_manifest.return_value = {
            "version": 1,
            "skills": [{"id": "v--skill", "file_hash": "sha256:old", "version": "1.0"}],
        }
        mock_catalog.return_value = {
            "version": 1,
            "skills": [{"id": "v--skill", "file_hash": "sha256:new", "version": "1.0"}],
        }
        stale = check_freshness()
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["id"], "v--skill")

    @patch("engine.work.skill_sync.load_skills_manifest")
    @patch("engine.work.skill_sync.load_skills_catalog")
    def test_fresh_skills_not_reported(self, mock_catalog, mock_manifest) -> None:
        mock_manifest.return_value = {
            "version": 1,
            "skills": [{"id": "v--skill", "file_hash": "sha256:same", "version": "1.0"}],
        }
        mock_catalog.return_value = {
            "version": 1,
            "skills": [{"id": "v--skill", "file_hash": "sha256:same", "version": "1.0"}],
        }
        stale = check_freshness()
        self.assertEqual(len(stale), 0)


class SkillSyncCliTest(unittest.TestCase):
    def test_dry_run_does_not_write_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog_path = Path(tmpdir) / "catalog.json"
            _write_json(catalog_path, {"version": 1, "skills": []})

            original_argv = sys.argv
            try:
                sys.argv = ["skill_sync.py", "--catalog", "--dry-run"]
                with patch("engine.work.skill_sync.SKILLS_CATALOG_PATH", catalog_path), \
                     patch("engine.work.skill_sync.build_catalog") as mock_build:
                    mock_build.return_value = {
                        "added": [{"id": "demo-skill", "description": "demo"}],
                        "updated": [],
                        "unchanged": [],
                    }
                    main()

                self.assertEqual(json.loads(catalog_path.read_text(encoding="utf-8"))["skills"], [])
                self.assertEqual(mock_build.call_args.kwargs["write_catalog"], False)
            finally:
                sys.argv = original_argv


if __name__ == "__main__":
    unittest.main()
