#!/usr/bin/env python3
"""Internal orchestration runtime for the Automator engine."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path
from dataclasses import asdict
from typing import Any

# --- Constants & Paths ---
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.work.repo_paths import (
    DEBUG_TRACKER_PATH,
    PROJECTS_DIR,
    RUNTIME_PROJECTS_DIR,
    DELIVERY_DIR,
    SECRETS_PROJECTS_DIR,
    REGISTRY_PATH,
    INPUTS_DIR,
    SKILLS_CATALOG_PATH,
    SKILLS_DIR,
)
from engine.work.toon_adapter import serialize_for_prompt, serialize_artifact_for_prompt, is_toon_available
from engine.work.secret_detector import detect_secrets, redact_secrets
from engine.work import prompts as prompt_work
from engine.work.debug_store import record_debug_issue as _record_debug_issue
from engine.work.capabilities import (
    _CAPABILITY_DISPATCH,
    _CAPABILITY_QUICK_REFERENCE,
    _cap_persist_artifact,
    _cap_write_file,
    configure_capability_environment,
    execute_capability,
    validate_capability_request,
)
from engine.work.orchestrator import (
    configure_orchestrator_environment,
    run_orchestration,
)
from engine.work.prompts import (
    _sample_data_file as _prompt_sample_data_file,
    _strip_execution_prompt_template,
    _strip_sections,
    _summarize_input_file as _prompt_summarize_input_file,
    minify_text,
    summarize_directory_input,
)
from engine.work.sessions import AgentSession
from engine.work.runtime_helpers import (
    detect_runtime_network_block,
    extract_session_id_from_text as _helper_extract_session_id_from_text,
    is_known_feedback,
    now_iso,
    resolve_active_project as _helper_resolve_active_project,
    runtime_check_output_has_success as _helper_runtime_check_output_has_success,
    should_ignore_cached_project_for_new_request,
)
from engine.work.json_io import (
    extract_json_payload,
    load_json,
    load_json_safe,
    write_json,
)
from engine.work.tokenization import estimate_tokens
from engine.work.error_classifier import classify_error
from engine.work import project_state as project_state_work
from engine.work import execution as execution_work
from engine.work import runtime_entry as runtime_entry_work
from engine.work.backend_config import resolve_backend as _resolve_backend, is_api_mode as _is_api_mode, get_api_agent_bin as _get_api_agent_bin
from engine.work.api_execution import run_agent_api as _run_agent_api, runtime_check_api as _runtime_check_api
from engine.work.orchestration_state import (
    CMD_OUTPUT_INLINE_LIMIT,
    DATA_FILE_EXTENSIONS,
    MAX_CAPABILITY_ROUNDS,
    MAX_CAPABILITY_WRITE_SIZE,
    MAX_FILE_READ_SIZE,
    MAX_INPUT_FILE_SIZE,
    MAX_STAGE_OUTPUT_BYTES,
    RUNTIME_CHECK_PROMPT,
    SPAWN_TIMEOUT_SECONDS,
)

REGISTRY_CSV_PATH = PROJECTS_DIR / "registry.csv"
STATE_TEMPLATE_PATH = Path(__file__).resolve().parent / "task_state.template.json"
CONFIG_TEMPLATE_PATH = Path(__file__).resolve().parent / "project_config.template.json"

# Knowledge-base paths live in knowledge_store.py; re-exported here for the
# prompt-layer attribute-injection at line ~820 and for backward compatibility
# with callers that import KNOWLEDGE_* from engine_runtime.
from engine.work.knowledge_store import (  # noqa: E402  — after constants block above
    KNOWLEDGE_DIR,
    KNOWLEDGE_MANIFEST_PATH,
    KNOWLEDGE_SOURCES_PATH,
    extract_project_knowledge as _extract_project_knowledge_impl,
    purge_project_knowledge as _purge_project_knowledge_impl,
)

# Environmental block phrases - used to prune stale transient failures from prior sessions.
# Uses multi-word phrases to avoid false positives on domain terms (e.g. "dns" in
# "DNS resolve module" or "timeout" in "timeout parameter").  A step is only pruned
# if it was blocked/failed AND its error_category is "environmental", OR if its
# summary contains one of these infrastructure-specific phrases.
ENVIRONMENTAL_BLOCK_PHRASES = {
    "network unreachable", "dns resolution failed", "sandbox restriction",
    "host unreachable", "socket timeout", "connection refused",
    "econnrefused", "enotfound", "ehostunreach", "enetunreach",
    "ssl certificate", "tls handshake", "proxy error", "firewall blocked",
    "rate limit exceeded", "http 429", "http 503", "http 502", "http 504",
    "http 500", "internal server error", "bad gateway", "service unavailable",
    "gateway timeout", "server overloaded", "model capacity exhausted",
    "environment did not provide", "could not resolve",
    "credentials not available", "credentials not found",
}

# ---------------------------------------------------------------------------
# Interactive guard prompt — session-level state and helpers
# ---------------------------------------------------------------------------

# Set of allow-keys approved by the operator for the entire process lifetime.
# Keyed by category strings (e.g. "http-block:Azure Logic Apps workflow deletion")
# so that "Allow all similar" is meaningful across different URLs / invocations.
_SESSION_ALLOWED: set[str] = set()


def _re_search_role(issue: str):
    """Extract (role, capability) from a role-allowlist block message, or None."""
    m = re.search(
        r"Role '([^']+)' is not permitted to use capability '([^']+)'",
        issue,
    )
    return (m.group(1), m.group(2)) if m else None


def _make_allow_key(cap_req: dict, blocked_result: dict) -> str:
    """Derive a session-allow key from a capability request and its block result."""
    capability = cap_req.get("capability", "")
    arguments = cap_req.get("arguments") or {}
    issue = blocked_result.get("issues", [""])[0]

    if capability == "http_request_with_secret_binding":
        if "requires delivery_mode" in issue:
            return f"deploy-gate:{capability}"
        desc = issue.removeprefix("[destructive-guard] BLOCKED: ").split(".")[0].strip()
        return f"http-block:{desc}"

    if capability in ("deploy_logic_app_definition", "powerbi_import_artifact"):
        return f"deploy-gate:{capability}"

    if capability == "run_command":
        desc = issue.removeprefix("[destructive-guard] BLOCKED: ").split(" is not allowed")[0].strip()
        return f"cmd-block:{desc}"

    if capability == "write_file":
        path = arguments.get("path", "")
        if "not permitted for agents" in issue:
            m = re.search(r"Writing to '([^']+)'", issue)
            prefix = m.group(1) if m else path
            return f"write-path:{prefix}"
        return f"write-overwrite:{path}"

    role_match = _re_search_role(issue)
    if role_match:
        r, cap = role_match
        return f"role-allowlist:{r}:{cap}"

    return f"generic:{capability}:{issue[:80]}"


def _extract_confirmation_token(url: str) -> str:
    """
    Extract a short, human-typeable confirmation token from a URL.

    Returns the last non-empty path segment (e.g. a GUID or UPN), falling
    back to the full URL when no meaningful segment can be found.
    """
    from urllib.parse import urlparse
    path = urlparse(url).path.rstrip("/")
    last_seg = path.rsplit("/", 1)[-1] if "/" in path else path
    if last_seg and len(last_seg) >= 3:
        return last_seg
    return url  # fallback: operator must type the full URL


def _prompt_absolute_block(
    cap_req: dict,
    blocked_result: dict,
    role: str,
    emit_progress_fn,
) -> bool:
    """
    Require the operator to type the exact resource ID to allow an absolute-protection block.

    Returns True if the operator confirmed with the correct token, False otherwise.
    Falls back to False (blocked) when stdin is not a TTY.
    """
    if not sys.stdin.isatty():
        emit_progress_fn("[destructive-guard] Non-interactive mode — defaulting to blocked.")
        return False

    capability = cap_req.get("capability", "")
    arguments = cap_req.get("arguments") or {}
    url = str(arguments.get("url", ""))
    issue = blocked_result.get("issues", ["(no reason)"])[0]
    reason = issue.removeprefix("[destructive-guard] BLOCKED: ")
    token = _extract_confirmation_token(url)

    separator = "─" * 76
    msg = (
        f"\n{separator}\n"
        f" PROTECTED RESOURCE — Explicit Confirmation Required\n"
        f"{separator}\n"
        f" Role      : {role}\n"
        f" Capability: {capability}\n"
        f" Reason    : {reason}\n"
        f"\n"
        f" \u26a0  This targets a PROTECTED IDENTITY OR SITE.\n"
        f"    To allow this ONE operation, type the resource ID exactly:\n"
        f"\n"
        f"      {token}\n"
        f"\n"
        f" Press Enter without typing to block.\n"
        f"{separator}\n"
        f"Confirm resource ID: "
    )
    sys.stderr.write(msg)
    sys.stderr.flush()

    try:
        answer = input().strip()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False

    return answer == token


def _prompt_guard_block(
    cap_req: dict,
    blocked_result: dict,
    role: str,
    emit_progress_fn,
) -> str:
    """
    Interactively ask the operator whether to allow a soft guard block.

    Returns 'allow_once', 'allow_always', or 'block'.
    Falls back to 'block' when stdin is not a TTY.
    """
    if not sys.stdin.isatty():
        emit_progress_fn("[destructive-guard] Non-interactive mode — defaulting to blocked.")
        return "block"

    capability = cap_req.get("capability", "")
    issue = blocked_result.get("issues", ["(no reason)"])[0]
    reason = issue.removeprefix("[destructive-guard] BLOCKED: ")

    separator = "─" * 60
    msg = (
        f"\n{separator}\n"
        f" DESTRUCTIVE ACTION — Review Required\n"
        f"{separator}\n"
        f" Role      : {role}\n"
        f" Capability: {capability}\n"
        f" Reason    : {reason}\n"
        f"\n"
        f" [y] Allow once\n"
        f" [A] Allow all similar operations this session\n"
        f" [N] Block (default — press Enter)\n"
        f"{separator}\n"
        f"Decision [y/A/N]: "
    )
    sys.stderr.write(msg)
    sys.stderr.flush()

    try:
        answer = input().strip()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return "block"

    if answer == "y":
        return "allow_once"
    if answer == "A":
        return "allow_always"
    return "block"


def emit_progress(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def record_debug_issue(
    *,
    issue_type: str,
    title: str,
    backend: str,
    request: str,
    role: str = "",
    error_category: str = "",
    active_project: dict[str, Any] | None = None,
    ctx: Any | None = None,
    task_state: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _record_debug_issue(
        issue_type=issue_type,
        title=title,
        backend=backend,
        request=request,
        role=role,
        error_category=error_category,
        active_project=active_project,
        ctx=ctx,
        task_state=task_state,
        details=details,
        repo_root=REPO_ROOT,
        tracker_path=DEBUG_TRACKER_PATH,
        load_json=load_json,
        write_json=write_json,
        now_iso=now_iso,
        emit_progress=emit_progress,
        ctx_to_dict=asdict,
    )

def _is_backend_available(agent_bin: str) -> bool:
    # If global config is API mode, the configured provider is always "available"
    if _is_api_mode():
        return True
    parts = shlex.split(agent_bin)
    return bool(parts) and shutil.which(parts[0]) is not None


def run_runtime_check(agent_bin: str, timeout_seconds: int = 25) -> dict[str, Any]:
    return execution_work.runtime_check(
        agent_bin,
        runtime_check_prompt=RUNTIME_CHECK_PROMPT,
        build_agent_command=execution_work.build_agent_command,
        extract_json_payload=extract_json_payload,
        runtime_check_output_has_success=_runtime_check_output_has_success,
        timeout_seconds=timeout_seconds,
        resolve_backend=_resolve_backend,
        runtime_check_api=_runtime_check_api,
    )


def run_runtime_checks(backends: list[str]) -> list[dict[str, Any]]:
    return execution_work.runtime_checks(
        backends,
        run_runtime_check=run_runtime_check,
    )


def _runtime_check_output_has_success(payload: dict[str, Any], full_text: str) -> bool:
    return _helper_runtime_check_output_has_success(
        payload,
        full_text,
        extract_json_payload=extract_json_payload,
    )


def _sample_data_file(p: Path, max_bytes: int = MAX_INPUT_FILE_SIZE) -> str:
    return _prompt_sample_data_file(
        p,
        data_file_extensions=DATA_FILE_EXTENSIONS,
        max_input_file_size=max_bytes,
    )


def _summarize_input_file(p: Path) -> str:
    return _prompt_summarize_input_file(
        p,
        data_file_extensions=DATA_FILE_EXTENSIONS,
        max_input_file_size=MAX_INPUT_FILE_SIZE,
    )

# --- Project & Session Resolution ---
def resolve_active_project(
    request: str,
    projects: list[dict[str, Any]],
    *,
    allow_registry_fallback: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    return _helper_resolve_active_project(
        request,
        projects,
        allow_registry_fallback=allow_registry_fallback,
        load_json=load_json,
        registry_path=REGISTRY_PATH,
    )

def bootstrap_project(decision: dict[str, Any]) -> dict[str, Any]:
    return project_state_work.bootstrap_project(
        decision,
        repo_root=REPO_ROOT,
        projects_dir=_projects_base_dir(),
        runtime_projects_dir=RUNTIME_PROJECTS_DIR,
        state_template_path=STATE_TEMPLATE_PATH,
        config_template_path=CONFIG_TEMPLATE_PATH,
        registry_path=REGISTRY_PATH,
        load_json=load_json,
        write_json=write_json,
        sync_registry_csv=sync_registry_csv,
        emit_progress=emit_progress,
    )

def fork_project(decision: dict[str, Any]) -> dict[str, Any]:
    return project_state_work.fork_project(
        decision,
        projects_dir=_projects_base_dir(),
        runtime_projects_dir=RUNTIME_PROJECTS_DIR,
        registry_path=REGISTRY_PATH,
        load_json=load_json,
        write_json=write_json,
        bootstrap_project=bootstrap_project,
        emit_progress=emit_progress,
        now_iso=now_iso,
    )


def detect_fork_intent(request: str, projects: list[dict[str, Any]]) -> dict[str, Any] | None:
    return project_state_work.detect_fork_intent(
        request,
        projects,
        resolve_active_project=lambda req, projs: resolve_active_project(req, projs),
    )


def save_last_active_project(project: dict[str, Any] | None) -> None:
    project_state_work.save_last_active_project(
        project,
        load_json=load_json,
        write_json=write_json,
        registry_path=REGISTRY_PATH,
    )


def close_project(project_id: str, agent_bin: str | None = None) -> int:
    """Close a project by ID: clear pending resolution and save a KB entry from the final worker output."""
    registry = load_json_safe(REGISTRY_PATH)
    project = next(
        (p for p in registry.get("projects", []) if p["project_id"] == project_id), None
    )
    if not project:
        emit_progress(f"Error: Project '{project_id}' not found in registry.")
        return 1

    runtime_dir = Path(project["runtime_dir"])
    task_state_path = runtime_dir / "state" / "active_task.json"
    task_state = load_json(task_state_path)
    task_state.pop("pending_resolution", None)
    write_json(task_state_path, task_state)

    _extract_project_knowledge(project, task_state)

    emit_progress("[engine] Project closed.")
    return 0


def _extract_project_knowledge(project: dict, task_state: dict) -> None:
    _extract_project_knowledge_impl(project, task_state, emit_progress=emit_progress)


def sync_registry_csv() -> None:
    project_state_work.sync_registry_csv(
        load_json=load_json,
        registry_path=REGISTRY_PATH,
        registry_csv_path=REGISTRY_CSV_PATH,
    )


def _projects_base_dir() -> Path:
    return RUNTIME_PROJECTS_DIR.parent if RUNTIME_PROJECTS_DIR.name == "runtime" else RUNTIME_PROJECTS_DIR


# --- Secrets Management ---

def _secrets_path(project_id: str) -> Path:
    return project_state_work.secrets_path(project_id, secrets_projects_dir=SECRETS_PROJECTS_DIR)


def store_secrets(project_id: str, entries: list[dict[str, Any]], source: str = "capability") -> None:
    project_state_work.store_secrets(
        project_id,
        entries,
        secrets_projects_dir=SECRETS_PROJECTS_DIR,
        load_json=load_json_safe,
        write_json=write_json,
        now_iso=now_iso,
        source=source,
    )


def load_secrets(project_id: str, keys: list[str] | None = None) -> dict[str, Any]:
    return project_state_work.load_secrets(
        project_id,
        secrets_projects_dir=SECRETS_PROJECTS_DIR,
        load_json=load_json_safe,
        keys=keys,
    )


def _get_project_secret_values(project_id: str) -> list[tuple[str, str]]:
    return project_state_work.get_project_secret_values(
        project_id,
        load_secrets=lambda pid: load_secrets(pid),
    )


def _is_binary_file(path: Path) -> bool:
    return project_state_work.is_binary_file(path)


def ingest_input_files(project_id: str) -> list[str]:
    return project_state_work.ingest_input_files(
        project_id,
        inputs_dir=INPUTS_DIR,
        projects_dir=_projects_base_dir(),
        runtime_projects_dir=RUNTIME_PROJECTS_DIR,
        detect_secrets=detect_secrets,
        store_secrets=lambda pid, entries, source: store_secrets(pid, entries, source=source),
        is_binary_file=_is_binary_file,
    )


def _get_project_input_paths(project_id: str) -> list[str]:
    return project_state_work.get_project_input_paths(
        project_id,
        projects_dir=_projects_base_dir(),
        runtime_projects_dir=RUNTIME_PROJECTS_DIR,
        is_binary_file=_is_binary_file,
    )


def _infer_project_id_from_path(p: Path) -> str | None:
    return project_state_work.infer_project_id_from_path(
        p,
        projects_dir=_projects_base_dir(),
        delivery_dir=DELIVERY_DIR,
        runtime_projects_dir=RUNTIME_PROJECTS_DIR,
    )


configure_capability_environment(
    REPO_ROOT=REPO_ROOT,
    SPAWN_TIMEOUT_SECONDS=SPAWN_TIMEOUT_SECONDS,
    CMD_OUTPUT_INLINE_LIMIT=CMD_OUTPUT_INLINE_LIMIT,
    MAX_STAGE_OUTPUT_BYTES=MAX_STAGE_OUTPUT_BYTES,
    MAX_CAPABILITY_WRITE_SIZE=MAX_CAPABILITY_WRITE_SIZE,
    MAX_FILE_READ_SIZE=MAX_FILE_READ_SIZE,
    load_json=load_json,
    write_json=write_json,
    bootstrap_project=bootstrap_project,
    load_secrets=load_secrets,
    store_secrets=store_secrets,
    _get_project_secret_values=_get_project_secret_values,
    _infer_project_id_from_path=_infer_project_id_from_path,
)


# --- AI Binary Adapter ---
build_agent_command = execution_work.build_agent_command


# Context window budget constants (based on skills: context-fundamentals, context-degradation).
# Effective capacity is ~60-70% of advertised window; degradation begins at that threshold.
# Compaction fires proactively to stay below the cliff, not after falling off it.

# Per-model effective context window sizes (prefix-matched on model name, lowercase).
# Effective = ~60% of advertised to account for cliff-edge degradation onset.
_MODEL_EFFECTIVE_CONTEXT_TOKENS: dict[str, int] = {
    "gemini-2.5":    600_000,   # 1M advertised → ~60% effective
    "gemini-2.0":    600_000,
    "gemini-1.5":    600_000,   # 1M / 2M advertised
    "claude-opus":   120_000,   # 200K advertised
    "claude-sonnet": 120_000,
    "claude-haiku":  120_000,
    "gpt-4o":         76_000,   # 128K advertised
    "gpt-4":          50_000,   # 128K advertised (older variants)
    "o1":             76_000,
    "o3":             76_000,
}
_CONTEXT_EFFECTIVE_TOKENS_DEFAULT = 120_000   # fallback when model is unknown


def _effective_context_tokens() -> int:
    """Return the effective context window for the currently configured model.

    Reads config/backends.json to find the active model.  Falls back to the
    120K default when config is absent or the model name is unrecognised.
    Re-reads config on every call so the value stays current across config changes
    within a long-running process.
    """
    try:
        from engine.work.backend_config import load_backend_config
        config = load_backend_config()
        model = (config.get("default_model") or "").lower()
        for prefix, tokens in _MODEL_EFFECTIVE_CONTEXT_TOKENS.items():
            if model.startswith(prefix):
                return tokens
    except Exception as exc:
        print(f"[engine] context window lookup failed: {exc}", file=sys.stderr)
    return _CONTEXT_EFFECTIVE_TOKENS_DEFAULT


# Ordered list of section header prefixes to drop when compaction fires (least → most critical).
# Each entry identifies a two-element block [header, data] in the flat sections list.
_COMPACTABLE_SECTION_PREFIXES = [
    "\nCoding Repo Fingerprint",          # optional coding hint; agent can read files directly
    "\nProject Files:",                   # directory listing; agent can discover via read_file
    "\nMatched Skills",                   # helpful but not required for correctness
    "\nResearch Artifact Summary",        # last resort — research findings are critical for correctness
]

_AGENT_OUTPUT_REMINDER = (
    "\nREMINDER: Return your result as a single JSON object matching the shared schema. "
    "Total JSON output must stay under 512KB."
)

# Injected when multiple knowledge sources are present in the same prompt.

def _compact_prompt_sections(sections: list[str]) -> list[str]:
    """Drop low-priority sections if the prompt exceeds 70% of the effective window.

    Each compactable entry is a two-element block [header, data] in the flat list.
    The skip is bounds-checked: if the element after the header is itself another
    section header (or the list ends), only the header is dropped, not a data element.
    """
    effective = _effective_context_tokens()
    compaction_threshold = int(effective * 0.70)
    joined = "\n".join(sections)
    if estimate_tokens(joined) <= compaction_threshold:
        return sections

    all_prefixes = set(_COMPACTABLE_SECTION_PREFIXES)
    result = list(sections)
    for header_prefix in _COMPACTABLE_SECTION_PREFIXES:
        if estimate_tokens("\n".join(result)) <= compaction_threshold:
            break
        compacted = []
        i = 0
        while i < len(result):
            if result[i].startswith(header_prefix):
                i += 1  # always skip the header
                # Skip the data element only if it exists and is not itself a header.
                if i < len(result) and not any(result[i].startswith(p) for p in all_prefixes):
                    i += 1
            else:
                compacted.append(result[i])
                i += 1
        result = compacted
    return result


# Compact output schema templates per agent role.
# Injected at the END of agent prompts (closest to generation) for max compliance.
_AGENT_OUTPUT_TEMPLATES: dict[str, str] = {
    "worker": """{
  "status": "success | failed | blocked",
  "summary": "one-sentence description of what was done",
  "changes_made": ["path/to/file: what changed"],
  "checks_run": [{"check": "description", "command": "cmd", "result": "passed | failed", "output": "..."}],
  "artifacts": ["path/to/delivered/file"],
  "open_issues": ["unresolved issue"],
  "needs_research": false,
  "needs_user_input": false
}""",
    "review": """{
  "status": "pass | fail",
  "summary": "one-sentence verdict",
  "findings": ["finding description"],
  "checks_run": [{"check": "description", "command": "cmd", "result": "passed | failed", "output": "..."}],
  "blocking": ["blocking issue preventing acceptance"],
  "rework_requests": ["specific fix the worker must apply"]
}""",
    "research": """{
  "status": "success | partial | failed",
  "summary": "one-sentence summary of findings",
  "sources": ["URL or document reference"],
  "technical_data": {
    "answers": [{"question": "Q1: ...", "answer": "direct answer", "facts": ["atomic fact (source: URL)"], "implementation_notes": ["how to apply"]}],
    "open_risks": ["risk or ambiguity affecting implementation"]
  }
}""",
}


_DELIVERY_INLINE_MAX_LINES = 100   # inline content for files under this many lines
_DELIVERY_INLINE_MAX_BYTES = 8_000  # skip files larger than ~8KB even if short
_DELIVERY_MAX_FILES = 30            # cap tree listing to prevent prompt bloat
_DELIVERY_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".whl",
    ".xlsx", ".docx", ".pptx",
}


def _build_delivery_context(project: dict[str, Any]) -> list[str]:
    """Build a compact snapshot of the delivery directory for worker context.

    Returns a file tree and inline contents of small text files so the worker
    doesn't waste capability rounds on list_dir + read_file discovery.
    Returns an empty list when no delivery directory or no files exist.
    """
    delivery_dir = Path(project.get("project_root", ""))
    if not delivery_dir.is_dir():
        return []

    try:
        all_files = sorted(delivery_dir.rglob("*"))
    except OSError:
        return []

    files = [f for f in all_files if f.is_file()]
    if not files:
        return []

    lines: list[str] = ["\nExisting delivery files:"]

    # File tree (relative paths)
    tree_files = files[:_DELIVERY_MAX_FILES]
    for f in tree_files:
        rel = f.relative_to(delivery_dir)
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        if size < 1024:
            size_str = f"{size}B"
        elif size < 1024 * 1024:
            size_str = f"{size // 1024}KB"
        else:
            size_str = f"{size // (1024 * 1024)}MB"
        lines.append(f"  {rel} ({size_str})")
    if len(files) > _DELIVERY_MAX_FILES:
        lines.append(f"  ... and {len(files) - _DELIVERY_MAX_FILES} more files")

    # Inline small text files
    inlined = 0
    for f in tree_files:
        if f.suffix.lower() in _DELIVERY_BINARY_EXTS:
            continue
        try:
            size = f.stat().st_size
            if size > _DELIVERY_INLINE_MAX_BYTES or size == 0:
                continue
            content = f.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        content_lines = content.splitlines()
        if len(content_lines) > _DELIVERY_INLINE_MAX_LINES:
            continue
        rel = f.relative_to(delivery_dir)
        lines.append(f"\n--- {rel} ---")
        lines.append(content)
        inlined += 1
        if inlined >= 10:  # cap inlined files to keep prompt reasonable
            break

    return lines


def _build_stage_summary(inputs: list[str]) -> list[str]:
    return prompt_work._build_stage_summary(inputs)


def _build_knowledge_context(role: str, task: str = "", reason: str = "", project_desc: str = "") -> list[str]:
    prompt_work.KNOWLEDGE_DIR = KNOWLEDGE_DIR
    prompt_work.KNOWLEDGE_MANIFEST_PATH = KNOWLEDGE_MANIFEST_PATH
    prompt_work.KNOWLEDGE_SOURCES_PATH = KNOWLEDGE_SOURCES_PATH
    prompt_work.SKILLS_CATALOG_PATH = SKILLS_CATALOG_PATH
    return prompt_work._build_knowledge_context(role, task, reason, project_desc)


def _build_skills_context(role: str, inputs: list[str]) -> list[str]:
    prompt_work.SKILLS_DIR = SKILLS_DIR
    return prompt_work._build_skills_context(role, inputs, estimate_tokens=estimate_tokens)




def run_agent_with_capabilities(
    role: str, task: str, reason: str, inputs: list[str],
    project: dict[str, Any] | None, agent_bin: str,
    force_full_artifacts: list[str] | None = None,
    assignment_mode: str | None = None,
    delivery_mode: str | None = None,
    expected_result_shape: dict[str, Any] | None = None,
    session: AgentSession | None = None,
    max_rounds: int | None = None,
) -> dict[str, Any]:
    from engine.work.destructive_guard import check_capability as _guard_check, is_absolute_block, _check_capability_specific

    _guard_blocks: list[str] = []

    def _guarded_execute_capability(cap_req: dict) -> dict:
        blocked = _guard_check(cap_req, role=role, delivery_mode=delivery_mode)
        if blocked is None:
            return execute_capability(cap_req)

        # Absolute blocks (SharePoint sites, Entra users/SPs) — require resource ID confirmation
        if is_absolute_block(blocked):
            confirmed = _prompt_absolute_block(cap_req, blocked, role, emit_progress)
            if confirmed:
                emit_progress(
                    f"[destructive-guard] Operator explicitly confirmed absolute-protected "
                    f"operation: '{cap_req.get('capability')}'"
                )
                return execute_capability(cap_req)
            msg = blocked["issues"][0]
            _guard_blocks.append(msg)
            emit_progress(
                f"[destructive-guard] Absolute block on '{cap_req.get('capability')}' "
                f"for role '{role}': {msg}"
            )
            return blocked

        # Check session-level allows first (operator previously said 'A')
        allow_key = _make_allow_key(cap_req, blocked)
        if allow_key in _SESSION_ALLOWED:
            # For role-allowlist session allows, still enforce capability-specific
            # guards (shell blocklist, write protection, HTTP mutation checks).
            # Without this, a session-allow for "research can use run_command"
            # would also bypass the shell command blocklist.
            if allow_key.startswith("role-allowlist:"):
                cap_specific = _check_capability_specific(
                    cap_req.get("capability", ""),
                    cap_req.get("arguments") or {},
                    delivery_mode,
                )
                if cap_specific is not None:
                    # Capability-specific guard blocked — do NOT session-allow this
                    blocked = cap_specific
                    # Fall through to the normal soft-block prompt below
                else:
                    emit_progress(
                        f"[destructive-guard] Session-allowed (previously approved): "
                        f"'{cap_req.get('capability')}' — {allow_key}"
                    )
                    return execute_capability(cap_req)
            else:
                emit_progress(
                    f"[destructive-guard] Session-allowed (previously approved): "
                    f"'{cap_req.get('capability')}' — {allow_key}"
                )
                return execute_capability(cap_req)

        # Soft block — prompt the operator
        decision = _prompt_guard_block(cap_req, blocked, role, emit_progress)

        if decision == "allow_once":
            emit_progress(
                f"[destructive-guard] Operator allowed once: '{cap_req.get('capability')}'"
            )
            return execute_capability(cap_req)

        if decision == "allow_always":
            _SESSION_ALLOWED.add(allow_key)
            emit_progress(
                f"[destructive-guard] Operator allowed for session: '{allow_key}'"
            )
            return execute_capability(cap_req)

        # Operator chose block (or non-interactive default)
        msg = blocked["issues"][0]
        _guard_blocks.append(msg)
        emit_progress(
            f"[destructive-guard] Operator blocked '{cap_req.get('capability')}' "
            f"for role '{role}': {msg}"
        )
        return blocked

    result = execution_work.run_agent_with_capabilities(
        role,
        task,
        reason,
        assignment_mode,
        inputs,
        project,
        agent_bin,
        delivery_mode=delivery_mode,
        force_full_artifacts=force_full_artifacts,
        expected_result_shape=expected_result_shape,
        session=session,
        run_agent=run_agent,
        max_capability_rounds=max_rounds if max_rounds is not None else MAX_CAPABILITY_ROUNDS,
        validate_capability_request=validate_capability_request,
        emit_progress=emit_progress,
        execute_capability=_guarded_execute_capability,
        serialize_for_prompt=serialize_for_prompt,
    )

    # If the capability loop was exhausted due to guard blocks, use a distinct
    # error_category so any reader (human or agent) knows this is an
    # intentional safety policy, not a defect to fix in the engine code.
    if result.get("error_category") == "capability_loop" and _guard_blocks:
        unique_blocks = list(dict.fromkeys(_guard_blocks))  # deduplicate, preserve order
        result = {
            **result,
            "error_category": "destructive_guard_block",
            "error": result["error"] + f". Guard blocks: {'; '.join(unique_blocks[:3])}",
        }

    return result


def _build_processed_inputs(
    role: str,
    inputs: list[str],
    project: dict[str, Any] | None,
) -> list[str]:
    """Process prompt inputs for the given role."""
    processed_inputs: list[str] = []
    project_root_path = Path(project["project_root"]) if project else None
    for inp_path in (inputs or []):
        if len(inp_path) > 4096 or (not inp_path.startswith(("/", ".")) and "/" not in inp_path):
            processed_inputs.append(f"Context: {inp_path}")
            continue
        p = Path(inp_path)
        try:
            path_exists = p.exists()
        except OSError:
            processed_inputs.append(f"Context: {inp_path}")
            continue
        if not path_exists:
            processed_inputs.append(f"Input path not found: {inp_path}")
            continue
        if p.is_dir():
            processed_inputs.append(summarize_directory_input(p, project_root_path))
            continue

        if p.suffix == ".json" and "result" in p.name:
            try:
                data = load_json(p)
            except (json.JSONDecodeError, OSError) as exc:
                processed_inputs.append(f"Artifact (CORRUPT): {p.name} - could not parse: {exc}")
                continue
            source_agent = data.get("agent", p.name.split("_result_")[0] if "_result_" in p.name else "unknown")
            source_status = data.get("status", "unknown")
            tech_data = data.get("technical_data", data)
            serialized = serialize_artifact_for_prompt(tech_data, source_role=source_agent)
            if source_agent == "research":
                # Two separate elements so the compaction system can drop [header, data] as a pair.
                processed_inputs.append(f"\nResearch Artifact Summary (status: {source_status}): {p.name}")
                processed_inputs.append(serialized)
            else:
                processed_inputs.append(f"Artifact from {source_agent} (status: {source_status}): {p.name}\n{serialized}")
        else:
            processed_inputs.append(_summarize_input_file(p))
    return processed_inputs


def build_prompt(
    role: str,
    task: str,
    reason: str,
    inputs: list[str],
    project: dict[str, Any] | None,
    assignment_mode: str | None = None,
    delivery_mode: str | None = None,
    force_full_artifacts: list[str] | None = None,
    expected_result_shape: dict[str, Any] | None = None,
    session: AgentSession | None = None,
) -> str:
    spec_path = REPO_ROOT / "agents" / f"{role}.md"
    try:
        agent_spec = spec_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"Agent spec not found for role '{role}': {spec_path}") from None

    agent_spec = minify_text(agent_spec)
    agent_spec = _strip_execution_prompt_template(agent_spec)
    agent_spec = _strip_sections(agent_spec, ["Required Output", "Runtime Capabilities"])

    project_context = []
    delivery_context: list[str] = []
    if project:
        project_context = [
            f"Active Project: {project['project_name']} ({project['project_id']})",
            f"Project Root: {project['project_root']}",
        ]
        # Pre-inject delivery directory context for the worker on continue runs.
        # Saves 1-2 capability rounds of list_dir + read_file discovery.
        if role == "worker":
            delivery_context = _build_delivery_context(project)
    else:
        project_context = ["Active Project: NONE (Requires resolution or bootstrap)"]

    processed_inputs = _build_processed_inputs(role, inputs, project)

    # Load project config for agent-specific settings
    project_config_context = []
    project_desc = ""
    if project and project.get("runtime_dir"):
        config_path = Path(project["runtime_dir"]) / "config.json"
        if config_path.exists():
            config = load_json(config_path)
            project_desc = config.get("description", "")
            relevant_config = {}
            for key in ("default_constraints", "allowed_tools"):
                if config.get(key):
                    relevant_config[key] = config[key]
            if relevant_config:
                project_config_context = [
                    "\nProject Configuration:",
                    serialize_for_prompt(relevant_config),
                ]

    stage_summary_context = _build_stage_summary(inputs or [])
    project_desc_context = [f"Project Description: {project_desc}"] if project_desc else []
    skills_context = _build_skills_context(role, inputs or [])
    knowledge_context = _build_knowledge_context(role, task, reason, project_desc)

    sections = [
        _CAPABILITY_QUICK_REFERENCE,
        "\nAgent Specification:",
        agent_spec,
        f"\nReturn exactly this JSON structure (fill in your data):\n{_AGENT_OUTPUT_TEMPLATES.get(role, '')}",
        "\nDo NOT wrap in markdown fences. Do NOT add prose before or after the JSON.",
        f"\nYou are acting as the {role} agent in the {REPO_ROOT} workflow.",
        *project_context,
        *project_desc_context,
        *delivery_context,
        *stage_summary_context,
        *knowledge_context,
        *skills_context,
        f"Reason for Invocation: {reason}",
        f"Task: {task}",
        "Processed Inputs:",
        *processed_inputs,
        *project_config_context,
    ]

    if is_toon_available():
        sections.insert(0, "Note: Structured data in this prompt uses TOON notation (compact JSON superset). Parse it as you would JSON.")

    sections = _compact_prompt_sections(sections)

    recall_anchor_threshold = int(_effective_context_tokens() * 0.40)
    if estimate_tokens("\n".join(sections)) >= recall_anchor_threshold:
        sections.append(_AGENT_OUTPUT_REMINDER)

    return "\n".join(sections)

# --- Execution & Persistence ---
def _extract_session_id_from_text(text: str) -> str | None:
    return _helper_extract_session_id_from_text(
        text,
        extract_json_payload=extract_json_payload,
    )


def run_agent(
    role: str,
    task: str,
    reason: str,
    assignment_mode_or_inputs: str | list[str] | None,
    inputs_or_project: list[str] | dict[str, Any] | None = None,
    project_or_agent_bin: dict[str, Any] | str | None = None,
    agent_bin: str | None = None,
    force_full_artifacts: list[str] | None = None,
    delivery_mode: str | None = None,
    expected_result_shape: dict[str, Any] | None = None,
    session: AgentSession | None = None,
) -> dict[str, Any]:
    if agent_bin is None:
        assignment_mode = None
        inputs = assignment_mode_or_inputs if isinstance(assignment_mode_or_inputs, list) else []
        project = inputs_or_project if isinstance(inputs_or_project, dict) or inputs_or_project is None else None
        agent_bin = project_or_agent_bin if isinstance(project_or_agent_bin, str) else None
    else:
        assignment_mode = assignment_mode_or_inputs if isinstance(assignment_mode_or_inputs, str) or assignment_mode_or_inputs is None else None
        inputs = inputs_or_project if isinstance(inputs_or_project, list) else []
        project = project_or_agent_bin if isinstance(project_or_agent_bin, dict) or project_or_agent_bin is None else None

    return execution_work.run_agent(
        role,
        task,
        reason,
        assignment_mode,
        inputs,
        project,
        agent_bin,
        delivery_mode=delivery_mode,
        force_full_artifacts=force_full_artifacts,
        expected_result_shape=expected_result_shape,
        session=session,
        build_prompt=build_prompt,
        estimate_tokens=estimate_tokens,
        build_agent_command=build_agent_command,
        is_toon_available=is_toon_available,
        emit_progress=emit_progress,
        repo_root=REPO_ROOT,
        spawn_timeout_seconds=SPAWN_TIMEOUT_SECONDS,
        classify_error=classify_error,
        extract_session_id_from_text=_extract_session_id_from_text,
        extract_json_payload=extract_json_payload,
        resolve_backend=_resolve_backend,
        run_agent_api=_run_agent_api,
    )

def persist_result(project: dict[str, Any], role: str, result: dict[str, Any]) -> str:
    return execution_work.persist_result(
        project,
        role,
        result,
        write_json=write_json,
    )


configure_orchestrator_environment(
    emit_progress=emit_progress,
    run_agent_with_capabilities=run_agent_with_capabilities,
    persist_result=persist_result,
    write_json=write_json,
    load_json=load_json,
    now_iso=now_iso,
    bootstrap_project=bootstrap_project,
    fork_project=fork_project,
    store_secrets=store_secrets,
    ingest_input_files=ingest_input_files,
    save_last_active_project=save_last_active_project,
    _get_project_input_paths=_get_project_input_paths,
    REGISTRY_PATH=REGISTRY_PATH,
    extract_project_knowledge=_extract_project_knowledge,
)


# --- Delete Projects ---
def delete_projects(project_ids: list[str], *, delete_all: bool = False) -> int:
    return project_state_work.delete_projects(
        project_ids,
        delete_all=delete_all,
        registry_path=REGISTRY_PATH,
        load_json_safe=load_json_safe,
        write_json=write_json,
        sync_registry_csv=sync_registry_csv,
        emit_progress=emit_progress,
    )


def purge_project_knowledge(project_id: str) -> int:
    return _purge_project_knowledge_impl(project_id, emit_progress=emit_progress)


# --- Main Orchestration ---
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unified Agent Engine")
    parser.add_argument("request", nargs="*")
    parser.add_argument("--gemini", action="store_true", help="Explicitly use 'gemini' as the AI parent binary.")
    parser.add_argument("--claude", action="store_true", help="Explicitly use 'claude' as the AI parent binary.")
    parser.add_argument("--codex", action="store_true", help="Explicitly use 'codex' as the AI parent binary.")
    parser.add_argument("--check-runtime", action="store_true", help="Probe available AI backends and report whether they can complete a minimal request.")
    parser.add_argument("--debug-mode", action="store_true", help="Run the normal orchestration path, but capture orchestration faults in debug/tracker.json and stop instead of self-healing.")
    parser.set_defaults(execute_agents=True, agent_bin=None)
    args = parser.parse_args(argv)

    if not args.check_runtime and not args.request:
        parser.error("request is required unless --check-runtime is used")

    request = " ".join(args.request)
    from engine.work.repo_bootstrap import ensure_repo_structure
    return runtime_entry_work.execute_main_flow(
        args,
        request,
        ensure_repo_structure=ensure_repo_structure,
        emit_progress=emit_progress,
        detect_runtime_network_block=detect_runtime_network_block,
        is_backend_available=_is_backend_available,
        get_api_agent_bin=_get_api_agent_bin,
        run_runtime_checks=run_runtime_checks,
        record_debug_issue=record_debug_issue,
        load_json=load_json,
        registry_path=REGISTRY_PATH,
        detect_fork_intent=detect_fork_intent,
        should_ignore_cached_project_for_new_request=should_ignore_cached_project_for_new_request,
        resolve_active_project=resolve_active_project,
        save_last_active_project=save_last_active_project,
        detect_secrets=detect_secrets,
        store_secrets=lambda project_id, entries, source: store_secrets(project_id, entries, source=source),
        redact_secrets=redact_secrets,
        inputs_dir=INPUTS_DIR,
        ingest_input_files=ingest_input_files,
        now_iso=now_iso,
        write_json=write_json,
        repo_root=REPO_ROOT,
        state_template_path=STATE_TEMPLATE_PATH,
        environmental_block_phrases=ENVIRONMENTAL_BLOCK_PHRASES,
        run_orchestration=run_orchestration,
    )

if __name__ == "__main__":
    raise SystemExit(main())
