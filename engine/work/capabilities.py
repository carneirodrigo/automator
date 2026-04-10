"""Runtime capability handlers and dispatch table."""

from __future__ import annotations

import json
import mimetypes
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from engine.work.credential_tester import CredentialTester
from engine.work import prompts as prompt_work
from engine.work.repo_paths import REGISTRY_PATH, ensure_within_repo
from engine.work.secret_detector import scan_for_leaked_values
from engine.work.skill_loader import fetch_skill as loader_fetch_skill, load_skill_body

_ENV: dict[str, Any] = {}


def configure_capability_environment(**kwargs: Any) -> None:
    _ENV.update(kwargs)


def _require(name: str) -> Any:
    value = _ENV.get(name)
    if value is None:
        raise RuntimeError(f"Capability environment missing: {name}")
    return value


def _detect_python() -> str:
    """Return the best Python interpreter for this project.

    Prefers the repo-local .venv so tests run against the same environment
    the project was installed into, regardless of which Python launched the
    engine.  Works for all LLM backends — the interpreter is resolved in the
    host, not inside any agent subprocess.
    """
    repo_root = _ENV.get("REPO_ROOT")
    if repo_root:
        for name in ("python3", "python"):
            candidate = Path(repo_root) / ".venv" / "bin" / name
            if candidate.exists():
                return str(candidate)
    return sys.executable


_CAPABILITY_QUICK_REFERENCE = """
Runtime Capabilities (how to request operations from the engine):
Include a top-level "capability_requests" array in your JSON response. The engine executes them and re-invokes you with results. You have up to 5 rounds — batch requests to save rounds.

Request format:
{"capability_requests": [{"capability": "read_file", "arguments": {"path": "/absolute/path"}, "reason": "why"}]}

Available capabilities with argument shapes:
- read_file: {"path": "/absolute/path"} — use to read source code, configs, or inputs. NOT for orchestration artifacts — use load_artifact for those.
- write_file: {"path": "/absolute/path", "content": "..."} — use to write user-facing deliverables (scripts, docs, configs) into delivery/. NOT for orchestration state — use persist_artifact for that.
- run_command: {"command": ["python3", "script.py"], "cwd": "/path", "timeout": 30} — execute shell command; use to run tests, linters, or probe scripts.
- load_artifact: {"artifact_path": "/absolute/path/artifact.json"} — use to load a prior agent's structured output for review or handoff. NOT for plain files — use read_file for those.
- persist_artifact: {"runtime_dir": "/path/to/runtime", "agent": "role-name", "data": {...}} — use to save your structured result for downstream agents. NOT for user deliverables — use write_file for those.
- test_credentials: {"credential_type": "azure_ad", "service": "graph", "credentials": {...}} — use to validate credential format/reachability before storing or using them.
- load_secrets: {"project_id": "my-project", "keys": ["optional"]} — use to retrieve project secrets before using them in commands or configs. Always load secrets rather than hardcoding values.
- save_secret: {"project_id": "my-project", "key": "...", "value": "...", "type": "generic"} — use to store a discovered or user-provided secret in the project vault.
- fetch_skill: {"skill_id": "vendor--skill-name"} — use to fetch a skill from the local catalog/cache for evaluation.
- get_kb_candidates: {"task": "...", "reason": "...", "project_desc": "...", "offset": 10, "limit": 10, "exclude_ids": ["entry-id"]} — request another compact batch of local KB candidate cards without loading the full manifest.
- http_request_with_secret_binding: {"project_id": "my-project", "method": "GET", "url": "...", "headers": {"Authorization": "Bearer {{secret:graph_token}}"}} — send an HTTPS request with secrets bound at runtime.
- validate_logic_app_workflow: {"path": "/absolute/path/workflow.json"} or {"definition": {...}} — validate a Logic Apps workflow or workflow resource shape.
- deploy_logic_app_definition: {"template_path": "/absolute/path/template.json", "resource_group": "rg-name"} — deploy a Logic Apps ARM template via Azure CLI.
- create_sharepoint_list_schema: {"path": "/absolute/path/list-schema.json", "schema": {...}} — persist a SharePoint list schema artifact in the project.
- create_powerbi_import_bundle: {"path": "/absolute/path/powerbi-import.json", "bundle": {...}} — persist a Power BI import bundle in the project.
- powerbi_import_artifact: {"project_id": "my-project", "group_id": "...", "file_path": "/absolute/path/report.pbix", "dataset_display_name": "Report", "access_token_secret_key": "powerbi_access_token"} — import a Power BI artifact using the REST API.
- powerbi_trigger_refresh: {"project_id": "my-project", "group_id": "...", "dataset_id": "...", "access_token_secret_key": "powerbi_access_token"} — trigger a Power BI dataset refresh.
- powerbi_check_refresh_status: {"project_id": "my-project", "group_id": "...", "dataset_id": "...", "access_token_secret_key": "powerbi_access_token"} — inspect recent Power BI refresh status.
- query_git_status: {"cwd": "/optional/path"} — structured git status: branch, ahead/behind, staged/unstaged/untracked lists. Use instead of run_command("git status") — returns typed data, not prose.
- query_git_diff: {"ref": "HEAD~1 HEAD", "paths": ["file.py"], "stat_only": false, "cwd": "/optional/path"} — structured diff grouped by file. ref is space-separated git args (e.g. "HEAD~1 HEAD", "HEAD~1..HEAD"). stat_only returns counts only.
- query_git_log: {"n": 10, "ref": "HEAD", "paths": [], "cwd": "/optional/path"} — structured commit log: hash, author, date, message per commit. n capped at 50.
- search_code: {"pattern": "regex", "path": "engine/work/", "file_glob": "*.py", "context_lines": 0, "case_insensitive": false, "max_matches": 50} — structured code search returning {file, line, content} per match. Use instead of run_command("grep ...") — structured, capped, relative paths.
- run_tests: {"path": "engine/tests/", "pattern": "ToonAdapter", "timeout": 120} — run project tests via the repo-local Python; returns {passed, failed, errors, skipped, failures[]}. path can be a directory or dotted module. pattern filters by test name.
- list_dir: {"path": "engine/work/", "show_hidden": false, "max_entries": 200} — structured directory listing: sorted entries with name, type, size_bytes. Skips .git, __pycache__, .venv, node_modules automatically. Use instead of run_command(['ls', ...]).
- find_files: {"pattern": "*.py", "path": "engine/", "type": "file", "max_results": 100} — find files by name glob. type: "file" (default), "dir", or "any". Returns paths relative to repo root. Skips noise dirs. Use instead of run_command(['find', ...]).
- stat_file: {"path": "engine/work/capabilities.py"} — file metadata without reading content: exists, type, size_bytes, line_count (for text files ≤ 10 MB). Use before read_file to check size. path can be absolute or relative to repo root.
- read_file_lines: {"path": "output.log", "mode": "head", "n": 50} — read first (head) or last (tail) N lines of a file (max 500). Returns total_lines alongside selected lines. Use instead of run_command(['head'/'tail', ...]) for large files.

Batch independent requests in the same round (for example, read 3 files + run 1 command = 1 round, not 4).
""".strip()


