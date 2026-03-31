"""Agent session model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentSession:
    """Carries session-resume state for a single agent invocation.

    mode: how the backend supports session resumption.
        "none"             — no resume; each invocation is stateless
        "gemini_resume"    — pass --resume <id> to the Gemini CLI
        "claude_session_id"— pass --session-id <id> to the Claude CLI
        "codex_portable"   — Codex thread.started ID; not passed back (portable only)
    conversation_id: session/thread ID extracted from the previous run's output.
        Set once on the first turn. Passed to subsequent invocations via CLI flags.
    persistent: whether the session should attempt to resume across turns.
    """

    mode: str = "none"
    conversation_id: str | None = None
    persistent: bool = False
