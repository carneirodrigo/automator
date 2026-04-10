# Capability Requests

## Purpose

Agents do not have direct access to the filesystem, command execution, or artifact storage. Instead, they request bounded runtime operations from the engine by including a `capability_requests` array in their JSON response.

## How It Works

1. The agent returns its normal output JSON but adds a `capability_requests` array at the top level.
2. The engine executes each request and re-invokes the agent with the results injected.
3. The agent then uses those results to complete its task and returns the final output (without `capability_requests`).
4. The engine allows up to 5 rounds of capability requests per stage. After that, the stage is treated as failed.

## Request Shape

```json
{
  "capability_requests": [
    {
      "capability": "read_file",
      "arguments": { "path": "/absolute/path/to/file.py" },
      "reason": "Need to inspect the implementation before review"
    }
  ]
}
```

Every request must include `capability`, `arguments`, and `reason`.

## Available Capabilities

| Capability | Arguments (JSON) | Use Case |
|---|---|---|
| `read_file` | `{"path": "/absolute/path/file.py"}` | Read a file (truncated to 1MB) |
| `write_file` | `{"path": "/absolute/path/file.py", "content": "..."}` | Write a file (must be inside repo) |
| `run_command` | `{"command": ["python3", "script.py"], "cwd": "/path", "timeout": 30}` | Execute a shell command |
| `load_artifact` | `{"artifact_path": "/absolute/path/artifact.json"}` | Load a previously persisted artifact |
| `persist_artifact` | `{"runtime_dir": "/path/runtime", "agent": "role-name", "data": {...}}` | Save an artifact |
| `save_memory` | `{"runtime_dir": "/path/runtime", "key": "topic-slug", "data": {...}}` | Persist a key-value memory entry in the project runtime (worker and research only) |
| `test_credentials` | `{"credential_type": "azure_ad", "service": "graph", "credentials": {...}}` | Validate credentials |
| `load_secrets` | `{"project_id": "my-project", "keys": ["azure_tenant_id"]}` | Retrieve stored secrets for a project (`keys` filter is optional) |
| `save_secret` | `{"project_id": "my-project", "key": "azure_tenant_id", "value": "...", "type": "azure", "label": "Tenant ID"}` | Store a secret in the project vault |
| `http_request_with_secret_binding` | `{"project_id": "my-project", "method": "GET", "url": "...", "headers": {"Authorization": "Bearer {{secret:graph_token}}"}}` | Execute a secret-bound HTTPS request without hardcoding credentials |
| `validate_logic_app_workflow` | `{"path": "/absolute/path/workflow.json"}` or `{"definition": {...}}` | Validate a Logic Apps workflow or workflow resource shape |
| `deploy_logic_app_definition` | `{"template_path": "/absolute/path/template.json", "resource_group": "rg-name"}` | Deploy a Logic Apps template through Azure CLI |
| `create_sharepoint_list_schema` | `{"path": "/absolute/path/list-schema.json", "schema": {...}}` | Persist a SharePoint list schema artifact |
| `create_powerbi_import_bundle` | `{"path": "/absolute/path/powerbi-import.json", "bundle": {...}}` | Persist a Power BI import/provisioning bundle |
| `powerbi_import_artifact` | `{"project_id": "my-project", "group_id": "...", "file_path": "/absolute/path/report.pbix", "dataset_display_name": "Report", "access_token_secret_key": "powerbi_access_token"}` | Import a PBIX or related artifact into Power BI |
| `powerbi_trigger_refresh` | `{"project_id": "my-project", "group_id": "...", "dataset_id": "...", "access_token_secret_key": "powerbi_access_token"}` | Trigger a Power BI dataset refresh |
| `powerbi_check_refresh_status` | `{"project_id": "my-project", "group_id": "...", "dataset_id": "...", "access_token_secret_key": "powerbi_access_token"}` | Inspect recent Power BI refresh status |
| `query_git_status` | `{"cwd": "/optional/path"}` | Structured git status: branch, ahead/behind counts, staged/unstaged/untracked file lists. Use instead of `run_command(["git", "status"])` |
| `query_git_diff` | `{"ref": "HEAD~1 HEAD", "paths": ["file.py"], "stat_only": false, "cwd": "/optional/path"}` | Structured diff grouped by file with added/removed line lists. `ref` is space-separated git args. `stat_only` returns file-level counts only |
| `query_git_log` | `{"n": 10, "ref": "HEAD", "paths": [], "cwd": "/optional/path"}` | Structured commit log: hash, full_hash, author, date, message per entry. `n` capped at 50 |
| `search_code` | `{"pattern": "regex", "path": "engine/work/", "file_glob": "*.py", "context_lines": 0, "case_insensitive": false, "max_matches": 50}` | Structured code search returning `{file, line, content}` per match. Paths are relative to repo root. Use instead of `run_command(["grep", ...])` |
| `run_tests` | `{"path": "engine/tests/", "pattern": "ToonAdapter", "timeout": 120}` | Run project tests via the repo-local Python. Returns `{total, passed, failed, errors, skipped, failures[]}`. `path` can be a directory or dotted module name. `pattern` filters by test name |
| `list_dir` | `{"path": "engine/work/", "show_hidden": false, "max_entries": 200}` | Structured directory listing: sorted entries with name, type, size_bytes. Skips `.git`, `__pycache__`, `.venv`, `node_modules` automatically. Use instead of `run_command(["ls", ...])` |
| `find_files` | `{"pattern": "*.py", "path": "engine/", "type": "file", "max_results": 100}` | Find files by name glob. `type`: `"file"` (default), `"dir"`, or `"any"`. Returns paths relative to repo root. Skips noise dirs. Use instead of `run_command(["find", ...])` |
| `stat_file` | `{"path": "engine/work/capabilities.py"}` | File metadata without reading content: exists, type, size_bytes, line_count (text files ≤ 10 MB). Check before `read_file` to avoid reading large files. `path` may be absolute or relative to repo root |
| `read_file_lines` | `{"path": "output.log", "mode": "head", "n": 50}` | Read first (`head`) or last (`tail`) N lines of a file (max 500). Returns `total_lines` + selected lines. Use instead of `run_command(["head"/"tail", ...])` for large files |

