"""Prompt construction helpers and prompt-context builders."""

from __future__ import annotations

import collections
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from engine.work.repo_paths import REPO_ROOT, SKILLS_CATALOG_PATH, SKILLS_DIR
from engine.work.toon_adapter import serialize_for_prompt

_RESULT_SHAPES_MARKER = "## Result Shapes"

KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
KNOWLEDGE_MANIFEST_PATH = KNOWLEDGE_DIR / "manifest.json"
KNOWLEDGE_SOURCES_PATH = KNOWLEDGE_DIR / "sources.json"
_KB_CANDIDATE_CACHE: collections.OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = collections.OrderedDict()
_KB_CANDIDATE_CACHE_MAX = 128


def _load_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}


def minify_text(text: str) -> str:
    """Reduce prompt size by stripping decoration and redundant whitespace."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"^```\w*\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\n)(?<!\A)\*(.+?)\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n\s*\n", "\n", text)
    return "\n".join(line.strip() for line in text.splitlines())


def _strip_execution_prompt_template(text: str) -> str:
    marker = "## Execution Prompt Template"
    idx = text.find(marker)
    if idx == -1:
        return text
    return text[:idx].rstrip()


def _strip_sections(text: str, headers: list[str]) -> str:
    for header in headers:
        marker = f"## {header}"
        idx = text.find(marker)
        if idx == -1:
            continue
        next_heading = text.find("\n## ", idx + len(marker))
        if next_heading == -1:
            text = text[:idx].rstrip()
        else:
            text = text[:idx] + text[next_heading:]
    return text


def _build_project_inventory(project: dict[str, Any] | None) -> list[str]:
    if not project or not project.get("runtime_dir"):
        return []
    runtime_dir = Path(project["runtime_dir"])
    sections: list[str] = []

    task_state_path = runtime_dir / "state" / "active_task.json"
    if task_state_path.exists():
        task_state = _load_json(task_state_path)
        steps = task_state.get("completed_steps", [])
        rework = task_state.get("rework_loop_count", 0)
        pending = task_state.get("pending_resolution")
        step_summaries = []
        for step in steps:
            agent = step.get("agent", "?")
            status = step.get("status", "?")
            summary = step.get("summary", "")[:80]
            step_summaries.append(f"  - {agent}: {status} — {summary}")
        parts = ["Task State Summary:"]
        if step_summaries:
            parts.append(f"  Completed steps ({len(steps)}):")
            parts.extend(step_summaries[-10:])
            if len(steps) > 10:
                parts.insert(2, f"  ... ({len(steps) - 10} earlier steps omitted)")
        else:
            parts.append("  No completed steps yet.")
        parts.append(f"  Rework loop count: {rework}")
        if pending:
            parts.append(f"  Pending resolution: {pending.get('type', '?')} — {pending.get('message', '')[:100]}")
        sections.extend(parts)

    artifacts_dir = runtime_dir / "artifacts"
    if artifacts_dir.exists():
        artifact_files = sorted(artifacts_dir.glob("*_result_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        if artifact_files:
            sections.append("\nArtifact Inventory:")
            for artifact in artifact_files[:15]:
                try:
                    data = _load_json(artifact)
                    status = data.get("status", "?")
                    agent = data.get("agent", artifact.name.split("_result_")[0] if "_result_" in artifact.name else "?")
                    size_kb = artifact.stat().st_size / 1024
                    sections.append(f"  - {artifact.name}: agent={agent}, status={status}, size={size_kb:.0f}KB")
                except (json.JSONDecodeError, OSError):
                    sections.append(f"  - {artifact.name}: (unreadable)")

    inputs_dir = runtime_dir / "inputs"
    if inputs_dir.exists():
        input_files = [item for item in sorted(inputs_dir.iterdir()) if item.is_file() and item.name != "inputs_manifest.json"]
        if input_files:
            sections.append(f"\nInput Files ({len(input_files)}):")
            for input_file in input_files[:10]:
                size_kb = input_file.stat().st_size / 1024
                sections.append(f"  - {input_file.name}: {size_kb:.0f}KB")

    config_path = runtime_dir / "config.json"
    if config_path.exists():
        config = _load_json(config_path)
        description = config.get("description", "")
        if description:
            sections.append(f"\nProject Description: {description}")

    return sections


def summarize_directory_input(path: Path, project_root: Path | None = None, max_entries: int = 50) -> str:
    try:
        entries = []
        for item in sorted(path.rglob("*")):
            rel = item.relative_to(path)
            if ".git" in rel.parts:
                continue
            display = str(rel)
            if item.is_dir():
                display = f"{display}/"
            entries.append(display)
            if len(entries) >= max_entries:
                break
    except OSError as exc:
        return f"Directory input: {path}\nUnable to inspect directory contents: {exc}"

    label = f"{path} (project root)" if project_root and path == project_root else str(path)
    body = "\n".join(entries) if entries else "[empty directory]"
    suffix = "\n... [TRUNCATED]" if len(entries) >= max_entries else ""
    return f"Directory input: {label}\n{body}{suffix}"


def _sample_data_file(
    path: Path,
    *,
    data_file_extensions: set[str],
    max_input_file_size: int,
) -> str:
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return f"[Could not stat {path.name}]"

    size_label = f"{size_bytes / 1024:.0f}KB" if size_bytes < 1024 * 1024 else f"{size_bytes / (1024 * 1024):.1f}MB"
    if path.suffix in (".csv", ".tsv"):
        try:
            head_lines: list[str] = []
            total_lines = 0
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    total_lines += 1
                    if total_lines <= 21:
                        head_lines.append(line.rstrip("\n\r"))
            tail_lines: list[str] = []
            if total_lines > 25:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    tail_buffer = collections.deque(maxlen=5)
                    for line in handle:
                        tail_buffer.append(line.rstrip("\n\r"))
                    tail_lines = list(tail_buffer)
        except OSError as exc:
            return f"[Could not read {path.name}: {exc}]"

        header = head_lines[0] if head_lines else ""
        delimiter = "," if path.suffix == ".csv" else "\t"
        col_count = len(header.split(delimiter))
        sample_head = head_lines[1:21]
        parts = [
            f"Data file: {path.name} ({size_label}, {total_lines - 1} data rows, {col_count} columns)",
            f"Header: {header}",
            f"First {len(sample_head)} rows:",
            "\n".join(sample_head),
        ]
        if tail_lines:
            parts.append(f"Last {len(tail_lines)} rows:")
            parts.append("\n".join(tail_lines))
        if total_lines > 26:
            parts.append(f"... [{total_lines - 1 - len(sample_head) - len(tail_lines)} rows omitted]")
        parts.append("Use read_file capability to inspect the full file if needed.")
        return "\n".join(parts)

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            truncated = handle.read(max_input_file_size + 1)
    except OSError as exc:
        return f"[Could not read {path.name}: {exc}]"
    is_truncated = len(truncated) > max_input_file_size
    if is_truncated:
        truncated = truncated[:max_input_file_size]
    total_lines = truncated.count("\n") + (1 if truncated else 0)
    line_label = f"{total_lines}+" if is_truncated else str(total_lines)
    suffix = f"\n... [TRUNCATED at {max_input_file_size} bytes, total {size_label}]" if is_truncated else ""
    return f"Data file: {path.name} ({size_label}, {line_label} lines)\n{truncated}{suffix}"


def _summarize_input_file(
    path: Path,
    *,
    data_file_extensions: set[str],
    max_input_file_size: int,
) -> str:
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return f"Input {path.name}:\n[Could not stat file]"

    if path.suffix in data_file_extensions:
        return _sample_data_file(
            path,
            data_file_extensions=data_file_extensions,
            max_input_file_size=max_input_file_size,
        )

    _BINARY_EXTENSIONS = {".docx", ".xlsx", ".xls", ".pdf", ".doc", ".pptx", ".ppt", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".bin"}
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        size_label = f"{size_bytes / 1024:.0f}KB" if size_bytes < 1024 * 1024 else f"{size_bytes / (1024 * 1024):.1f}MB"
        return f"Input {path.name}: [binary file, {size_label} — use read_file capability to inspect if needed]"

    if size_bytes <= max_input_file_size:
        try:
            return f"Input {path.name}:\n{path.read_text(encoding='utf-8')}"
        except (OSError, UnicodeDecodeError) as exc:
            return f"Input {path.name}:\n[Could not read as text: {exc}]"

    size_label = f"{size_bytes / 1024:.0f}KB" if size_bytes < 1024 * 1024 else f"{size_bytes / (1024 * 1024):.1f}MB"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:max_input_file_size]
    except OSError as exc:
        return f"Input {path.name}:\n[Could not read: {exc}]"
    return f"Input {path.name} ({size_label}, truncated to {max_input_file_size // 1024}KB):\n{text}\n... [TRUNCATED]"


def _build_stage_summary(inputs: list[str]) -> list[str]:
    for input_path in inputs:
        path = Path(input_path)
        try:
            if not path.exists() or path.suffix != ".json" or "task" not in path.name:
                continue
        except OSError:
            continue
        try:
            state = _load_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        steps = state.get("completed_steps", [])
        if not steps:
            return []
        parts = []
        for step in steps:
            agent = step.get("agent", "?")
            status = step.get("status", "?")
            summary = step.get("summary", "")
            short = summary[:80] + "..." if len(summary) > 80 else summary
            parts.append(f"{agent}({status}: {short})" if short else f"{agent}({status})")
        return [f"\nPrior Stages: {' -> '.join(parts)}"]
    return []


def _append_skills_catalog_context(context: list[str]) -> None:
    if not SKILLS_CATALOG_PATH.exists():
        return
    try:
        catalog = _load_json(SKILLS_CATALOG_PATH)
        skills = catalog.get("skills", [])
        if not skills:
            return
        compact = [{"id": skill["id"], "desc": skill.get("description", ""), "roles": skill.get("roles", [])} for skill in skills]
        catalog_json = serialize_for_prompt(compact)
        if len(catalog_json) > 15_000:
            catalog_json = catalog_json[:15_000] + "\n... [TRUNCATED]"
        context.extend([
            "\nAgent Skills Catalog (vendor skills available for download — Anthropic, OpenAI, Google, Microsoft):",
            catalog_json,
            "If a skill looks relevant to the project, use `fetch_skill` capability to read its content and evaluate it.",
            "Only fetch skills whose description clearly matches the project scope — do not fetch speculatively.",
            "If after reading a skill it proves relevant, note its ID so you can reference it in your output.",
            "If not useful, discard it — do not mention it in your output.",
        ])
    except (json.JSONDecodeError, OSError):
        pass


def _parse_iso_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _manifest_cache_marker(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _kb_query_signature(task: str, reason: str, project_desc: str) -> tuple[str, ...]:
    query_text = f"{task} {reason} {project_desc}".lower()
    query_tokens = sorted(set(re.findall(r"[a-z0-9]{3,}", query_text)))
    return tuple(query_tokens[:32])


def _get_cached_kb_candidate_cards(cache_key: tuple[Any, ...]) -> list[dict[str, Any]] | None:
    cards = _KB_CANDIDATE_CACHE.get(cache_key)
    if cards is None:
        return None
    _KB_CANDIDATE_CACHE.move_to_end(cache_key)
    return [dict(card) for card in cards]


def _store_cached_kb_candidate_cards(cache_key: tuple[Any, ...], cards: list[dict[str, Any]]) -> None:
    _KB_CANDIDATE_CACHE[cache_key] = [dict(card) for card in cards]
    _KB_CANDIDATE_CACHE.move_to_end(cache_key)
    while len(_KB_CANDIDATE_CACHE) > _KB_CANDIDATE_CACHE_MAX:
        _KB_CANDIDATE_CACHE.popitem(last=False)


def _build_kb_candidate_cards(
    manifest: dict[str, Any],
    *,
    task: str,
    reason: str,
    project_desc: str,
    limit: int = 10,
    offset: int = 0,
    exclude_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    query_signature = _kb_query_signature(task, reason, project_desc)
    if not query_signature:
        return []
    exclude_key = tuple(sorted(exclude_ids or set()))
    cache_key = ("kb-candidates", _manifest_cache_marker(KNOWLEDGE_MANIFEST_PATH), limit, offset, exclude_key, query_signature)
    cached = _get_cached_kb_candidate_cards(cache_key)
    if cached is not None:
        return cached

    query_tokens = set(query_signature)
    excluded = {item for item in (exclude_ids or set()) if item}

    now = dt.datetime.now(dt.timezone.utc)
    candidates: list[tuple[float, dict[str, Any], list[str]]] = []
    for entry in manifest.get("entries", []):
        title = str(entry.get("title", ""))
        summary = str(entry.get("summary", ""))
        entry_id = str(entry.get("id", ""))
        if entry_id in excluded:
            continue
        source_family = str(entry.get("source_family", ""))
        coverage_type = str(entry.get("coverage_type", ""))
        tags = [str(tag) for tag in entry.get("tags", []) if str(tag).strip()]
        haystack = " ".join([title.lower(), summary.lower(), entry_id.lower(), source_family.lower(), coverage_type.lower(), *[tag.lower() for tag in tags]])
        haystack_tokens = set(re.findall(r"[a-z0-9]{3,}", haystack))
        overlap = query_tokens & haystack_tokens
        if not overlap:
            continue

        score = float(len(overlap))
        reasons: list[str] = []

        if title:
            title_tokens = set(re.findall(r"[a-z0-9]{3,}", title.lower()))
            title_overlap = query_tokens & title_tokens
            if title_overlap:
                score += len(title_overlap) * 1.5
                reasons.append(f"title matched: {', '.join(sorted(title_overlap)[:3])}")

        if tags:
            tag_overlap = sorted(query_tokens & {tag.lower() for tag in tags})
            if tag_overlap:
                score += len(tag_overlap) * 1.25
                reasons.append(f"tag overlap: {', '.join(tag_overlap[:4])}")

        lower_title = title.lower()
        if "playbook" in lower_title or any(str(tag).lower() == "playbook" for tag in tags):
            score += 1.0
            reasons.append("playbook")

        fresh_until = _parse_iso_datetime(str(entry.get("fresh_until", "")))
        last_verified = _parse_iso_datetime(str(entry.get("last_verified", "")))
        if fresh_until and fresh_until >= now:
            score += 0.75
            reasons.append("fresh")
        elif last_verified and (now - last_verified).days <= 30:
            score += 0.35
            reasons.append("recently verified")

        card = {
            "id": entry.get("id", ""),
            "file": entry.get("file", ""),
            "title": title,
            "summary": summary,
            "tags": tags[:8],
            "source_family": source_family,
            "coverage_type": coverage_type,
            "last_verified": entry.get("last_verified", ""),
            "fresh_until": entry.get("fresh_until", ""),
            "match_reason": reasons[:3] or ["keyword overlap"],
        }
        candidates.append((score, card, reasons))

    if not candidates:
        return []

    candidates.sort(key=lambda item: (-item[0], str(item[1].get("id", ""))))
    selected = [card for _, card, _ in candidates[offset:offset + limit]]
    _store_cached_kb_candidate_cards(cache_key, selected)
    return [dict(card) for card in selected]


def get_kb_candidate_batch(
    *,
    task: str,
    reason: str = "",
    project_desc: str = "",
    limit: int = 10,
    offset: int = 0,
    exclude_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not KNOWLEDGE_MANIFEST_PATH.exists():
        return {
            "manifest_available": False,
            "manifest_path": str(KNOWLEDGE_MANIFEST_PATH),
            "total_entries": 0,
            "returned": 0,
            "offset": max(0, int(offset)),
            "next_offset": max(0, int(offset)),
            "exhausted": True,
            "candidates": [],
        }
    try:
        manifest = _load_json(KNOWLEDGE_MANIFEST_PATH)
    except (json.JSONDecodeError, OSError):
        return {
            "manifest_available": False,
            "manifest_path": str(KNOWLEDGE_MANIFEST_PATH),
            "total_entries": 0,
            "returned": 0,
            "offset": max(0, int(offset)),
            "next_offset": max(0, int(offset)),
            "exhausted": True,
            "candidates": [],
            "issues": ["Knowledge manifest could not be parsed."],
        }
    entries = manifest.get("entries", [])
    normalized_limit = max(1, min(int(limit), 20))
    normalized_offset = max(0, int(offset))
    normalized_exclude = [str(item) for item in (exclude_ids or []) if str(item).strip()]
    cards = _build_kb_candidate_cards(
        manifest,
        task=task,
        reason=reason,
        project_desc=project_desc,
        limit=normalized_limit,
        offset=normalized_offset,
        exclude_ids=set(normalized_exclude),
    )
    return {
        "manifest_available": True,
        "manifest_path": str(KNOWLEDGE_MANIFEST_PATH),
        "total_entries": len(entries),
        "returned": len(cards),
        "offset": normalized_offset,
        "next_offset": normalized_offset + len(cards),
        "exhausted": len(cards) < normalized_limit,
        "candidates": cards,
    }


def _build_knowledge_context(role: str, task: str = "", reason: str = "", project_desc: str = "") -> list[str]:
    # research gets cards + sources catalog (needs external discovery guidance)
    # worker gets cards only (just needs to know what to fetch)
    if role not in ("worker", "research"):
        return []

    context: list[str] = []

    # Sources catalog only for research — worker doesn't need external source routing
    if role == "research" and KNOWLEDGE_SOURCES_PATH.exists():
        try:
            sources_catalog = _load_json(KNOWLEDGE_SOURCES_PATH)
            sources_json = serialize_for_prompt(sources_catalog)
            if len(sources_json) > 35_000:
                sources_json = sources_json[:35_000] + "\n... [TRUNCATED]"
            context.extend([
                "\nLocal Knowledge Source Catalog (use this to choose authoritative vendor/platform docs before open-ended search):",
                sources_json,
                "Use this as a discovery aid, not an answer by itself. Prefer matching source families first, then load only the relevant knowledge entries from the manifest.",
            ])
        except (json.JSONDecodeError, OSError):
            context.append("\nLocal Knowledge Source Catalog: Could not be parsed. Fall back to manifest lookup and live research.")

    if not KNOWLEDGE_MANIFEST_PATH.exists():
        return context

    try:
        manifest = _load_json(KNOWLEDGE_MANIFEST_PATH)
        entries = manifest.get("entries", [])
        if not entries:
            return context

        candidate_cards = _build_kb_candidate_cards(
            manifest,
            task=task,
            reason=reason,
            project_desc=project_desc,
            limit=10,
        )
        candidate_json = serialize_for_prompt({
            "total_entries": len(entries),
            "shortlist_size": len(candidate_cards),
            "candidates": candidate_cards,
        })

        if role == "worker":
            context.extend([
                "\nLocal Knowledge Base — relevant entries for this task (compact cards; fetch full entry on demand):",
                candidate_json,
                f"Knowledge directory: {KNOWLEDGE_DIR}",
                "Use read_file to load the full content of any entry by its file path (e.g., knowledge/<file>.json).",
                "If the shortlist has no matches, the KB may not cover this task — proceed without it.",
                "If an entry has stale freshness metadata, treat it as a lead and verify before relying on it.",
            ])
        else:  # research
            context.extend([
                "\nLocal Knowledge Base Candidate Cards (compact shortlist; open full entries on demand):",
                candidate_json,
                f"Knowledge directory: {KNOWLEDGE_DIR}",
                f"Manifest path: {KNOWLEDGE_MANIFEST_PATH}",
                "Check local KB first. Use read_file to load entries you recognise as relevant. Do NOT load all entries up front.",
                "Use get_kb_candidates to request another batch if the shortlist is insufficient before going external.",
                "Only move to external discovery after local retrieval is exhausted or clearly insufficient.",
                "If an entry has stale freshness metadata, treat it as a lead and re-verify against live authoritative sources.",
            ])
        return context
    except (json.JSONDecodeError, OSError):
        return context




def _default_role_skill_ids(role: str, manifest: dict[str, Any], inputs: list[str]) -> list[str]:
    """Return skill IDs to auto-inject for the worker role based on task/input signals."""
    if role != "worker":
        return []

    available_ids = {str(entry.get("id", "")) for entry in manifest.get("skills", [])}

    # Scan inputs for document-type signals (task text + input file paths)
    haystack = " ".join(inputs or []).lower()
    hints: set[str] = set()
    if any(kw in haystack for kw in (".docx", "docx", "word document", "word doc", ".doc")):
        hints.add("docx")
    if any(kw in haystack for kw in (".xlsx", "xlsx", "spreadsheet", "excel")):
        hints.add("xlsx")
    if any(kw in haystack for kw in (".pdf", "pdf")):
        hints.add("pdf")

    if not hints:
        return []

    preferred_by_type = {
        "docx": ("anthropic--docx", "openai--doc"),
        "xlsx": ("anthropic--xlsx", "openai--spreadsheet"),
        "pdf":  ("anthropic--pdf", "openai--pdf"),
    }
    selected: list[str] = []
    for artifact_type in ("docx", "xlsx", "pdf"):
        if artifact_type not in hints:
            continue
        for skill_id in preferred_by_type[artifact_type]:
            if skill_id in available_ids and skill_id not in selected:
                selected.append(skill_id)
                break
    return selected


def _build_skills_context(role: str, inputs: list[str], *, estimate_tokens: Any) -> list[str]:
    from engine.work.skill_loader import fetch_skill as loader_fetch_skill
    from engine.work.skill_loader import is_skill_stale, load_skill_body, load_skills_manifest

    manifest = load_skills_manifest()
    manifest_by_id = {entry.get("id", ""): entry for entry in manifest.get("skills", [])}
    combined_skill_ids: list[str] = []
    for skill_id in _default_role_skill_ids(role, manifest, inputs or []):
        if skill_id and skill_id not in combined_skill_ids:
            combined_skill_ids.append(skill_id)

    if not combined_skill_ids:
        return []

    sections: list[str] = []
    total_tokens = 0
    max_skill_tokens = 8000

    for skill_id in combined_skill_ids:
        entry = manifest_by_id.get(skill_id)
        if not entry:
            continue
        skill_path = SKILLS_DIR / entry.get("path", "")
        if not skill_path.exists():
            continue
        if is_skill_stale(entry):
            loader_fetch_skill(entry["id"])
        body = load_skill_body(skill_path)
        if not body:
            continue
        est = estimate_tokens(body)
        if total_tokens + est > max_skill_tokens:
            break
        total_tokens += est
        skill_name = entry.get("name", entry.get("id", "unknown"))
        sections.append(f"\n### Agent Skill: {skill_name}\n{body}")

    if not sections:
        return []
    return ["\nMatched Agent Skills (curated vendor instructions relevant to this task):", *sections]


