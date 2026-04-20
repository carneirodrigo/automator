"""General runtime helper functions shared by the engine entrypoint."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Compiled regex constants — built once at import, reused across calls.
_RE_NEW_PROJECT = re.compile(
    r"\b(start|create|bootstrap|init(?:ialize)?)\s+(?:a\s+)?new\s+project\b",
    re.IGNORECASE,
)
_RE_PROJECT_NAMED = re.compile(
    r"\b(start|create|bootstrap|init(?:ialize)?)\s+project\s+named\b",
    re.IGNORECASE,
)
_RE_FORK = re.compile(r"\bfork\b", re.IGNORECASE)
_RE_ACTION_VERB = re.compile(
    r"^\s*(create|build|implement|write|generate|develop|start|make)\b",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_runtime_network_block(agent_bin: str | None = None) -> str | None:
    """Return a remediation message when the outer runtime has disabled network."""
    # Intentionally strict: the Codex launcher always sets exactly "1".
    # "true" / "yes" are not expected from that environment.
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") != "1":
        return None
    selected = agent_bin or "the selected AI backend"
    return (
        "Error: the outer runtime disabled network access for spawned AI CLIs. "
        f"{selected} will not be able to reach provider endpoints from this session.\n"
        "Detected environment: CODEX_SANDBOX_NETWORK_DISABLED=1\n"
        "Required shared fix: change the parent Codex launcher/harness to keep file sandboxing "
        "but enable outbound network access.\n"
        "Expected policy: filesystem sandbox enabled, network sandbox enabled.\n"
        "Do not rely on per-user shell wrappers for shared setups; they do not override an outer "
        "sandbox that already forced network off."
    )


def runtime_check_output_has_success(
    payload: dict[str, Any],
    full_text: str,
    *,
    extract_json_payload: Callable[[str], dict[str, Any]],
) -> bool:
    # Last-resort string fallback: catches "ok":true in raw text when the outer
    # payload parse failed. Compact form handles compact JSON; spaced form handles
    # pretty-printed JSON (e.g. "ok": true with a space after the colon).
    compact = full_text.replace(" ", "")
    if (
        payload.get("ok") is True
        or '"ok":true' in compact
        or '"ok": true' in full_text
        or '\\"ok\\":true' in compact
    ):
        return True
    for key in ("response", "result", "text"):
        value = payload.get(key)
        if isinstance(value, str):
            inner = extract_json_payload(value)
            if inner.get("ok") is True:
                return True
    for line in full_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item")
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and extract_json_payload(text).get("ok") is True:
                return True
        result = event.get("result")
        if isinstance(result, str) and extract_json_payload(result).get("ok") is True:
            return True
    return False


def count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def is_known_feedback(request: str) -> bool:
    """Return True if the request looks like acceptance OR rejection feedback.

    Identifies short responses to a delivered result — does not distinguish
    between accept and reject. Requests over 20 words are treated as new work
    regardless of matching phrases.
    """
    user_response = request.lower().strip()
    acceptance_phrases = ("yes", "approved", "looks good", "accept", "lgtm", "ok", "correct", "done", "ship it")
    rejection_phrases = ("no", "reject", "wrong", "incorrect", "fix", "rework", "redo", "not what")
    # Requests over 20 words are too long to be simple feedback — treat as new work.
    if count_words(user_response) > 20:
        return False
    is_rejected = any(re.search(r"\b" + re.escape(phrase) + r"\b", user_response) for phrase in rejection_phrases)
    is_accepted = any(re.search(r"\b" + re.escape(phrase) + r"\b", user_response) for phrase in acceptance_phrases)
    return is_rejected or is_accepted


def looks_like_explicit_new_project_request(request: str) -> bool:
    return bool(
        _RE_NEW_PROJECT.search(request)
        or _RE_PROJECT_NAMED.search(request)
        or _RE_FORK.search(request)
    )


def looks_like_task_shaped_new_work_request(request: str) -> bool:
    """Return True for action-verb-led requests with 8+ words.

    These are considered clearly task-shaped. Shorter requests (3-7 words)
    are handled by the fallback in looks_like_new_work_request.
    """
    return bool(count_words(request) >= 8 and _RE_ACTION_VERB.search(request))


def looks_like_new_work_request(request: str) -> bool:
    return bool(
        looks_like_explicit_new_project_request(request)
        or looks_like_task_shaped_new_work_request(request)
        # Fallback: action-verb-led requests with 3-7 words are ambiguous but lean new-work.
        or (count_words(request) >= 3 and _RE_ACTION_VERB.search(request))
    )


def should_ignore_cached_project_for_new_request(
    pending_resolution: dict[str, Any] | None,
    request: str,
) -> bool:
    if not pending_resolution:
        return False
    if is_known_feedback(request):
        return False
    # A project with pending acceptance is never auto-attached to unrelated requests.
    # The user must explicitly target it with --project continue --id <id>.
    return True


def resolve_active_project(
    request: str,
    projects: list[dict[str, Any]],
    *,
    allow_registry_fallback: bool,
    load_json: Callable[[Path], Any],
    registry_path: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    if looks_like_explicit_new_project_request(request) and not _RE_FORK.search(request):
        return None, None

    haystack = f" {request.lower()} "
    matches = []
    for project in projects:
        names = [project.get("project_name", ""), project.get("project_id", "")] + project.get("aliases", [])
        if any(name and f" {name.lower()} " in haystack for name in names):
            matches.append(project)

    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"Multiple projects match: {', '.join(project['project_name'] for project in matches)}"
    if not allow_registry_fallback:
        return None, None
    if looks_like_new_work_request(request):
        return None, None
    registry = load_json(registry_path)
    cached = registry.get("last_active_project")
    if cached and Path(cached.get("project_root", "")).exists():
        return cached, None
    return None, None


def extract_session_id_from_text(text: str, *, extract_json_payload: Callable[[str], dict[str, Any]]) -> str | None:
    """Extract a backend session identifier from CLI output text."""
    if not text:
        return None

    payload = extract_json_payload(text)
    if payload:
        for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        item = event.get("item")
        if isinstance(item, dict):
            for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None
