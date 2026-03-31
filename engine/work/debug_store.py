"""Engine-side debug issue capture and storage helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEBUG_TRACKER_VERSION = 1


def _slugify_debug_text(value: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "issue")[:max_len].rstrip("-") or "issue"


def _debug_tracker_template() -> dict[str, Any]:
    return {"version": DEBUG_TRACKER_VERSION, "issues": []}


def _build_debug_issue_fingerprint(
    issue_type: str,
    backend: str,
    project_id: str,
    role: str,
    error_category: str,
) -> str:
    return "|".join(
        [
            issue_type,
            backend or "unknown",
            project_id or "none",
            role or "none",
            error_category or "unknown",
        ]
    )


def _debug_issue_summary(
    issue_type: str,
    title: str,
    error_category: str,
    details: dict[str, Any] | None,
) -> str:
    payload = details or {}
    if isinstance(payload.get("validation_errors"), list) and payload["validation_errors"]:
        return str(payload["validation_errors"][0])[:240]
    for key in ("error", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:240]
    if error_category:
        return f"{title}: {error_category}"[:240]
    return title[:240]


def _debug_issue_criticality(issue_type: str, role: str, error_category: str) -> str:
    if issue_type in ("startup_configuration_error", "project_resolution_error"):
        return "medium"
    if issue_type in ("startup_runtime_error", "agent_execution_failed"):
        return "high"
    if error_category in (
        "network_blocked",
        "binary_not_found",
        "invalid_decision",
        "invalid_output",
    ):
        return "high"
    return "medium"


def record_debug_issue(
    *,
    issue_type: str,
    title: str,
    backend: str,
    request: str,
    role: str = "",
    error_category: str = "",
    active_project: dict[str, Any] | None = None,
    ctx: Any = None,
    task_state: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    repo_root: Path,
    tracker_path: Path,
    load_json: Callable[[Path], Any],
    write_json: Callable[[Path, Any], None],
    now_iso: Callable[[], str],
    emit_progress: Callable[[str], None],
    ctx_to_dict: Callable[[Any], Any],
) -> dict[str, Any]:
    tracker = load_json(tracker_path)
    if not isinstance(tracker, dict) or not isinstance(tracker.get("issues"), list):
        tracker = _debug_tracker_template()
    project_id = active_project["project_id"] if active_project else ""
    fingerprint = _build_debug_issue_fingerprint(
        issue_type, backend, project_id, role, error_category
    )
    summary = _debug_issue_summary(issue_type, title, error_category, details)
    criticality = _debug_issue_criticality(issue_type, role, error_category)
    now = now_iso()
    entry = None
    for candidate in tracker["issues"]:
        if candidate.get("fingerprint") == fingerprint and candidate.get("status") != "fixed":
            entry = candidate
            break

    if entry is None:
        issue_id = (
            f"dbg-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{_slugify_debug_text(role or issue_type)}-"
            f"{_slugify_debug_text(error_category or title, 24)}"
        )
        detail_rel = Path("debug") / "issues" / f"{issue_id}.json"
        entry = {
            "issue_id": issue_id,
            "fingerprint": fingerprint,
            "title": title,
            "status": "open",
            "issue_type": issue_type,
            "backend": backend,
            "role": role or None,
            "error_category": error_category or None,
            "summary": summary,
            "criticality": criticality,
            "project_id": project_id or None,
            "detail_path": str(detail_rel),
            "created_at": now,
            "updated_at": now,
            "occurrence_count": 0,
        }
        tracker["issues"].insert(0, entry)
    else:
        detail_rel = Path(entry["detail_path"])
        entry["updated_at"] = now

    entry["occurrence_count"] = int(entry.get("occurrence_count", 0)) + 1
    entry["summary"] = summary
    entry["criticality"] = criticality
    detail_path = repo_root / detail_rel
    detail_payload = {
        "version": 1,
        "issue_id": entry["issue_id"],
        "title": title,
        "issue_type": issue_type,
        "status": entry["status"],
        "fingerprint": fingerprint,
        "backend": backend,
        "role": role or None,
        "error_category": error_category or None,
        "summary": summary,
        "criticality": criticality,
        "request": request,
        "project": {
            "project_id": active_project.get("project_id") if active_project else None,
            "project_name": active_project.get("project_name") if active_project else None,
            "runtime_dir": active_project.get("runtime_dir") if active_project else None,
            "project_root": active_project.get("project_root") if active_project else None,
        },
        "stage_context": ctx_to_dict(ctx) if ctx else None,
        "task_state_summary": {
            "active_agent": task_state.get("active_agent") if task_state else None,
            "last_updated": task_state.get("last_updated") if task_state else None,
            "completed_step_count": len(task_state.get("completed_steps", [])) if task_state else 0,
            "artifact_count": len(task_state.get("artifacts", [])) if task_state else 0,
            "pending_resolution": task_state.get("pending_resolution") if task_state else None,
        },
        "details": details or {},
        "captured_at": now,
        "occurrence_count": entry["occurrence_count"],
    }
    write_json(detail_path, detail_payload)
    write_json(tracker_path, tracker)
    emit_progress(f"[debug] Captured issue {entry['issue_id']} -> {detail_rel}")
    return entry
