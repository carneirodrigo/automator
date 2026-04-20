"""Lean orchestration runner — worker → review lifecycle."""

from __future__ import annotations

import re
import time as _time_module
from pathlib import Path
from typing import Any

from engine.work.file_lock import LockUnavailable, locked
from engine.work.task_state import TaskState

_ENV: dict[str, Any] = {}

# Normalized review status values. LLMs may return "failed", "FAIL", "reject",
# etc. — anything not explicitly "pass" is treated as failure to prevent
# broken work from being presented as complete.
_REVIEW_PASS_VALUES = frozenset({"pass", "passed", "approve", "approved", "lgtm", "ok", "success"})

# Error categories that are transient and worth retrying.
_RETRIABLE_ERRORS = frozenset({"rate_limited", "timeout", "provider_error"})
_MAX_STAGE_RETRIES = 2
_RETRY_BACKOFF_BASE = 5  # seconds; doubles each retry


def _normalize_review_status(raw: Any) -> str:
    """Map an LLM review status to 'pass' or 'fail'."""
    if not isinstance(raw, str) or not raw.strip():
        return "fail"
    return "pass" if raw.strip().lower() in _REVIEW_PASS_VALUES else "fail"


def _run_with_retry(
    run_fn: Any,
    role: str,
    emit_progress: Any,
) -> dict[str, Any]:
    """Run an agent stage with retry on transient errors.

    Retries up to _MAX_STAGE_RETRIES times with exponential backoff for
    rate_limited, timeout, and provider_error categories.
    """
    last_result: dict[str, Any] = {}
    for attempt in range(_MAX_STAGE_RETRIES + 1):
        last_result = run_fn() or {}
        if last_result.get("status") != "failed":
            return last_result
        category = last_result.get("error_category", "unknown")
        if category not in _RETRIABLE_ERRORS or attempt >= _MAX_STAGE_RETRIES:
            return last_result
        delay = _RETRY_BACKOFF_BASE * (2 ** attempt)
        emit_progress(
            f"[engine] {role} failed with {category} (attempt {attempt + 1}/{_MAX_STAGE_RETRIES + 1}). "
            f"Retrying in {delay}s..."
        )
        _time_module.sleep(delay)
    return last_result


def _validate_agent_output(output: Any, role: str) -> str | None:
    """Check that agent output has minimum required fields.

    Returns an error message if validation fails, None if output is acceptable.
    """
    if not output or not isinstance(output, dict):
        return f"{role} returned empty output"
    if role in ("worker",) and not output.get("summary"):
        return f"{role} output is missing 'summary' field"
    # Workers sometimes self-report failure without also setting the 'blocked'
    # branch — treat that as a hard failure rather than silently advancing to
    # review, which would ship broken work.
    if role == "worker" and output.get("status") == "failed":
        return f"worker reported status=failed: {output.get('summary', '(no summary)')}"
    if role == "review" and not output.get("status"):
        return f"review output is missing 'status' field — treating as fail"
    return None


# ---------------------------------------------------------------------------
# Blocker classification — hard vs researchable
# ---------------------------------------------------------------------------

# Patterns that indicate a blocker requires human intervention, not research.
_HARD_BLOCKER_PATTERNS = re.compile(
    r"\b("
    r"credentials?|password|secrets?|api\s*key|tokens?|client\s*id|client\s*secret"
    r"|tenant\s*id|subscription\s*id|access\s*denied|permissions?|forbidden"
    r"|401|403|unauthori[sz]ed|authorize|authorization|consent|mfa|multi.factor"
    r"|user\s+decision|user\s+input|user\s+confirmation|manual\s+step"
    r"|vpn|firewall|network\s+access|whitelisted|allowlisted"
    r")\b",
    re.IGNORECASE,
)

MAX_RESEARCH_CYCLES = 2


def _classify_blockers(open_issues: list[str]) -> tuple[list[str], list[str]]:
    """Split blockers into hard (need human) and researchable (need research agent).

    Returns (hard_blockers, researchable_blockers).
    """
    hard: list[str] = []
    researchable: list[str] = []
    for issue in open_issues:
        if not isinstance(issue, str) or not issue.strip():
            continue
        if _HARD_BLOCKER_PATTERNS.search(issue):
            hard.append(issue)
        else:
            researchable.append(issue)
    return hard, researchable


# ---------------------------------------------------------------------------
# Lightweight task planning — heuristic gate + minimal LLM call
# ---------------------------------------------------------------------------

# Signals that suggest a task needs planning before implementation.
_COMPLEXITY_SIGNALS = re.compile(
    r"\b("
    r"and\s+then|step\s+\d|first.*then|after\s+that|finally"
    r"|authenticate|credentials|secret|token|api\s+key"
    r"|sharepoint|graph\s+api|power\s+bi|azure|qualys|defender"
    r"|multiple|several|each|every|all\s+the"
    r"|deploy|migrate|integrate|connect\s+to"
    r")\w*\b",
    re.IGNORECASE,
)

# Short, direct requests that don't need planning.
_SIMPLICITY_SIGNALS = re.compile(
    r"^(write|create|build|make|generate|add)\s+a?\s*(simple\s+)?(script|file|function|class|test|hello)",
    re.IGNORECASE,
)

