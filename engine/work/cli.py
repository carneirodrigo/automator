"""Unified front-door CLI for project runs, debug supervision, skills, agents, and config."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from engine.work import agent_admin
from engine.work import config_wizard
from engine.work import debug_supervisor
from engine.work import engine_runtime
from engine.work import skill_sync
from engine.work.json_io import load_json as _load_json, write_json as _write_json
from engine.work.project_state import reconcile_registry as _reconcile_registry
from engine.work.repo_paths import PROJECTS_DIR, REGISTRY_PATH


def _ensure_registry_reconciled() -> None:
    """Reconcile registry.json against on-disk folders before local-only reads.

    Engine-backed commands reconcile via ensure_repo_structure(); this covers
    the local-only paths (--project list, --project continue/fork id validation,
    --project delete) so users never see a stale registry.
    """
    try:
        _reconcile_registry(
            projects_dir=PROJECTS_DIR,
            registry_path=REGISTRY_PATH,
            load_json=_load_json,
            write_json=_write_json,
        )
    except Exception:  # noqa: BLE001 — best-effort
        pass

_VALID_DEBUG_ACTIONS = {"list", "open", "analyse", "verify"}


# ---------------------------------------------------------------------------
# Request composition
# ---------------------------------------------------------------------------

def _compose_project_request(action: str, task: str, project_id: str | None) -> str:
    parts: list[str] = []
    if action == "new":
        parts.append("start new project.")
        if task:
            parts.append(f"Task: {task}")
    elif action == "continue":
        if project_id:
            parts.append(project_id)
        if task:
            parts.append(task)
    elif action == "fork":
        parts.append(f"fork {project_id} into a new project.")
        if task:
            parts.append(f"Task: {task}")
    return " ".join(p for p in parts if p).strip()


def _resolve_backend_flags(args: argparse.Namespace) -> list[str]:
    cli = getattr(args, "cli", None)
    if cli:
        return [f"--{cli}"]
    return []


def _run_engine(request: str, args: argparse.Namespace, *, force_debug: bool = False) -> int:
    forwarded = _resolve_backend_flags(args)
    if getattr(args, "check_runtime", False):
        forwarded.append("--check-runtime")
    if force_debug:
        forwarded.append("--debug-mode")
    if request:
        forwarded.append(request)
    return int(engine_runtime.main(forwarded))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _first_id(args: argparse.Namespace) -> str | None:
    ids = getattr(args, "id", None)
    return ids[0] if isinstance(ids, list) and ids else None


def _require(value: Any, command: str, flag_desc: str) -> None:
    """Raise SystemExit with ``<command> requires <flag_desc>`` when value is falsy.

    Consolidates the repeated ``if not x: raise SystemExit(f"{cmd} requires {flag}")``
    pattern across subcommand handlers so the phrasing stays consistent.
    """
    if not value:
        raise SystemExit(f"{command} requires {flag_desc}")


def _cmd_project(args: argparse.Namespace) -> int:
    action = args.project
    task = " ".join(args.task or []).strip()
    project_id = _first_id(args)
    capture_mode = getattr(args, "debug", None) is not None

    if action == "close":
        _require(project_id, "--project close", "--id <project-id>")
        agent_bin = getattr(args, "cli", None) or ("api" if getattr(args, "api", False) else None)
        return engine_runtime.close_project(project_id, agent_bin)

    if action == "delete":
        id_all = getattr(args, "id_all", False)
        ids = getattr(args, "id", None) or []
        _require(id_all or ids, "--project delete", "--id <project-id> (repeat for multiple) or --all")
        return engine_runtime.delete_projects(ids, delete_all=id_all)

    if action in ("continue", "fork"):
        _require(project_id, f"--project {action}", "--id <project-id>")

    if action in ("continue", "fork") and project_id:
        # Validate that the project exists before proceeding
        _ensure_registry_reconciled()
        try:
            registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8")) if REGISTRY_PATH.exists() else {}
        except (json.JSONDecodeError, OSError):
            registry = {}
        known_ids = {p.get("project_id", "") for p in registry.get("projects", [])}
        if project_id not in known_ids:
            raise SystemExit(
                f"Project '{project_id}' not found. "
                f"Run: ./automator --project list   to see available projects."
            )

    if action in ("new", "fork"):
        _require(task, f"--project {action}", "--task <description>")

    request = _compose_project_request(action, task, project_id)
    return _run_engine(request, args, force_debug=capture_mode)


def _cmd_project_list() -> int:
    _ensure_registry_reconciled()
    if not REGISTRY_PATH.exists():
        print("No projects found.")
        return 0
    try:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("Error: project registry is unreadable or corrupt.")
        return 1
    projects = registry.get("projects", [])
    if not projects:
        print("No projects found.")
        return 0

    # Load pending state for each project
    pending_ids: set[str] = set()
    for p in projects:
        try:
            state_path = Path(p["runtime_dir"]) / "state" / "active_task.json"
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if state.get("pending_resolution"):
                    pending_ids.add(p.get("project_id", ""))
        except (OSError, KeyError, json.JSONDecodeError):
            pass  # best-effort: skip projects with unreadable state

    col_id = max(len(p.get("project_id", "")) for p in projects)
    col_name = max(len(p.get("project_name", "")) for p in projects)
    col_id = max(col_id, 10)
    col_name = max(col_name, 12)
    header = f"{'PROJECT ID':<{col_id}}  {'PROJECT NAME':<{col_name}}  DESCRIPTION"
    print(header)
    print("-" * len(header))
    for p in projects:
        pid = p.get("project_id", "")
        name = p.get("project_name", "")
        desc = p.get("description", "")
        pending_flag = "  [PENDING]" if pid in pending_ids else ""
        print(f"{pid:<{col_id}}  {name:<{col_name}}  {desc}{pending_flag}")

    if pending_ids:
        print(f"\n{len(pending_ids)} project(s) awaiting your response — use --project close --id <id> to accept, or --project continue --id <id> --task <feedback> to rework")
    return 0


def _cmd_debug(args: argparse.Namespace) -> int:
    action = args.debug
    if action not in _VALID_DEBUG_ACTIONS:
        raise SystemExit(f"--debug requires an action: {', '.join(sorted(_VALID_DEBUG_ACTIONS))}")

    if action == "verify":
        issue_id = _first_id(args)
        _require(issue_id, "--debug verify", "--id <issue-id>")
        verify_commands = getattr(args, "verify_command", None) or []
        _require(verify_commands, "--debug verify", "--verify-command <cmd>")
        summary = getattr(args, "summary", None)
        _require(summary, "--debug verify", "--summary <text>")
        forwarded = ["verify", issue_id]
        for cmd in verify_commands:
            forwarded.extend(["--verify-command", cmd])
        forwarded.extend(["--summary", summary, "--supervisor", args.supervisor])
        return int(debug_supervisor.main(forwarded))

    if action in ("list", "analyse"):
        forwarded = [action]
        for status in getattr(args, "status", None) or []:
            forwarded.extend(["--status", status])
        return int(debug_supervisor.main(forwarded))

    if action == "open":
        return int(debug_supervisor.main(["open"]))

    raise SystemExit(f"Unknown --debug action: {action}")


def _cmd_config(args: argparse.Namespace) -> int:
    action = args.config
    if action == "setup":
        return int(config_wizard.cmd_setup())
    if action == "show":
        return int(config_wizard.cmd_show())
    if action == "validate":
        return int(config_wizard.cmd_validate())
    raise SystemExit(f"Unknown --config action: {action}")


def _cmd_skill(args: argparse.Namespace) -> int:
    action = args.skill
    if action == "list":
        return int(skill_sync.main(["--list"]))
    if action == "check":
        return int(skill_sync.main(["--check"]))
    if action == "catalog":
        forwarded: list[str] = ["--catalog"]
        if getattr(args, "repo", None):
            forwarded.extend(["--repo", args.repo])
        if getattr(args, "dry_run", False):
            forwarded.append("--dry-run")
        return int(skill_sync.main(forwarded))
    if action == "fetch":
        skill_id = _first_id(args)
        _require(skill_id, "--skill fetch", "--id <skill-id>")
        return int(skill_sync.main(["--skill", skill_id]))
    if action == "rebuild-manifest":
        return int(skill_sync.main(["--rebuild-manifest"]))
    raise SystemExit(f"Unknown --skill action: {action}")


def _cmd_knowledge(args: argparse.Namespace) -> int:
    action = args.knowledge
    if action == "purge":
        project_id = _first_id(args)
        _require(project_id, "--knowledge purge", "--id <project-id>")
        return engine_runtime.purge_project_knowledge(project_id)
    raise SystemExit(f"Unknown --knowledge action: {action}")


def _cmd_agent(args: argparse.Namespace) -> int:
    action = args.agent
    if action == "list":
        return int(agent_admin.main(["list"]))
    if action == "add":
        role = _first_id(args)
        purpose = getattr(args, "purpose", None)
        _require(role, "--agent add", "--id <role-slug>")
        _require(purpose, "--agent add", "--purpose <text>")
        forwarded: list[str] = ["add", role, "--purpose", purpose]
        if getattr(args, "title", None):
            forwarded.extend(["--title", args.title])
        if getattr(args, "force", False):
            forwarded.append("--force")
        return int(agent_admin.main(forwarded))
    raise SystemExit(f"Unknown --agent action: {action}")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_EPILOG = """\
