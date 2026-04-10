"""Compact progress message helpers for orchestration runs."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_WHITESPACE_RE = re.compile(r"\s+")


def summarize_text(text: str, *, max_len: int = 72) -> str:
    """Normalize free text into a short single-line summary."""
    if not isinstance(text, str):
        return ""
    collapsed = _WHITESPACE_RE.sub(" ", text).strip()
    if not collapsed:
        return ""
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 3].rstrip() + "..."


def stage_start_message(role: str, task: str, *, prompt_tokens: int = 0) -> str:
    # Surface per-stage token cost for multi-agent runs (skill: multi-agent-patterns —
    # systems cost ~15x baseline; teams consistently underbudget without visibility).
    token_hint = f" (~{prompt_tokens:,} tokens)" if prompt_tokens > 1000 else ""
    return f"{role}: starting{token_hint}"


def capability_message(role: str, capability: str) -> str:
    capability_name = summarize_text(capability, max_len=32) or "capability"
    return f"{role}: requested {capability_name}"


def elapsed_label(elapsed_seconds: float) -> str:
    total_seconds = max(int(elapsed_seconds), 0)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {seconds:02d}s"


def heartbeat_message(role: str, started_at: datetime, *, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    elapsed_seconds = (current - started_at).total_seconds()
    label = elapsed_label(elapsed_seconds)
    if elapsed_seconds >= 600:
        return f"{role}: still running ({label}) — taking unusually long, press Ctrl+C if you think it is stuck"
    if elapsed_seconds >= 120:
        return f"{role}: still running ({label}) — working, please wait"
    return f"{role}: still running ({label})"


def should_emit_heartbeat(
    started_at: datetime,
    last_heartbeat_at: datetime | None,
    *,
    now: datetime | None = None,
    initial_delay_seconds: int = 30,
    repeat_interval_seconds: int = 30,
) -> bool:
    current = now or datetime.now(timezone.utc)
    elapsed = (current - started_at).total_seconds()
    if elapsed < initial_delay_seconds:
        return False
    if last_heartbeat_at is None:
        return True
    return (current - last_heartbeat_at).total_seconds() >= repeat_interval_seconds