## When To Use Capabilities

- Use `read_file` to inspect project files, outputs, or configurations.
- Use `run_command` to execute tests, linters, or verification scripts.
- Use `persist_artifact` when your task generates files that need to be tracked.
- Use `test_credentials` to validate credentials before making API calls.
- Use `load_secrets` to retrieve stored secrets (API keys, tokens, tenant IDs) for a project. Never hardcode secrets in source files.
- Use `save_secret` to store secrets discovered during work (e.g., credentials from clarification responses). The engine automatically detects and stores secrets from user prompts.
- Use `fetch_skill` to download and read a vendor skill from the Agent Skills catalog.
- Use `get_kb_candidates` to widen local KB retrieval in compact batches before reading more entry bodies or moving to external search.
- Use `query_git_status`, `query_git_diff`, `query_git_log` in place of `run_command(["git", ...])` whenever you need git information — these return typed structured data and are significantly more token-efficient than raw git output.
- Use `search_code` in place of `run_command(["grep", ...])` — it returns `{file, line, content}` per match with paths relative to the repo root, capped at `max_matches`.
- Use `run_tests` in place of `run_command(["python3", "-m", "unittest", ...])` — it auto-detects the repo-local Python interpreter, parses results into structured pass/fail counts, and returns only failure tracebacks rather than the full test output.
- Use `list_dir` in place of `run_command(["ls", ...])` — returns structured entries with sizes, skips noise dirs automatically.
- Use `find_files` in place of `run_command(["find", ...])` — glob-based file discovery returning relative paths, skips `.git` and `__pycache__` automatically.
- Use `stat_file` before `read_file` on any file whose size is unknown — avoids wasting a round attempting to read a multi-megabyte file. Also useful to check file existence without reading content.
- Use `read_file_lines` for large log files, test output, or CSVs where only the first or last N lines matter — avoids loading the full file into context.

## Web Search and Fetch

Web search and web fetch are primarily **native capabilities of the agent binary** (Claude has WebSearch/WebFetch, Gemini has built-in search). They are **NOT engine capabilities** in the preferred execution model, and agents should prefer those native tools in new work.

The runtime still recognizes compatibility aliases `fetch_source` and `search_sources`. These are engine-recognized compatibility shims, not first-choice capabilities. They exist to preserve older flows and should be treated as fallback routing helpers rather than the preferred search/fetch interface for new work.

