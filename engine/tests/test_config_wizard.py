"""Tests for config_wizard: environment checks, show, and validate commands."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.work.config_wizard import (
    CheckResult,
    _print_check_results,
    _print_fixes,
    _redact_key,
    check_api_sdk,
    check_cli_tools,
    check_git,
    check_node,
    check_optional_system_tools,
    check_python_packages,
    check_python_version,
    check_venv,
    cmd_show,
    cmd_validate,
    run_all_checks,
)


class TestRedactKey(unittest.TestCase):
    def test_short_key(self):
        self.assertEqual(_redact_key("abc"), "***")

    def test_normal_key(self):
        result = _redact_key("sk-ant-api03-abcdefgh")
        self.assertEqual(result, "sk-a...efgh")

    def test_empty_key(self):
        self.assertEqual(_redact_key(""), "***")


# ---------------------------------------------------------------------------
# Environment check tests
# ---------------------------------------------------------------------------


class TestCheckPythonVersion(unittest.TestCase):
    def test_current_python_passes(self):
        result = check_python_version()
        self.assertEqual(result.status, "ok")
        self.assertIn(str(sys.version_info.major), result.message)

    def test_old_python_fails(self):
        fake_info = (3, 9, 1)
        with patch.object(sys, "version_info", type("V", (), {
            "major": 3, "minor": 9, "micro": 1,
            "__iter__": lambda s: iter(fake_info),
        })()):
            result = check_python_version()
        self.assertEqual(result.status, "fail")
        self.assertIn("3.10", result.message)
        self.assertTrue(result.fix)


class TestCheckGit(unittest.TestCase):
    @patch("engine.work.config_wizard.shutil.which", return_value="/usr/bin/git")
    @patch("engine.work.config_wizard.subprocess.run")
    def test_git_found(self, mock_run, mock_which):
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "git version 2.40.0"})()
        result = check_git()
        self.assertEqual(result.status, "ok")
        self.assertIn("2.40.0", result.message)

    @patch("engine.work.config_wizard.shutil.which", return_value=None)
    def test_git_missing(self, mock_which):
        result = check_git()
        self.assertEqual(result.status, "fail")
        self.assertTrue(result.fix)


class TestCheckNode(unittest.TestCase):
    @patch("engine.work.config_wizard.shutil.which")
    @patch("engine.work.config_wizard.subprocess.run")
    def test_node_found(self, mock_run, mock_which):
        mock_which.side_effect = lambda x: f"/usr/bin/{x}" if x in ("npm", "node") else None
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "v20.10.0"})()
        result = check_node()
        self.assertEqual(result.status, "ok")
        self.assertIn("v20.10.0", result.message)

    @patch("engine.work.config_wizard.shutil.which", return_value=None)
    def test_node_missing(self, mock_which):
        result = check_node()
        self.assertEqual(result.status, "warn")
        self.assertTrue(result.fix)

    @patch("engine.work.config_wizard.shutil.which")
    def test_node_without_npm(self, mock_which):
        mock_which.side_effect = lambda x: "/usr/bin/node" if x == "node" else None
        result = check_node()
        self.assertEqual(result.status, "warn")
        self.assertIn("npm", result.message)


class TestCheckCliTools(unittest.TestCase):
    @patch("engine.work.config_wizard.shutil.which")
    def test_some_found(self, mock_which):
        mock_which.side_effect = lambda x: "/usr/bin/claude" if x == "claude" else None
        results = check_cli_tools()
        self.assertEqual(len(results), 3)
        claude = [r for r in results if "claude" in r.name][0]
        self.assertEqual(claude.status, "ok")
        gemini = [r for r in results if "gemini" in r.name][0]
        self.assertEqual(gemini.status, "info")

    @patch("engine.work.config_wizard.shutil.which", return_value=None)
    def test_none_found(self, mock_which):
        results = check_cli_tools()
        self.assertTrue(all(r.status == "info" for r in results))


class TestCheckPythonPackages(unittest.TestCase):
    def test_returns_results_for_all_core_packages(self):
        results = check_python_packages()
        self.assertGreater(len(results), 5)
        for r in results:
            self.assertIn(r.status, ("ok", "fail"))


class TestCheckApiSdk(unittest.TestCase):
    def test_unknown_provider(self):
        result = check_api_sdk("invalid")
        self.assertEqual(result.status, "fail")

    @patch("builtins.__import__")
    def test_installed_sdk(self, mock_import):
        mock_import.return_value = None
        result = check_api_sdk("anthropic")
        self.assertEqual(result.status, "ok")

    @patch("builtins.__import__", side_effect=ImportError("no module"))
    def test_missing_sdk(self, mock_import):
        result = check_api_sdk("anthropic")
        self.assertEqual(result.status, "fail")
        self.assertIn("pip install", result.fix)


class TestCheckVenv(unittest.TestCase):
    def test_returns_check_result(self):
        # Just verify it returns a CheckResult without error
        result = check_venv()
        self.assertIn(result.status, ("ok", "warn"))
        self.assertIsInstance(result, CheckResult)

    def test_no_venv_dir(self):
        with patch("engine.work.config_wizard.REPO_ROOT", Path("/nonexistent/repo")):
            result = check_venv()
        self.assertEqual(result.status, "warn")
        self.assertIn(".venv", result.message)


class TestCheckOptionalSystemTools(unittest.TestCase):
    @patch("engine.work.config_wizard.shutil.which")
    def test_some_found(self, mock_which):
        mock_which.side_effect = lambda x: f"/usr/bin/{x}" if x == "qpdf" else None
        results = check_optional_system_tools()
        self.assertEqual(len(results), 3)
        qpdf = [r for r in results if "qpdf" in r.name][0]
        self.assertEqual(qpdf.status, "ok")
        pdftotext = [r for r in results if "pdftotext" in r.name][0]
        self.assertEqual(pdftotext.status, "info")


class TestRunAllChecks(unittest.TestCase):
    @patch("engine.work.config_wizard.shutil.which", return_value="/usr/bin/x")
    @patch("engine.work.config_wizard.subprocess.run")
    def test_returns_list(self, mock_run, mock_which):
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "ok"})()
        results = run_all_checks()
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 10)
        for r in results:
            self.assertIsInstance(r, CheckResult)

    @patch("engine.work.config_wizard.shutil.which", return_value="/usr/bin/x")
    @patch("engine.work.config_wizard.subprocess.run")
    def test_api_mode_includes_sdk_check(self, mock_run, mock_which):
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "ok"})()
        results_cli = run_all_checks(mode="cli")
        results_api = run_all_checks(mode="api", provider="anthropic")
        sdk_checks = [r for r in results_api if "SDK" in r.name]
        self.assertEqual(len(sdk_checks), 1)
        sdk_checks_cli = [r for r in results_cli if "SDK" in r.name]
        self.assertEqual(len(sdk_checks_cli), 0)


class TestPrintCheckResults(unittest.TestCase):
    def test_counts(self):
        results = [
            CheckResult("A", "ok", "good"),
            CheckResult("B", "fail", "bad", fix="do X"),
            CheckResult("C", "warn", "iffy", fix="do Y"),
            CheckResult("D", "ok", "good"),
            CheckResult("E", "info", "fyi"),
        ]
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            ok, warn, fail = _print_check_results(results)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(ok, 2)
        self.assertEqual(warn, 1)
        self.assertEqual(fail, 1)


class TestPrintFixes(unittest.TestCase):
    def test_shows_fail_and_warn_fixes(self):
        results = [
            CheckResult("A", "ok", "good"),
            CheckResult("B", "fail", "bad", fix="install B"),
            CheckResult("C", "warn", "iffy", fix="try C"),
            CheckResult("D", "info", "fyi", fix="optional D"),
        ]
        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            _print_fixes(results)
        finally:
            sys.stdout = old_stdout
        output = captured.getvalue()
        self.assertIn("install B", output)
        self.assertIn("try C", output)
        # info-level fixes should not be shown
        self.assertNotIn("optional D", output)

    def test_no_fixes_prints_nothing(self):
        results = [CheckResult("A", "ok", "good")]
        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            _print_fixes(results)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(captured.getvalue(), "")


# ---------------------------------------------------------------------------
# cmd_show tests
# ---------------------------------------------------------------------------


class TestCmdShow(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_dir = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_config_returns_1(self):
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            result = cmd_show(self.config_dir)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(result, 1)

    def test_show_cli_mode(self):
        config = {"version": 2, "mode": "cli"}
        (self.config_dir / "backends.json").write_text(json.dumps(config))

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            result = cmd_show(self.config_dir)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(result, 0)
        output = captured.getvalue()
        self.assertIn("cli", output)

    def test_show_api_mode(self):
        config = {
            "version": 2,
            "mode": "api",
            "provider": "anthropic",
            "default_model": "claude-sonnet-4-20250514",
            "role_overrides": {
                "worker": {"model": "claude-opus-4-20250514"},
            },
        }
        secrets = {"anthropic_api_key": "sk-ant-test1234567890"}
        (self.config_dir / "backends.json").write_text(json.dumps(config))
        (self.config_dir / "secrets.json").write_text(json.dumps(secrets))

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            result = cmd_show(self.config_dir)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(result, 0)
        output = captured.getvalue()
        self.assertIn("api", output)
        self.assertIn("anthropic", output)
        self.assertIn("claude-sonnet-4-20250514", output)
        self.assertIn("worker", output)
        # Key should be redacted
        self.assertNotIn("sk-ant-test1234567890", output)


# ---------------------------------------------------------------------------
# cmd_validate tests
# ---------------------------------------------------------------------------


class TestCmdValidate(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_dir = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_config_returns_1(self):
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            result = cmd_validate(self.config_dir)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(result, 1)

    def test_cli_mode_always_passes(self):
        config = {"version": 2, "mode": "cli"}
        (self.config_dir / "backends.json").write_text(json.dumps(config))

        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            result = cmd_validate(self.config_dir)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(result, 0)

    def test_api_mode_valid(self):
        config = {"version": 2, "mode": "api", "provider": "anthropic"}
        secrets = {"anthropic_api_key": "sk-ant-test-key"}
        (self.config_dir / "backends.json").write_text(json.dumps(config))
        (self.config_dir / "secrets.json").write_text(json.dumps(secrets))

        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            result = cmd_validate(self.config_dir)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(result, 0)

    def test_api_mode_missing_key_fails(self):
        config = {"version": 2, "mode": "api", "provider": "anthropic"}
        (self.config_dir / "backends.json").write_text(json.dumps(config))

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            result = cmd_validate(self.config_dir)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(result, 1)
        output = captured.getvalue()
        self.assertIn("ERROR", output)
        self.assertIn("anthropic_api_key", output)

    def test_role_override_provider_missing_key_fails(self):
        config = {
            "version": 2,
            "mode": "api",
            "provider": "anthropic",
            "role_overrides": {
                "worker": {"provider": "openai"},
            },
        }
        secrets = {"anthropic_api_key": "sk-ant-test"}
        (self.config_dir / "backends.json").write_text(json.dumps(config))
        (self.config_dir / "secrets.json").write_text(json.dumps(secrets))

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            result = cmd_validate(self.config_dir)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(result, 1)
        output = captured.getvalue()
        self.assertIn("ERROR", output)
        self.assertIn("openai_api_key", output)

    def test_invalid_provider_fails(self):
        config = {"version": 2, "mode": "api", "provider": "invalid"}
        (self.config_dir / "backends.json").write_text(json.dumps(config))

        old_stdout = sys.stdout
        sys.stdout = captured = StringIO()
        try:
            result = cmd_validate(self.config_dir)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(result, 1)
        output = captured.getvalue()
        self.assertIn("ERROR", output)


if __name__ == "__main__":
    unittest.main()
