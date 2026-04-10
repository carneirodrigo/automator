"""Lean orchestration runner — worker → review lifecycle."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from engine.work.task_state import TaskState

_ENV: dict[str, Any] = {}

# Normalized review status values. LLMs may return "failed", "FAIL", "reject",
# etc. — anything not explicitly "pass" is treated as failure to prevent
# broken work from being presented as complete.
_REVIEW_PASS_VALUES = frozenset({"pass", "passed", "approve", "approved", "lgtm", "ok", "success"})


def _normalize_review_status(raw: str) -> str:
    """Map an LLM review status to 'pass' or 'fail'."""
    return "pass" if raw.strip().lower() in _REVIEW_PASS_VALUES else "fail"


def configure_orchestrator_environment(**kwargs: Any) -> None:
    _ENV.update(kwargs)


def _require(name: str) -> Any:
    value = _ENV.get(name)
    if value is None:
        raise RuntimeError(f"Orchestrator environment missing: {name}")
    return value


def _next_project_id(registry: dict[str, Any]) -> str:
    nums = [
        int(p["project_id"])
        for p in registry.get("projects", [])
        if p.get("project_id", "").isdigit()
    ]
    return str(max(nums) + 1 if nums else 1).zfill(3)


def _project_name_from_request(request: str) -> str:
    words = re.sub(r"[^\w\s]", "", request).split()
    return " ".join(w.capitalize() for w in words[:5]) or "Untitled Project"


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

    # ── Worker ───────────────────────────────────────────────────────────────
    worker_artifact: str = ""
    emit_progress("[engine] Running worker...")
    worker_res = run_agent_with_capabilities(
        "worker", worker_task, "Implement the task",
        project_inputs, active_project, agent_bin,
    )
    if worker_res["status"] == "failed":
        emit_progress(
            f"[engine] Worker failed ({worker_res.get('error_category', 'unknown')}): "
            f"{worker_res.get('error', '')}"
        )
        return 1

    worker_output_1 = worker_res.get("output") or {}
    if worker_output_1.get("status") == "blocked":
        blockers    = worker_output_1.get("open_issues", [])
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

    worker_artifact = persist_result(active_project, "worker", worker_output_1)
    worker_summary  = worker_output_1.get("summary", "Worker completed.")
    _record_step(task_state, "worker", "success", worker_artifact, worker_summary, now_iso)
    task_state["last_updated"] = now_iso()
    write_json(task_state_path, task_state)
    emit_progress(f"[engine] Worker done: {worker_summary}")

    # ── Optional research if worker flagged external unknowns ────────────────
    research_questions = (
        [q for q in worker_output_1.get("open_issues", []) if isinstance(q, str) and q.strip()]
        if worker_output_1.get("needs_research") else []
    )
    if worker_output_1.get("needs_research") and not research_questions:
        emit_progress("[engine] Worker flagged needs_research but provided no questions — skipping research.")
    if research_questions:
        questions = research_questions
        numbered = "\n".join(f"Q{i+1}: {q}" for i, q in enumerate(questions))
        research_task = (
            f"Answer these specific questions needed to complete the task:\n"
            + numbered
            + f"\n\nOriginal task: {worker_task}"
        )
        emit_progress("[engine] Worker needs research. Running research agent...")
        research_res = run_agent_with_capabilities(
            "research", research_task, "Answer worker's open questions",
            project_inputs, active_project, agent_bin,
        )
        if research_res["status"] == "failed":
            emit_progress(f"[engine] Research failed: {research_res.get('error', '')}")
            return 1

        research_output   = research_res.get("output") or {}
        research_artifact = persist_result(active_project, "research", research_output)
        research_summary  = research_output.get("summary", "Research completed.")
        _record_step(task_state, "research", "success", research_artifact, research_summary, now_iso)
        task_state["last_updated"] = now_iso()
        write_json(task_state_path, task_state)
        emit_progress(f"[engine] Research done: {research_summary}")

        # Re-run worker with research findings
        emit_progress("[engine] Re-running worker with research findings...")
        research_context = (
            f"Research findings are in the injected artifact. "
            f"Questions answered: {numbered}. "
            f"Focus on `technical_data.answers[].implementation_notes` for direct guidance."
        )
        worker_res = run_agent_with_capabilities(
            "worker", f"{worker_task}\n\n{research_context}", "Implement the task using research findings",
            [p for p in project_inputs + [research_artifact] if p],
            active_project, agent_bin,
        )
        if worker_res["status"] == "failed":
            emit_progress(f"[engine] Worker (post-research) failed: {worker_res.get('error', '')}")
            return 1

        worker_output = worker_res.get("output") or {}
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

        worker_artifact = persist_result(active_project, "worker", worker_output)
        worker_summary  = worker_output.get("summary", "Worker completed.")
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
    review_res = run_agent_with_capabilities(
        "review", review_task, "Review worker delivery",
        [p for p in project_inputs + [worker_artifact] if p],
        active_project, agent_bin,
    )
    if review_res["status"] == "failed":
        emit_progress(
            f"[engine] Review failed ({review_res.get('error_category', 'unknown')}): "
            f"{review_res.get('error', '')}"
        )
        return 1

    review_output  = review_res.get("output") or {}
    review_artifact = persist_result(active_project, "review", review_output)
    review_status  = _normalize_review_status(review_output.get("status", "pass"))
    review_summary = review_output.get("summary", "Review completed.")
    _record_step(task_state, "review", review_status, review_artifact, review_summary, now_iso)
    task_state["last_updated"] = now_iso()
    write_json(task_state_path, task_state)
    emit_progress(f"[engine] Review {review_status}: {review_summary}")

    # ── One rework cycle if review failed ────────────────────────────────────
    if review_status == "fail":
        blocking         = review_output.get("blocking", [])
        rework_requests  = review_output.get("rework_requests", [])
        blocking_lines   = "\n".join(f"- {r}" for r in blocking)
        fix_lines        = "\n".join(f"- {r}" for r in rework_requests)
        rework_task = (
            f"Rework required. Original task:\n{worker_task}\n\n"
            f"Blocking issues:\n{blocking_lines}"
            + (f"\n\nRequired fixes:\n{fix_lines}" if fix_lines else "")
        )

        task_state["rework_loop_count"] = task_state.get("rework_loop_count", 0) + 1
        write_json(task_state_path, task_state)

        emit_progress("[engine] Review requested rework. Running one rework cycle...")
        worker_res2 = run_agent_with_capabilities(
            "worker", rework_task, "Rework based on review feedback",
            [p for p in project_inputs + [worker_artifact, review_artifact] if p],
            active_project, agent_bin,
        )
        if worker_res2["status"] == "failed":
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
            f"This is a final review after a rework cycle. The previous review found these blocking issues:\n"
            f"{blocking_lines}"
            + (f"\n\nThe worker was asked to apply these fixes:\n{fix_lines}" if fix_lines else "")
            + "\n\nVerify each issue above is resolved before passing."
        )
        review_res2 = run_agent_with_capabilities(
            "review", final_review_task, "Final review after rework",
            [p for p in project_inputs + [worker_artifact2, review_artifact] if p],
            active_project, agent_bin,
        )
        if review_res2["status"] == "failed":
            emit_progress(f"[engine] Final review failed: {review_res2.get('error', '')}")
            return 1

        review_output2  = review_res2.get("output") or {}
        review_artifact2 = persist_result(active_project, "review", review_output2)
        review_status2  = _normalize_review_status(review_output2.get("status", "pass"))
        review_summary2 = review_output2.get("summary", "Final review completed.")
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
