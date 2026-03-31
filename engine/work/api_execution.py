"""API execution path for vendor backends (Anthropic, Google, OpenAI)."""

from __future__ import annotations

import concurrent.futures
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from engine.work.progress import heartbeat_message, should_emit_heartbeat, stage_start_message

logger = logging.getLogger(__name__)

# Default models per vendor when none specified in config
_DEFAULT_MODELS: dict[str, str] = {
    "claude": "claude-sonnet-4-20250514",
    "gemini": "gemini-2.5-pro",
    "openai": "gpt-4.1",
}

# Maximum tokens for API responses
_DEFAULT_MAX_TOKENS = 16384


# ---------------------------------------------------------------------------
# Vendor-specific API callers (lazy imports)
# ---------------------------------------------------------------------------


def _call_anthropic(
    prompt: str,
    model: str | None,
    api_key: str,
    base_url: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Call the Anthropic Messages API."""
    try:
        import anthropic  # type: ignore[import-untyped]
    except ImportError:
        return {
            "ok": False,
            "error": "anthropic SDK not installed. Run: pip install anthropic",
            "text": "",
        }

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client_kwargs["timeout"] = float(timeout_seconds)

    client = anthropic.Anthropic(**client_kwargs)
    resolved_model = model or _DEFAULT_MODELS["claude"]

    try:
        response = client.messages.create(
            model=resolved_model,
            max_tokens=_DEFAULT_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        # Extract text from response content blocks
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        return {"ok": True, "text": "".join(text_parts), "error": ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "text": ""}


def _call_google(
    prompt: str,
    model: str | None,
    api_key: str,
    base_url: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Call the Google Gemini API."""
    try:
        from google import genai  # type: ignore[import-untyped]
    except ImportError:
        return {
            "ok": False,
            "error": "google-genai SDK not installed. Run: pip install google-genai",
            "text": "",
        }

    resolved_model = model or _DEFAULT_MODELS["gemini"]

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=resolved_model,
            contents=prompt,
        )
        text = response.text if response.text else ""
        return {"ok": True, "text": text, "error": ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "text": ""}