────────────────────────────────────────────────────────────────
 PROJECT — start, continue, or fork  (requires --api or --cli)
────────────────────────────────────────────────────────────────
  Start a new project:
    ./automator --api --project new --task build a script that fetches GitHub issues
    ./automator --cli claude --project new --task build a Power Automate approval flow
    ./automator --cli gemini --project new --task write an employee onboarding runbook
    ./automator --cli codex  --project new --task refactor the auth module

  Continue an existing project  (--id = exact folder name in projects/):
    ./automator --api         --project continue --id github-issues --task add retry logic on failure
    ./automator --cli claude  --project continue --id github-issues --task add unit tests

  Fork a project into a new one:
    ./automator --cli claude  --project fork --id github-issues --task store results in SharePoint
    ./automator --api         --project fork --id github-issues --task write a guide documenting how this works

  Run in debug / capture mode  (add --debug to any project action):
    ./automator --cli claude  --project new      --debug --task build a GitHub issues fetcher
    ./automator --cli claude  --project continue --debug --id github-issues --task investigate auth failure

  Close a project and extract knowledge  (backend optional — needed for extraction):
    ./automator --cli claude  --project close --id github-issues   # closes + extracts knowledge
    ./automator --api         --project close --id github-issues   # closes + extracts knowledge
    ./automator               --project close --id github-issues   # closes only, skips extraction

  Delete projects  (removes folder + registry entry, local, no backend needed):
    ./automator --project delete --id github-issues
    ./automator --project delete --id proj-a --id proj-b --id proj-c
    ./automator --project delete --all

  List all projects  (local, no backend needed):
    ./automator --project list