_PLANNING_PROMPT_TEMPLATE = """\
You are a task planner for an autonomous coding engine. Analyse the request and return a JSON object.

Context:
- The worker can write Python, call REST APIs, create documents, and run shell commands.
- Credentials are provided by the user as files in the inputs/ directory (e.g. inputs/creds.json, inputs/secrets.txt). The engine auto-detects and vaults them.
- The worker has access to a local knowledge base with API patterns for Microsoft Graph, Azure, SharePoint, Power BI, Qualys, and Defender.
- The worker cannot ask follow-up questions once started — anything unclear must be resolved now.

Rules:
- Return a plan with 3-7 ordered steps. Each step is one concrete action.
- Return questions when critical prerequisites are missing. You MUST ask if:
  * The task references an external API or service but no credentials or auth method is mentioned.
  * The task mentions a specific resource (tenant, site, repo, workspace) but doesn't identify which one.
  * The task is ambiguous about output format or destination (e.g. "store the results" — where?).
- Frame credential questions as: "Please provide <what> in the inputs/ directory (e.g. inputs/<filename>)."
- Do NOT ask about things the worker can discover by reading code, APIs, or the KB.
- Do NOT ask more than 3 questions. Focus on what blocks implementation.
- If no questions are needed, return an empty questions list.

Request: {request}

Return exactly this JSON (no markdown fences, no prose):
{{"plan": ["step 1", "step 2", ...], "questions": ["question for the user, if any"], "reasoning": "one line on why you chose plan-only or plan+questions"}}
"""


def _needs_planning(request: str) -> bool:
    """Heuristic: does this request warrant a planning step?

    Returns False for short, simple, or rework requests — these go straight
    to the worker.  Returns True when multiple complexity signals are present.
    """
    if not request:
        return False
    # Never plan on rework, continue, or acceptance flows.
    if any(kw in request.lower() for kw in ("rework required", "rework based on", "continue:")):
        return False
    # Very short requests are almost always simple.
    words = request.split()
    if len(words) < 10:
        return False
    # Count complexity signals — need at least 2 distinct matches.
    matches = set(m.group().lower() for m in _COMPLEXITY_SIGNALS.finditer(request))
    # For moderately short requests, simplicity signals override low complexity.
    if _SIMPLICITY_SIGNALS.search(request) and len(matches) < 3:
        return False
    return len(matches) >= 2


def _capability_rounds_for_task(request: str, has_plan: bool) -> int:
    """Return the capability round budget based on task complexity.

    Simple tasks: 5 rounds (current baseline).
    Medium tasks: 8 rounds (multiple complexity signals but no planning).
    Complex/planned tasks: 12 rounds (planning triggered or prior plan exists).
    """
    from engine.work.orchestration_state import (
        MAX_CAPABILITY_ROUNDS,
        MAX_CAPABILITY_ROUNDS_COMPLEX,
        MAX_CAPABILITY_ROUNDS_MEDIUM,
    )
    if has_plan:
        return MAX_CAPABILITY_ROUNDS_COMPLEX
    if not request:
        return MAX_CAPABILITY_ROUNDS
    matches = set(m.group().lower() for m in _COMPLEXITY_SIGNALS.finditer(request))
    if len(matches) >= 2:
        return MAX_CAPABILITY_ROUNDS_MEDIUM
    return MAX_CAPABILITY_ROUNDS


def _verify_delivery_files(output: dict[str, Any], project_root: str) -> list[str]:
    """Check that files the worker claims to have created actually exist.

    Returns a list of problem descriptions (empty if all OK). A path that
    resolves to a directory, a zero-byte file, or is missing entirely is
    reported — "wrote the file" is not the same as "wrote useful content."
    Workers run from the repo root, so relative paths are tried from cwd
    first, then from project_root as a fallback.
    """
    missing: list[str] = []
    root = Path(project_root) if project_root else None

    def _resolve(raw_path: str) -> Path | None:
        p = Path(raw_path)
        if p.is_absolute():
            return p if p.exists() else None
        if p.exists():
            return p
        if root and (root / raw_path).exists():
            return root / raw_path
        return None

    def _check(raw_path: str, label: str) -> None:
        resolved = _resolve(raw_path)
        if resolved is None:
            missing.append(f"{label} not found: {raw_path}")
            return
        if not resolved.is_file():
            missing.append(f"{label} is not a regular file: {raw_path}")
            return
        try:
            if resolved.stat().st_size == 0:
                missing.append(f"{label} is empty (0 bytes): {raw_path}")
        except OSError as exc:
            missing.append(f"{label} stat failed: {raw_path} ({exc})")

    for entry in output.get("artifacts") or []:
        if not isinstance(entry, str) or not entry.strip():
            continue
        _check(entry, "artifact")

    for entry in output.get("changes_made") or []:
        if not isinstance(entry, str):
            continue
        # Format is "path/to/file: what changed" — extract the path part.
        path_part = entry.split(":")[0].strip()
        if not path_part or path_part.startswith("("):
            continue
        _check(path_part, "changed file")

    return missing


def configure_orchestrator_environment(**kwargs: Any) -> None:
    _ENV.update(kwargs)


def _require(name: str) -> Any:
    value = _ENV.get(name)
    if value is None:
        raise RuntimeError(f"Orchestrator environment missing: {name}")
    return value


