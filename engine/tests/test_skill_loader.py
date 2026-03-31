"""Tests for engine.work.skill_loader module."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.work.skill_loader import (
    _coerce_string_list,
    _parse_frontmatter_fallback,
    parse_skill_md,
    load_skills_manifest,
    rebuild_skills_manifest,
    match_skills_for_role,
    load_skill_body,
    is_skill_stale,
    role_heuristic,
    _file_hash,
)


class ParseSkillMdTest(unittest.TestCase):
    """Tests for parse_skill_md()."""

    def test_valid_skill_md(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("---\nname: test-skill\ndescription: A test skill\ntags:\n  - python\n  - testing\nversion: \"1.0\"\n---\n# Test Skill\n\nThis is the body.\n")
            f.flush()
            result = parse_skill_md(Path(f.name))

        self.assertIsNotNone(result)
        self.assertEqual(result["frontmatter"]["name"], "test-skill")
        self.assertEqual(result["frontmatter"]["description"], "A test skill")
        self.assertIn("python", result["frontmatter"]["tags"])
        self.assertIn("Test Skill", result["body"])

    def test_no_frontmatter_returns_none(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Just a markdown file\n\nNo frontmatter here.\n")
            f.flush()
            result = parse_skill_md(Path(f.name))

        self.assertIsNone(result)

    def test_malformed_frontmatter_uses_fallback(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("---\nname: broken-skill\ndescription: Has no closing tags properly\n---\n# Body\n")
            f.flush()
            result = parse_skill_md(Path(f.name))

        self.assertIsNotNone(result)
        self.assertEqual(result["frontmatter"]["name"], "broken-skill")

    def test_nonexistent_file_returns_none(self) -> None:
        result = parse_skill_md(Path("/nonexistent/SKILL.md"))
        self.assertIsNone(result)


class FrontmatterFallbackTest(unittest.TestCase):
    """Tests for the regex-based YAML fallback parser."""

    def test_simple_key_value(self) -> None:
        raw = 'name: my-skill\ndescription: "A skill"\nversion: "2.0"'
        result = _parse_frontmatter_fallback(raw)
        self.assertEqual(result["name"], "my-skill")
        self.assertEqual(result["description"], "A skill")
        self.assertEqual(result["version"], "2.0")

    def test_list_values(self) -> None:
        raw = "name: my-skill\ntags:\n  - python\n  - testing\n  - async"
        result = _parse_frontmatter_fallback(raw)
        self.assertEqual(result["tags"], ["python", "testing", "async"])

    def test_mixed_keys_and_lists(self) -> None:
        raw = "name: test\nroles:\n  - coding\n  - qa\nversion: \"1.0\""
        result = _parse_frontmatter_fallback(raw)
        self.assertEqual(result["name"], "test")
        self.assertEqual(result["roles"], ["coding", "qa"])
        self.assertEqual(result["version"], "1.0")


class CoerceStringListTest(unittest.TestCase):
    """Tests for _coerce_string_list() — verifies complex types are dropped, not coerced."""

    def test_plain_strings_pass_through(self) -> None:
        self.assertEqual(_coerce_string_list(["coding", "qa"]), ["coding", "qa"])

    def test_scalar_string_wrapped_in_list(self) -> None:
        self.assertEqual(_coerce_string_list("coding"), ["coding"])

    def test_empty_string_dropped(self) -> None:
        self.assertEqual(_coerce_string_list(["coding", "", "  "]), ["coding"])

    def test_int_coerced_to_string(self) -> None:
        self.assertEqual(_coerce_string_list([1, 2]), ["1", "2"])

    def test_bool_dropped(self) -> None:
        # bool is a subclass of int — must NOT be coerced to "True"/"False"
        self.assertEqual(_coerce_string_list([True, False, "coding"]), ["coding"])

    def test_dict_dropped(self) -> None:
        self.assertEqual(_coerce_string_list([{"role": "coding"}, "qa"]), ["qa"])

    def test_none_dropped(self) -> None:
        self.assertEqual(_coerce_string_list([None, "coding"]), ["coding"])

    def test_nested_list_dropped(self) -> None:
        self.assertEqual(_coerce_string_list([["coding"], "qa"]), ["qa"])

    def test_empty_list_returns_empty(self) -> None:
        self.assertEqual(_coerce_string_list([]), [])

    def test_non_string_non_list_returns_empty(self) -> None:
        self.assertEqual(_coerce_string_list(42), [])
        self.assertEqual(_coerce_string_list(None), [])


class RoleHeuristicTest(unittest.TestCase):
    """Tests for role_heuristic()."""

    def test_explicit_roles_returned(self) -> None:
        meta = {"roles": ["worker", "review"]}
        self.assertEqual(role_heuristic(meta), ["worker", "review"])

    def test_scalar_role_is_normalized(self) -> None:
        meta = {"roles": "worker"}
        self.assertEqual(role_heuristic(meta), ["worker"])

    def test_any_tags_infer_worker_role(self) -> None:
        meta = {"name": "owasp-checker", "tags": ["security", "vulnerability"]}
        roles = role_heuristic(meta)
        self.assertEqual(roles, ["worker"])

    def test_no_matches_defaults_to_worker(self) -> None:
        meta = {"name": "some-obscure-thing", "tags": ["obscure"]}
        roles = role_heuristic(meta)
        self.assertEqual(roles, ["worker"])


class MatchSkillsForRoleTest(unittest.TestCase):
    """Tests for match_skills_for_role()."""

    def _make_manifest(self, skills: list[dict]) -> dict:
        return {"version": 1, "skills": skills}

    @patch("engine.work.skill_loader.load_skills_manifest")
    def test_returns_empty_when_no_skills(self, mock_manifest) -> None:
        mock_manifest.return_value = self._make_manifest([])
        result = match_skills_for_role("worker", "build a web app", "worker task")
        self.assertEqual(result, [])

    @patch("engine.work.skill_loader.load_skills_manifest")
    def test_filters_by_role(self, mock_manifest) -> None:
        mock_manifest.return_value = self._make_manifest([
            {"id": "s1", "name": "security-check", "description": "Security audit", "roles": ["review"], "tags": ["security", "audit"]},
            {"id": "s2", "name": "web-deploy", "description": "Deploy web apps", "roles": ["worker"], "tags": ["deploy", "web"]},
        ])
        result = match_skills_for_role("worker", "deploy a web application", "deploy task")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "s2")

    @patch("engine.work.skill_loader.load_skills_manifest")
    def test_keyword_scoring(self, mock_manifest) -> None:
        mock_manifest.return_value = self._make_manifest([
            {"id": "s1", "name": "playwright-testing", "description": "Browser testing with Playwright", "roles": ["worker"], "tags": ["playwright", "testing", "browser"]},
            {"id": "s2", "name": "unit-testing", "description": "Unit testing patterns", "roles": ["worker"], "tags": ["testing", "unit"]},
        ])
        result = match_skills_for_role("worker", "run playwright browser tests", "worker check")
        self.assertTrue(len(result) >= 1)
        # playwright-testing should score higher due to keyword overlap
        self.assertEqual(result[0]["id"], "s1")

    @patch("engine.work.skill_loader.load_skills_manifest")
    def test_max_three_results(self, mock_manifest) -> None:
        skills = [
            {"id": f"s{i}", "name": f"skill-{i}", "description": "Deploy apps", "roles": ["worker"], "tags": ["deploy", "web", "app"]}
            for i in range(5)
        ]
        mock_manifest.return_value = self._make_manifest(skills)
        result = match_skills_for_role("worker", "deploy a web app", "worker task")
        self.assertLessEqual(len(result), 3)


class LoadSkillBodyTest(unittest.TestCase):
    """Tests for load_skill_body()."""

    def test_returns_body_without_frontmatter(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("---\nname: test\n---\n# Body Content\n\nSome instructions.\n")
            f.flush()
            body = load_skill_body(Path(f.name))

        self.assertIn("Body Content", body)
        self.assertNotIn("---", body)

    def test_truncation(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("---\nname: test\n---\n" + "x" * 10000)
            f.flush()
            body = load_skill_body(Path(f.name), max_chars=100)

        self.assertLessEqual(len(body), 120)  # 100 + truncation marker
        self.assertIn("[TRUNCATED]", body)


class IsSkillStaleTest(unittest.TestCase):
    """Tests for is_skill_stale()."""

    def test_different_hash_is_stale(self) -> None:
        manifest_entry = {"id": "test--skill", "file_hash": "sha256:aaa", "version": "1.0"}
        catalog = {"skills": [{"id": "test--skill", "file_hash": "sha256:bbb", "version": "1.0"}]}
        self.assertTrue(is_skill_stale(manifest_entry, catalog))

    def test_same_hash_is_fresh(self) -> None:
        manifest_entry = {"id": "test--skill", "file_hash": "sha256:aaa", "version": "1.0"}
        catalog = {"skills": [{"id": "test--skill", "file_hash": "sha256:aaa", "version": "1.0"}]}
        self.assertFalse(is_skill_stale(manifest_entry, catalog))

    def test_not_in_catalog_is_fresh(self) -> None:
        manifest_entry = {"id": "local--skill", "file_hash": "sha256:aaa"}
        catalog = {"skills": []}
        self.assertFalse(is_skill_stale(manifest_entry, catalog))

    def test_different_version_is_stale(self) -> None:
        manifest_entry = {"id": "test--skill", "file_hash": "", "version": "1.0"}
        catalog = {"skills": [{"id": "test--skill", "file_hash": "", "version": "2.0"}]}
        self.assertTrue(is_skill_stale(manifest_entry, catalog))


class RebuildManifestTest(unittest.TestCase):
    """Tests for rebuild_skills_manifest()."""

    def test_rebuild_from_cached_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skill_dir = skills_dir / "vendor--test-skill"
            skill_dir.mkdir(parents=True)
            skill_md = skill_dir / "SKILL.md"
            skill_md.write_text("---\nname: test-skill\ndescription: A test\ntags:\n  - test\nversion: \"1.0\"\n---\n# Body\n")

            manifest_path = skills_dir / "manifest.json"

            with patch("engine.work.skill_loader.SKILLS_DIR", skills_dir), \
                 patch("engine.work.skill_loader.SKILLS_MANIFEST_PATH", manifest_path):
                manifest = rebuild_skills_manifest()

            self.assertEqual(len(manifest["skills"]), 1)
            self.assertEqual(manifest["skills"][0]["id"], "vendor--test-skill")
            self.assertEqual(manifest["skills"][0]["name"], "test-skill")
            self.assertTrue(manifest_path.exists())

    def test_rebuild_normalizes_scalar_roles_and_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skill_dir = skills_dir / "vendor--test-skill"
            skill_dir.mkdir(parents=True)
            skill_md = skill_dir / "SKILL.md"
            skill_md.write_text(
                "---\nname: test-skill\nroles: coding\ntags: api\n---\n# Body\n"
            )

            manifest_path = skills_dir / "manifest.json"

            with patch("engine.work.skill_loader.SKILLS_DIR", skills_dir), \
                 patch("engine.work.skill_loader.SKILLS_MANIFEST_PATH", manifest_path):
                manifest = rebuild_skills_manifest()

            self.assertEqual(manifest["skills"][0]["roles"], ["coding"])
            self.assertEqual(manifest["skills"][0]["tags"], ["api"])


if __name__ == "__main__":
    unittest.main()