## Batching Strategy

You have a limited number of capability rounds (5). Each round is a full re-invocation of your agent, so rounds are expensive. To maximize what you can accomplish:

- **Batch independent requests in the same round.** If you need to write 3 files, include all 3 `write_file` requests in a single `capability_requests` array. They execute sequentially within one round.
- **Combine write + execute.** If you write a file and then need to run a test, include both `write_file` and `run_command` in the same round — the file is written before the command runs.
- **Plan ahead.** A typical coding workflow is: round 1 (read existing files), round 2 (write code + run tests), round 3 (fix issues + re-run). That leaves rounds 4-5 for additional fixes.
- **Do NOT issue one capability per round.** Writing 5 files across 5 separate rounds leaves no room for testing.

## Destructive Action Guards

The engine enforces a set of hard safety policies on every capability request. Requests that violate these policies are blocked and returned as failures before the handler runs.

### Role-based capability allowlists

Each role is restricted to a specific set of capabilities. Requesting a capability outside your role's allowed set is a hard error, not a hint. The allowed sets are:

| Role | Allowed capabilities |
|---|---|
| `worker` | `write_file`, `run_command`, `run_tests`, `persist_artifact`, `load_memory`, `save_memory`, `http_request_with_secret_binding`, `test_credentials`, all platform caps (`validate_logic_app_workflow`, `deploy_logic_app_definition`, `create_sharepoint_list_schema`, `create_powerbi_import_bundle`, `powerbi_import_artifact`, `powerbi_trigger_refresh`, `powerbi_check_refresh_status`), all read/git/secrets caps — deployment guarded by `delivery_mode: build_and_deploy` + operator prompt |
| `research` | `http_request_with_secret_binding`, `test_credentials`, `persist_artifact`, `load_memory`, `save_memory`, all read/secrets caps — no `write_file`, `run_command`, or deployment |
| `review` | `run_command`, `run_tests`, `persist_artifact`, all read/git/secrets caps — no `write_file` or deployment |

### Engine-created path tracking and write protection

`write_file` enforces two layers of protection:

1. **Protected source directories** — writes to `engine/`, `agents/`, `docs/`, `config/`, `knowledge/`, or `skills/` are hard-blocked. Deliverables must be written to the `projects/` directory.
2. **Pre-existing file protection** — if a file already exists and was *not* created by the engine, the overwrite is blocked. The engine tracks every file it creates (in `projects/<id>/runtime/engine_created_paths.json`), and only engine-created files may be updated. If you need to modify a user-provided file, report it as a blocker instead of writing over it.

### Blocked HTTP operations — absolute vs soft

The engine enforces two tiers of HTTP blocks:

**Absolute blocks — require explicit resource ID confirmation:**

SharePoint sites and Entra ID identities are protected (DELETE, PATCH, PUT all blocked). These can
only be allowed by an operator who types the **exact resource ID** shown in the prompt — a GUID,
UPN, or site ID extracted from the URL. Each resource requires its own individual confirmation;
there is no "allow all similar" option and no session-level allow.

- `/_api/site`, `/_api/web` — classical REST API site/web root
- `graph.microsoft.com/.../sites/{id}` — Graph site root
- `graph.microsoft.com/.../groups/{id}` — Microsoft 365 groups (backing SharePoint team sites)
- `graph.microsoft.com/.../users/{id}` — Entra ID users
- `graph.microsoft.com/.../servicePrincipals/{id}` — Entra ID service principals

Example absolute-block prompt:

```
────────────────────────────────────────────────────────────────────────────────
 PROTECTED RESOURCE — Explicit Confirmation Required
────────────────────────────────────────────────────────────────────────────────
 Role      : platform-builder
 Capability: http_request_with_secret_binding
 Reason    : Entra ID user modification or deletion is permanently prohibited.
             Method: DELETE  URL: https://graph.microsoft.com/v1.0/users/john@contoso.com

 ⚠  This targets a PROTECTED IDENTITY OR SITE.
    To allow this ONE operation, type the resource ID exactly:

      john@contoso.com

 Press Enter without typing to block.
────────────────────────────────────────────────────────────────────────────────
Confirm resource ID:
```