────────────────────────────────────────────────────────────────
 HEALTH CHECK  (requires --api or --cli)
────────────────────────────────────────────────────────────────
    ./automator --cli claude  --check-runtime
    ./automator --cli gemini  --check-runtime
    ./automator --api         --check-runtime

────────────────────────────────────────────────────────────────
 DEBUG ISSUE MANAGEMENT  (local, no backend needed)
────────────────────────────────────────────────────────────────
    ./automator --debug                          # open is the default
    ./automator --debug open
    ./automator --debug list
    ./automator --debug list   --status open --status in_progress
    ./automator --debug analyse
    ./automator --debug analyse --status regressed
    ./automator --debug verify --id dbg-001 --verify-command "pytest -v" --summary "all tests pass"
    ./automator --debug verify --id dbg-001 --verify-command "cmd1" --verify-command "cmd2" \\
                               --summary "verified" --supervisor claude

────────────────────────────────────────────────────────────────
 CONFIGURATION  (local, no backend needed)
────────────────────────────────────────────────────────────────
    ./automator --config setup       # interactive wizard — run this first
    ./automator --config show        # display current config (keys redacted)
    ./automator --config validate    # check API keys are reachable

────────────────────────────────────────────────────────────────
 SKILLS  (local, no backend needed)
────────────────────────────────────────────────────────────────
    ./automator --skill list
    ./automator --skill check                         # check cached skills for staleness
    ./automator --skill catalog                       # refresh full catalog
    ./automator --skill catalog --repo openai         # refresh one repo only
    ./automator --skill catalog --dry-run             # preview without writing
    ./automator --skill fetch   --id openai--playwright
    ./automator --skill rebuild-manifest

