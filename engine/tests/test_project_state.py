"""Tests for engine.work.project_state helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from engine.work.project_state import secrets_path


class SecretsPathTest(unittest.TestCase):
    """Tests for secrets_path() dual-layout resolution."""

    def test_returns_canonical_when_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir)
            canonical = projects_dir / "my-project" / "secrets" / "secrets.json"
            canonical.parent.mkdir(parents=True)
            canonical.write_text("{}", encoding="utf-8")

            result = secrets_path("my-project", secrets_projects_dir=projects_dir)
            self.assertEqual(result, canonical)

    def test_returns_legacy_when_only_legacy_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir)
            legacy_dir = projects_dir / "secrets"
            legacy = legacy_dir / "my-project" / "secrets.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("{}", encoding="utf-8")

            result = secrets_path("my-project", secrets_projects_dir=legacy_dir)
            self.assertEqual(result, legacy)

    def test_returns_canonical_when_neither_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir)
            expected = projects_dir / "my-project" / "secrets" / "secrets.json"

            result = secrets_path("my-project", secrets_projects_dir=projects_dir)
            self.assertEqual(result, expected)

    def test_canonical_takes_precedence_when_both_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir)
            canonical = projects_dir / "my-project" / "secrets" / "secrets.json"
            canonical.parent.mkdir(parents=True)
            canonical.write_text("{}", encoding="utf-8")
            legacy_dir = projects_dir / "secrets"
            legacy = legacy_dir / "my-project" / "secrets.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("{}", encoding="utf-8")

            result = secrets_path("my-project", secrets_projects_dir=projects_dir)
            self.assertEqual(result, canonical)

    def test_normalises_when_secrets_subdir_passed_as_root(self) -> None:
        """secrets_projects_dir may be projects/secrets/ — must normalise to projects/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir)
            secrets_subdir = projects_dir / "secrets"
            canonical = projects_dir / "my-project" / "secrets" / "secrets.json"
            canonical.parent.mkdir(parents=True)
            canonical.write_text("{}", encoding="utf-8")

            result = secrets_path("my-project", secrets_projects_dir=secrets_subdir)
            self.assertEqual(result, canonical)


if __name__ == "__main__":
    unittest.main()