def _next_project_id(registry: dict[str, Any]) -> str:
    projects = (registry or {}).get("projects") or []
    nums = [
        int(p["project_id"])
        for p in projects
        if isinstance(p, dict) and p.get("project_id", "").isdigit()
    ]
    return str(max(nums) + 1 if nums else 1).zfill(3)


def _project_name_from_request(request: str) -> str:
    if not request:
        return "Untitled Project"
    # Strip engine-injected framing so the name reflects the user's actual task.
    cleaned = re.sub(
        r"^(?:start\s+new\s+project\.?\s*(?:task:\s*)?|fork\s+\S+\s+into\s+a\s+new\s+project\.?\s*(?:task:\s*)?)",
        "",
        request,
        flags=re.IGNORECASE,
    ).strip()
    words = re.sub(r"[^\w\s]", "", cleaned or request).split()
    return " ".join(w.capitalize() for w in words[:6]) or "Untitled Project"


def _record_step(
    task_state: TaskState,
    role: str,
    status: str,
    artifact_path: str,
    summary: str,
    now_iso: Any,
) -> None:
    if not task_state.get("completed_steps"):
        task_state["completed_steps"] = []
    task_state["completed_steps"].append({
        "agent": role,
        "timestamp": now_iso(),
        "status": status,
        "summary": summary,
        "artifact": artifact_path,
    })
    if artifact_path:
        if not task_state.get("artifacts"):
            task_state["artifacts"] = []
        task_state["artifacts"].append(artifact_path)


