from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.work import debug_supervisor


class DebugSupervisorTest(unittest.TestCase):
    def test_analyse_defaults_to_open_and_regressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tracker_path = root / "debug" / "tracker.json"
            issues_dir = root / "debug" / "issues"
            issues_dir.mkdir(parents=True, exist_ok=True)
            tracker = {
                "version": 1,
                "issues": [
                    {
                        "issue_id": "dbg-open",
                        "status": "open",
                        "backend": "gemini",
                        "title": "Open issue",
                        "detail_path": "debug/issues/dbg-open.json",
                    },
                    {
                        "issue_id": "dbg-fixed",
                        "status": "fixed",
                        "backend": "claude",
                        "title": "Fixed issue",
                        "detail_path": "debug/issues/dbg-fixed.json",
                    },
                ],
            }
            tracker_path.parent.mkdir(parents=True, exist_ok=True)
            tracker_path.write_text(json.dumps(tracker), encoding="utf-8")
            (issues_dir / "dbg-open.json").write_text(
                json.dumps({"details": {"error": "Open failure"}}),
                encoding="utf-8",
            )
            (issues_dir / "dbg-fixed.json").write_text(
                json.dumps({"details": {"error": "Fixed failure"}}),
                encoding="utf-8",
            )

            args = debug_supervisor.build_parser().parse_args(["analyse"])

            with mock.patch.object(debug_supervisor, "DEBUG_TRACKER_PATH", tracker_path), \
                 mock.patch.object(debug_supervisor, "REPO_ROOT", root), \
                 mock.patch("sys.stdout") as stdout:
                exit_code = args.func(args)

            self.assertEqual(exit_code, 0)
            output = "".join(call.args[0] for call in stdout.write.call_args_list)
            self.assertIn("dbg-open", output)
            self.assertNotIn("dbg-fixed", output)
            self.assertIn("Open failure", output)

    def test_open_lists_only_open_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tracker_path = root / "debug" / "tracker.json"
            issues_dir = root / "debug" / "issues"
            tracker = {
                "version": 1,
                "issues": [
                    {
                        "issue_id": "dbg-open",
                        "status": "open",
                        "backend": "gemini",
                        "title": "Open issue",
                        "detail_path": "debug/issues/dbg-open.json",
                        "summary": "Open issue summary",
                        "criticality": "high",
                    },
                    {
                        "issue_id": "dbg-fixed",
                        "status": "fixed",
                        "backend": "claude",
                        "title": "Fixed issue",
                        "detail_path": "debug/issues/dbg-fixed.json",
                        "summary": "Fixed issue summary",
                        "criticality": "low",
                    },
                ],
            }
            issues_dir.mkdir(parents=True, exist_ok=True)
            tracker_path.write_text(json.dumps(tracker), encoding="utf-8")
            (issues_dir / "dbg-open.json").write_text(json.dumps({"summary": "Open issue summary"}), encoding="utf-8")
            (issues_dir / "dbg-fixed.json").write_text(json.dumps({"summary": "Fixed issue summary"}), encoding="utf-8")

            args = debug_supervisor.build_parser().parse_args(["open"])

            with mock.patch.object(debug_supervisor, "DEBUG_TRACKER_PATH", tracker_path), \
                 mock.patch.object(debug_supervisor, "REPO_ROOT", root), \
                 mock.patch("sys.stdout") as stdout:
                exit_code = args.func(args)

            self.assertEqual(exit_code, 0)
            output = "".join(call.args[0] for call in stdout.write.call_args_list)
            self.assertIn("dbg-open", output)
            self.assertIn("Open issue summary", output)
            self.assertIn("\thigh\t", output)
            self.assertNotIn("dbg-fixed", output)

    def test_verify_marks_issue_fixed_after_passing_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tracker_path = root / "debug" / "tracker.json"
            detail_path = root / "debug" / "issues" / "dbg-1.json"
            detail_path.parent.mkdir(parents=True, exist_ok=True)
            tracker = {
                "version": 1,
                "issues": [
                    {
                        "issue_id": "dbg-1",
                        "status": "open",
                        "detail_path": "debug/issues/dbg-1.json",
                    }
                ],
            }
            tracker_path.write_text(json.dumps(tracker), encoding="utf-8")
            detail_path.write_text(json.dumps({"issue_id": "dbg-1", "status": "open"}), encoding="utf-8")

            args = debug_supervisor.build_parser().parse_args(
                [
                    "verify",
                    "dbg-1",
                    "--verify-command",
                    "python3 -m unittest engine.tests.test_engine_runtime.ProjectResolutionIntentTest -v",
                    "--summary",
                    "Regression tests passed.",
                ]
            )
            fake_results = [
                {
                    "command": "python3 -m unittest engine.tests.test_engine_runtime.ProjectResolutionIntentTest -v",
                    "returncode": 0,
                    "passed": True,
                    "stdout": "ok",
                    "stderr": "",
                    "ran_at": "2026-03-23T00:00:00+00:00",
                }
            ]

            with mock.patch.object(debug_supervisor, "DEBUG_TRACKER_PATH", tracker_path), \
                 mock.patch.object(debug_supervisor, "REPO_ROOT", root), \
                 mock.patch.object(debug_supervisor, "run_verification_commands", return_value=fake_results):
                exit_code = args.func(args)

            self.assertEqual(exit_code, 0)
            tracker_after = json.loads(tracker_path.read_text())
            self.assertEqual(tracker_after["issues"][0]["status"], "fixed")
            detail_after = json.loads(detail_path.read_text())
            self.assertEqual(detail_after["status"], "fixed")
            self.assertTrue(detail_after["resolution"]["verification_passed"])

    def test_verify_keeps_in_progress_status_on_failed_verification(self) -> None:
        """An in_progress issue that fails verification must stay in_progress, not reset to open."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tracker_path = root / "debug" / "tracker.json"
            detail_path = root / "debug" / "issues" / "dbg-3.json"
            detail_path.parent.mkdir(parents=True, exist_ok=True)
            tracker = {
                "version": 1,
                "issues": [
                    {
                        "issue_id": "dbg-3",
                        "status": "in_progress",
                        "detail_path": "debug/issues/dbg-3.json",
                    }
                ],
            }
            tracker_path.write_text(json.dumps(tracker), encoding="utf-8")
            detail_path.write_text(json.dumps({"issue_id": "dbg-3", "status": "in_progress"}), encoding="utf-8")

            args = debug_supervisor.build_parser().parse_args(
                ["verify", "dbg-3", "--verify-command", "false", "--summary", "Still failing."]
            )
            fake_results = [
                {"command": "false", "returncode": 1, "passed": False, "stdout": "", "stderr": "", "ran_at": "2026-03-28T00:00:00+00:00"}
            ]

            with mock.patch.object(debug_supervisor, "DEBUG_TRACKER_PATH", tracker_path), \
                 mock.patch.object(debug_supervisor, "REPO_ROOT", root), \
                 mock.patch.object(debug_supervisor, "run_verification_commands", return_value=fake_results):
                exit_code = args.func(args)

            self.assertEqual(exit_code, 1)
            tracker_after = json.loads(tracker_path.read_text())
            self.assertEqual(tracker_after["issues"][0]["status"], "in_progress")

    def test_verify_marks_fixed_issue_regressed_after_failed_retest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tracker_path = root / "debug" / "tracker.json"
            detail_path = root / "debug" / "issues" / "dbg-2.json"
            detail_path.parent.mkdir(parents=True, exist_ok=True)
            tracker = {
                "version": 1,
                "issues": [
                    {
                        "issue_id": "dbg-2",
                        "status": "fixed",
                        "detail_path": "debug/issues/dbg-2.json",
                    }
                ],
            }
            tracker_path.write_text(json.dumps(tracker), encoding="utf-8")
            detail_path.write_text(json.dumps({"issue_id": "dbg-2", "status": "fixed"}), encoding="utf-8")

            args = debug_supervisor.build_parser().parse_args(
                [
                    "verify",
                    "dbg-2",
                    "--verify-command",
                    "python3 -m unittest missing.module -v",
                    "--summary",
                    "Retest failed.",
                ]
            )
            fake_results = [
                {
                    "command": "python3 -m unittest missing.module -v",
                    "returncode": 1,
                    "passed": False,
                    "stdout": "",
                    "stderr": "ImportError",
                    "ran_at": "2026-03-23T00:00:00+00:00",
                }
            ]

            with mock.patch.object(debug_supervisor, "DEBUG_TRACKER_PATH", tracker_path), \
                 mock.patch.object(debug_supervisor, "REPO_ROOT", root), \
                 mock.patch.object(debug_supervisor, "run_verification_commands", return_value=fake_results):
                exit_code = args.func(args)

            self.assertEqual(exit_code, 1)
            tracker_after = json.loads(tracker_path.read_text())
            self.assertEqual(tracker_after["issues"][0]["status"], "regressed")
            detail_after = json.loads(detail_path.read_text())
            self.assertEqual(detail_after["status"], "regressed")
            self.assertFalse(detail_after["resolution"]["verification_passed"])


if __name__ == "__main__":
    unittest.main()
