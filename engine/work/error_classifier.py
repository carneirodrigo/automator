"""Error classification for agent execution failures."""

from __future__ import annotations


def classify_error(error_msg: str) -> str:
    """Classify an agent error into a category for orchestration handling."""
    lower = error_msg.lower()
    # Exclude shared-library (.so) warnings (e.g. libsecret on WSL2) — those are
    # recoverable and should not be classified as a missing binary.
    _is_so_warning = ".so" in lower and "cannot open shared object" in lower
    if not _is_so_warning and any(kw in lower for kw in ("not found", "no such file", "command not found", "enoent")):
        return "binary_not_found"
    if any(kw in lower for kw in ("timed out", "timeout")):
        return "timeout"
    if "prompt is too long" in lower:
        return "prompt_too_long"
    if any(kw in lower for kw in ("invalid json", "json", "parse")):
        return "invalid_output"
    if any(kw in lower for kw in ("permission denied", "eacces", "eperm", "access denied")):
        return "permission_denied"
    if any(kw in lower for kw in ("exit code",)):
        return "process_error"
    if any(kw in lower for kw in ("network unreachable", "dns resolution", "connection refused", "socket timeout")):
        return "environmental"
    if "already in use" in lower and "session" in lower:
        return "session_conflict"
    if any(kw in lower for kw in ("rate limit", "http 429", "too many requests", "error 429", "status 429", "code: 429", "code 429", "model_capacity_exhausted", "resource_exhausted", "quotaexceeded", "quota exceeded")):
        return "rate_limited"
    return "unknown"