def run_orchestration(
    *,
    request: str,
    agent_bin: str,
    debug_mode: bool,
    execute_agents: bool,
    active_project: dict[str, Any] | None,
    task_state: TaskState,
    task_state_path: Path,
    fork_hint: dict[str, Any] | None,
    pending_secrets: list[dict[str, Any]],
    pending_input_files: bool,
) -> int:
    emit_progress             = _require("emit_progress")
    run_agent_with_capabilities = _require("run_agent_with_capabilities")
    persist_result            = _require("persist_result")
    write_json                = _require("write_json")
    load_json                 = _require("load_json")
    now_iso                   = _require("now_iso")
    bootstrap_project         = _require("bootstrap_project")
    fork_project              = _require("fork_project")
    store_secrets             = _require("store_secrets")
    ingest_input_files        = _require("ingest_input_files")
    save_last_active_project  = _require("save_last_active_project")
    _get_project_input_paths  = _require("_get_project_input_paths")
    REGISTRY_PATH             = _require("REGISTRY_PATH")
    extract_project_knowledge = _require("extract_project_knowledge")

    # ── Handle pending resolution from a prior run ──────────────────────────
    worker_task = request
    pending = task_state.get("pending_resolution")
    if pending:
        prior_type = pending.get("type", "")
        del task_state["pending_resolution"]
        write_json(task_state_path, task_state)

        if prior_type == "user_acceptance":
            user_response = request.lower().strip()
            # Acceptance: explicit positive phrases only — short responses that clearly signal done.
            acceptance = ("yes", "approved", "looks good", "accept", "lgtm", "correct", "done", "ship it", "approved it", "good to go")
            # Rejection: concrete action words only — avoids false positives on "not what" phrasing.
            rejection  = ("no", "reject", "wrong", "incorrect", "fix this", "rework", "redo", "revert")
            is_accepted = any(re.search(r"\b" + re.escape(p) + r"\b", user_response) for p in acceptance)
            is_rejected = not is_accepted and any(re.search(r"\b" + re.escape(p) + r"\b", user_response) for p in rejection)

            if is_accepted:
                extract_project_knowledge(active_project, task_state)
                emit_progress("[engine] User accepted. Knowledge captured. Project closed.")
                return 0

            if is_rejected:
                emit_progress("[engine] User rejected — treating as rework feedback.")
            else:
                # Unrecognised response: treat as rework feedback rather than silently closing.
                emit_progress("[engine] Response not recognised as explicit accept/reject — treating as rework feedback.")

            feedback = request.strip()
            if active_project:
                for v in (active_project.get("project_id", ""), active_project.get("project_name", "")):
                    if v:
                        feedback = re.sub(re.escape(v), "", feedback, flags=re.IGNORECASE).strip()
            feedback = re.sub(r"^[\s,]+", "", feedback)
            original = pending.get("original_request", request)
            worker_task = f"Rework based on user feedback:\n\n{feedback}\n\nOriginal task: {original}"
            emit_progress("[engine] User feedback received. Running rework.")
        elif prior_type == "planning_questions":
            # User answered planning questions — the plan is in task_state["plan"],
            # and the user's answers are in `request`.  The planning injection
            # below (the _has_prior_plan branch) will combine them.
            worker_task = request
        else:
            worker_task = f"Continue: {pending.get('message', '')}. User input: {request}"

    # ── Bootstrap project if not yet resolved ───────────────────────────────
    if active_project is None:
        registry = load_json(REGISTRY_PATH)
        project_id   = _next_project_id(registry)
        project_name = _project_name_from_request(request)

        if fork_hint:
            decision = {
                "project_id":        project_id,
                "project_name":      project_name,
                "description":       request,
                "source_project_id": fork_hint["source_project_id"],
                "inherit_artifacts": fork_hint.get("inherit_artifacts", []),
            }
            active_project = fork_project(decision)
        else:
            decision = {
                "project_id":   project_id,
                "project_name": project_name,
                "description":  request,
            }
            active_project = bootstrap_project(decision)

        runtime_dir     = Path(active_project["runtime_dir"])
        task_state_path = runtime_dir / "state" / "active_task.json"
        task_state      = load_json(task_state_path)
        task_state["user_request"] = request
        task_state["last_updated"] = now_iso()
        write_json(task_state_path, task_state)
        save_last_active_project(active_project)

        if pending_secrets:
            store_secrets(active_project["project_id"], pending_secrets, source="user_prompt")
            emit_progress(f"[engine] Stored {len(pending_secrets)} secret(s) in project vault.")

        if pending_input_files:
            ingested = ingest_input_files(active_project["project_id"])
            emit_progress(f"[engine] Ingested {len(ingested)} input file(s).")

    emit_progress(f"[engine] Project: {active_project['project_name']} ({active_project['project_id']})")

    if not execute_agents:
        emit_progress("[engine] Manual mode — skipping agent execution.")
        return 0

    project_inputs = _get_project_input_paths(active_project["project_id"])

    # ── Lightweight planning for complex tasks ──────────────────────────────
    # Only fires on fresh runs (no pending_resolution consumed above) when
    # the heuristic detects complexity.  Uses a minimal prompt — no KB, no
    # skills, no capability reference — to keep token cost low.
    _has_prior_plan = bool(task_state.get("plan"))
    if not _has_prior_plan and _needs_planning(worker_task):
        emit_progress("[engine] Complex task detected — running lightweight planning step...")
        planning_prompt = _PLANNING_PROMPT_TEMPLATE.format(request=worker_task)
        plan_res = _run_with_retry(
            lambda: run_agent_with_capabilities(
                "worker", planning_prompt, "Plan the task before implementation",
                [], active_project, agent_bin,
            ),
            "worker", emit_progress,
        )
        plan_output = (plan_res.get("output") or {}) if plan_res.get("status") != "failed" else {}
        plan_steps = plan_output.get("plan", [])
        plan_questions = plan_output.get("questions", [])

        if plan_steps:
            task_state["plan"] = plan_steps
            write_json(task_state_path, task_state)

        if plan_questions:
            questions_text = "\n".join(f"  {i+1}. {q}" for i, q in enumerate(plan_questions))
            emit_progress(f"[engine] Planning identified questions:\n{questions_text}")
            task_state["pending_resolution"] = {
                "type":             "planning_questions",
                "message":          f"Before starting, the engine needs clarification:\n{questions_text}",
                "original_request": request,
            }
            task_state["last_updated"] = now_iso()
            write_json(task_state_path, task_state)
            _pid = active_project["project_id"]
            emit_progress(
                f"[engine] Answer the questions above to proceed:\n"
                f"  ./automator --cli {agent_bin} --project continue --id {_pid} --task '<your answers>'"
            )
            return 0

        if plan_steps:
            numbered_plan = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(plan_steps))
            worker_task = (
                f"Execute this plan step by step:\n{numbered_plan}\n\n"
                f"Original request: {worker_task}"
            )
            emit_progress(f"[engine] Plan ready ({len(plan_steps)} steps). Proceeding to worker.")

    elif _has_prior_plan:
        # Resume from a prior planning step — inject the stored plan.
        plan_steps = task_state["plan"]
        numbered_plan = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(plan_steps))
        worker_task = (
            f"Execute this plan step by step:\n{numbered_plan}\n\n"
            f"Original request: {task_state.get('user_request', worker_task)}\n\n"
            f"User clarifications: {worker_task}"
        )

    # ── Adaptive capability round budget ─────────────────────────────────────
    _round_budget = _capability_rounds_for_task(worker_task, _has_prior_plan or bool(task_state.get("plan")))

    # ── Stage resume: reuse successful worker output from a prior run ──────
    _resumed_worker = False
    worker_artifact: str = ""
    worker_res: dict[str, Any] = {}

    last_worker_step = None
    for step in reversed(task_state.get("completed_steps", [])):
        if step.get("agent") == "worker":
            last_worker_step = step
            break

    if (last_worker_step
            and last_worker_step.get("status") == "success"
            and last_worker_step.get("artifact")
            and Path(last_worker_step["artifact"]).exists()):
        # Prior run completed the worker successfully — reload its output.
        prior_output = load_json(Path(last_worker_step["artifact"]))
        if prior_output and prior_output.get("summary"):
            worker_artifact = last_worker_step["artifact"]
            worker_res = {"status": "success", "output": prior_output}
            _resumed_worker = True
            emit_progress("[engine] Resuming from prior worker output — skipping to review.")

    if not _resumed_worker:
        emit_progress("[engine] Running worker...")
        worker_res = _run_with_retry(
            lambda: run_agent_with_capabilities(
                "worker", worker_task, "Implement the task",
                project_inputs, active_project, agent_bin,
                max_rounds=_round_budget,
            ),
            "worker", emit_progress,
        )
        if worker_res.get("status") == "failed":
            emit_progress(
                f"[engine] Worker failed ({worker_res.get('error_category', 'unknown')}): "
                f"{worker_res.get('error', '')}"
            )
            return 1

    worker_output_1 = worker_res.get("output") or {}
    if not _resumed_worker:
        validation_err = _validate_agent_output(worker_output_1, "worker")
        if validation_err:
            emit_progress(f"[engine] Worker output validation failed: {validation_err}")
            return 1
    _research_cycles_used = 0  # track across blocker-challenge and explicit research
    if not _resumed_worker:
        if worker_output_1.get("status") == "blocked":
            blockers = worker_output_1.get("open_issues", [])
            hard, researchable = _classify_blockers(blockers)

            # If there are researchable blockers and we haven't exhausted research cycles,
            # auto-dispatch research instead of stopping.
            if researchable and _research_cycles_used < MAX_RESEARCH_CYCLES:
                emit_progress(
                    f"[engine] Worker reported blocked but {len(researchable)} issue(s) look "
                    f"researchable. Challenging with research agent..."
                )
                numbered = "\n".join(f"Q{i+1}: {q}" for i, q in enumerate(researchable))
                challenge_task = (
                    f"The worker reported these as blockers. Research each one and provide "
                    f"concrete answers so the worker can proceed:\n{numbered}"
                    f"\n\nOriginal task: {worker_task}"
                )
                challenge_res = _run_with_retry(
                    lambda: run_agent_with_capabilities(
                        "research", challenge_task, "Challenge worker blockers with research",
                        project_inputs, active_project, agent_bin,
                    ),
                    "research", emit_progress,
                )
                _research_cycles_used += 1
                if challenge_res.get("status") != "failed":
                    challenge_output = challenge_res.get("output") or {}
                    challenge_artifact = persist_result(active_project, "research", challenge_output)
                    challenge_summary = challenge_output.get("summary", "Research completed.")
                    _record_step(task_state, "research", "success", challenge_artifact, challenge_summary, now_iso)
                    task_state["last_updated"] = now_iso()
                    write_json(task_state_path, task_state)
                    emit_progress(f"[engine] Blocker research done: {challenge_summary}")

                    # Re-run worker with research findings to unblock it.
                    emit_progress("[engine] Re-running worker with research findings to resolve blockers...")
                    research_context = (
                        f"Research findings are in the injected artifact. "
                        f"Questions answered: {numbered}. "
                        f"Focus on `technical_data.answers[].implementation_notes` for direct guidance. "
                        f"The issues you previously reported as blockers have been researched — "
                        f"use the findings to proceed."
                    )
                    _challenge_inputs = [p for p in project_inputs + [challenge_artifact] if p]
                    _challenge_task = f"{worker_task}\n\n{research_context}"
                    worker_res = _run_with_retry(
                        lambda: run_agent_with_capabilities(
                            "worker", _challenge_task, "Implement the task using research findings",
                            _challenge_inputs,
                            active_project, agent_bin,
                            max_rounds=_round_budget,
                        ),
                        "worker", emit_progress,
                    )
                    if worker_res.get("status") != "failed":
                        worker_output_1 = worker_res.get("output") or {}
                        # Fall through to re-evaluate the new output below
                        # (the output may now be success, still blocked, or needs_research)
                    else:
                        emit_progress(f"[engine] Worker (post-challenge) failed: {worker_res.get('error', '')}")
                        return 1
                else:
                    emit_progress("[engine] Blocker research failed — accepting original blockers.")

            # After challenge attempt (or if no researchable blockers), check if still blocked.
            if worker_output_1.get("status") == "blocked":
                blockers = worker_output_1.get("open_issues", [])
                blocker_msg = "; ".join(blockers) or "Worker reported a hard blocker."
                emit_progress(f"[engine] Worker blocked: {blocker_msg}")
                worker_artifact = persist_result(active_project, "worker", worker_output_1)
                _record_step(task_state, "worker", "blocked", worker_artifact, blocker_msg, now_iso)
                task_state["pending_resolution"] = {
                    "type":             "user_input_required",
                    "message":          blocker_msg,
                    "original_request": request,
                }
                task_state["last_updated"] = now_iso()
                write_json(task_state_path, task_state)
                return 1

        if worker_output_1.get("needs_user_input"):
            needed      = worker_output_1.get("open_issues", [])
            needed_msg  = "; ".join(needed) or "Worker requires user input to continue."
            emit_progress(f"[engine] Worker needs user input: {needed_msg}")
            worker_artifact = persist_result(active_project, "worker", worker_output_1)
            _record_step(task_state, "worker", "blocked", worker_artifact, needed_msg, now_iso)
            task_state["pending_resolution"] = {
                "type":             "user_input_required",
                "message":          needed_msg,
                "original_request": request,
            }
            task_state["last_updated"] = now_iso()
            write_json(task_state_path, task_state)
            return 1

        # Verify claimed delivery files actually exist on disk.
        missing = _verify_delivery_files(worker_output_1, active_project.get("project_root", ""))
        if missing:
            for m in missing:
                emit_progress(f"[engine] Delivery verification: {m}")
            emit_progress(f"[engine] Warning: {len(missing)} claimed file(s) not found. Review will catch this.")

        worker_artifact = persist_result(active_project, "worker", worker_output_1)
        worker_summary  = worker_output_1.get("summary", "Worker completed.")
        _record_step(task_state, "worker", "success", worker_artifact, worker_summary, now_iso)
        task_state["last_updated"] = now_iso()
        write_json(task_state_path, task_state)
        emit_progress(f"[engine] Worker done: {worker_summary}")

    # ── Optional research loop (up to MAX_RESEARCH_CYCLES) ───────────────────
    # Skip research on resume — the resumed artifact already completed this path.
    _current_worker_output = worker_output_1
    while _research_cycles_used < MAX_RESEARCH_CYCLES and not _resumed_worker:
        research_questions = (
            [q for q in _current_worker_output.get("open_issues", []) if isinstance(q, str) and q.strip()]
            if _current_worker_output.get("needs_research") else []
        )
        if _current_worker_output.get("needs_research") and not research_questions:
            emit_progress("[engine] Worker flagged needs_research but provided no questions — skipping research.")
            break
        if not research_questions:
            break

        questions = research_questions
        numbered = "\n".join(f"Q{i+1}: {q}" for i, q in enumerate(questions))
        _cycle_label = f" (cycle {_research_cycles_used + 1})" if _research_cycles_used > 0 else ""
        research_task = (
            f"Answer these specific questions needed to complete the task:\n"
            + numbered
            + f"\n\nOriginal task: {worker_task}"
        )
        emit_progress(f"[engine] Worker needs research{_cycle_label}. Running research agent...")
        research_res = _run_with_retry(
            lambda: run_agent_with_capabilities(
                "research", research_task, "Answer worker's open questions",
                project_inputs, active_project, agent_bin,
            ),
            "research", emit_progress,
        )
        _research_cycles_used += 1
        if research_res.get("status") == "failed":
            emit_progress(f"[engine] Research failed: {research_res.get('error', '')}")
            return 1

        research_output   = research_res.get("output") or {}
        research_artifact = persist_result(active_project, "research", research_output)
        research_summary  = research_output.get("summary", "Research completed.")
        _record_step(task_state, "research", "success", research_artifact, research_summary, now_iso)
        task_state["last_updated"] = now_iso()
        write_json(task_state_path, task_state)
        emit_progress(f"[engine] Research done{_cycle_label}: {research_summary}")

        # Re-run worker with research findings
        emit_progress(f"[engine] Re-running worker with research findings{_cycle_label}...")
        research_context = (
            f"Research findings are in the injected artifact. "
            f"Questions answered: {numbered}. "
            f"Focus on `technical_data.answers[].implementation_notes` for direct guidance."
        )
        _post_research_inputs = [p for p in project_inputs + [research_artifact] if p]
        _post_research_task = f"{worker_task}\n\n{research_context}"
        worker_res = _run_with_retry(
            lambda: run_agent_with_capabilities(
                "worker", _post_research_task, "Implement the task using research findings",
                _post_research_inputs,
                active_project, agent_bin,
                max_rounds=_round_budget,
            ),
            "worker", emit_progress,
        )
        if worker_res.get("status") == "failed":
            emit_progress(f"[engine] Worker (post-research) failed: {worker_res.get('error', '')}")
            return 1

        worker_output = worker_res.get("output") or {}
        validation_err = _validate_agent_output(worker_output, "worker")
        if validation_err:
            emit_progress(f"[engine] Worker (post-research) output validation failed: {validation_err}")
            return 1
        if worker_output.get("status") == "blocked":
            blockers    = worker_output.get("open_issues", [])
            blocker_msg = "; ".join(blockers) or "Worker reported a hard blocker."
            emit_progress(f"[engine] Worker (post-research) blocked: {blocker_msg}")
            worker_artifact = persist_result(active_project, "worker", worker_output)
            _record_step(task_state, "worker", "blocked", worker_artifact, blocker_msg, now_iso)
            task_state["pending_resolution"] = {
                "type":             "user_input_required",
                "message":          blocker_msg,
                "original_request": request,
            }
            task_state["last_updated"] = now_iso()
            write_json(task_state_path, task_state)
            return 1

        if worker_output.get("needs_user_input"):
            needed      = worker_output.get("open_issues", [])
            needed_msg  = "; ".join(needed) or "Worker requires user input to continue."
            emit_progress(f"[engine] Worker (post-research) needs user input: {needed_msg}")
            worker_artifact = persist_result(active_project, "worker", worker_output)
            _record_step(task_state, "worker", "blocked", worker_artifact, needed_msg, now_iso)
            task_state["pending_resolution"] = {
                "type":             "user_input_required",
                "message":          needed_msg,
                "original_request": request,
            }
            task_state["last_updated"] = now_iso()
            write_json(task_state_path, task_state)
            return 1

        # Update for next iteration check — if worker still needs_research, loop.
        _current_worker_output = worker_output
        # If worker is done (no more needs_research), break out.
        if not worker_output.get("needs_research"):
            break

    # Persist final post-research worker output if research ran.
    if _research_cycles_used > 0 and not _resumed_worker and not _current_worker_output.get("needs_research"):
        missing = _verify_delivery_files(_current_worker_output, active_project.get("project_root", ""))
        if missing:
            for m in missing:
                emit_progress(f"[engine] Delivery verification: {m}")
            emit_progress(f"[engine] Warning: {len(missing)} claimed file(s) not found. Review will catch this.")
        worker_artifact = persist_result(active_project, "worker", _current_worker_output)
        worker_summary  = _current_worker_output.get("summary", "Worker completed.")
        _record_step(task_state, "worker", "success", worker_artifact, worker_summary, now_iso)
        task_state["last_updated"] = now_iso()
        write_json(task_state_path, task_state)
        emit_progress(f"[engine] Worker done: {worker_summary}")

    # ── Review ───────────────────────────────────────────────────────────────
    _final_worker_output      = worker_res.get("output") or {}
    worker_summary_for_review = _final_worker_output.get("summary", "")
    worker_open_issues        = _final_worker_output.get("open_issues", [])
    review_context = f"Worker summary: {worker_summary_for_review}"
    if worker_open_issues:
        review_context += "\nWorker flagged open issues:\n" + "\n".join(f"- {i}" for i in worker_open_issues)
    review_task = f"Review the worker output for this task:\n\n{worker_task}\n\n{review_context}"
    emit_progress("[engine] Running review...")
    review_res = _run_with_retry(
        lambda: run_agent_with_capabilities(
            "review", review_task, "Review worker delivery",
            [p for p in project_inputs + [worker_artifact] if p],
            active_project, agent_bin,
        ),
        "review", emit_progress,
    )
    if review_res.get("status") == "failed":
        emit_progress(
            f"[engine] Review failed ({review_res.get('error_category', 'unknown')}): "
            f"{review_res.get('error', '')}"
        )
        return 1

    review_output  = review_res.get("output") or {}
    review_artifact = persist_result(active_project, "review", review_output)
    # Default to "fail" — if the review agent returned no status field, the output
    # is unparseable and should not be silently accepted as passing.
    review_status  = _normalize_review_status(review_output.get("status", "fail"))
    review_summary = review_output.get("summary", "Review completed.")

    # Enforce review verification: a pass with zero capability rounds AND zero
    # native tool uses means the review didn't actually execute any check —
    # demote to fail. Either counter being >0 is real work signal from the
    # execution layer, not the agent's self-reported checks_run field (which
    # would be trivially spoofable).
    review_rounds_used = review_res.get("capability_rounds_used", 0)
    review_native_tool_uses = review_res.get("native_tool_uses", 0)
    if review_status == "pass" and review_rounds_used == 0 and review_native_tool_uses == 0:
        review_status = "fail"
        review_summary = "Review passed without running any capability — demoted to fail."
        if not review_output.get("blocking"):
            review_output["blocking"] = []
        review_output["blocking"].append("Review must run at least one command or test before passing.")
        if not review_output.get("rework_requests"):
            review_output["rework_requests"] = []
        review_output["rework_requests"].append("Re-run review with actual test execution.")
        emit_progress("[engine] Review passed without running any capability — treating as fail.")
    _record_step(task_state, "review", review_status, review_artifact, review_summary, now_iso)
    task_state["last_updated"] = now_iso()
    write_json(task_state_path, task_state)
    emit_progress(f"[engine] Review {review_status}: {review_summary}")

    # ── One rework cycle if review failed ────────────────────────────────────
    if review_status == "fail":
        # Enforce the "one rework" contract. The counter persists across
        # --project continue invocations, so a fresh review after
        # review_blocked must not dispatch another rework worker.
        if task_state.get("rework_loop_count", 0) >= 1:
            blocking_after_cap = review_output.get("blocking", [])
            emit_progress("[engine] Rework budget exhausted — review still blocking.")
            task_state["pending_resolution"] = {
                "type":             "review_blocked",
                "message":          f"Review blocked after rework. Issues: {blocking_after_cap}",
                "original_request": request,
            }
            task_state["last_updated"] = now_iso()
            write_json(task_state_path, task_state)
            return 1

        blocking         = review_output.get("blocking", [])
        rework_requests  = review_output.get("rework_requests", [])
        blocking_lines   = "\n".join(f"- {r}" for r in blocking)
        fix_lines        = "\n".join(f"- {r}" for r in rework_requests)

        # Build a structured rework checklist pairing issues with fixes.
        checklist_lines: list[str] = []
        for i, issue in enumerate(blocking):
            fix = rework_requests[i] if i < len(rework_requests) else ""
            entry = f"{i + 1}. ISSUE: {issue}"
            if fix:
                entry += f"\n   FIX: {fix}"
            checklist_lines.append(entry)
        # Append any extra fixes that don't have a paired issue.
        for j in range(len(blocking), len(rework_requests)):
            checklist_lines.append(f"{j + 1}. FIX: {rework_requests[j]}")

        checklist = "\n".join(checklist_lines) if checklist_lines else blocking_lines
        rework_task = (
            f"Rework required. Original task:\n{worker_task}\n\n"
            f"Review found {len(blocking)} blocking issue(s). "
            f"Fix each item below and verify it before reporting success:\n\n"
            f"{checklist}"
        )

        task_state["rework_loop_count"] = task_state.get("rework_loop_count", 0) + 1
        write_json(task_state_path, task_state)

        emit_progress("[engine] Review requested rework. Running one rework cycle...")
        _rework_inputs = [p for p in project_inputs + [worker_artifact, review_artifact] if p]
        worker_res2 = _run_with_retry(
            lambda: run_agent_with_capabilities(
                "worker", rework_task, "Rework based on review feedback",
                _rework_inputs,
                active_project, agent_bin,
                max_rounds=_round_budget,
            ),
            "worker", emit_progress,
        )
        if worker_res2.get("status") == "failed":
            emit_progress(f"[engine] Rework worker failed: {worker_res2.get('error', '')}")
            return 1

        rework_output = worker_res2.get("output") or {}
        if rework_output.get("status") == "blocked":
            blockers    = rework_output.get("open_issues", [])
            blocker_msg = "; ".join(blockers) or "Rework worker reported a hard blocker."
            emit_progress(f"[engine] Rework worker blocked: {blocker_msg}")
            rework_artifact = persist_result(active_project, "worker", rework_output)
            _record_step(task_state, "worker", "blocked", rework_artifact, blocker_msg, now_iso)
            task_state["pending_resolution"] = {
                "type":             "user_input_required",
                "message":          blocker_msg,
                "original_request": request,
            }
            task_state["last_updated"] = now_iso()
            write_json(task_state_path, task_state)
            return 1

        if rework_output.get("needs_user_input"):
            needed      = rework_output.get("open_issues", [])
            needed_msg  = "; ".join(needed) or "Rework worker requires user input to continue."
            emit_progress(f"[engine] Rework worker needs user input: {needed_msg}")
            worker_artifact2 = persist_result(active_project, "worker", rework_output)
            _record_step(task_state, "worker", "blocked", worker_artifact2, needed_msg, now_iso)
            task_state["pending_resolution"] = {
                "type":             "user_input_required",
                "message":          needed_msg,
                "original_request": request,
            }
            task_state["last_updated"] = now_iso()
            write_json(task_state_path, task_state)
            return 1

        missing = _verify_delivery_files(rework_output, active_project.get("project_root", ""))
        if missing:
            for m in missing:
                emit_progress(f"[engine] Delivery verification: {m}")
            emit_progress(f"[engine] Warning: {len(missing)} claimed file(s) not found. Review will catch this.")
        worker_artifact2 = persist_result(active_project, "worker", rework_output)
        worker_summary2  = rework_output.get("summary", "Rework completed.")
        _record_step(task_state, "worker", "success", worker_artifact2, worker_summary2, now_iso)
        task_state["last_updated"] = now_iso()
        write_json(task_state_path, task_state)
        emit_progress(f"[engine] Rework done: {worker_summary2}")

        emit_progress("[engine] Running final review...")
        final_review_task = (
            f"Review the worker output for this task:\n\n{worker_task}\n\n"
            f"Worker summary: {worker_summary2}\n\n"
            f"This is a final review after a rework cycle. The previous review found these issues:\n\n"
            f"{checklist}"
            f"\n\nVerify each item above is resolved before passing."
        )
        _final_review_inputs = [p for p in project_inputs + [worker_artifact2, review_artifact] if p]
        review_res2 = _run_with_retry(
            lambda: run_agent_with_capabilities(
                "review", final_review_task, "Final review after rework",
                _final_review_inputs,
                active_project, agent_bin,
            ),
            "review", emit_progress,
        )
        if review_res2.get("status") == "failed":
            emit_progress(f"[engine] Final review failed: {review_res2.get('error', '')}")
            return 1

        review_output2  = review_res2.get("output") or {}
        review_artifact2 = persist_result(active_project, "review", review_output2)
        # Same as first review: default to "fail" when status field is missing.
        review_status2  = _normalize_review_status(review_output2.get("status", "fail"))
        review_summary2 = review_output2.get("summary", "Final review completed.")
        # Apply the same "no real work => auto-demote" check to the final
        # review so rework can't be rubber-stamped without execution. Either
        # capability rounds or native tool uses prove the review did something.
        if (
            review_status2 == "pass"
            and review_res2.get("capability_rounds_used", 0) == 0
            and review_res2.get("native_tool_uses", 0) == 0
        ):
            review_status2 = "fail"
            review_summary2 = "Final review passed without running any capability — demoted to fail."
            emit_progress("[engine] Final review passed without running any capability — treating as fail.")
        _record_step(task_state, "review", review_status2, review_artifact2, review_summary2, now_iso)
        task_state["last_updated"] = now_iso()
        write_json(task_state_path, task_state)
        emit_progress(f"[engine] Final review {review_status2}: {review_summary2}")

        if review_status2 == "fail":
            blocking2 = review_output2.get("blocking", [])
            emit_progress("[engine] Review still blocking after rework. Stopping.")
            task_state["pending_resolution"] = {
                "type":             "review_blocked",
                "message":          f"Review blocked after rework. Issues: {blocking2}",
                "original_request": request,
            }
            task_state["last_updated"] = now_iso()
            write_json(task_state_path, task_state)
            return 1

    # ── Complete ─────────────────────────────────────────────────────────────
    delivery_path = active_project.get("project_root", "")
    emit_progress(f"[engine] Task complete. Delivery at: {delivery_path}")
    task_state["pending_resolution"] = {
        "type":             "user_acceptance",
        "message":          "Work delivered and review passed. Accept or give feedback.",
        "original_request": request,
    }
    task_state["last_updated"] = now_iso()
    write_json(task_state_path, task_state)
    _pid = active_project["project_id"]
    emit_progress(
        f"[engine] To accept: ./automator --project close --id {_pid}\n"
        f"[engine] To give feedback: ./automator --cli {agent_bin} --project continue --id {_pid} --task <feedback>"
    )
    return 0
