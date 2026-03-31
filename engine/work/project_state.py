"""Project/runtime state helpers extracted from the engine entrypoint."""

from __future__ import annotations

import csv
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def sync_registry_csv(
    *,
    load_json: Callable[[Path], Any],
    registry_path: Path,
    registry_csv_path: Path,
) -> None:
    registry = load_json(registry_path)
    projects = registry.get("projects", [])
    with registry_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["project_id", "project_name", "aliases", "description"])
        for project in projects:
            aliases = ",".join(project.get("aliases", []))
            writer.writerow(
                [
                    project.get("project_id", ""),
                    project.get("project_name", ""),
                    aliases,
                    project.get("description", ""),
                ]
            )


def bootstrap_project(
    decision: dict[str, Any],
    *,
    repo_root: Path,
    projects_dir: Path,
    runtime_projects_dir: Path,
    state_template_path: Path,
    config_template_path: Path,
    registry_path: Path,
    load_json: Callable[[Path], Any],
    write_json: Callable[[Path, Any], None],
    sync_registry_csv: Callable[[], None],
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    project_id = decision["project_id"]
    project_name = decision["project_name"]
    project_home = projects_dir / project_id
    project_root = project_home / "delivery"
    runtime_dir = project_home / "runtime"
    secrets_dir = project_home / "secrets"
    rel_root = str(project_root.relative_to(repo_root))

    emit_progress(f"[engine] Bootstrapping project: {project_name} at {rel_root}")

    project_home.mkdir(parents=True, exist_ok=True)
    project_root.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (runtime_dir / "memory").mkdir(parents=True, exist_ok=True)
    (runtime_dir / "state").mkdir(parents=True, exist_ok=True)
    (runtime_dir / "inputs").mkdir(parents=True, exist_ok=True)
    secrets_dir.mkdir(parents=True, exist_ok=True)

    if state_template_path.exists():
        state = load_json(state_template_path)
        state["task_id"] = f"{project_id}-init"
        write_json(runtime_dir / "state" / "active_task.json", state)

    if config_template_path.exists():
        config = load_json(config_template_path)
        config["project_id"] = project_id
        config["project_name"] = project_name
        config["project_root"] = str(project_root)
        config["runtime_dir"] = str(runtime_dir)
        config["description"] = decision.get("description", "")
        config["deliverables_dir"] = str(project_root)
        write_json(runtime_dir / "config.json", config)

    registry = load_json(registry_path)
    if not isinstance(registry.get("projects"), list):
        registry["projects"] = []

    new_entry = {
        "project_id": project_id,
        "project_name": project_name,
        "project_home": str(project_home),
        "project_root": str(project_root),
        "runtime_dir": str(runtime_dir),
        "description": decision.get("description", ""),
    }

    exists = False
    for idx, project in enumerate(registry["projects"]):
        if project["project_id"] == project_id:
            registry["projects"][idx] = new_entry
            exists = True
            break
    if not exists:
        registry["projects"].append(new_entry)

    write_json(registry_path, registry)
    sync_registry_csv()
    return new_entry


def fork_project(
    decision: dict[str, Any],
    *,
    projects_dir: Path,
    runtime_projects_dir: Path,
    registry_path: Path,
    load_json: Callable[[Path], Any],
    write_json: Callable[[Path, Any], None],
    bootstrap_project: Callable[[dict[str, Any]], dict[str, Any]],
    emit_progress: Callable[[str], None],
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    source_project_id = decision["source_project_id"]
    project_id = decision["project_id"]
    inherit_roles = decision.get("inherit_artifacts", [])

    registry = load_json(registry_path)
    source = None
    for project in registry.get("projects", []):
        if project["project_id"] == source_project_id:
            source = project
            break
    if not source:
        raise KeyError(f"Source project not found: {source_project_id}")

    new_entry = bootstrap_project(decision)
    runtime_dir = Path(new_entry["runtime_dir"])
    source_artifacts_dir = Path(source["runtime_dir"]) / "artifacts"
    new_artifacts_dir = runtime_dir / "artifacts"
    inherited_steps = []

    for role in inherit_roles:
        latest_path = source_artifacts_dir / f"latest_{role}.json"
        if not latest_path.exists():
            emit_progress(f"[engine] Fork: no artifact found for '{role}' in source project — skipping.")
            continue

        data = load_json(latest_path)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        new_artifact_path = new_artifacts_dir / f"{role}_inherited_{ts}.json"
        data["_inherited_from"] = {
            "source_project_id": source_project_id,
            "source_project_name": source.get("project_name", ""),
            "original_role": role,
        }
        write_json(new_artifact_path, data)

        inherited_steps.append(
            {
                "agent": role,
                "timestamp": now_iso(),
                # "unknown" avoids silently promoting artifacts that lack status metadata.
                "status": data.get("status", "unknown"),
                "summary": f"Inherited from {source_project_id}: {data.get('summary', 'no summary')}"[:300],
                "artifact": str(new_artifact_path),
                "artifact_size_bytes": new_artifact_path.stat().st_size,
                "inherited": True,
            }
        )
        emit_progress(f"[engine] Fork: inherited {role} artifact from {source_project_id}.")

    state_path = runtime_dir / "state" / "active_task.json"
    state = load_json(state_path)
    state["completed_steps"] = inherited_steps
    state["artifacts"] = [step["artifact"] for step in inherited_steps]
    state["forked_from"] = source_project_id
    write_json(state_path, state)

    emit_progress(f"[engine] Fork complete: {len(inherited_steps)} artifact(s) inherited from {source_project_id}.")
    return new_entry


def detect_fork_intent(
    request: str,
    projects: list[dict[str, Any]],
    *,
    resolve_active_project: Callable[[str, list[dict[str, Any]]], tuple[dict[str, Any] | None, str | None]],
) -> dict[str, Any] | None:
    if not re.search(r"\bfork\b", request, re.IGNORECASE):
        return None
    source_project, _ = resolve_active_project(request, projects)
    if not source_project:
        return None
    return {
        "source_project_id": source_project["project_id"],
        "source_project_name": source_project["project_name"],
        "inherit_artifacts": ["worker"],
    }


def save_last_active_project(
    project: dict[str, Any] | None,
    *,
    load_json: Callable[[Path], Any],
    write_json: Callable[[Path, Any], None],
    registry_path: Path,
) -> None:
    registry = load_json(registry_path)
    registry["last_active_project"] = project
    write_json(registry_path, registry)


def secrets_path(project_id: str, *, secrets_projects_dir: Path) -> Path:
    # Two supported layouts:
    #   Canonical: projects/<id>/secrets/secrets.json  (current)
    #   Legacy:    projects/secrets/<id>/secrets.json  (old layout, pre-migration)
    # secrets_projects_dir may be the top-level projects/ dir OR the nested secrets/ dir
    # depending on caller context.  Normalise to projects/ either way.
    projects_dir = secrets_projects_dir.parent if secrets_projects_dir.name == "secrets" else secrets_projects_dir
    canonical = projects_dir / project_id / "secrets" / "secrets.json"
    legacy = secrets_projects_dir / project_id / "secrets.json"
    if legacy.exists() and not canonical.exists():
        return legacy
    return canonical


def store_secrets(
    project_id: str,
    entries: list[dict[str, Any]],
    *,
    secrets_projects_dir: Path,
    load_json: Callable[[Path], Any],
    write_json: Callable[[Path, Any], None],
    now_iso: Callable[[], str],
    source: str,
) -> None:
    path = secrets_path(project_id, secrets_projects_dir=secrets_projects_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = load_json(path) if path.exists() else {"entries": []}
    if "entries" not in store:
        store["entries"] = []
    for entry in entries:
        entry["source"] = source
        entry["created_at"] = now_iso()
        updated = False
        for existing in store["entries"]:
            if existing.get("key") == entry["key"]:
                existing.update(entry)
                updated = True
                break
        if not updated:
            store["entries"].append(entry)
    write_json(path, store)


def load_secrets(
    project_id: str,
    *,
    secrets_projects_dir: Path,
    load_json: Callable[[Path], Any],
    keys: list[str] | None,
) -> dict[str, Any]:
    path = secrets_path(project_id, secrets_projects_dir=secrets_projects_dir)
    if not path.exists():
        return {"entries": []}
    store = load_json(path)
    if "entries" not in store:
        store["entries"] = []
    if keys:
        store["entries"] = [entry for entry in store["entries"] if entry.get("key") in keys]
    return store


def get_project_secret_values(
    project_id: str,
    *,
    load_secrets: Callable[[str], dict[str, Any]],
) -> list[tuple[str, str]]:
    store = load_secrets(project_id)
    return [
        (entry["key"], entry["value"])
        for entry in store.get("entries", [])
        if entry.get("key") and entry.get("value")
    ]


def is_binary_file(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" in chunk
    except OSError as exc:
        print(f"[project_state] is_binary_file: cannot read {path}: {exc}", file=sys.stderr)
        return True


def _extract_structured_secret_entries(content: str) -> list[dict[str, Any]]:
    """Parse known structured secret-file shapes and return normalized entries.

    This prevents regex scanning from mis-identifying nearby metadata fields
    (for example a `source` value) as the actual secret value in secret export
    files that already have explicit `entries[{key,value,type}]` objects.
    """
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return []

    if not isinstance(payload, dict):
        return []

    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []

    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        value = entry.get("value")
        stype = entry.get("type")
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(value, str) or not value:
            continue
        normalized_entry = {
            "key": key,
            "value": value,
        }
        if isinstance(stype, str) and stype:
            normalized_entry["type"] = stype
        label = entry.get("label")
        if isinstance(label, str) and label:
            normalized_entry["label"] = label
        normalized.append(normalized_entry)
    return normalized


def ingest_input_files(
    project_id: str,
    *,
    inputs_dir: Path,
    projects_dir: Path,
    runtime_projects_dir: Path,
    detect_secrets: Callable[[str], list[dict[str, Any]]],
    store_secrets: Callable[[str, list[dict[str, Any]], str], None],
    is_binary_file: Callable[[Path], bool],
) -> list[str]:
    if not inputs_dir.exists():
        return []
    inbox_files = [file for file in sorted(inputs_dir.iterdir()) if file.is_file()]
    if not inbox_files:
        return []
    canonical_dir = projects_dir / project_id / "runtime" / "inputs"
    legacy_dir = runtime_projects_dir / project_id / "inputs"
    dest_dir = legacy_dir if legacy_dir.exists() and not canonical_dir.exists() else canonical_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    text_paths: list[str] = []
    manifest_entries: list[dict[str, Any]] = []
    for file in inbox_files:
        binary = is_binary_file(file)
        secrets_found = 0
        if not binary:
            try:
                content = file.read_text(encoding="utf-8", errors="replace")
                detected = _extract_structured_secret_entries(content)
                if not detected:
                    detected = detect_secrets(content)
                if detected:
                    store_secrets(project_id, detected, f"input_file:{file.name}")
                    secrets_found = len(detected)
            except OSError:
                pass  # secret detection is best-effort; proceed without scanning if file is unreadable
        dest = dest_dir / file.name
        shutil.move(str(file), str(dest))
        manifest_entries.append(
            {
                "original_name": file.name,
                "type": "binary" if binary else "text",
                "size_bytes": dest.stat().st_size,
                "secrets_detected": secrets_found,
            }
        )
        if not binary:
            text_paths.append(str(dest))
    manifest = {
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "files": manifest_entries,
    }
    (dest_dir / "inputs_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return text_paths


def get_project_input_paths(
    project_id: str,
    *,
    projects_dir: Path,
    runtime_projects_dir: Path,
    is_binary_file: Callable[[Path], bool],
) -> list[str]:
    canonical_dir = projects_dir / project_id / "runtime" / "inputs"
    legacy_dir = runtime_projects_dir / project_id / "inputs"
    inputs_dir = legacy_dir if legacy_dir.exists() and not canonical_dir.exists() else canonical_dir
    if not inputs_dir.exists():
        return []
    return [
        str(file)
        for file in sorted(inputs_dir.iterdir())
        if file.is_file() and file.name != "inputs_manifest.json" and not is_binary_file(file)
    ]


def infer_project_id_from_path(
    path: Path,
    *,
    projects_dir: Path,
    delivery_dir: Path,
    runtime_projects_dir: Path,
) -> str | None:
    try:
        resolved = path.resolve()
        projects = projects_dir.resolve()
        delivery = delivery_dir.resolve()
        runtime_projects = runtime_projects_dir.resolve()
        try:
            rel = resolved.relative_to(projects)
            if len(rel.parts) >= 2 and rel.parts[1] in {"delivery", "runtime", "secrets"}:
                return rel.parts[0]
        except ValueError:
            pass
        try:
            rel = resolved.relative_to(delivery)
            return rel.parts[0] if rel.parts else None
        except ValueError:
            pass
        try:
            rel = resolved.relative_to(runtime_projects)
            return rel.parts[0] if rel.parts else None
        except ValueError:
            pass
    except (OSError, ValueError):
        pass
    return None