def _call_openai(
    prompt: str,
    model: str | None,
    api_key: str,
    base_url: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Call the OpenAI Chat Completions API."""
    try:
        import openai  # type: ignore[import-untyped]
    except ImportError:
        return {
            "ok": False,
            "error": "openai SDK not installed. Run: pip install openai",
            "text": "",
        }

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client_kwargs["timeout"] = float(timeout_seconds)

    client = openai.OpenAI(**client_kwargs)
    resolved_model = model or _DEFAULT_MODELS["openai"]

    try:
        response = client.chat.completions.create(
            model=resolved_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=_DEFAULT_MAX_TOKENS,
        )
        text = response.choices[0].message.content if response.choices else ""
        return {"ok": True, "text": text or "", "error": ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "text": ""}


def _get_api_caller(backend_name: str) -> Callable[..., dict[str, Any]] | None:
    """Look up vendor API caller by name. Uses late binding so patches work in tests."""
    callers = {
        "claude": _call_anthropic,
        "gemini": _call_google,
        "openai": _call_openai,
    }
    return callers.get(backend_name)


# ---------------------------------------------------------------------------
# Main API execution function
# ---------------------------------------------------------------------------


def run_agent_api(
    role: str,
    task: str,
    reason: str,
    assignment_mode: str | None,
    inputs: list[str],
    project: dict[str, Any] | None,
    *,
    delivery_mode: str | None = None,
    backend_name: str,
    model: str | None,
    api_key: str,
    base_url: str | None,
    timeout_seconds: int,
    session: Any,
    force_full_artifacts: list[str] | None,
    expected_result_shape: dict[str, Any] | None,
    build_prompt: Callable[..., str],
    estimate_tokens: Callable[[str], int],
    is_toon_available: Callable[[], bool],
    emit_progress: Callable[[str], None],
    extract_json_payload: Callable[[str], dict[str, Any]],
    classify_error: Callable[[str], str],
) -> dict[str, Any]:
    """Execute an agent role via vendor HTTP API instead of CLI subprocess.

    Returns the same envelope as the CLI run_agent():
    {"status": "success"|"failed"|"capability_requested", "output": {...}, "duration": float}
    """
    if not api_key:
        return {
            "status": "failed",
            "error": f"No API key configured for backend '{backend_name}'. Check config/secrets.json.",
            "error_category": "configuration_error",
        }

    caller = _get_api_caller(backend_name)
    if caller is None:
        return {
            "status": "failed",
            "error": f"No API caller implemented for backend '{backend_name}'. Supported: claude, gemini, openai",
            "error_category": "configuration_error",
        }

    # Build prompt identically to CLI path
    prompt = build_prompt(
        role,
        task,
        reason,
        inputs,
        project,
        assignment_mode,
        delivery_mode,
        force_full_artifacts,
        expected_result_shape,
        session=session,
    )
    prompt_tokens = estimate_tokens(prompt)
    emit_progress(stage_start_message(role, task, prompt_tokens=prompt_tokens))

    resolved_timeout = timeout_seconds or 660
    start_time = datetime.now(timezone.utc)
    last_heartbeat_at: datetime | None = None
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(caller, prompt, model, api_key, base_url, resolved_timeout)
        while True:
            try:
                result = future.result(timeout=1)
                break
            except concurrent.futures.TimeoutError:
                now = datetime.now(timezone.utc)
                if should_emit_heartbeat(start_time, last_heartbeat_at, now=now):
                    emit_progress(heartbeat_message(role, start_time, now=now))
                    last_heartbeat_at = now
                if (now - start_time).total_seconds() >= resolved_timeout:
                    future.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    return {
                        "status": "failed",
                        "error": f"API error ({backend_name}): Timed out after {resolved_timeout}s",
                        "error_category": "timeout",
                    }
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"API error ({backend_name}): {exc}",
            "error_category": classify_error(str(exc)),
        }
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    duration = (datetime.now(timezone.utc) - start_time).total_seconds()

    if not result["ok"]:
        error_msg = result["error"]
        return {
            "status": "failed",
            "error": f"API error ({backend_name}): {error_msg}",
            "error_category": classify_error(error_msg),
        }

    full_text = result["text"]
    if not full_text:
        return {
            "status": "failed",
            "error": "Empty response from API",
            "error_category": "invalid_output",
        }

    # Parse JSON payload from response text (reuses existing parser)
    payload = extract_json_payload(full_text)
    if not payload:
        error_msg = f"Invalid JSON in API response: {full_text[:200]}..."
        return {"status": "failed", "error": error_msg, "error_category": "invalid_output"}

    capability_requests = payload.get("capability_requests", [])
    if capability_requests:
        return {
            "status": "capability_requested",
            "output": payload,
            "capability_requests": capability_requests,
            "duration": duration,
        }

    return {"status": "success", "output": payload, "duration": duration}


# ---------------------------------------------------------------------------
# API runtime check
# ---------------------------------------------------------------------------


def runtime_check_api(
    backend_name: str,
    api_key: str | None,
    model: str | None,
    base_url: str | None,
) -> dict[str, Any]:
    """Lightweight API reachability probe (equivalent of CLI runtime_check)."""
    if not api_key:
        return {
            "backend": f"{backend_name} (api)",
            "ok": False,
            "reason": "no_api_key",
            "details": f"No API key configured for {backend_name}",
        }

    caller = _get_api_caller(backend_name)
    if caller is None:
        return {
            "backend": f"{backend_name} (api)",
            "ok": False,
            "reason": "unsupported",
            "details": f"No API caller for {backend_name}",
        }

    result = caller(
        'Respond with exactly: {"status": "ok"}',
        model,
        api_key,
        base_url,
        25,
    )

    if result["ok"]:
        return {
            "backend": f"{backend_name} (api)",
            "ok": True,
            "reason": "ok",
            "details": "",
        }

    return {
        "backend": f"{backend_name} (api)",
        "ok": False,
        "reason": "api_error",
        "details": result["error"][:500],
    }
