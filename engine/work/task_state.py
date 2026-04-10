"""Schema types for the active_task.json state file."""

from __future__ import annotations

from typing import Any, Optional
from typing_extensions import TypedDict, NotRequired


class PendingResolution(TypedDict):
    """Outcome requiring user action before the project can be closed."""
    type: str               # "user_acceptance" | "review_blocked" | "user_input_required"
    message: str
    original_request: str


class CompletedStep(TypedDict):
    """Single pipeline step recorded in the state timeline."""
    agent: str              # "worker" | "review" | "research"
    status: str             # "success" | "fail"
    artifact: NotRequired[str | None]
    summary: NotRequired[str]
    timestamp: str


class TaskState(TypedDict, total=False):
    """Schema for projects/<id>/runtime/state/active_task.json.

    All fields are optional because the file may not exist yet or may be
    partially written. Callers should use .get() with a default or rely on
    the engine writing a complete state before reading a field.
    """
    user_request: str
    last_updated: str                          # ISO-8601 UTC
    completed_steps: list[CompletedStep]
    completed_stages: list[str]                # stages that completed successfully (for resume)
    plan: list[str]                            # planning step output (ordered implementation steps)
    pruned_environmental_steps: list[Any]
    artifacts: list[str]
    rework_loop_count: int
    pending_resolution: Optional[PendingResolution]
