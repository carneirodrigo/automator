"""Tests for the unified automator entrypoint."""

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import automator
from engine.work.repo_bootstrap import ensure_repo_structure


@contextlib.contextmanager
def _fake_registry(*project_ids: str):
    """Create a temp registry file containing the given project IDs."""
    registry = {"projects": [{"project_id": pid} for pid in project_ids]}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(registry, f)
        f.flush()
        with mock.patch("engine.work.cli.REGISTRY_PATH", Path(f.name)):
            yield


class AutomatorProjectSubcommandTest(unittest.TestCase):
    def test_project_new_builds_structured_request(self) -> None:
        with mock.patch("engine.automator.engine_runtime.main", return_value=0) as mocked:
            result = automator.main([
                "--cli", "claude",
                "--project", "new",
                "--task", "show", "me", "how", "to", "build", "the", "flow",
            ])
        self.assertEqual(result, 0)
        forwarded = mocked.call_args.args[0]
        self.assertEqual(forwarded[0], "--claude")
        request = forwarded[-1]
        self.assertIn("start new project.", request)
        self.assertIn("Task: show me how to build the flow", request)

    def test_project_continue_uses_project_id_prefix(self) -> None:
        with _fake_registry("my-project"), \
             mock.patch("engine.automator.engine_runtime.main", return_value=0) as mocked:
            automator.main([
                "--cli", "claude",
                "--project", "continue",
                "--id", "my-project",
                "--task", "add", "sharepoint", "steps",
            ])
        request = mocked.call_args.args[0][-1]
        self.assertTrue(request.startswith("my-project "))
        self.assertIn("add sharepoint steps", request)

    def test_project_fork_builds_fork_request(self) -> None:
        with _fake_registry("api-project"), \
             mock.patch("engine.automator.engine_runtime.main", return_value=0) as mocked:
            automator.main([
                "--cli", "claude",
                "--project", "fork",
                "--id", "api-project",
                "--task", "store", "results", "in", "sharepoint",
            ])
        request = mocked.call_args.args[0][-1]
        self.assertIn("fork api-project into a new project.", request)
        self.assertIn("Task: store results in sharepoint", request)

    def test_check_runtime_routes_without_request(self) -> None:
        with mock.patch("engine.automator.engine_runtime.main", return_value=0) as mocked:
            automator.main(["--cli", "gemini", "--check-runtime"])
        mocked.assert_called_once_with(["--gemini", "--check-runtime"])

    def test_project_new_with_debug_forces_capture_mode(self) -> None:
        with mock.patch("engine.automator.engine_runtime.main", return_value=0) as mocked:
            automator.main([
                "--cli", "claude",
                "--project", "new",
                "--debug",
                "--task", "investigate", "oauth", "failure",
            ])
        forwarded = mocked.call_args.args[0]
        self.assertIn("--debug-mode", forwarded)
        self.assertEqual(forwarded[0], "--claude")

    def test_project_continue_requires_id(self) -> None:
        with self.assertRaises(SystemExit):
            automator.main(["--cli", "claude", "--project", "continue", "--task", "do something"])

    def test_project_continue_nonexistent_id_errors(self) -> None:
        with _fake_registry("real-project"):
            with self.assertRaises(SystemExit) as ctx:
                automator.main(["--cli", "claude", "--project", "continue", "--id", "ghost", "--task", "fix"])
        self.assertIn("not found", str(ctx.exception))

    def test_project_fork_nonexistent_id_errors(self) -> None:
        with _fake_registry("real-project"):
            with self.assertRaises(SystemExit) as ctx:
                automator.main(["--cli", "claude", "--project", "fork", "--id", "ghost", "--task", "fix"])
        self.assertIn("not found", str(ctx.exception))

    def test_project_invalid_action_errors(self) -> None:
        with self.assertRaises(SystemExit):
            automator.main(["--cli", "claude", "--project", "run", "--task", "do something"])


class AutomatorDebugSubcommandTest(unittest.TestCase):
    def test_debug_open_routes_to_debug_supervisor(self) -> None:
        with mock.patch("engine.automator.debug_supervisor.main", return_value=0) as mocked:
            result = automator.main(["--debug", "open"])
        self.assertEqual(result, 0)
        mocked.assert_called_once_with(["open"])

    def test_debug_default_action_is_open(self) -> None:
        with mock.patch("engine.automator.debug_supervisor.main", return_value=0) as mocked:
            result = automator.main(["--debug"])
        self.assertEqual(result, 0)
        mocked.assert_called_once_with(["open"])

    def test_debug_verify_routes_with_verification_flags(self) -> None:
        with mock.patch("engine.automator.debug_supervisor.main", return_value=0) as mocked:
            result = automator.main([
                "--debug", "verify",
                "--id", "dbg-123",
                "--verify-command", "python3 -m unittest engine.tests.test_engine_runtime -v",
                "--summary", "verification passed",
                "--supervisor", "codex",
            ])
        self.assertEqual(result, 0)
        mocked.assert_called_once_with([
            "verify",
            "dbg-123",
            "--verify-command",
            "python3 -m unittest engine.tests.test_engine_runtime -v",
            "--summary",
            "verification passed",
            "--supervisor",
            "codex",
        ])

    def test_debug_continue_project_forces_debug_mode(self) -> None:
        with _fake_registry("my-project"), \
             mock.patch("engine.automator.engine_runtime.main", return_value=0) as mocked:
            automator.main([
                "--cli", "claude",
                "--project", "continue",
                "--id", "my-project",
                "--debug",
                "--task", "investigate", "oauth", "failure",
            ])
        forwarded = mocked.call_args.args[0]
        self.assertIn("--debug-mode", forwarded)
        self.assertEqual(forwarded[0], "--claude")
        self.assertTrue(forwarded[-1].startswith("my-project "))