def validate_capability_request(request: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for field in ("capability", "arguments", "reason"):
        if field not in request:
            warnings.append(f"capability_request: missing required field '{field}'")
    return warnings


def _load_secret_map(project_id: str, keys: list[str] | None = None) -> dict[str, str]:
    load_secrets = _require("load_secrets")
    store = load_secrets(project_id, keys)
    return {
        entry.get("key", ""): entry.get("value", "")
        for entry in store.get("entries", [])
        if entry.get("key") and entry.get("value")
    }


def _bind_secret_placeholders(value: Any, secret_map: dict[str, str]) -> Any:
    if isinstance(value, str):
        result = value
        # Cap iterations to prevent infinite loops when a secret value itself
        # contains {{secret:...}} placeholders (e.g. mutual references).
        max_replacements = len(secret_map) + 5
        iterations = 0
        while "{{secret:" in result:
            iterations += 1
            if iterations > max_replacements:
                raise ValueError("Secret placeholder expansion exceeded maximum iterations — possible circular reference")
            start = result.find("{{secret:")
            end = result.find("}}", start)
            if end == -1:
                break
            key = result[start + 9:end].strip()
            if key not in secret_map:
                raise KeyError(f"Missing secret binding for key '{key}'")
            result = result[:start] + secret_map[key] + result[end + 2:]
        return result
    if isinstance(value, list):
        return [_bind_secret_placeholders(item, secret_map) for item in value]
    if isinstance(value, dict):
        return {k: _bind_secret_placeholders(v, secret_map) for k, v in value.items()}
    return value


def _redact_secrets_from_text(text: str, secret_map: dict[str, str]) -> str:
    """Replace any secret values found in text with [REDACTED]."""
    for secret_value in secret_map.values():
        if secret_value and len(secret_value) >= 8 and secret_value in text:
            text = text.replace(secret_value, "[REDACTED]")
    return text


def _write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _powerbi_request(
    capability: str,
    *,
    project_id: str,
    group_id: str,
    relative_path: str,
    access_token_secret_key: str,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not project_id or not group_id or not access_token_secret_key:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": ["project_id, group_id, and access_token_secret_key are required"],
        }
    secret_map = _load_secret_map(project_id, [access_token_secret_key])
    token = secret_map.get(access_token_secret_key)
    if not token:
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Secret not found: {access_token_secret_key}"]}
    request_headers = {"Authorization": f"Bearer {token}"}
    if headers:
        request_headers.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    url = f"https://api.powerbi.com/v1.0/myorg/groups/{group_id}/{relative_path.lstrip('/')}"
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
            raw_body = _redact_secrets_from_text(raw_body, secret_map)
            content_type = resp.headers.get("Content-Type", "")
            parsed_body: Any = raw_body
            if "json" in content_type.lower() and raw_body:
                try:
                    parsed_body = json.loads(raw_body)
                except json.JSONDecodeError:
                    parsed_body = raw_body
            return {
                "capability": capability,
                "status": "ok",
                "result": {
                    "status_code": getattr(resp, "status", 200),
                    "headers": dict(resp.headers.items()),
                    "body": parsed_body,
                },
                "issues": [],
            }
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        body_text = _redact_secrets_from_text(body_text, secret_map)
        return {
            "capability": capability,
            "status": "failed",
            "result": {"status_code": exc.code, "body": body_text},
            "issues": [f"HTTP {exc.code}: {body_text[:500]}"],
        }
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}


def _cap_test_credentials(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    tester = CredentialTester()
    cred_type = arguments.get("credential_type", "")
    service = arguments.get("service", "")
    creds = arguments.get("credentials", {})

    cred_dispatch = {
        "api_key": lambda: tester.test_api_key(
            api_key=creds.get("api_key", ""),
            service=service,
            endpoint=creds.get("endpoint"),
        ),
        "bearer_token": lambda: tester.test_bearer_token(
            token=creds.get("token", ""),
            endpoint=creds.get("endpoint", ""),
        ),
        "basic_auth": lambda: tester.test_basic_auth(
            username=creds.get("username", ""),
            password=creds.get("password", ""),
            endpoint=creds.get("endpoint", ""),
        ),
        "aws": lambda: tester.test_aws_credentials(
            access_key_id=creds.get("access_key_id", ""),
            secret_access_key=creds.get("secret_access_key", ""),
            region=creds.get("region", "us-east-1"),
        ),
        "azure": lambda: tester.test_azure_credentials(
            tenant_id=creds.get("tenant_id", ""),
            client_id=creds.get("client_id", ""),
            client_secret=creds.get("client_secret", ""),
        ),
    }

    handler = cred_dispatch.get(cred_type)
    if not handler:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": [f"Unknown credential type: {cred_type}"],
        }

    result = handler()
    return {
        "capability": capability,
        "status": "ok" if result.valid else "failed",
        "result": result.to_dict(),
        "issues": [result.error_detail] if result.error_detail else [],
    }


