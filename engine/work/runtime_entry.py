"""Post-parse runtime assembly for the engine entrypoint."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

from engine.work.file_lock import LockUnavailable, locked


def execute_main_flow(
    args: Any,
    request: str,
    *,
    ensure_repo_structure: Callable[[], None],
    emit_progress: Callable[[str], None],
    detect_runtime_network_block: Callable[[str | None], str | None],
    is_backend_available: Callable[[str], bool],
    get_api_agent_bin: Callable[[], str | None],
    run_runtime_checks: Callable[[list[str]], list[dict[str, Any]]],
    record_debug_issue: Callable[..., dict[str, Any]],
    load_json: Callable[[Path], Any],
    registry_path: Path,
    detect_fork_intent: Callable[[str, list[dict[str, Any]]], dict[str, Any] | None],
    should_ignore_cached_project_for_new_request: Callable[[dict[str, Any] | None, str], bool],
    resolve_active_project: Callable[..., tuple[dict[str, Any] | None, str | None]],
    save_last_active_project: Callable[[dict[str, Any] | None], None],
    detect_secrets: Callable[[str], list[dict[str, Any]]],
    store_secrets: Callable[[str, list[dict[str, Any]], str], None],
    redact_secrets: Callable[[str, list[dict[str, Any]]], str],
    inputs_dir: Path,
    ingest_input_files: Callable[[str], list[str]],
    now_iso: Callable[[], str],
    write_json: Callable[[Path, Any], None],
    repo_root: Path,
    state_template_path: Path,
    environmental_block_phrases: set[str],
    run_orchestration: Callable[..., int],
) -> int:
    ensure_repo_structure()
    debug_mode = args.debug_mode
    if debug_mode:
        emit_progress(
            "[debug] Capture-only mode enabled. This run will only record orchestration faults. "
            "Do not perform repair from --debug-mode; use ./automator debug afterwards."
        )

    agent_bin = None
    if args.gemini:
        agent_bin = "gemini"
    elif args.claude:
        agent_bin = "claude"
    elif args.codex:
        agent_bin = "codex"

    if not agent_bin:
        agent_bin = os.environ.get("AGENT_BIN")

    # API mode override: if config says mode=api, ignore --LLM flags and use
    # the provider from config as the agent_bin equivalent.
    api_bin = get_api_agent_bin()
    if api_bin:
        if agent_bin and agent_bin != api_bin:
            emit_progress(f"[engine] API mode active — ignoring --{agent_bin} flag, using provider from config ({api_bin}).")
        agent_bin = api_bin

    if not agent_bin:
        for tool in ["gemini", "claude", "codex"]:
            if is_backend_available(tool):
                agent_bin = tool
                emit_progress(f"[engine] Auto-detected AI Parent binary: {agent_bin}")
                break

    if args.check_runtime:
        explicit_backend = bool(args.gemini or args.claude or args.codex)
        if explicit_backend:
            if not agent_bin:
                emit_progress("Error: No AI Parent binary identified. Pass --cli <llm> or set AGENT_BIN env var.")
                return 1
            backends = [agent_bin]
        else:
            backends = [tool for tool in ["gemini", "claude", "codex"] if is_backend_available(tool)]
            if not backends:
                emit_progress("Error: No supported AI backends found on PATH.")
                return 1
        network_block_message = detect_runtime_network_block(None)
        if network_block_message:
            emit_progress(network_block_message)
        results = run_runtime_checks(backends)
        for result in results:
            status = "ok" if result["ok"] else "failed"
            emit_progress(f"[runtime-check] {result['backend']}: {status} ({result['reason']})")
            if result["details"]:
                emit_progress(f"[runtime-check] {result['backend']} details: {result['details']}")
        return 0 if all(result["ok"] for result in results) else 1

    if not agent_bin:
        emit_progress("Error: No AI Parent binary identified. Pass --cli <llm> or set AGENT_BIN env var.")
        if debug_mode:
            record_debug_issue(
                issue_type="startup_configuration_error",
                title="No AI parent binary could be identified",
                backend="",
                request=request,
                error_category="missing_agent_bin",
                details={"args": vars(args)},
            )
        return 1

    network_block_message = detect_runtime_network_block(agent_bin)
    if network_block_message:
        emit_progress(network_block_message)
        if debug_mode:
            record_debug_issue(
                issue_type="startup_runtime_error",
                title="Outer runtime blocked AI backend network access",
                backend=agent_bin,
                request=request,
                error_category="network_blocked",
                details={"message": network_block_message},
            )
        return 1

    emit_progress(f"[engine] Running with AI Parent: {agent_bin}")

    registry = load_json(registry_path)
    projects = registry.get("projects", [])
    cached_project = registry.get("last_active_project")
    cached_task_state = {}
    cached_pending = None
    if cached_project and Path(cached_project.get("project_root", "")).exists():
        cached_runtime_dir = Path(cached_project.get("runtime_dir", ""))
        cached_task_state_path = cached_runtime_dir / "state" / "active_task.json"
        cached_task_state = load_json(cached_task_state_path)
        cached_pending = cached_task_state.get("pending_resolution")

    # Startup banner: if the last-active project has a pending response, remind
    # the user. Scanning all projects is too noisy — use --project list for that.
    if cached_pending and cached_project:
        _pid = cached_project.get("project_id", "")
        if _pid:
            emit_progress(
                f"[engine] Project '{_pid}' is awaiting your response. "
                f"Run: ./automator --project close --id {_pid}   (accept) or --project continue --id {_pid} --task <feedback>"
            )

    fork_hint = detect_fork_intent(request, projects)
    if fork_hint:
        emit_progress(
            f"[engine] Fork intent detected. Source project: "
            f"{fork_hint['source_project_name']} ({fork_hint['source_project_id']}). "
            f"Not setting as active — engine will create a new project."
        )
        active_project: dict[str, Any] | None = None
        err = None
    elif re.search(r"\bfork\b", request, re.IGNORECASE):
        emit_progress("[engine] Fork intent detected but no source project matched in request.")
        active_project, err = None, None
    else:
        allow_registry_fallback = True
        if should_ignore_cached_project_for_new_request(cached_pending, request):
            allow_registry_fallback = False
            emit_progress(
                f"[engine] Project '{cached_project.get('project_name', '')}' has pending acceptance — "
                "it will not be auto-attached to this request. "
                "To accept: --project close --id <id>. "
                "To give feedback: --project continue --id <id> --task <feedback>."
            )
        active_project, err = resolve_active_project(
            request,
            projects,
            allow_registry_fallback=allow_registry_fallback,
        )
        if err:
            emit_progress(f"Error: {err}")
            if debug_mode:
                record_debug_issue(
                    issue_type="project_resolution_error",
                    title="Failed to resolve active project",
                    backend=agent_bin,
                    request=request,
                    error_category="project_resolution_failed",
                    details={"message": err},
                )
            return 1

    save_last_active_project(active_project)

    pending_secrets: list[dict[str, Any]] = []
    detected = detect_secrets(request)
    if detected:
        if active_project:
            store_secrets(active_project["project_id"], detected, source="user_prompt")
            emit_progress(f"[engine] Detected {len(detected)} secret(s) in prompt. Stored in secrets vault.")
        else:
            pending_secrets = detected
            emit_progress(f"[engine] Detected {len(detected)} secret(s) in prompt (pre-project). Will store after bootstrap.")
        request = redact_secrets(request, detected)

    pending_input_files = False
    if inputs_dir.exists() and any(inputs_dir.iterdir()):
        inbox_count = sum(1 for file in inputs_dir.iterdir() if file.is_file())
        if inbox_count:
            if active_project:
                ingested = ingest_input_files(active_project["project_id"])
                emit_progress(f"[engine] Ingested {len(ingested)} input file(s) from inbox.")
            else:
                pending_input_files = True
                emit_progress(
                    f"[engine] Found {inbox_count} file(s) in inbox (pre-project). Will ingest after project resolution."
                )

    if active_project:
        emit_progress(f"[engine] Active project: {active_project['project_name']} (ID: {active_project['project_id']})")

        runtime_dir = Path(active_project["runtime_dir"])
        task_state_path = runtime_dir / "state" / "active_task.json"
        task_state = load_json(task_state_path)

        task_state["user_request"] = request
        task_state["last_updated"] = now_iso()

        prior_steps = task_state.get("completed_steps", [])
        cleaned_steps = []
        pruned_env = task_state.get("pruned_environmental_steps", [])
        for step in prior_steps:
            if step.get("status") not in ("blocked", "failed"):
                cleaned_steps.append(step)
                continue
            if step.get("error_category") == "environmental":
                pruned_env.append(step)
                continue
            summary_lower = step.get("summary", "").lower()
            if any(phrase in summary_lower for phrase in environmental_block_phrases):
                pruned_env.append(step)
                continue
            cleaned_steps.append(step)
        task_state["completed_steps"] = cleaned_steps
        task_state["pruned_environmental_steps"] = pruned_env

        write_json(task_state_path, task_state)
    else:
        emit_progress("[engine] No active project resolved. Engine will bootstrap project on first run.")
        task_state_path = state_template_path
        task_state = load_json(task_state_path)

    # Serialize concurrent runs on the same project. Only hold the lock once
    # a real project is resolved — the template path is shared and read-only.
    should_lock = bool(active_project) and task_state_path != state_template_path
    try:
        if should_lock:
            with locked(task_state_path, non_blocking=True):
                return run_orchestration(
                    request=request,
                    agent_bin=agent_bin,
                    debug_mode=debug_mode,
                    execute_agents=args.execute_agents,
                    active_project=active_project,
                    task_state=task_state,
                    task_state_path=task_state_path,
                    fork_hint=fork_hint,
                    pending_secrets=pending_secrets,
                    pending_input_files=pending_input_files,
                )
        return run_orchestration(
            request=request,
            agent_bin=agent_bin,
            debug_mode=debug_mode,
            execute_agents=args.execute_agents,
            active_project=active_project,
            task_state=task_state,
            task_state_path=task_state_path,
            fork_hint=fork_hint,
            pending_secrets=pending_secrets,
            pending_input_files=pending_input_files,
        )
    except LockUnavailable as exc:
        emit_progress(f"[engine] {exc}")
        return 1