**Soft blocks — promptable by the operator (see Interactive Confirmation Prompt below):**

Detection is **action-based**: any `DELETE`, `PATCH`, or `PUT` via `http_request_with_secret_binding`
triggers a soft-block prompt, regardless of which domain or API is being called. This covers
Microsoft Graph, Azure management, Power BI, SharePoint, Qualys, Bitsight, or any other API.

The domain-specific entries below are retained to provide clearer block messages for known
Microsoft 365 and Azure resources; the catch-all covers everything else:
- `graph.microsoft.com/.../sites/.../lists/{id}` — SharePoint list deletion
- `graph.microsoft.com/.../teams/{id}` and `.../channels/{id}` — Teams deletion
- `graph.microsoft.com/.../termStore/groups/{id}` — term store deletion
- `management.azure.com/.../subscriptions/{id}` — Azure subscription deletion
- `management.azure.com/.../resourceGroups/{id}` — resource group deletion
- `management.azure.com/.../Microsoft.Logic/workflows/{name}` — Logic Apps deletion
- `management.azure.com/.../Microsoft.Web/connections/{name}` — connector deletion
- `api.powerbi.com/.../groups/{id}[/datasets|reports|dashboards|dataflows/{id}]` — Power BI deletion
- **Any other API** — caught by the action-based catch-all

Any mutating HTTP method (`POST`, `PUT`, `PATCH`, `DELETE`) via `http_request_with_secret_binding` additionally requires `delivery_mode: build_and_deploy`.

### Interactive Confirmation Prompt

When a **soft block** fires mid-run, the engine pauses and asks the operator:

```
────────────────────────────────────────────────────────────────────────────
 DESTRUCTIVE ACTION — Review Required
────────────────────────────────────────────────────────────────────────────
 Role      : platform-builder
 Capability: http_request_with_secret_binding
 Reason    : Azure Logic Apps workflow deletion is permanently prohibited.
             Method: DELETE  URL: https://management.azure.com/.../workflows/wf1

 [y] Allow once
 [A] Allow all similar operations this session
 [N] Block (default — press Enter)
────────────────────────────────────────────────────────────────────────────
Decision [y/A/N]:
```

- **`y`** — allow this one operation; guard stays active for everything else
- **`A`** — add a session-level allow for this block category; future identical soft blocks of the same type pass silently
- **`N` / Enter** — block, returned to the agent as a capability failure

**Non-interactive mode (CI/CD, stdin not a TTY):** the engine defaults to blocked and emits:
`[destructive-guard] Non-interactive mode — defaulting to blocked.`

### Deployment capability gate

`deploy_logic_app_definition` and `powerbi_import_artifact` require `delivery_mode: build_and_deploy`. Calling them in `build_only` mode or without a delivery mode is blocked.

### Blocked shell commands

`run_command` blocks the following patterns:

- `rm -r`, `rm -rf`, `rm --recursive` — recursive file deletion
- `find -delete` or `find -exec rm` — find with destructive action
- Fork bomb patterns (`: () { };:`)
- `shred` — irreversible file destruction
- `Remove-PnPListItem`, `Remove-PnPFile`, `Remove-PnPFolder` — PowerShell PnP bulk data removal
- `Remove-MgSiteListItem` — Microsoft Graph PowerShell list item removal

**Shell HTTP tool block (action-based):**

Using `curl`, `wget`, `az rest`, `Invoke-RestMethod`, `Invoke-WebRequest`, or similar HTTP CLI
tools with a mutating method (`DELETE`, `PATCH`, `PUT`) is blocked regardless of which domain
or API is being called. This prevents bypassing the `http_request_with_secret_binding` guard.

Example blocked commands:
```
curl -X DELETE https://qualys.qualys.com/api/2.0/fo/asset/host/
az rest --method PATCH --url https://bitsight.com/ratings/company
Invoke-RestMethod -Method DELETE -Uri https://any-api.example.com/items/abc
wget --method PUT https://my-internal-system.corp/config/reset
```

## Output Size Warning

The engine truncates agent output larger than 512KB before injecting it into downstream prompts. If your response includes large data (samples, full file contents, verbose logs), keep the total JSON output under 512KB. For large data, write it to a file via `write_file` or `persist_artifact` and reference the path instead.
