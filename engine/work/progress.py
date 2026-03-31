"""Compact progress message helpers for orchestration runs."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

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


def stage_done_message(role: str, summary: str | None = None) -> str:
    # Show the agent's own summary in full — truncating it mid-sentence is worse
    # than showing nothing. Collapse whitespace but do not cut.
    if not summary:
        return f"{role}: done"
    collapsed = _WHITESPACE_RE.sub(" ", summary).strip()
    if not collapsed or collapsed.lower() == "no summary provided.":
        return f"{role}: done"
    return f"{role}: {collapsed}"


def capability_message(role: str, capability: str) -> str:
    capability_name = summarize_text(capability, max_len=32) or "capability"
    return f"{role}: requested {capability_name}"


def next_stage_message(role: str) -> str:
    return f"engine: running {role} next"


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


def run_summary_message(task_state: dict[str, Any], mode: str, *, max_stages: int = 12) -> str:
    """Build a compact end-of-run summary block."""
    _OUTCOME_LABELS = {
        "complete": "complete — awaiting your response",
        "blocked": "blocked — engine could not proceed",
        "needs_clarification": "paused — clarification needed",
        "rework_limit": "stopped — rework loop limit reached",
        "manual": "stopped — manual mode",
        "error": "stopped — unrecoverable error",
    }
    outcome = _OUTCOME_LABELS.get(mode, mode)

    completed = task_state.get("completed_steps", [])
    visible = completed

    lines = [
        "[engine] ── Run summary ──────────────────────────────────────",
        f"[engine]  Outcome : {outcome}",
    ]

    if visible:
        display = visible[-max_stages:] if len(visible) > max_stages else visible
        stage_parts = []
        for s in display:
            agent = s.get("agent", "?")
            status = s.get("status", "success")
            stage_parts.append(f"{agent}[FAIL]" if status in ("failed", "blocked") else agent)
        prefix = f"...+{len(visible) - max_stages} earlier... → " if len(visible) > max_stages else ""
        lines.append(f"[engine]  Stages  : {prefix}{' → '.join(stage_parts)}")

    rework = task_state.get("rework_loop_count", 0)
    if rework:
        lines.append(f"[engine]  Rework  : {rework} {'cycle' if rework == 1 else 'cycles'}")

    lines.append("[engine] ─────────────────────────────────────────────────────")
    return "\n".join(lines)


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
