"""Knowledge-base extraction and purge helpers.

The KB has two kinds of entries:

1. Specialist entries (tracked) — hand-written under ``knowledge/`` in the repo.
2. Project-output entries (per-project) — generated on project close and stored
   under ``projects/<id>/runtime/project-knowledge.json``. The shared manifest
   at ``knowledge/manifest.json`` indexes both kinds by id and records the
   repo-relative file path for every entry.

These helpers own the write path for project-output entries and the cleanup
path when a project is deleted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from engine.work.file_lock import locked
from engine.work.json_io import load_json, write_json
from engine.work.repo_paths import REPO_ROOT
from engine.work.runtime_helpers import now_iso

KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
KNOWLEDGE_MANIFEST_PATH = KNOWLEDGE_DIR / "manifest.json"
KNOWLEDGE_SOURCES_PATH = KNOWLEDGE_DIR / "sources.json"


def extract_project_knowledge(
    project: dict[str, Any],
    task_state: dict[str, Any],
    *,
    emit_progress: Callable[[str], None],
) -> None:
    """Save a compact KB entry from the project's final worker artifact."""
    artifacts_dir = Path(project["runtime_dir"]) / "artifacts"
    if not artifacts_dir.exists():
        return

    worker_artifacts = sorted(
        artifacts_dir.glob("worker_result_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not worker_artifacts:
        return

    try:
        artifact = load_json(worker_artifacts[0])
    except (json.JSONDecodeError, OSError, ValueError):
        return

    output = artifact if "status" in artifact else artifact.get("output", artifact)
    if output.get("status") not in ("success",):
        return

    summary = output.get("summary", "").strip()
    if not summary:
        return

    user_request = task_state.get("user_request", project.get("project_name", ""))
    project_id = project["project_id"]
    project_name = project.get("project_name", project_id)

    slug = re.sub(r"[^\w]+", "-", project_name.lower()).strip("-")[:60]
    entry_id = f"project-{project_id}-{slug}"
    runtime_dir = Path(project["runtime_dir"])
    kb_path = runtime_dir / "project-knowledge.json"
    try:
        file_ref = str(kb_path.relative_to(REPO_ROOT))
    except ValueError:
        file_ref = str(kb_path)

    ts = now_iso()
    entry_data = {
        "id": entry_id,
        "title": project_name,
        "source_project_id": project_id,
        "task": user_request,
        "summary": summary,
        "changes_made": output.get("changes_made", []),
        "artifacts": output.get("artifacts", []),
        "checks_run": output.get("checks_run", []),
        "created": ts,
        "last_verified": ts,
    }

    try:
        kb_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(kb_path, entry_data)

        KNOWLEDGE_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with locked(KNOWLEDGE_MANIFEST_PATH):
            manifest = load_json(KNOWLEDGE_MANIFEST_PATH) if KNOWLEDGE_MANIFEST_PATH.exists() else {"version": 1, "entries": []}
            entries = manifest.setdefault("entries", [])
            entries[:] = [e for e in entries if e.get("id") != entry_id]
            entries.append({
                "id": entry_id,
                "title": project_name,
                "file": file_ref,
                "tags": ["project-output"],
                "source_family": "project",
                "coverage_type": "output",
                "source_project_id": project_id,
                "created": ts,
                "updated": ts,
                "last_verified": ts,
                "fresh_until": ts,  # immediately stale — treat as historical record
                "summary": summary,
            })
            write_json(KNOWLEDGE_MANIFEST_PATH, manifest)
        emit_progress(f"[engine] KB entry saved: {entry_id}")
    except OSError as exc:
        emit_progress(f"[engine] KB entry save failed for {entry_id}: {exc}")


def purge_project_knowledge(
    project_id: str,
    *,
    emit_progress: Callable[[str], None],
) -> int:
    """Remove knowledge entries owned by a specific project and scrub shared provenance."""
    if not KNOWLEDGE_MANIFEST_PATH.exists():
        emit_progress("[engine] No knowledge manifest found.")
        return 0

    with locked(KNOWLEDGE_MANIFEST_PATH):
        manifest = load_json(KNOWLEDGE_MANIFEST_PATH)
        entries = manifest.get("entries", [])
        if not isinstance(entries, list):
            emit_progress("[engine] Knowledge manifest is invalid: 'entries' must be a list.")
            return 1

        kept_entries: list[dict[str, Any]] = []
        removed_files = 0
        removed_entries = 0
        scrubbed_files = 0

        for entry in entries:
            if not isinstance(entry, dict):
                kept_entries.append(entry)
                continue

            entry_file = entry.get("file", "")
            if entry_file:
                # entry_file can be either a bare filename (legacy: resolved under
                # KNOWLEDGE_DIR) or a repo-relative path (new: project-output files
                # live under projects/<id>/runtime/). Both must stay inside the repo.
                if "/" in entry_file or "\\" in entry_file:
                    candidate = (REPO_ROOT / entry_file).resolve()
                    repo_resolved = REPO_ROOT.resolve()
                    entry_path = candidate if candidate.is_relative_to(repo_resolved) else None
                else:
                    candidate = (KNOWLEDGE_DIR / entry_file).resolve()
                    entry_path = candidate if candidate.is_relative_to(KNOWLEDGE_DIR.resolve()) else None
            else:
                entry_path = None
            owns_entry = entry.get("source_project_id") == project_id

            if owns_entry:
                removed_entries += 1
                if entry_path and entry_path.exists() and entry_path.is_file():
                    entry_path.unlink()
                    removed_files += 1
                continue

            if entry_path and entry_path.exists() and entry_path.is_file():
                try:
                    entry_data = load_json(entry_path)
                except (OSError, json.JSONDecodeError, ValueError):
                    kept_entries.append(entry)
                    continue

                source_projects = entry_data.get("source_projects")
                if isinstance(source_projects, list) and project_id in source_projects:
                    updated_projects = [pid for pid in source_projects if pid != project_id]
                    if updated_projects != source_projects:
                        entry_data["source_projects"] = updated_projects
                        write_json(entry_path, entry_data)
                        scrubbed_files += 1

            kept_entries.append(entry)

        manifest["entries"] = kept_entries
        write_json(KNOWLEDGE_MANIFEST_PATH, manifest)

    emit_progress(
        f"[engine] Knowledge purge for project '{project_id}' complete. "
        f"Removed {removed_entries} manifest entr{'y' if removed_entries == 1 else 'ies'}, "
        f"deleted {removed_files} file{'s' if removed_files != 1 else ''}, "
        f"scrubbed {scrubbed_files} shared file{'s' if scrubbed_files != 1 else ''}."
    )
    return 0
