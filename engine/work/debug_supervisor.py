#!/usr/bin/env python3
"""Supervisor CLI for retesting and updating debug issues with enforcement."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.work.repo_paths import DEBUG_TRACKER_PATH, REPO_ROOT
from engine.work.json_io import load_json, write_json
from engine.work.runtime_helpers import now_iso

VALID_ISSUE_STATUSES: frozenset[str] = frozenset({"open", "in_progress", "fixed", "regressed"})


def load_tracker() -> dict[str, Any]:
    tracker = load_json(DEBUG_TRACKER_PATH)
    if not isinstance(tracker, dict) or not isinstance(tracker.get("issues"), list):
        raise SystemExit(f"Invalid tracker format: {DEBUG_TRACKER_PATH}")
    return tracker


def find_issue(tracker: dict[str, Any], issue_id: str) -> dict[str, Any]:
    for issue in tracker["issues"]:
        if issue.get("issue_id") == issue_id:
            return issue
    raise SystemExit(f"Issue not found: {issue_id}")


def issue_detail_path(issue: dict[str, Any]) -> Path:
    raw = str(issue.get("detail_path", ""))
    # Reject ".." components before any symlink resolution so a symlink
    # pointing outside the repo cannot sneak past the containment check.
    if ".." in Path(raw).parts:
        raise SystemExit(
            f"Unsafe detail_path for {issue.get('issue_id')}: contains '..' traversal"
        )
    detail_path = REPO_ROOT / raw
    try:
        detail_path.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        raise SystemExit(
            f"Unsafe detail_path for {issue.get('issue_id')}: traverses outside repo root"
        )
    if not detail_path.exists():
        raise SystemExit(f"Detail file not found for {issue.get('issue_id')}: {detail_path}")
    return detail_path


def run_verification_commands(commands: list[str], *, timeout: int = 300) -> list[dict[str, Any]]:
    if not commands:
        raise SystemExit("At least one --verify-command is required.")
    results: list[dict[str, Any]] = []
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            results.append(
                {
                    "command": command,
                    "returncode": completed.returncode,
                    "passed": completed.returncode == 0,
                    "stdout": completed.stdout[-8000:],
                    "stderr": completed.stderr[-8000:],
                    "ran_at": now_iso(),
                }
            )
        except subprocess.TimeoutExpired:
            results.append(
                {
                    "command": command,
                    "returncode": -1,
                    "passed": False,
                    "stdout": "",
                    "stderr": f"Command timed out after {timeout}s",
                    "ran_at": now_iso(),
                }
            )
    return results


def summarize_results(results: list[dict[str, Any]]) -> bool:
    return all(result.get("passed") for result in results)


def append_history(detail: dict[str, Any], entry: dict[str, Any]) -> None:
    history = detail.setdefault("supervisor_history", [])
    if not isinstance(history, list):
        history = []
        detail["supervisor_history"] = history
    history.append(entry)


def infer_issue_summary(issue: dict[str, Any], detail: dict[str, Any]) -> str:
    if isinstance(issue.get("summary"), str) and issue["summary"].strip():
        return issue["summary"].strip()
    if isinstance(detail.get("summary"), str) and detail["summary"].strip():
        return detail["summary"].strip()
    details = detail.get("details", {}) if isinstance(detail, dict) else {}
    if isinstance(details, dict):
        if isinstance(details.get("validation_errors"), list) and details["validation_errors"]:
            return str(details["validation_errors"][0])
        for key in ("error", "message"):
            value = details.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(issue.get("title", "")).strip()


def infer_issue_criticality(issue: dict[str, Any], detail: dict[str, Any]) -> str:
    if isinstance(issue.get("criticality"), str) and issue["criticality"].strip():
        return issue["criticality"].strip()
    if isinstance(detail.get("criticality"), str) and detail["criticality"].strip():
        return detail["criticality"].strip()
    role = str(issue.get("role", "") or detail.get("role", ""))
    error_category = str(issue.get("error_category", "") or detail.get("error_category", ""))
    issue_type = str(issue.get("issue_type", "") or detail.get("issue_type", ""))
    if issue_type in ("agent_execution_failed",):
        return "high"
    if error_category in ("binary_not_found", "network_blocked", "invalid_decision", "invalid_output"):
        return "high"
    return "medium"


def print_issue_with_context(issue: dict[str, Any], detail: dict[str, Any]) -> None:
    summary = infer_issue_summary(issue, detail)
    criticality = infer_issue_criticality(issue, detail)
    print(f"{issue['issue_id']}\t{issue.get('status','')}\t{criticality}\t{issue.get('backend','')}\t{issue.get('title','')}")
    print(f"summary\t{summary}")
    print(f"detail\t{issue.get('detail_path','')}")


def cmd_list(args: argparse.Namespace) -> int:
    tracker = load_tracker()
    wanted = set(args.status or [])
    for issue in tracker["issues"]:
        status = str(issue.get("status", ""))
        if wanted and status not in wanted:
            continue
        print(f"{issue['issue_id']}\t{status}\t{issue.get('backend','')}\t{issue.get('title','')}")
    return 0


def cmd_open(_: argparse.Namespace) -> int:
    tracker = load_tracker()
    for issue in tracker["issues"]:
        status = str(issue.get("status", ""))
        if status != "open":
            continue
        detail = load_json(issue_detail_path(issue))
        print_issue_with_context(issue, detail)
    return 0


def cmd_analyse(args: argparse.Namespace) -> int:
    tracker = load_tracker()
    wanted = set(args.status or ["open", "regressed"])
    for issue in tracker["issues"]:
        status = str(issue.get("status", ""))
        if status not in wanted:
            continue
        detail_path = issue_detail_path(issue)
        detail = load_json(detail_path)
        print_issue_with_context(issue, detail)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    tracker = load_tracker()
    issue = find_issue(tracker, args.issue_id)
    detail_path = issue_detail_path(issue)
    detail = load_json(detail_path)

    verification_runs = run_verification_commands(args.verify_command)
    passed = summarize_results(verification_runs)
    prior_status = str(issue.get("status", "open"))
    if passed:
        target_status = "fixed"
    elif prior_status == "fixed":
        target_status = "regressed"
    else:
        # Keep the existing status on failure so in_progress/open aren't silently reset.
        target_status = prior_status if prior_status in VALID_ISSUE_STATUSES else "open"
    summary = args.summary.strip()
    if not summary:
        raise SystemExit("--summary is required.")

    issue["status"] = target_status
    issue["updated_at"] = now_iso()

    detail["status"] = target_status
    detail["updated_at"] = issue["updated_at"]
    detail["supervisor"] = {
        "name": args.supervisor,
        "updated_at": issue["updated_at"],
    }
    detail["resolution"] = {
        "summary": summary,
        "status": target_status,
        "verification_passed": passed,
        "verification_runs": verification_runs,
    }
    append_history(
        detail,
        {
            "timestamp": issue["updated_at"],
            "supervisor": args.supervisor,
            "summary": summary,
            "status": target_status,
            "verification_passed": passed,
        },
    )

    write_json(detail_path, detail)
    write_json(DEBUG_TRACKER_PATH, tracker)

    print(f"{issue['issue_id']}: {prior_status} -> {target_status}")
    for run in verification_runs:
        outcome = "PASS" if run["passed"] else "FAIL"
        print(f"[{outcome}] {run['command']}")

    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retest and update debug issues with enforced verification.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List tracked debug issues.")
    list_parser.add_argument(
        "--status",
        action="append",
        choices=["open", "in_progress", "fixed", "regressed"],
        help="Filter by issue status. Repeat for multiple statuses.",
    )
    list_parser.set_defaults(func=cmd_list)

    open_parser = subparsers.add_parser("open", help="List open debug issues.")
    open_parser.set_defaults(func=cmd_open)

    analyse_parser = subparsers.add_parser(
        "analyse",
        help="Summarize open or regressed issues without changing status.",
    )
    analyse_parser.add_argument(
        "--status",
        action="append",
        choices=["open", "in_progress", "fixed", "regressed"],
        help="Filter by issue status. Defaults to open and regressed.",
    )
    analyse_parser.set_defaults(func=cmd_analyse)

    verify_parser = subparsers.add_parser(
        "verify",
        help="Run verification commands and update status. fixed requires passing verification.",
    )
    verify_parser.add_argument("issue_id", help="Issue ID from debug/tracker.json")
    verify_parser.add_argument(
        "--verify-command",
        action="append",
        required=True,
        help="Command to run from the repository root. Repeat for multiple commands.",
    )
    verify_parser.add_argument(
        "--summary",
        required=True,
        help="Short summary of what changed and what this verification covers.",
    )
    verify_parser.add_argument(
        "--supervisor",
        default="codex",
        help="Supervisor/backend updating the issue record.",
    )
    verify_parser.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
