"""
Destructive action guard for capability execution.

Enforces safety policies before any capability handler runs:

1. Role-based capability allowlists — each agent role is only permitted to use
   a specific set of capabilities. Requests outside the allowlist are blocked.
2. Engine-created path tracking — write_file is allowed to overwrite a file only
   if the engine created it in this or a prior session for the same project. Pre-
   existing (user) files are never overwritten. Protected source directories are
   always blocked regardless of who created the file.
3. HTTP destructive method guards:
   - SharePoint sites and Entra ID users can NEVER be deleted or modified
     (DELETE, PATCH, PUT are hard-blocked on those resources).
   - All other protected cloud resources (resource groups, Logic Apps, Power BI
     assets, Teams, etc.) are hard-blocked for DELETE.
   - Any mutating method (POST/PUT/PATCH/DELETE) via http_request_with_secret_binding
     requires delivery_mode: build_and_deploy.
4. Shell command blocklist — recursive deletion, find-delete, fork-bomb, and
   PowerShell bulk-removal patterns are hard-blocked in run_command.
5. Deployment capability gate — deploy_logic_app_definition and
   powerbi_import_artifact require delivery_mode: build_and_deploy.

Engine-created path registry
-----------------------------
When write_file creates a NEW file (not overwriting), capabilities.py calls
register_created_path() to record the absolute path. The registry is persisted
per-project at:
    projects/<project-id>/runtime/engine_created_paths.json

Subsequent write_file calls against the same path check is_engine_created()
before allowing the overwrite. This ensures the engine can update its own
deliverables across capability rounds and re-runs, while never silently
overwriting files the user placed in the repo.

Usage (from engine_runtime.py):
    from engine.work.destructive_guard import check_capability

    def _guarded_execute(cap_req):
        blocked = check_capability(cap_req, role=role, delivery_mode=delivery_mode)
        if blocked is not None:
            return blocked
        return execute_capability(cap_req)

Usage (from capabilities.py _cap_write_file, after a successful new-file write):
    from engine.work.destructive_guard import register_created_path
    register_created_path(safe_path)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Role-based capability allowlists
# ---------------------------------------------------------------------------
# Each role is restricted to the set of capabilities listed here. Capabilities
# absent from the set are hard-blocked for that role. A None entry (or an
# unrecognised role) falls back to PERMISSIVE_ROLES, which passes through
# without restriction (backwards-compatible for future roles).
#
# Active roles:
#   worker         — DevOps/DevSecOps: full write/run/http/platform; deployment guarded by operator prompt
#   research       — read + http probing only; no write or execution
#   review         — read + run/test for verification; no write or deployment
#
# Legacy roles (retained for backwards compatibility with old project artifacts):
#   master, requirements, technical-research, implementation-guide,
#   platform-builder, coding, qa, security, data-analyst, validator

_SHARED_READ_CAPS: frozenset[str] = frozenset(
    {
        "read_file",
        "read_file_lines",
        "stat_file",
        "list_dir",
        "find_files",
        "search_code",
        "load_artifact",
        "fetch_skill",
        "get_kb_candidates",
        "fetch_source",
        "search_sources",
    }
)

_SHARED_SECRETS_CAPS: frozenset[str] = frozenset({"load_secrets", "save_secret"})

_SHARED_GIT_CAPS: frozenset[str] = frozenset(
    {"query_git_status", "query_git_diff", "query_git_log"}
)

ROLE_ALLOWED_CAPABILITIES: dict[str, frozenset[str]] = {
    # --- Lean pipeline roles ---
    "worker": frozenset(
        {
            "write_file",
            "run_command",
            "run_tests",
            "persist_artifact",
            "http_request_with_secret_binding",
            "test_credentials",
            "validate_logic_app_workflow",
            "deploy_logic_app_definition",
            "create_sharepoint_list_schema",
            "create_powerbi_import_bundle",
            "powerbi_import_artifact",
            "powerbi_trigger_refresh",
            "powerbi_check_refresh_status",
        }
        | _SHARED_READ_CAPS
        | _SHARED_SECRETS_CAPS
        | _SHARED_GIT_CAPS
    ),
    "research": frozenset(
        {
            "persist_artifact",
            "http_request_with_secret_binding",
            "test_credentials",
        }
        | _SHARED_READ_CAPS
        | _SHARED_SECRETS_CAPS
    ),
    "review": frozenset(
        {
            "run_command",
            "run_tests",
            "persist_artifact",
        }
        | _SHARED_READ_CAPS
        | _SHARED_SECRETS_CAPS
        | _SHARED_GIT_CAPS
    ),
}

# Roles that bypass the allowlist check (open/unknown roles for compatibility).
_PERMISSIVE_ROLES: frozenset[str] = frozenset()

# ---------------------------------------------------------------------------
# Engine-created path registry
# ---------------------------------------------------------------------------
# In-memory cache, populated on first access per project and on each new write.
# Persisted to projects/<project-id>/runtime/engine_created_paths.json so that
# a new engine run can still overwrite files it created in a previous session.

_ENGINE_CREATED_PATHS: set[str] = set()
_LOADED_PROJECTS: set[str] = set()


def _registry_file(project_id: str) -> Path:
    from engine.work.repo_paths import REPO_ROOT  # noqa: PLC0415
    return REPO_ROOT / "projects" / project_id / "runtime" / "engine_created_paths.json"


def _project_id_from_path(path: Path) -> str | None:
    """Extract project-id from a path like …/projects/{id}/… ."""
    try:
        from engine.work.repo_paths import REPO_ROOT  # noqa: PLC0415
        rel_parts = path.relative_to(REPO_ROOT).parts
        if len(rel_parts) >= 2 and rel_parts[0] == "projects":
            return rel_parts[1]
    except (ValueError, ImportError):
        pass
    return None


def _ensure_project_loaded(project_id: str) -> None:
    """Load the on-disk registry for project_id into the in-memory cache once."""
    if project_id in _LOADED_PROJECTS:
        return
    _LOADED_PROJECTS.add(project_id)
    reg = _registry_file(project_id)
    if reg.exists():
        try:
            entries = json.loads(reg.read_text(encoding="utf-8"))
            if isinstance(entries, list):
                _ENGINE_CREATED_PATHS.update(str(p) for p in entries)
        except Exception:  # noqa: BLE001
            pass


def register_created_path(path: Path | str) -> None:
    """
    Record a path as engine-created so future overwrites are allowed.

    Called by capabilities._cap_write_file after a successful new-file write.
    Persists the entry to disk so re-runs can still update the same file.
    """
    abs_str = str(Path(path).resolve() if not Path(path).is_absolute() else Path(path))
    _ENGINE_CREATED_PATHS.add(abs_str)

    project_id = _project_id_from_path(Path(abs_str))
    if project_id is None:
        return

    reg = _registry_file(project_id)
    try:
        existing: list[str] = []
        if reg.exists():
            existing = json.loads(reg.read_text(encoding="utf-8"))
        if abs_str not in existing:
            existing.append(abs_str)
            reg.parent.mkdir(parents=True, exist_ok=True)
            reg.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass  # Non-fatal — in-memory cache is still updated


def is_engine_created(path: Path | str) -> bool:
    """Return True if this path was created by the engine (current or prior session)."""
    p = Path(path)
    abs_str = str(p.resolve() if not p.is_absolute() else p)
    if abs_str in _ENGINE_CREATED_PATHS:
        return True
    project_id = _project_id_from_path(Path(abs_str))
    if project_id:
        _ensure_project_loaded(project_id)
        return abs_str in _ENGINE_CREATED_PATHS
    return False


# ---------------------------------------------------------------------------
# HTTP guard: permanently blocked URL patterns
# ---------------------------------------------------------------------------
# Each entry: (compiled_regex, frozenset_of_blocked_methods, description)
# These operations can NEVER be performed regardless of delivery_mode.
#
# Two tiers of protection:
#   ABSOLUTE — SharePoint sites and Entra ID users/service principals:
#       DELETE, PATCH, and PUT are all blocked.
#       Rationale: these represent identity and tenant structure that must
#       never be modified or destroyed by an automated agent.
#   DELETION-ONLY — all other protected resources:
#       Only DELETE is hard-blocked.
#       PATCH/PUT still require build_and_deploy but are not permanently
#       forbidden (e.g. updating a Logic App workflow definition is valid work).

# Number of entries in _HARD_BLOCKED_HTTP that are absolute (never promptable).
# These are the first N entries: SharePoint sites + Entra ID users/service principals.
# Everything after this index is a soft block (promptable by the operator).
_ABSOLUTE_HTTP_BLOCK_COUNT: int = 6

_HARD_BLOCKED_HTTP: list[tuple[re.Pattern[str], frozenset[str], str]] = [

    # ── SharePoint sites — NEVER modified or deleted ─────────────────────────
    # Classical REST API
    (
        re.compile(r"/_api/site\b", re.IGNORECASE),
        frozenset({"DELETE", "PATCH", "PUT"}),
        "SharePoint site modification or deletion via classical REST API",
    ),
    (
        re.compile(r"/_api/web\b", re.IGNORECASE),
        frozenset({"DELETE", "PATCH", "PUT"}),
        "SharePoint web modification or deletion via classical REST API",
    ),
    # Microsoft Graph — site root
    (
        re.compile(r"graph\.microsoft\.com/[^/]+/sites/[^/?#]+/?$", re.IGNORECASE),
        frozenset({"DELETE", "PATCH", "PUT"}),
        "Microsoft Graph SharePoint site modification or deletion",
    ),
    # Microsoft Graph — Microsoft 365 group (= team site backbone)
    (
        re.compile(r"graph\.microsoft\.com/[^/]+/groups/[^/?#]+/?$", re.IGNORECASE),
        frozenset({"DELETE", "PATCH", "PUT"}),
        "Microsoft 365 group modification or deletion (destroys associated SharePoint site)",
    ),

    # ── Entra ID users and service principals — NEVER modified or deleted ────
    (
        re.compile(r"graph\.microsoft\.com/[^/]+/users/[^/?#]+/?$", re.IGNORECASE),
        frozenset({"DELETE", "PATCH", "PUT"}),
        "Entra ID user modification or deletion",
    ),
    (
        re.compile(
            r"graph\.microsoft\.com/[^/]+/servicePrincipals/[^/?#]+/?$",
            re.IGNORECASE,
        ),
        frozenset({"DELETE", "PATCH", "PUT"}),
        "Entra ID service principal modification or deletion",
    ),

    # ── Everything below: DELETE-only hard block ──────────────────────────────

    # Azure management — subscription and resource group deletion
    (
        re.compile(
            r"management\.azure\.com/subscriptions/[^/?#]+/?$", re.IGNORECASE
        ),
        frozenset({"DELETE"}),
        "Azure subscription deletion",
    ),
    (
        re.compile(
            r"management\.azure\.com/subscriptions/[^/]+/resourceGroups/[^/?#]+/?$",
            re.IGNORECASE,
        ),
        frozenset({"DELETE"}),
        "Azure resource group deletion",
    ),
    # Azure management — Logic Apps workflow deletion
    (
        re.compile(
            r"management\.azure\.com/subscriptions/[^/]+/resourceGroups/[^/]+"
            r"/providers/Microsoft\.Logic/workflows/[^/?#]+/?$",
            re.IGNORECASE,
        ),
        frozenset({"DELETE"}),
        "Azure Logic Apps workflow deletion",
    ),
    # Azure management — managed API connector deletion
    (
        re.compile(
            r"management\.azure\.com/subscriptions/[^/]+/resourceGroups/[^/]+"
            r"/providers/Microsoft\.Web/connections/[^/?#]+/?$",
            re.IGNORECASE,
        ),
        frozenset({"DELETE"}),
        "Azure managed connector connection deletion",
    ),

    # Power BI — workspace and content deletion
    # URL format: api.powerbi.com/v1.0/myorg/groups/{id}[/resource/{id}]
    (
        re.compile(r"api\.powerbi\.com/.+/groups/[^/?#]+/?$", re.IGNORECASE),
        frozenset({"DELETE"}),
        "Power BI workspace deletion",
    ),
    (
        re.compile(
            r"api\.powerbi\.com/.+/groups/[^/]+/datasets/[^/?#]+/?$", re.IGNORECASE
        ),
        frozenset({"DELETE"}),
        "Power BI dataset deletion",
    ),
    (
        re.compile(
            r"api\.powerbi\.com/.+/groups/[^/]+/reports/[^/?#]+/?$", re.IGNORECASE
        ),
        frozenset({"DELETE"}),
        "Power BI report deletion",
    ),
    (
        re.compile(
            r"api\.powerbi\.com/.+/groups/[^/]+/dashboards/[^/?#]+/?$", re.IGNORECASE
        ),
        frozenset({"DELETE"}),
        "Power BI dashboard deletion",
    ),
    (
        re.compile(
            r"api\.powerbi\.com/.+/groups/[^/]+/dataflows/[^/?#]+/?$", re.IGNORECASE
        ),
        frozenset({"DELETE"}),
        "Power BI dataflow deletion",
    ),

    # Microsoft Graph — SharePoint list deletion
    (
        re.compile(
            r"graph\.microsoft\.com/[^/]+/sites/[^/]+/lists/[^/?#]+/?$",
            re.IGNORECASE,
        ),
        frozenset({"DELETE"}),
        "Microsoft Graph SharePoint list deletion",
    ),

    # Microsoft Graph — Teams team and channel deletion
    (
        re.compile(r"graph\.microsoft\.com/[^/]+/teams/[^/?#]+/?$", re.IGNORECASE),
        frozenset({"DELETE"}),
        "Microsoft Teams team deletion",
    ),
    (
        re.compile(
            r"graph\.microsoft\.com/[^/]+/teams/[^/]+/channels/[^/?#]+/?$",
            re.IGNORECASE,
        ),
        frozenset({"DELETE"}),
        "Microsoft Teams channel deletion",
    ),

    # Microsoft Graph — managed metadata term store deletion
    (
        re.compile(
            r"graph\.microsoft\.com/[^/]+/termStore/groups/[^/?#]+/?$",
            re.IGNORECASE,
        ),
        frozenset({"DELETE"}),
        "SharePoint managed metadata term store group deletion",
    ),
]

# HTTP methods that mutate state and require delivery_mode: build_and_deploy
_MUTATING_METHODS: frozenset[str] = frozenset({"DELETE", "PATCH", "PUT", "POST"})

# ---------------------------------------------------------------------------
# Shell command blocklist
# ---------------------------------------------------------------------------
# Each entry: (compiled_regex, description)
# Commands matching any pattern are hard-blocked in run_command.
_BLOCKED_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    # Recursive deletion — rm -r, rm -rf, rm -fr, rm --recursive, etc.
    (
        re.compile(r"\brm\b.*\s(-[^\s]*r|-r\b|--recursive\b)", re.IGNORECASE),
        "recursive file deletion (rm -r / rm -rf / rm --recursive)",
    ),
    # find with -delete or piped to rm
    (
        re.compile(r"\bfind\b.+(-delete\b|-exec\s+rm\b)", re.IGNORECASE),
        "find with destructive action (-delete or -exec rm)",
    ),
    # Fork bomb
    (
        re.compile(r":\s*\(\s*\)\s*\{.*\}\s*;\s*:"),
        "fork bomb pattern",
    ),
    # shred — irreversible file overwrite/deletion
    (
        re.compile(r"\bshred\b", re.IGNORECASE),
        "shred (irreversible file destruction)",
    ),
    # Windows-style recursive delete
    (
        re.compile(r"\brd\b.*\/s\b|\brmdir\b.*\/s\b", re.IGNORECASE),
        "Windows recursive directory removal (rd /s or rmdir /s)",
    ),
    # PowerShell — bulk SharePoint item/file removal cmdlets
    (
        re.compile(r"\bRemove-PnPListItem\b", re.IGNORECASE),
        "PowerShell PnP SharePoint list item removal (destructive to list data)",
    ),
    (
        re.compile(r"\bRemove-PnPFile\b|\bRemove-PnPFolder\b", re.IGNORECASE),
        "PowerShell PnP SharePoint file or folder removal",
    ),
    (
        re.compile(r"\bRemove-MgSiteListItem\b", re.IGNORECASE),
        "Microsoft Graph PowerShell SharePoint list item removal",
    ),
]

# ---------------------------------------------------------------------------
# Shell HTTP tool bypass detection
# ---------------------------------------------------------------------------
# Agents may try to call curl/wget/az-rest/PowerShell web cmdlets with mutating
# HTTP methods — bypassing the structured http_request_with_secret_binding guard.
# Detection is action-based: any DELETE/PATCH/PUT via a shell HTTP tool is caught
# regardless of which domain or API is being called.
#
# Detection strategy: both conditions must be true in the same command:
#   1. a recognised HTTP CLI tool is present
#   2. a mutating method flag is present (e.g. -X DELETE, --method PATCH)

_SHELL_HTTP_TOOLS: re.Pattern[str] = re.compile(
    r"\b(curl|wget|Invoke-RestMethod|Invoke-WebRequest|iwr|irm"
    r"|az\s+rest\b|az\s+resource\b)\b",
    re.IGNORECASE,
)

# Matches the method flag + method value for common HTTP CLI styles:
#   curl: -X DELETE, -XDELETE, --request DELETE
#   wget: --method DELETE
#   az rest: --method DELETE
#   PowerShell: -Method DELETE
_SHELL_HTTP_MUTATING: re.Pattern[str] = re.compile(
    r"(?:-X\s*|-X(?=DELETE|PATCH|PUT)|--request\s+|--method\s+|-Method\s+)"
    r"(DELETE|PATCH|PUT)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Script content scanning
# ---------------------------------------------------------------------------
# When an agent writes a script file or runs one, the engine scans its
# content for destructive patterns before the operation proceeds.
#
# Two tiers are checked inside script content:
#   1. Shell-level blocklist patterns (_BLOCKED_COMMANDS) — catches rm -rf,
#      fork bombs, PowerShell removal cmdlets, etc. embedded in any script.
#   2. HTTP mutation + protected domain — catches Python/PowerShell/shell
#      HTTP client calls (requests.delete, Invoke-RestMethod -Method DELETE,
#      curl -X DELETE …) targeting protected cloud resources.

# Script file extensions whose content is scanned on write and run.
_SCRIPT_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".sh", ".bash", ".zsh", ".fish",
    ".ps1", ".psm1", ".psd1",
    ".bat", ".cmd",
    ".js", ".ts", ".rb",
})

# Interpreter names that indicate the next argument is a script file.
_SCRIPT_INTERPRETER_RE: re.Pattern[str] = re.compile(
    r"^(?:python3?|bash|sh|zsh|fish|pwsh|powershell(?:\.exe)?|node|ruby|perl|rscript)$",
    re.IGNORECASE,
)

# HTTP mutation patterns for high-level language HTTP clients.
# Detection is action-based — no domain allowlist. Two conditions must hold in
# the same file to avoid false positives against ORM/cache .delete() calls:
#   1. A mutation call pattern is present
#   2. Any HTTPS URL is present in the file (confirms it's making remote HTTP calls)
#
# Patterns caught:
#   Python requests/httpx:   requests.delete(url), httpx.patch(url)
#   Generic method call:     session.delete("https://..."), client.put("https://...")
#   Keyword argument style:  method="DELETE", method='PATCH'
#   Dict/JSON style:         "method": "DELETE"
# Shell-style patterns are already covered by _SHELL_HTTP_MUTATING above.
_SCRIPT_HTTP_MUTATION: re.Pattern[str] = re.compile(
    r"(?:"
    r"(?:requests|httpx|aiohttp)\s*\.\s*(?:delete|patch|put)\s*\("  # lib.method(
    r'|\.\s*(?:delete|patch|put)\s*\(\s*["\']https?://'             # .method("https://
    r'|method\s*=\s*["\']?(DELETE|PATCH|PUT)["\']?'                 # method="DELETE"
    r'|["\']method["\']\s*:\s*["\']?(DELETE|PATCH|PUT)'             # "method": "DELETE"
    r")",
    re.IGNORECASE,
)

# Presence of any remote URL in the file — used as a co-condition with
# _SCRIPT_HTTP_MUTATION to exclude ORM/cache .delete() calls that have no URL.
_SCRIPT_HAS_URL: re.Pattern[str] = re.compile(r"https?://", re.IGNORECASE)


def _scan_script_content(content: str) -> str | None:
    """
    Scan script content for destructive patterns.

    Returns a human-readable description of the first issue found,
    or None if the content is clean.
    """
    # Tier 1: shell-level blocklist (rm -rf, fork bomb, PowerShell cmdlets, etc.)
    for pattern, description in _BLOCKED_COMMANDS:
        if pattern.search(content):
            return description

    # Tier 2a: shell HTTP tool + mutating method (any URL — action-based, not domain-based)
    if _SHELL_HTTP_TOOLS.search(content) and _SHELL_HTTP_MUTATING.search(content):
        return "HTTP mutation via shell tool (curl/wget/az rest/PowerShell) — DELETE/PATCH/PUT"

    # Tier 2b: high-level language HTTP client mutation + any remote URL present
    # The URL co-condition filters out ORM/cache .delete() calls that have no URL.
    if _SCRIPT_HTTP_MUTATION.search(content) and _SCRIPT_HAS_URL.search(content):
        return "HTTP mutation call (DELETE/PATCH/PUT) to a remote URL"

    return None


def _find_script_in_command(cmd: list, cwd: str | None = None) -> "Path | None":
    """
    Return the script file Path referenced by a run_command argument list, or None.

    Handles two forms:
      - Direct:      ["./deploy.sh"] or ["/abs/path/script.py"]
      - Interpreter: ["python3", "script.py"] or ["bash", "-e", "deploy.sh"]
    """
    if not cmd:
        return None

    def _resolve(p_str: str) -> "Path":
        p = Path(p_str)
        if not p.is_absolute() and cwd:
            p = Path(cwd) / p
        return p

    first = str(cmd[0])

    # Direct script execution
    if Path(first).suffix.lower() in _SCRIPT_EXTENSIONS:
        return _resolve(first)

    # Interpreter + script (skip flags between interpreter and file)
    if _SCRIPT_INTERPRETER_RE.match(Path(first).name):
        for arg in cmd[1:]:
            s = str(arg)
            if not s.startswith("-") and Path(s).suffix.lower() in _SCRIPT_EXTENSIONS:
                return _resolve(s)

    return None


# ---------------------------------------------------------------------------
# Protected write paths
# ---------------------------------------------------------------------------
# Agents must not write to engine source, agent specs, docs, or config dirs.
# Writes are expected to land in projects/<project-id>/ directories.
_PROTECTED_WRITE_PREFIXES: tuple[str, ...] = (
    "engine/",
    "agents/",
    "docs/",
    "config/",
    "knowledge/",
    "skills/",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _blocked(capability: str, message: str, *, absolute: bool = False) -> dict[str, Any]:
    return {
        "capability": capability,
        "status": "failed",
        "result": None,
        "issues": [f"[destructive-guard] BLOCKED: {message}"],
        "absolute": absolute,
    }


def is_absolute_block(blocked_result: dict[str, Any]) -> bool:
    """Return True if this block cannot be overridden by the operator."""
    return bool(blocked_result.get("absolute", False))


def _check_http_request(
    capability: str,
    arguments: dict[str, Any],
    delivery_mode: str | None,
) -> dict[str, Any] | None:
    method = str(arguments.get("method", "GET")).upper()
    url = str(arguments.get("url", ""))

    # Absolute blocks — SharePoint sites + Entra ID users/SPs; never promptable
    for pattern, blocked_methods, description in _HARD_BLOCKED_HTTP[:_ABSOLUTE_HTTP_BLOCK_COUNT]:
        if method in blocked_methods and pattern.search(url):
            return _blocked(
                capability,
                f"{description} is permanently prohibited. "
                f"Method: {method}  URL: {url}",
                absolute=True,
            )

    # Soft blocks — promptable by the operator
    for pattern, blocked_methods, description in _HARD_BLOCKED_HTTP[_ABSOLUTE_HTTP_BLOCK_COUNT:]:
        if method in blocked_methods and pattern.search(url):
            return _blocked(
                capability,
                f"{description} is permanently prohibited. "
                f"Method: {method}  URL: {url}",
            )

    # All mutating methods require build_and_deploy
    if method in _MUTATING_METHODS and delivery_mode != "build_and_deploy":
        return _blocked(
            capability,
            f"HTTP {method} requires delivery_mode 'build_and_deploy'. "
            f"Current mode: '{delivery_mode or 'not set'}'. "
            f"URL: {url}",
        )

    # Catch-all: any DELETE, PATCH, or PUT not already caught above requires
    # operator approval regardless of which API or domain is being called.
    # (POST is excluded — it creates resources and is less inherently destructive.)
    if method in frozenset({"DELETE", "PATCH", "PUT"}):
        return _blocked(
            capability,
            f"HTTP {method} to '{url}' requires operator approval. "
            f"All mutating operations must be reviewed.",
        )

    return None


def _check_deploy_logic_app(
    capability: str,
    _arguments: dict[str, Any],
    delivery_mode: str | None,
) -> dict[str, Any] | None:
    if delivery_mode != "build_and_deploy":
        return _blocked(
            capability,
            f"deploy_logic_app_definition requires delivery_mode 'build_and_deploy'. "
            f"Current mode: '{delivery_mode or 'not set'}'.",
        )
    return None


def _check_powerbi_import(
    capability: str,
    _arguments: dict[str, Any],
    delivery_mode: str | None,
) -> dict[str, Any] | None:
    if delivery_mode != "build_and_deploy":
        return _blocked(
            capability,
            f"powerbi_import_artifact requires delivery_mode 'build_and_deploy'. "
            f"Current mode: '{delivery_mode or 'not set'}'.",
        )
    return None


def _check_run_command(
    capability: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    cmd = arguments.get("command", [])
    cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)
    for pattern, description in _BLOCKED_COMMANDS:
        if pattern.search(cmd_str):
            return _blocked(
                capability,
                f"{description} is not allowed. Command: {cmd_str[:300]}",
            )

    # Detect HTTP CLI tools (curl/wget/az rest/PowerShell) used with mutating methods.
    # Action-based detection — any DELETE/PATCH/PUT is caught regardless of domain.
    if _SHELL_HTTP_TOOLS.search(cmd_str):
        method_match = _SHELL_HTTP_MUTATING.search(cmd_str)
        if method_match:
            method = method_match.group(1).upper()
            return _blocked(
                capability,
                f"HTTP {method} via shell tool is not allowed. "
                f"Use http_request_with_secret_binding instead — it routes through "
                f"the operator approval flow. Command: {cmd_str[:300]}",
            )

    # Script content scan — read the script file being executed and scan it
    # for destructive patterns before the command runs.
    cmd_list = arguments.get("command", [])
    if isinstance(cmd_list, list):
        script_path = _find_script_in_command(cmd_list, arguments.get("cwd"))
        if script_path is not None and script_path.exists():
            try:
                content = script_path.read_text(encoding="utf-8", errors="ignore")
                issue = _scan_script_content(content)
                if issue:
                    return _blocked(
                        capability,
                        f"Script '{script_path.name}' contains a destructive pattern: "
                        f"{issue}. Review the script before running it.",
                    )
            except Exception:  # noqa: BLE001
                pass  # Non-fatal — guard must never crash the pipeline

    return None


def _check_write_file(
    capability: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Block writes to protected directories and to pre-existing user files.

    Allows overwrites only if the file was previously created by the engine
    (tracked in the engine-created path registry).
    """
    raw_path = arguments.get("path", "")
    if not raw_path:
        return None

    try:
        from engine.work.repo_paths import REPO_ROOT  # noqa: PLC0415

        path = Path(raw_path)
        if not path.is_absolute():
            path = REPO_ROOT / path

        # 1. Protected directory check
        try:
            rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        except ValueError:
            # Outside repo — caught by ensure_within_repo in the handler
            return None

        for prefix in _PROTECTED_WRITE_PREFIXES:
            if rel.startswith(prefix) or rel == prefix.rstrip("/"):
                return _blocked(
                    capability,
                    f"Writing to '{prefix}' is not permitted for agents. "
                    f"Deliverables must be written to the projects/ directory.",
                )

        # 2. Pre-existing file overwrite protection
        # If the file exists and was NOT created by the engine, block the overwrite.
        if path.exists() and not is_engine_created(path):
            return _blocked(
                capability,
                f"Cannot overwrite '{raw_path}' — this file was not created by the "
                f"engine. Only engine-created files may be updated. If you need to "
                f"replace a user file, report it as a blocker instead.",
            )

        # 3. Script content scan — detect destructive operations in scripts being written.
        if path.suffix.lower() in _SCRIPT_EXTENSIONS:
            content = str(arguments.get("content", ""))
            if content:
                issue = _scan_script_content(content)
                if issue:
                    return _blocked(
                        capability,
                        f"Script '{path.name}' contains a destructive pattern: {issue}. "
                        f"Review the script before writing.",
                    )

    except Exception:  # noqa: BLE001
        # Guard must never crash the pipeline; pass through on unexpected errors
        pass

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_capability(
    request: dict[str, Any],
    *,
    role: str | None,
    delivery_mode: str | None,
) -> dict[str, Any] | None:
    """
    Evaluate a capability request against the destructive-action policy.

    Returns a failure result dict if the request is blocked, or None to
    allow the request to proceed to the normal capability handler.

    Parameters
    ----------
    request:
        The raw capability request dict from the agent response.
    role:
        The orchestration role of the currently executing agent.
    delivery_mode:
        The delivery mode set by master (e.g. 'build_only', 'build_and_deploy').
        None means no delivery mode was specified (safe default: non-deploy).
    """
    capability = request.get("capability", "")
    arguments = request.get("arguments") or {}

    # 1. Role-based allowlist
    if role is not None and role not in _PERMISSIVE_ROLES:
        allowed = ROLE_ALLOWED_CAPABILITIES.get(role)
        if allowed is not None and capability not in allowed:
            return _blocked(
                capability,
                f"Role '{role}' is not permitted to use capability '{capability}'. "
                f"This capability is outside the role's authorised set.",
            )

    # 2. Capability-specific destructive checks
    if capability == "http_request_with_secret_binding":
        return _check_http_request(capability, arguments, delivery_mode)

    if capability == "deploy_logic_app_definition":
        return _check_deploy_logic_app(capability, arguments, delivery_mode)

    if capability == "powerbi_import_artifact":
        return _check_powerbi_import(capability, arguments, delivery_mode)

    if capability == "run_command":
        return _check_run_command(capability, arguments)

    if capability == "write_file":
        return _check_write_file(capability, arguments)

    return None