def _cap_list_projects(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    load_json = _require("load_json")
    registry = load_json(REGISTRY_PATH)
    projects = registry.get("projects", [])
    return {
        "capability": capability,
        "status": "ok",
        "result": {
            "projects": [
                {"project_id": p["project_id"], "project_name": p["project_name"]}
                for p in projects
            ]
        },
        "issues": [],
    }


def _cap_resolve_project(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    load_json = _require("load_json")
    project_id = arguments.get("project_id", "")
    registry = load_json(REGISTRY_PATH)
    for project in registry.get("projects", []):
        if project["project_id"] == project_id:
            return {"capability": capability, "status": "ok", "result": project, "issues": []}
    return {
        "capability": capability,
        "status": "failed",
        "result": None,
        "issues": [f"Project not found: {project_id}"],
    }


def _cap_init_project(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    bootstrap_project = _require("bootstrap_project")
    try:
        new_project = bootstrap_project(arguments)
        return {"capability": capability, "status": "ok", "result": new_project, "issues": []}
    except (KeyError, OSError) as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}


def _cap_load_task_state(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    load_json = _require("load_json")
    runtime_dir = arguments.get("runtime_dir", "")
    try:
        safe_dir = ensure_within_repo(Path(runtime_dir), "load_task_state runtime_dir")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    state_path = safe_dir / "state" / "active_task.json"
    if not state_path.exists():
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": [f"State file not found: {state_path}"],
        }
    return {"capability": capability, "status": "ok", "result": load_json(state_path), "issues": []}


def _cap_save_task_state(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    write_json = _require("write_json")
    runtime_dir = arguments.get("runtime_dir", "")
    try:
        safe_dir = ensure_within_repo(Path(runtime_dir), "save_task_state runtime_dir")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    state_path = safe_dir / "state" / "active_task.json"
    try:
        write_json(state_path, arguments.get("state", {}))
        return {"capability": capability, "status": "ok", "result": {"path": str(state_path)}, "issues": []}
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}


def _cap_load_memory(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    load_json = _require("load_json")
    runtime_dir = arguments.get("runtime_dir", "")
    try:
        safe_dir = ensure_within_repo(Path(runtime_dir), "load_memory runtime_dir")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    memory_dir = safe_dir / "memory"
    if not memory_dir.exists():
        return {"capability": capability, "status": "ok", "result": {"entries": []}, "issues": []}
    entries = [{"file": file.name, "data": load_json(file)} for file in sorted(memory_dir.glob("*.json"))]
    return {"capability": capability, "status": "ok", "result": {"entries": entries}, "issues": []}


def _cap_save_memory(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    write_json = _require("write_json")
    runtime_dir = arguments.get("runtime_dir", "")
    try:
        safe_dir = ensure_within_repo(Path(runtime_dir), "save_memory runtime_dir")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    key = str(arguments.get("key", "")).replace("/", "_").replace("\\", "_").replace("..", "_")
    memory_path = safe_dir / "memory" / f"{key}.json"
    try:
        ensure_within_repo(memory_path, "save_memory final path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    try:
        write_json(memory_path, arguments.get("data", {}))
        return {"capability": capability, "status": "ok", "result": {"path": str(memory_path)}, "issues": []}
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}


def _cap_load_artifact(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    load_json = _require("load_json")
    artifact_path = arguments.get("artifact_path", "")
    try:
        safe_path = ensure_within_repo(Path(artifact_path), "load_artifact path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    if not safe_path.exists():
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": [f"Artifact not found: {artifact_path}"],
        }
    return {"capability": capability, "status": "ok", "result": load_json(safe_path), "issues": []}


def _cap_persist_artifact(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    write_json = _require("write_json")
    infer_project_id = _require("_infer_project_id_from_path")
    get_project_secret_values = _require("_get_project_secret_values")
    max_size = _require("MAX_CAPABILITY_WRITE_SIZE")
    runtime_dir = arguments.get("runtime_dir", "")
    try:
        safe_dir = ensure_within_repo(Path(runtime_dir), "persist_artifact runtime_dir")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    data = arguments.get("data", {})
    serialized = json.dumps(data)
    if len(serialized) > max_size:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": [f"Artifact payload too large ({len(serialized)} bytes, limit {max_size})"],
        }
    project_id = infer_project_id(safe_dir)
    if project_id:
        secret_values = get_project_secret_values(project_id)
        if secret_values:
            leaked = scan_for_leaked_values(serialized, secret_values)
            if leaked:
                return {
                    "capability": capability,
                    "status": "failed",
                    "result": None,
                    "issues": [
                        f"BLOCKED: Artifact contains secret value(s): {', '.join(leaked)}. Do not embed secrets in artifacts."
                    ],
                }
    artifacts_dir = safe_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    agent_name = str(arguments.get('agent', 'unknown')).replace("/", "_").replace("\\", "_").replace("..", "_")
    artifact_path = artifacts_dir / f"{agent_name}_result_{ts}.json"
    try:
        ensure_within_repo(artifact_path, "persist_artifact final path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    write_json(artifact_path, data)
    return {"capability": capability, "status": "ok", "result": {"path": str(artifact_path)}, "issues": []}


def _cap_read_file(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    max_input_size = _require("MAX_FILE_READ_SIZE")
    file_path = arguments.get("path", "")
    try:
        safe_path = ensure_within_repo(Path(file_path), "read_file path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    if not safe_path.exists():
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"File not found: {file_path}"]}
    try:
        content = safe_path.read_text(encoding="utf-8")
        return {
            "capability": capability,
            "status": "ok",
            "result": {"path": file_path, "content": content[:max_input_size]},
            "issues": [],
        }
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}


def _cap_write_file(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    infer_project_id = _require("_infer_project_id_from_path")
    get_project_secret_values = _require("_get_project_secret_values")
    max_size = _require("MAX_CAPABILITY_WRITE_SIZE")
    file_path = arguments.get("path", "")
    content = arguments.get("content", "")
    if not isinstance(content, str):
        return {"capability": capability, "status": "failed", "result": None, "issues": ["content must be a string"]}
    if len(content) > max_size:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": [f"Content too large ({len(content)} bytes, limit {max_size})"],
        }
    try:
        safe_path = ensure_within_repo(Path(file_path), "write_file path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    project_id = infer_project_id(safe_path)
    if project_id:
        secret_values = get_project_secret_values(project_id)
        if secret_values:
            leaked = scan_for_leaked_values(content, secret_values)
            if leaked:
                return {
                    "capability": capability,
                    "status": "failed",
                    "result": None,
                    "issues": [
                        f"BLOCKED: Content contains secret value(s): {', '.join(leaked)}. Use environment variables or config references instead of embedding secrets in files."
                    ],
                }
    try:
        file_existed = safe_path.exists()
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text(content, encoding="utf-8")
        if not file_existed:
            try:
                from engine.work.destructive_guard import register_created_path  # noqa: PLC0415
                register_created_path(safe_path)
            except Exception:  # noqa: BLE001
                pass
        return {"capability": capability, "status": "ok", "result": {"path": file_path}, "issues": []}
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}


def _cap_run_command(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _require("REPO_ROOT")
    timeout_limit = _require("SPAWN_TIMEOUT_SECONDS")
    inline_limit = _require("CMD_OUTPUT_INLINE_LIMIT")
    cmd = arguments.get("command", [])
    if isinstance(cmd, str):
        import shlex  # noqa: PLC0415
        cmd = shlex.split(cmd)
    cwd = arguments.get("cwd", str(repo_root))
    try:
        safe_cwd = ensure_within_repo(Path(cwd), "run_command cwd")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    timeout = min(arguments.get("timeout", 120), timeout_limit)
    if not cmd:
        return {"capability": capability, "status": "failed", "result": None, "issues": ["No command provided"]}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(safe_cwd), timeout=timeout)

        def _trim(text: str) -> str:
            if len(text) <= inline_limit:
                return text
            kept = text[:inline_limit]
            remaining = len(text) - inline_limit
            return kept + f"\n[... {remaining} more bytes truncated. Use run_command with grep/tail/head for targeted retrieval.]"

        return {
            "capability": capability,
            "status": "ok" if proc.returncode == 0 else "failed",
            "result": {
                "returncode": proc.returncode,
                "stdout": _trim(proc.stdout),
                "stderr": _trim(proc.stderr),
            },
            "issues": [f"Exit code {proc.returncode}"] if proc.returncode != 0 else [],
        }
    except subprocess.TimeoutExpired:
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Command timed out after {timeout}s"]}
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}


def _cap_validate_schema(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _require("REPO_ROOT")
    load_json = _require("load_json")
    schema_name = arguments.get("schema_name", "")
    data = arguments.get("data", {})
    schema_path = repo_root / "docs" / "schemas" / schema_name
    try:
        schema_path = ensure_within_repo(schema_path, "validate_schema schema_name")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    if not schema_path.exists():
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Schema not found: {schema_name}"]}
    schema = load_json(schema_path)
    issues = [f"Missing required field: {field}" for field in schema.get("required", []) if field not in data]
    return {
        "capability": capability,
        "status": "ok" if not issues else "failed",
        "result": {"valid": len(issues) == 0, "checked_fields": schema.get("required", [])},
        "issues": issues,
    }


def _cap_load_secrets(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    load_secrets = _require("load_secrets")
    project_id = arguments.get("project_id", "")
    if not project_id:
        return {"capability": capability, "status": "failed", "result": None, "issues": ["project_id is required"]}
    secrets = load_secrets(project_id, arguments.get("keys"))
    return {"capability": capability, "status": "ok", "result": secrets, "issues": []}


def _cap_save_secret(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    store_secrets = _require("store_secrets")
    project_id = arguments.get("project_id", "")
    key = arguments.get("key", "")
    value = arguments.get("value", "")
    if not project_id or not key or not value:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": ["project_id, key, and value are required"],
        }
    store_secrets(
        project_id,
        [{"key": key, "value": value, "type": arguments.get("type", "generic"), "label": arguments.get("label", "")}],
        source="capability",
    )
    return {"capability": capability, "status": "ok", "result": {"key": key, "stored": True}, "issues": []}


def _cap_use_native_tools(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "capability": capability,
        "status": "failed",
        "result": None,
        "issues": [
            f"'{capability}' is not an engine capability. Use your native tools instead (e.g., web search, web fetch). "
            "The agent binary (Claude/Gemini) provides these directly — do not request them through the capability system."
        ],
    }


def _cap_fetch_skill(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    skill_id = arguments.get("skill_id", "")
    if not skill_id or "--" not in skill_id:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": [f"Invalid skill_id: {skill_id!r}. Expected format: 'vendor--skill-name'."],
        }
    skill_md = loader_fetch_skill(skill_id)
    if not skill_md:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": [f"Could not fetch skill: {skill_id}. Check catalog and network."],
        }
    return {
        "capability": capability,
        "status": "ok",
        "result": {"skill_id": skill_id, "path": str(skill_md), "content": load_skill_body(skill_md)},
        "issues": [],
    }


def _cap_get_kb_candidates(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    task = str(arguments.get("task", "")).strip()
    if not task:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": ["task is required"],
        }
    reason = str(arguments.get("reason", ""))
    project_desc = str(arguments.get("project_desc", ""))
    try:
        limit = int(arguments.get("limit", 10))
        offset = int(arguments.get("offset", 0))
    except (TypeError, ValueError):
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": ["limit and offset must be integers"],
        }
    exclude_ids = arguments.get("exclude_ids", [])
    if exclude_ids is None:
        exclude_ids = []
    if not isinstance(exclude_ids, list):
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": ["exclude_ids must be an array when provided"],
        }
    result = prompt_work.get_kb_candidate_batch(
        task=task,
        reason=reason,
        project_desc=project_desc,
        limit=limit,
        offset=offset,
        exclude_ids=[str(item) for item in exclude_ids],
    )
    issues = result.pop("issues", []) if isinstance(result, dict) else []
    return {
        "capability": capability,
        "status": "ok",
        "result": result,
        "issues": issues,
    }


def _cap_http_request_with_secret_binding(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = arguments.get("project_id", "")
    url = arguments.get("url", "")
    if not project_id or not url:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": ["project_id and url are required"],
        }
    method = str(arguments.get("method", "GET")).upper()
    try:
        secret_map = _load_secret_map(project_id)
        bound_url = _bind_secret_placeholders(url, secret_map)
        headers = _bind_secret_placeholders(arguments.get("headers", {}), secret_map)
        params = _bind_secret_placeholders(arguments.get("params", {}), secret_map)
        json_body = _bind_secret_placeholders(arguments.get("json_body"), secret_map)
        raw_body = _bind_secret_placeholders(arguments.get("body"), secret_map)
    except KeyError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        separator = "&" if "?" in bound_url else "?"
        bound_url = f"{bound_url}{separator}{query}"

    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        if isinstance(headers, dict):
            headers.setdefault("Content-Type", "application/json")
    elif raw_body is not None:
        data = str(raw_body).encode("utf-8")

    req = urllib.request.Request(bound_url, data=data, headers=headers or {}, method=method)
    timeout = min(int(arguments.get("timeout", 120)), 300)  # Cap at 5 minutes
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            raw = _redact_secrets_from_text(raw, secret_map)
            content_type = resp.headers.get("Content-Type", "")
            body: Any = raw
            if "json" in content_type.lower() and raw:
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = raw
            redacted_url = _redact_secrets_from_text(bound_url, secret_map)
            return {
                "capability": capability,
                "status": "ok",
                "result": {
                    "url": redacted_url,
                    "status_code": getattr(resp, "status", 200),
                    "headers": dict(resp.headers.items()),
                    "body": body,
                },
                "issues": [],
            }
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        error_body = _redact_secrets_from_text(error_body, secret_map)
        redacted_url = _redact_secrets_from_text(bound_url, secret_map)
        return {
            "capability": capability,
            "status": "failed",
            "result": {"url": redacted_url, "status_code": exc.code, "body": error_body},
            "issues": [f"HTTP {exc.code}: {error_body[:500]}"],
        }
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}


def _cap_validate_logic_app_workflow(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    load_json = _require("load_json")
    definition = arguments.get("definition")
    if definition is None:
        workflow_path = arguments.get("path", "")
        if not workflow_path:
            return {
                "capability": capability,
                "status": "failed",
                "result": None,
                "issues": ["Either definition or path is required"],
            }
        try:
            safe_path = ensure_within_repo(Path(workflow_path), "validate_logic_app_workflow path")
        except ValueError as exc:
            return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
        definition = load_json(safe_path)

    workflow = definition
    if isinstance(workflow, dict) and "resources" in workflow:
        for resource in workflow.get("resources", []):
            if isinstance(resource, dict) and resource.get("type") == "Microsoft.Logic/workflows":
                workflow = resource.get("properties", {}).get("definition", {})
                break
    elif isinstance(workflow, dict) and "properties" in workflow and "definition" in workflow.get("properties", {}):
        workflow = workflow.get("properties", {}).get("definition", {})

    if not isinstance(workflow, dict):
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": ["Workflow definition must resolve to an object"],
        }

    triggers = workflow.get("triggers", {})
    actions = workflow.get("actions", {})
    issues: list[str] = []
    if not isinstance(triggers, dict) or not triggers:
        issues.append("Workflow definition has no triggers.")
    if not isinstance(actions, dict):
        issues.append("Workflow definition has invalid actions structure.")
    return {
        "capability": capability,
        "status": "ok" if not issues else "failed",
        "result": {
            "trigger_count": len(triggers) if isinstance(triggers, dict) else 0,
            "action_count": len(actions) if isinstance(actions, dict) else 0,
            "content_version": workflow.get("contentVersion"),
            "definition_valid": not issues,
        },
        "issues": issues,
    }


def _cap_deploy_logic_app_definition(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _require("REPO_ROOT")
    template_path = arguments.get("template_path", "")
    resource_group = arguments.get("resource_group", "")
    if not template_path or not resource_group:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": ["template_path and resource_group are required"],
        }
    try:
        safe_template = ensure_within_repo(Path(template_path), "deploy_logic_app_definition template_path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    if not safe_template.exists():
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Template not found: {template_path}"]}

    cmd = [
        "az", "deployment", "group", "create",
        "--resource-group", resource_group,
        "--template-file", str(safe_template),
    ]
    if arguments.get("deployment_name"):
        cmd.extend(["--name", str(arguments["deployment_name"])])
    if arguments.get("subscription"):
        cmd.extend(["--subscription", str(arguments["subscription"])])
    parameters_path = arguments.get("parameters_path")
    if parameters_path:
        try:
            safe_parameters = ensure_within_repo(Path(parameters_path), "deploy_logic_app_definition parameters_path")
        except ValueError as exc:
            return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
        cmd.extend(["--parameters", f"@{safe_parameters}"])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(repo_root), timeout=600)
        return {
            "capability": capability,
            "status": "ok" if proc.returncode == 0 else "failed",
            "result": {"command": cmd, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr},
            "issues": [] if proc.returncode == 0 else [f"Exit code {proc.returncode}"],
        }
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    except subprocess.TimeoutExpired:
        return {"capability": capability, "status": "failed", "result": None, "issues": ["Deployment command timed out after 600s"]}


def _check_write_path_allowed(safe_path: Path, capability: str) -> dict[str, Any] | None:
    """Shared check: block writes to protected directories (engine/, agents/, docs/, etc.)."""
    from engine.work.repo_paths import REPO_ROOT  # noqa: PLC0415
    try:
        rel = str(safe_path.resolve().relative_to(REPO_ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return None
    from engine.work.destructive_guard import _PROTECTED_WRITE_PREFIXES  # noqa: PLC0415
    for prefix in _PROTECTED_WRITE_PREFIXES:
        if rel.startswith(prefix) or rel == prefix.rstrip("/"):
            return {
                "capability": capability,
                "status": "failed",
                "result": None,
                "issues": [f"Writing to '{prefix}' is not permitted. Deliverables must be in the projects/ directory."],
            }
    return None


def _cap_create_sharepoint_list_schema(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    infer_project_id = _require("_infer_project_id_from_path")
    get_project_secret_values = _require("_get_project_secret_values")
    target_path = arguments.get("path", "")
    schema = arguments.get("schema")
    if not target_path or schema is None:
        return {"capability": capability, "status": "failed", "result": None, "issues": ["path and schema are required"]}
    try:
        safe_path = ensure_within_repo(Path(target_path), "create_sharepoint_list_schema path")
        blocked = _check_write_path_allowed(safe_path, capability)
        if blocked:
            return blocked
        serialized = json.dumps(schema)
        project_id = infer_project_id(safe_path)
        if project_id:
            secret_values = get_project_secret_values(project_id)
            if secret_values:
                leaked = scan_for_leaked_values(serialized, secret_values)
                if leaked:
                    return {"capability": capability, "status": "failed", "result": None, "issues": [f"BLOCKED: Content contains secret value(s): {', '.join(leaked)}."]}
        _write_json_file(safe_path, schema)
        return {"capability": capability, "status": "ok", "result": {"path": target_path}, "issues": []}
    except (ValueError, OSError) as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}


def _cap_create_powerbi_import_bundle(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    infer_project_id = _require("_infer_project_id_from_path")
    get_project_secret_values = _require("_get_project_secret_values")
    target_path = arguments.get("path", "")
    bundle = arguments.get("bundle")
    if not target_path or bundle is None:
        return {"capability": capability, "status": "failed", "result": None, "issues": ["path and bundle are required"]}
    try:
        safe_path = ensure_within_repo(Path(target_path), "create_powerbi_import_bundle path")
        blocked = _check_write_path_allowed(safe_path, capability)
        if blocked:
            return blocked
        serialized = json.dumps(bundle)
        project_id = infer_project_id(safe_path)
        if project_id:
            secret_values = get_project_secret_values(project_id)
            if secret_values:
                leaked = scan_for_leaked_values(serialized, secret_values)
                if leaked:
                    return {"capability": capability, "status": "failed", "result": None, "issues": [f"BLOCKED: Content contains secret value(s): {', '.join(leaked)}."]}
        _write_json_file(safe_path, bundle)
        return {"capability": capability, "status": "ok", "result": {"path": target_path}, "issues": []}
    except (ValueError, OSError) as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}


def _cap_powerbi_import_artifact(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = arguments.get("project_id", "")
    group_id = arguments.get("group_id", "")
    token_key = arguments.get("access_token_secret_key", "")
    file_path = arguments.get("file_path", "")
    dataset_display_name = arguments.get("dataset_display_name", "")
    if not project_id or not group_id or not token_key or not file_path or not dataset_display_name:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": ["project_id, group_id, access_token_secret_key, file_path, and dataset_display_name are required"],
        }
    try:
        safe_path = ensure_within_repo(Path(file_path), "powerbi_import_artifact file_path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    if not safe_path.exists():
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Artifact not found: {file_path}"]}
    secret_map = _load_secret_map(project_id, [token_key])
    token = secret_map.get(token_key)
    if not token:
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Secret not found: {token_key}"]}

    boundary = f"----automator-{uuid.uuid4().hex}"
    mime_type = mimetypes.guess_type(str(safe_path))[0] or "application/octet-stream"
    file_bytes = safe_path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_path.name}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    params = urllib.parse.urlencode({
        "datasetDisplayName": dataset_display_name,
        "nameConflict": arguments.get("name_conflict", "CreateOrOverwrite"),
    })
    req = urllib.request.Request(
        f"https://api.powerbi.com/v1.0/myorg/groups/{group_id}/imports?{params}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw_body) if raw_body else {}
            return {
                "capability": capability,
                "status": "ok",
                "result": {"status_code": getattr(resp, "status", 200), "body": parsed},
                "issues": [],
            }
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        return {
            "capability": capability,
            "status": "failed",
            "result": {"status_code": exc.code, "body": body_text},
            "issues": [f"HTTP {exc.code}: {body_text[:500]}"],
        }
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}


def _cap_powerbi_trigger_refresh(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    dataset_id = arguments.get("dataset_id", "")
    if not dataset_id:
        return {"capability": capability, "status": "failed", "result": None, "issues": ["dataset_id is required"]}
    return _powerbi_request(
        capability,
        project_id=arguments.get("project_id", ""),
        group_id=arguments.get("group_id", ""),
        relative_path=f"datasets/{dataset_id}/refreshes",
        access_token_secret_key=arguments.get("access_token_secret_key", ""),
        method="POST",
        body=arguments.get("body"),
    )


def _cap_powerbi_check_refresh_status(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    dataset_id = arguments.get("dataset_id", "")
    if not dataset_id:
        return {"capability": capability, "status": "failed", "result": None, "issues": ["dataset_id is required"]}
    return _powerbi_request(
        capability,
        project_id=arguments.get("project_id", ""),
        group_id=arguments.get("group_id", ""),
        relative_path=f"datasets/{dataset_id}/refreshes?$top=1",
        access_token_secret_key=arguments.get("access_token_secret_key", ""),
        method="GET",
    )


def _cap_query_git_status(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return structured git status — branch, ahead/behind, file lists."""
    repo_root = _require("REPO_ROOT")
    cwd = arguments.get("cwd", str(repo_root))
    try:
        safe_cwd = ensure_within_repo(Path(cwd), "query_git_status cwd")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "-b"],
            capture_output=True, text=True, cwd=str(safe_cwd), timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    branch = "unknown"
    tracking: str | None = None
    ahead = 0
    behind = 0
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []

    for i, line in enumerate(proc.stdout.splitlines()):
        if i == 0 and line.startswith("## "):
            rest = line[3:]
            if "..." in rest:
                branch, tail = rest.split("...", 1)
                if " [" in tail:
                    tracking = tail[: tail.index(" [")]
                    ab = tail[tail.index(" ["):]
                    m_a = re.search(r"ahead (\d+)", ab)
                    m_b = re.search(r"behind (\d+)", ab)
                    if m_a:
                        ahead = int(m_a.group(1))
                    if m_b:
                        behind = int(m_b.group(1))
                else:
                    tracking = tail.strip()
            else:
                branch = rest.split(" ")[0]
            continue
        if len(line) < 4:
            continue
        xy, fname = line[:2], line[3:]
        if xy == "??":
            untracked.append(fname)
        else:
            x, y = xy[0], xy[1]
            if x not in (" ", "?"):
                staged.append(fname)
            if y not in (" ", "?"):
                unstaged.append(fname)

    return {
        "capability": capability,
        "status": "ok",
        "result": {
            "branch": branch,
            "tracking": tracking,
            "ahead": ahead,
            "behind": behind,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "clean": not staged and not unstaged and not untracked,
        },
        "issues": [],
    }


def _cap_query_git_diff(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return structured diff grouped by file with added/removed line lists.

    ref is passed directly as space-separated git args, e.g. "HEAD~1 HEAD",
    "HEAD~1..HEAD", or "HEAD" (diff against working tree).
    stat_only=true returns file-level counts without line content.
    """
    repo_root = _require("REPO_ROOT")
    cwd = arguments.get("cwd", str(repo_root))
    try:
        safe_cwd = ensure_within_repo(Path(cwd), "query_git_diff cwd")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    stat_only: bool = arguments.get("stat_only", False)
    ref_args: list[str] = arguments["ref"].split() if arguments.get("ref") else []
    paths: list[str] = arguments.get("paths", [])

    base_cmd = ["git", "diff"]
    if stat_only:
        cmd = base_cmd + ["--stat"] + ref_args
    else:
        cmd = base_cmd + ["--unified=0"] + ref_args
    if paths:
        cmd += ["--"] + paths

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(safe_cwd), timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    if proc.returncode != 0:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": [proc.stderr.strip() or f"git diff exit {proc.returncode}"],
        }

    if stat_only:
        files_stat: list[dict[str, Any]] = []
        summary: dict[str, int] = {}
        for line in proc.stdout.splitlines():
            if "|" in line:
                parts = line.split("|", 1)
                changes = parts[1].strip()
                files_stat.append({
                    "file": parts[0].strip(),
                    "insertions": changes.count("+"),
                    "deletions": changes.count("-"),
                })
            elif "changed" in line:
                m_c = re.search(r"(\d+) file", line)
                m_i = re.search(r"(\d+) insertion", line)
                m_d = re.search(r"(\d+) deletion", line)
                summary = {
                    "files_changed": int(m_c.group(1)) if m_c else len(files_stat),
                    "insertions": int(m_i.group(1)) if m_i else 0,
                    "deletions": int(m_d.group(1)) if m_d else 0,
                }
        return {"capability": capability, "status": "ok", "result": {"files": files_stat, **summary}, "issues": []}

    # Parse unified diff — group added/removed lines per file (cap 80 lines each, 30 files).
    _MAX_LINES_PER_FILE = 80
    _MAX_FILES = 30
    changes: list[dict[str, Any]] = []
    cur_file: str | None = None
    cur_added: list[str] = []
    cur_removed: list[str] = []

    def _flush() -> None:
        if cur_file is not None:
            changes.append({
                "file": cur_file,
                "added": cur_added[:_MAX_LINES_PER_FILE],
                "removed": cur_removed[:_MAX_LINES_PER_FILE],
                "truncated": len(cur_added) > _MAX_LINES_PER_FILE or len(cur_removed) > _MAX_LINES_PER_FILE,
            })

    for line in proc.stdout.splitlines():
        if line.startswith("diff --git "):
            _flush()
            if len(changes) >= _MAX_FILES:
                break
            cur_file = None
            cur_added = []
            cur_removed = []
        elif line.startswith("+++ b/"):
            cur_file = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            cur_added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            cur_removed.append(line[1:])
    _flush()

    return {
        "capability": capability,
        "status": "ok",
        "result": {"files_changed": len(changes), "changes": changes},
        "issues": [],
    }


def _cap_query_git_log(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return structured commit log: hash, author, date, message per entry."""
    repo_root = _require("REPO_ROOT")
    cwd = arguments.get("cwd", str(repo_root))
    try:
        safe_cwd = ensure_within_repo(Path(cwd), "query_git_log cwd")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    n = min(int(arguments.get("n", 10)), 50)
    ref = arguments.get("ref", "HEAD")
    paths: list[str] = arguments.get("paths", [])
    sep = "\x1f"
    fmt = f"%H{sep}%h{sep}%an{sep}%ad{sep}%s"
    cmd = ["git", "log", f"-{n}", f"--format={fmt}", "--date=short", ref]
    if paths:
        cmd += ["--"] + paths

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(safe_cwd), timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    if proc.returncode != 0:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": [proc.stderr.strip() or f"git log exit {proc.returncode}"],
        }

    commits: list[dict[str, str]] = []
    for line in proc.stdout.strip().splitlines():
        parts = line.split(sep)
        if len(parts) == 5:
            commits.append({
                "hash": parts[1],
                "full_hash": parts[0],
                "author": parts[2],
                "date": parts[3],
                "message": parts[4],
            })

    return {
        "capability": capability,
        "status": "ok",
        "result": {"count": len(commits), "commits": commits},
        "issues": [],
    }


def _cap_search_code(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Structured code search returning {file, line, content} per match.

    Uses grep internally — works on any POSIX system without additional deps.
    Returns paths relative to repo root for clean output.
    """
    repo_root = _require("REPO_ROOT")
    pattern = arguments.get("pattern", "")
    if not pattern:
        return {"capability": capability, "status": "failed", "result": None, "issues": ["pattern is required"]}

    search_path_raw = arguments.get("path", str(repo_root))
    try:
        safe_path = ensure_within_repo(Path(search_path_raw), "search_code path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    file_glob: str | None = arguments.get("file_glob")
    try:
        context_lines = min(int(arguments.get("context_lines", 0)), 3)
        max_matches   = min(int(arguments.get("max_matches", 50)), 200)
    except (ValueError, TypeError) as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Invalid numeric argument: {exc}"]}
    case_insensitive: bool = arguments.get("case_insensitive", False)

    cmd = ["grep", "-rn", "-I"]  # -I skips binary files
    if case_insensitive:
        cmd.append("-i")
    if context_lines > 0:
        cmd.append(f"-C{context_lines}")
    if file_glob:
        cmd.append(f"--include={file_glob}")
    cmd.extend([pattern, str(safe_path)])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    matches: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if len(matches) >= max_matches:
            break
        if ":" not in line:
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        try:
            rel = str(Path(parts[0]).relative_to(repo_root))
        except ValueError:
            rel = parts[0]
        try:
            lineno = int(parts[1])
        except ValueError:
            continue
        matches.append({"file": rel, "line": lineno, "content": parts[2].strip()})

    return {
        "capability": capability,
        "status": "ok",
        "result": {
            "total_matches": len(matches),
            "truncated": len(matches) >= max_matches,
            "matches": matches,
        },
        "issues": [],
    }


def _cap_run_tests(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run project tests via the repo-local Python and return structured results.

    Uses python -m unittest so no pytest install is required.  Detects the
    repo-local .venv automatically — no LLM backend dependency.
    Returns failures with tracebacks; suppresses passing test noise.
    """
    repo_root = _require("REPO_ROOT")
    timeout_limit = _require("SPAWN_TIMEOUT_SECONDS")
    timeout = min(int(arguments.get("timeout", 120)), timeout_limit)
    pattern: str | None = arguments.get("pattern")

    raw_path = arguments.get("path", "engine/tests/")
    python = _detect_python()

    # Determine if path is a directory/file or a dotted module name.
    path_obj = Path(raw_path) if ("/" in raw_path or raw_path.endswith(".py")) else None
    if path_obj is not None:
        abs_path = path_obj if path_obj.is_absolute() else repo_root / path_obj
        try:
            safe_path = ensure_within_repo(abs_path, "run_tests path")
        except ValueError as exc:
            return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}
        if safe_path.is_dir():
            cmd = [python, "-m", "unittest", "discover", "-s", str(safe_path), "-v"]
        else:
            # Convert file path to module: engine/tests/test_foo.py → engine.tests.test_foo
            try:
                rel = safe_path.relative_to(repo_root)
                module = str(rel.with_suffix("")).replace("/", ".")
            except ValueError:
                module = str(safe_path)
            cmd = [python, "-m", "unittest", module, "-v"]
    else:
        # Dotted module path passed directly
        cmd = [python, "-m", "unittest", raw_path, "-v"]

    if pattern:
        cmd.extend(["-k", pattern])

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=str(repo_root), timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Tests timed out after {timeout}s"]}
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    # unittest writes results to stderr; stdout is usually empty.
    output = proc.stderr + proc.stdout

    # Parse summary line: "Ran N tests in X.Xs" then "OK" / "FAILED (failures=N, errors=M)"
    total = passed = failed = errors = skipped = 0
    m_ran = re.search(r"Ran (\d+) tests?", output)
    if m_ran:
        total = int(m_ran.group(1))
    m_fail = re.search(r"FAILED\s*\(([^)]+)\)", output)
    if m_fail:
        detail = m_fail.group(1)
        mf = re.search(r"failures?=(\d+)", detail)
        me = re.search(r"errors?=(\d+)", detail)
        ms = re.search(r"skipped=(\d+)", detail)
        failed = int(mf.group(1)) if mf else 0
        errors = int(me.group(1)) if me else 0
        skipped = int(ms.group(1)) if ms else 0
    else:
        m_skip = re.search(r"skipped=(\d+)", output)
        skipped = int(m_skip.group(1)) if m_skip else 0
    passed = max(0, total - failed - errors - skipped)

    # Extract individual failure/error blocks (between === separators)
    failures: list[dict[str, str]] = []
    block_re = re.compile(r"^(FAIL|ERROR): (.+)$", re.MULTILINE)
    sep_re = re.compile(r"^-{50,}$", re.MULTILINE)
    separators = [m.start() for m in sep_re.finditer(output)]

    for m in block_re.finditer(output):
        kind = m.group(1)
        name = m.group(2).strip()
        # Traceback follows until the next separator
        tb_start = output.find("\n", m.end()) + 1
        tb_end = next((s for s in separators if s > tb_start), len(output))
        traceback_text = output[tb_start:tb_end].strip()
        failures.append({"kind": kind, "test": name, "traceback": traceback_text})

    return {
        "capability": capability,
        "status": "ok" if proc.returncode == 0 else "failed",
        "result": {
            "python_used": python,
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "clean": proc.returncode == 0,
            "failures": failures,
        },
        "issues": (
            [f"{failed} failure(s), {errors} error(s)"] if proc.returncode != 0 else []
        ),
    }


_NOISE_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", ".venv", "node_modules", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".tox",
})


def _cap_list_dir(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Structured directory listing — replaces run_command(['ls', '-la', ...]).

    Returns sorted entries with name, type, and size.  Hidden entries and
    common noise directories (.git, __pycache__, .venv, node_modules) are
    excluded by default.
    """
    repo_root = _require("REPO_ROOT")
    raw_path = arguments.get("path", str(repo_root))
    show_hidden: bool = arguments.get("show_hidden", False)
    max_entries = min(int(arguments.get("max_entries", 200)), 500)

    try:
        safe_path = ensure_within_repo(Path(raw_path), "list_dir path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    if not safe_path.exists():
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Path does not exist: {raw_path}"]}
    if not safe_path.is_dir():
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Path is not a directory: {raw_path}"]}

    entries: list[dict[str, Any]] = []
    total_files = 0
    total_dirs = 0
    truncated = False

    try:
        items = sorted(safe_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    for item in items:
        if not show_hidden and item.name.startswith("."):
            continue
        if item.name in _NOISE_DIRS:
            continue
        if len(entries) >= max_entries:
            truncated = True
            break
        entry: dict[str, Any] = {"name": item.name, "type": "dir" if item.is_dir() else "file"}
        if item.is_file():
            try:
                entry["size_bytes"] = item.stat().st_size
            except OSError:
                entry["size_bytes"] = None
            total_files += 1
        else:
            total_dirs += 1
        entries.append(entry)

    try:
        rel = str(safe_path.relative_to(repo_root))
    except ValueError:
        rel = str(safe_path)

    return {
        "capability": capability,
        "status": "ok",
        "result": {
            "path": rel,
            "entries": entries,
            "total_files": total_files,
            "total_dirs": total_dirs,
            "truncated": truncated,
        },
        "issues": [],
    }


def _cap_find_files(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Find files by name glob pattern — replaces run_command(['find', ...]).

    Uses Python's Path.rglob internally.  Returns paths relative to the
    repo root.  Automatically skips noise directories.
    """
    repo_root = _require("REPO_ROOT")
    pattern = arguments.get("pattern", "")
    if not pattern:
        return {"capability": capability, "status": "failed", "result": None, "issues": ["pattern is required"]}

    raw_path = arguments.get("path", str(repo_root))
    max_results = min(int(arguments.get("max_results", 100)), 500)
    entry_type: str = arguments.get("type", "file")  # "file", "dir", "any"

    try:
        safe_path = ensure_within_repo(Path(raw_path), "find_files path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    if not safe_path.exists():
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Path does not exist: {raw_path}"]}

    matches: list[str] = []
    truncated = False

    try:
        for p in safe_path.rglob(pattern):
            # Skip noise directories anywhere in the path
            if any(part in _NOISE_DIRS for part in p.parts):
                continue
            if entry_type == "file" and not p.is_file():
                continue
            if entry_type == "dir" and not p.is_dir():
                continue
            if len(matches) >= max_results:
                truncated = True
                break
            try:
                matches.append(str(p.relative_to(repo_root)))
            except ValueError:
                matches.append(str(p))
    except (OSError, PermissionError) as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    return {
        "capability": capability,
        "status": "ok",
        "result": {"matches": sorted(matches), "total": len(matches), "truncated": truncated},
        "issues": [],
    }


def _cap_stat_file(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return file metadata without reading content.

    Use before read_file to check size (avoid accidentally reading a 50 MB
    file) or to verify a path exists before attempting operations on it.
    Includes line_count for text files ≤ 10 MB — useful for wc -l equivalents
    on CSVs and logs without loading the full content.
    """
    repo_root = _require("REPO_ROOT")
    raw_path = arguments.get("path", "")
    if not raw_path:
        return {"capability": capability, "status": "failed", "result": None, "issues": ["path is required"]}

    path_obj = Path(raw_path) if Path(raw_path).is_absolute() else repo_root / raw_path
    try:
        safe_path = ensure_within_repo(path_obj, "stat_file path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    try:
        rel = str(safe_path.relative_to(repo_root))
    except ValueError:
        rel = str(safe_path)

    if not safe_path.exists():
        return {
            "capability": capability,
            "status": "ok",
            "result": {"path": rel, "exists": False},
            "issues": [],
        }

    try:
        st = safe_path.stat()
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    entry_type = "dir" if safe_path.is_dir() else ("symlink" if safe_path.is_symlink() else "file")
    size_bytes = st.st_size if entry_type == "file" else None

    # Count lines for text files ≤ 10 MB (stream to avoid full load)
    line_count: int | None = None
    _LINE_COUNT_LIMIT = 10 * 1024 * 1024
    if entry_type == "file" and size_bytes is not None and size_bytes <= _LINE_COUNT_LIMIT:
        try:
            count = 0
            with open(safe_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    count += chunk.count(b"\n")
            line_count = count
        except OSError:
            pass

    return {
        "capability": capability,
        "status": "ok",
        "result": {
            "path": rel,
            "exists": True,
            "type": entry_type,
            "size_bytes": size_bytes,
            "line_count": line_count,
        },
        "issues": [],
    }


def _cap_read_file_lines(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Read the first or last N lines of a file — head/tail equivalent.

    Use for large files where read_file would load too much context:
    - mode 'head': inspect file structure, headers, schema rows
    - mode 'tail': inspect recent log entries, test output, error traces
    Returns total line count alongside the selected lines.
    """
    repo_root = _require("REPO_ROOT")
    raw_path = arguments.get("path", "")
    if not raw_path:
        return {"capability": capability, "status": "failed", "result": None, "issues": ["path is required"]}

    mode = arguments.get("mode", "head")
    if mode not in ("head", "tail"):
        return {"capability": capability, "status": "failed", "result": None, "issues": ["mode must be 'head' or 'tail'"]}

    n = min(int(arguments.get("n", 50)), 500)

    path_obj = Path(raw_path) if Path(raw_path).is_absolute() else repo_root / raw_path
    try:
        safe_path = ensure_within_repo(path_obj, "read_file_lines path")
    except ValueError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    if not safe_path.exists():
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"File not found: {raw_path}"]}
    if safe_path.is_dir():
        return {"capability": capability, "status": "failed", "result": None, "issues": [f"Path is a directory: {raw_path}"]}

    try:
        rel = str(safe_path.relative_to(repo_root))
    except ValueError:
        rel = str(safe_path)

    try:
        if mode == "head":
            selected = []
            total = 0
            with open(safe_path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    total += 1
                    if len(selected) < n:
                        selected.append(line)
            # Count remaining lines without storing
        else:
            # Tail: use a deque to keep only last N lines in memory.
            from collections import deque  # noqa: PLC0415
            tail_buf: deque[str] = deque(maxlen=n)
            total = 0
            with open(safe_path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    total += 1
                    tail_buf.append(line)
            selected = list(tail_buf)
    except OSError as exc:
        return {"capability": capability, "status": "failed", "result": None, "issues": [str(exc)]}

    # Strip trailing newlines for clean output
    lines = [line.rstrip("\n") for line in selected]

    return {
        "capability": capability,
        "status": "ok",
        "result": {
            "path": rel,
            "mode": mode,
            "n": n,
            "total_lines": total,
            "lines": lines,
        },
        "issues": [],
    }


_CAPABILITY_DISPATCH: dict[str, Callable[[str, dict[str, Any]], dict[str, Any]]] = {
    "test_credentials": _cap_test_credentials,
    "list_projects": _cap_list_projects,
    "resolve_project": _cap_resolve_project,
    "init_project": _cap_init_project,
    "load_task_state": _cap_load_task_state,
    "save_task_state": _cap_save_task_state,
    "load_memory": _cap_load_memory,
    "save_memory": _cap_save_memory,
    "load_artifact": _cap_load_artifact,
    "persist_artifact": _cap_persist_artifact,
    "read_file": _cap_read_file,
    "write_file": _cap_write_file,
    "run_command": _cap_run_command,
    "validate_schema": _cap_validate_schema,
    "load_secrets": _cap_load_secrets,
    "save_secret": _cap_save_secret,
    "http_request_with_secret_binding": _cap_http_request_with_secret_binding,
    "validate_logic_app_workflow": _cap_validate_logic_app_workflow,
    "deploy_logic_app_definition": _cap_deploy_logic_app_definition,
    "create_sharepoint_list_schema": _cap_create_sharepoint_list_schema,
    "create_powerbi_import_bundle": _cap_create_powerbi_import_bundle,
    "powerbi_import_artifact": _cap_powerbi_import_artifact,
    "powerbi_trigger_refresh": _cap_powerbi_trigger_refresh,
    "powerbi_check_refresh_status": _cap_powerbi_check_refresh_status,
    "fetch_source": _cap_use_native_tools,
    "search_sources": _cap_use_native_tools,
    "fetch_skill": _cap_fetch_skill,
    "get_kb_candidates": _cap_get_kb_candidates,
    "query_git_status": _cap_query_git_status,
    "query_git_diff": _cap_query_git_diff,
    "query_git_log": _cap_query_git_log,
    "search_code": _cap_search_code,
    "run_tests": _cap_run_tests,
    "list_dir": _cap_list_dir,
    "find_files": _cap_find_files,
    "stat_file": _cap_stat_file,
    "read_file_lines": _cap_read_file_lines,
}


def execute_capability(request: dict[str, Any]) -> dict[str, Any]:
    capability = request.get("capability", "")
    arguments = request.get("arguments", {})
    handler = _CAPABILITY_DISPATCH.get(capability)
    if handler is None:
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": [f"Unknown capability: {capability}"],
        }
    try:
        return handler(capability, arguments)
    except Exception as exc:
        # Never let a capability handler crash the pipeline — return a
        # structured error so the agent can react or the engine can continue.
        return {
            "capability": capability,
            "status": "failed",
            "result": None,
            "issues": [f"Internal error in capability handler: {type(exc).__name__}: {exc}"],
        }