class AutomatorSkillsAndAgentsSubcommandTest(unittest.TestCase):
    def test_skills_catalog_routes_to_skill_sync(self) -> None:
        with mock.patch("engine.automator.skill_sync.main", return_value=0) as mocked:
            result = automator.main(["--skill", "catalog", "--repo", "openai", "--dry-run"])
        self.assertEqual(result, 0)
        mocked.assert_called_once_with(["--catalog", "--repo", "openai", "--dry-run"])

    def test_agents_list_routes_to_agent_admin(self) -> None:
        with mock.patch("engine.automator.agent_admin.main", return_value=0) as mocked:
            result = automator.main(["--agent", "list"])
        self.assertEqual(result, 0)
        mocked.assert_called_once_with(["list"])

    def test_agents_add_routes_to_agent_admin(self) -> None:
        with mock.patch("engine.automator.agent_admin.main", return_value=0) as mocked:
            result = automator.main([
                "--agent", "add",
                "--id", "platform-implementation",
                "--purpose", "Produce low-code implementation guides.",
            ])
        self.assertEqual(result, 0)
        mocked.assert_called_once_with([
            "add",
            "platform-implementation",
            "--purpose",
            "Produce low-code implementation guides.",
        ])

    def test_knowledge_purge_routes_to_engine_runtime(self) -> None:
        with mock.patch("engine.automator.engine_runtime.purge_project_knowledge", return_value=0) as mocked:
            result = automator.main(["--knowledge", "purge", "--id", "project-a"])
        self.assertEqual(result, 0)
        mocked.assert_called_once_with("project-a")

    def test_knowledge_purge_requires_id(self) -> None:
        with self.assertRaises(SystemExit):
            automator.main(["--knowledge", "purge"])


class AutomatorHelpCommandTest(unittest.TestCase):
    def test_dash_help_prints_top_level_help(self) -> None:
        with mock.patch("sys.stdout") as stdout:
            result = automator.main(["--help"])
        self.assertEqual(result, 0)
        written = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("--project", written)
        self.assertIn("--cli", written)
        self.assertIn("--api", written)

    def test_no_args_prints_help_and_returns_1(self) -> None:
        with mock.patch("sys.stdout"):
            result = automator.main([])
        self.assertEqual(result, 1)


class AutomatorLegacyCompatibilityTest(unittest.TestCase):
    def test_legacy_project_request_routes_to_engine_runtime(self) -> None:
        with mock.patch("engine.automator.engine_runtime.main", return_value=0) as mocked:
            result = automator.main(["--claude", "build", "a", "script"])
        self.assertEqual(result, 0)
        mocked.assert_called_once_with(["--claude", "build a script"])

    def test_legacy_debug_open_routes_to_debug_supervisor(self) -> None:
        with mock.patch("engine.automator.debug_supervisor.main", return_value=0) as mocked:
            result = automator.main(["--debug-open"])
        self.assertEqual(result, 0)
        mocked.assert_called_once_with(["open"])

    def test_legacy_skills_list_routes_to_skill_sync(self) -> None:
        with mock.patch("engine.automator.skill_sync.main", return_value=0) as mocked:
            result = automator.main(["--skills-list"])
        self.assertEqual(result, 0)
        mocked.assert_called_once_with(["--list"])

    def test_mixed_legacy_debug_and_skills_modes_error(self) -> None:
        with self.assertRaises(SystemExit) as exc:
            automator.main(["--debug-open", "--skills-list"])
        self.assertEqual(exc.exception.code, 2)


class RepoBootstrapLauncherPreservationTest(unittest.TestCase):
    def test_preserves_tracked_automator_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "engine").mkdir()
            (root / "engine" / "work").mkdir(parents=True)
            (root / "engine" / "ORCHESTRATION.md").write_text("# Orchestration\n")
            (root / "engine" / "work" / "engine_runtime.py").write_text("# engine\n")
            (root / "engine" / "automator.py").write_text("# automator\n")
            launcher_body = "#!/usr/bin/env bash\nexec python3 \"$PWD/engine/automator.py\" \"$@\"\n"
            (root / "automator").write_text(launcher_body, encoding="utf-8")

            skills_dir = root / "skills"
            debug_dir = root / "debug"
            with mock.patch("engine.work.repo_bootstrap.REPO_ROOT", root), \
                 mock.patch("engine.work.repo_bootstrap.SKILLS_DIR", skills_dir), \
                 mock.patch("engine.work.repo_bootstrap.SKILLS_CATALOG_PATH", skills_dir / "catalog.json"), \
                 mock.patch("engine.work.repo_bootstrap.SKILLS_MANIFEST_PATH", skills_dir / "manifest.json"), \
                 mock.patch("engine.work.repo_bootstrap.SKILLS_SOURCES_PATH", skills_dir / "sources.json"), \
                 mock.patch("engine.work.repo_bootstrap.DEBUG_DIR", debug_dir), \
                 mock.patch("engine.work.repo_bootstrap.DEBUG_ISSUES_DIR", debug_dir / "issues"), \
                 mock.patch("engine.work.repo_bootstrap.DEBUG_TRACKER_PATH", debug_dir / "tracker.json"):
                ensure_repo_structure()

            self.assertFalse((root / "automator").is_symlink())
            self.assertEqual((root / "automator").read_text(encoding="utf-8"), launcher_body)


if __name__ == "__main__":
    unittest.main()