────────────────────────────────────────────────────────────────
 KNOWLEDGE  (local, no backend needed)
────────────────────────────────────────────────────────────────
    ./automator --knowledge purge --id github-issues

────────────────────────────────────────────────────────────────
 AGENTS  (local, no backend needed)
────────────────────────────────────────────────────────────────
    ./automator --agent list
    ./automator --agent add --id my-role --purpose "Short purpose statement."
    ./automator --agent add --id my-role --purpose "..." --title "My Role"
    ./automator --agent add --id my-role --purpose "..." --force   # overwrite existing

────────────────────────────────────────────────────────────────
 NOTES
────────────────────────────────────────────────────────────────
  --task  Multi-word values need no quotes unless the description
          contains shell special characters (& | > $).
          Put --task last by convention, but any order is accepted.

  --id    Always an exact match — no fuzzy resolution.
          For projects: matches the folder name under projects/ and
          the project_id in the registry.
"""


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Automator — multi-agent orchestration engine.\n"
            "All actions are flags. Use --api or --cli <llm> whenever a language model is needed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )

    # ── Backend ──────────────────────────────────────────────────────────────
    backend_group = parser.add_argument_group(
        "backend  (required for --project, --check-runtime, and --project --debug)"
    )
    backend = backend_group.add_mutually_exclusive_group()
    backend.add_argument(
        "--api",
        action="store_true",
        help="Use API backend configured in config/backends.json",
    )
    backend.add_argument(
        "--cli",
        metavar="LLM",
        choices=["claude", "gemini", "codex"],
        help="Use CLI backend — one of: claude, gemini, codex",
    )

    # ── Project ───────────────────────────────────────────────────────────────
    project_group = parser.add_argument_group(
        "project  (new|continue|fork requires --api or --cli; close benefits from it; delete and list are local)"
    )
    project_group.add_argument(
        "--project",
        metavar="ACTION",
        help="new · continue · fork · close · delete · list",
    )
    project_group.add_argument(
        "--task",
        nargs="+",
        metavar="WORD",
        help="Task description — multi-word, no quotes needed",
    )
    project_group.add_argument(
        "--debug",
        nargs="?",
        const="open",
        metavar="ACTION",
        help=(
            "With --project: enables capture/debug mode for that run.\n"
            "Alone: manage debug issues — open (default), list, analyse, verify"
        ),
    )

    # ── Health check ─────────────────────────────────────────────────────────
    health_group = parser.add_argument_group("health check  (requires --api or --cli)")
    health_group.add_argument(
        "--check-runtime",
        action="store_true",
        dest="check_runtime",
        help="Probe that the configured backend is reachable",
    )

    # ── Debug issue management ────────────────────────────────────────────────
    debug_group = parser.add_argument_group(
        "debug issue management  (local, no backend needed)"
    )
    debug_group.add_argument(
        "--status",
        action="append",
        choices=["open", "in_progress", "fixed", "regressed"],
        metavar="STATUS",
        help="Filter for --debug list/analyse — repeat for multiple values",
    )
    debug_group.add_argument(
        "--verify-command",
        action="append",
        metavar="CMD",
        help="Verification command for --debug verify — repeat for multiple",
    )
    debug_group.add_argument(
        "--summary",
        metavar="TEXT",
        help="Verification summary for --debug verify",
    )
    debug_group.add_argument(
        "--supervisor",
        default="codex",
        metavar="LLM",
        help="Backend for --debug verify (default: codex)",
    )

    # ── Shared identifier ─────────────────────────────────────────────────────
    id_group = parser.add_argument_group(
        "shared identifier  (exact match, no fuzzy resolution)"
    )
    id_group.add_argument(
        "--id",
        action="append",
        metavar="ID",
        help=(
            "Project ID for --project continue/fork/close/delete — repeat for bulk delete.  ·  "
            "Issue ID for --debug verify  ·  "
            "Skill ID for --skill fetch  ·  "
            "Role slug for --agent add"
        ),
    )
    id_group.add_argument(
        "--all",
        action="store_true",
        dest="id_all",
        help="Delete all projects — use with --project delete",
    )

    # ── Configuration ─────────────────────────────────────────────────────────
    config_group = parser.add_argument_group("configuration  (local, no backend needed)")
    config_group.add_argument(
        "--config",
        metavar="ACTION",
        help="setup · show · validate",
    )

    # ── Skills ────────────────────────────────────────────────────────────────
    skill_group = parser.add_argument_group("skills  (local, no backend needed)")
    skill_group.add_argument(
        "--skill",
        metavar="ACTION",
        help="list · check · catalog · fetch · rebuild-manifest",
    )
    skill_group.add_argument(
        "--repo",
        metavar="REPO_ID",
        help="Limit --skill catalog to one repo",
    )
    skill_group.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Preview --skill catalog changes without writing",
    )

    # ── Knowledge ─────────────────────────────────────────────────────────────
    knowledge_group = parser.add_argument_group("knowledge  (local, no backend needed)")
    knowledge_group.add_argument(
        "--knowledge",
        metavar="ACTION",
        help="purge",
    )

    # ── Agents ────────────────────────────────────────────────────────────────
    agent_group = parser.add_argument_group("agents  (local, no backend needed)")
    agent_group.add_argument(
        "--agent",
        metavar="ACTION",
        help="list · add",
    )
    agent_group.add_argument(
        "--purpose",
        metavar="TEXT",
        help="Purpose statement — required for --agent add",
    )
    agent_group.add_argument(
        "--title",
        metavar="TEXT",
        help="Human-readable title — optional for --agent add",
    )
    agent_group.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing agent spec — for --agent add",
    )

    return parser


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        build_cli_parser().print_help()
        return 1
    if argv in (["-h"], ["--help"]):
        build_cli_parser().print_help()
        return 0

    parser = build_cli_parser()
    args = parser.parse_args(argv)

    has_backend = args.api or bool(getattr(args, "cli", None))

    # --check-runtime
    if args.check_runtime:
        if not has_backend:
            parser.error("--check-runtime requires --api or --cli <llm>")
        return _run_engine("", args)

    # --project
    if args.project is not None:
        action = args.project
        if action == "list":
            return _cmd_project_list()
        if action in ("close", "delete"):
            return _cmd_project(args)
        if action not in ("new", "continue", "fork"):
            parser.error(f"--project accepts: new, continue, fork, close, delete, list — got '{action}'")
        if not has_backend:
            parser.error(f"--project {action} requires --api or --cli <llm>")
        return _cmd_project(args)

    # --debug (standalone management — project capture mode is handled inside _cmd_project)
    if args.debug is not None:
        return _cmd_debug(args)

    # --config
    if args.config is not None:
        action = args.config
        if action not in ("setup", "show", "validate"):
            parser.error(f"--config accepts: setup, show, validate — got '{action}'")
        return _cmd_config(args)

    # --skill
    if args.skill is not None:
        action = args.skill
        if action not in ("list", "check", "catalog", "fetch", "rebuild-manifest"):
            parser.error(f"--skill accepts: list, check, catalog, fetch, rebuild-manifest — got '{action}'")
        return _cmd_skill(args)

    # --knowledge
    if args.knowledge is not None:
        action = args.knowledge
        if action not in ("purge",):
            parser.error(f"--knowledge accepts: purge — got '{action}'")
        return _cmd_knowledge(args)

    # --agent
    if args.agent is not None:
        action = args.agent
        if action not in ("list", "add"):
            parser.error(f"--agent accepts: list, add — got '{action}'")
        return _cmd_agent(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
