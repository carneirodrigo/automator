# AI Repository & Orchestration Guide

## Purpose

This file is the authoritative AI-facing guide for this repository. It covers both:

1. **Operating the system** — running the orchestration pipeline to deliver working outputs for user requests, including code, guides, and professional document artifacts.
2. **Developing the system** — modifying the engine, agent specs, schemas, and tests that make up the control plane itself.

Any AI agent (Claude, Gemini, Codex) working in this repository must understand both roles.

## Terminology

This repository implements a multi-agent orchestration system.

The agents in this system are orchestration agents: role-specific workers executed through external AI CLI backends and coordinated by the local engine. They have bounded responsibilities, capability access, artifact handoff, and structured routing.

These orchestration agents are conceptually similar to vendor SDK agents, but they are not defined by a vendor-specific agent runtime or schema. The difference is the implementation model, not the presence of agent behavior.

Terms used in this repository:

- `backend`: the AI execution provider or CLI, such as `gemini`, `claude`, or `codex`
- `agent`: an orchestration worker with a defined role in this system
- `role`: the responsibility assigned to an agent — `worker`, `review`, or `research`
- `supervisor`: the repair workflow that analyses and fixes captured debug issues

## What This Repository Is

This repository is a **multi-agent orchestration engine AND a Python codebase**. It has two sides:

- **As an orchestration system:** It takes a user request (e.g., "build a script that fetches GitHub issues"), runs it through a pipeline (worker → review), and delivers working outputs under `projects/<project-id>/delivery/`.
- **As a Python project:** The canonical Python entrypoint is `engine/automator.py`, the runtime implementation lives under `engine/work/`, agent specifications live in `agents/*.md`, shared contracts in `docs/`, and verification lives in `engine/tests/`. All of this is code that can be read, modified, tested, and extended.

The three roles are:

- `worker` — a DevOps/DevSecOps engineer. Implements the task, produces delivery artifacts, writes and runs tests, makes API calls, creates documents, and deploys when explicitly asked.
- `review` — verifies the worker's output; one rework cycle is allowed if review finds issues.
- `research` — answers specific external questions (API behaviour, third-party docs) that the worker cannot verify locally. Dispatched automatically when the worker signals `needs_research: true`.

Role definitions live under `agents/`.

## AI Interface & Entrypoint Consistency

To ensure a seamless experience across different AI platforms and maintain a single source of truth for repository mandates:

- **Universal AI Guidance:** `ORCHESTRATION.md`, `GEMINI.md`, `CLAUDE.md`, and `AGENTS.md` at the repo root are symlinks to `engine/ORCHESTRATION.md`. This architecture ensures that any AI assistant (Gemini, Claude, or Codex) automatically loads the same authoritative orchestration rules and project context, regardless of which specific filename its internal logic prioritizes for repository discovery.
- **Simplified Command Entry:** `engine/automator.py` is the canonical Python entrypoint for project runs, debug supervision, skills commands, and agent scaffolding. The tracked root-level `automator` launcher prefers `./.venv/bin/python3` when available.
- **Auto-Created, Not Tracked:** The repo-root symlinks (`ORCHESTRATION.md`, `CLAUDE.md`, `GEMINI.md`, `AGENTS.md`) and `.gitignore` are auto-created by `ensure_repo_structure()` (in `engine/work/repo_bootstrap.py`). Bootstrap runs automatically at engine startup and from `./automator --config setup`. The `automator` launcher itself is tracked in git.

First-time setup for new users:

1. `pip install -r requirements.txt` — installs dependencies
2. `./automator --config setup` — checks environment, configures CLI or API backend
3. `./automator --cli claude --check-runtime` — verifies backend reachability

Python dependency model:

- Automator is designed to work with Python 3.10 or higher.
- The recommended installation flow is `pip install -r requirements.txt` followed by `./automator --config setup`.
- Prefer running the engine as `./automator ...`. The launcher uses `./.venv/bin/python3` if available and falls back to `python3`.
- Optional lightweight document/PDF helpers:
  - `sudo apt-get install -y poppler-utils qpdf`
- With the repo-local `.venv` plus those helpers, the current tested baseline is:
  - `.docx` creation and editing works
  - `.pdf` creation, extraction, and render checks work
  - `.xlsx` creation and editing works
  - Excel formula recalculation does not work without LibreOffice
- `pandoc` and LibreOffice are not required for this baseline.

## CLI Contract

Everything is a flag. The task description (`--task`) is the only multi-word value and needs no quotes unless it contains shell special characters. Flags are order-independent; `--task` last by convention.

**Rule:** `--cli <llm>` or `--api` required whenever a backend is needed. Omitting both for a backend-dependent command is a hard error.

### Backend selection (required for project/debug-run/check-runtime)

```
--api                         Use API backend from config/backends.json
--cli claude|gemini|codex     Use CLI backend
```

### Project work (needs backend)

```
./automator --api --project new --task <description>
./automator --cli claude --project new --task <description>
./automator --cli claude --project continue --id <project-id> --task <description>
./automator --cli claude --project fork --id <project-id> --task <description>
./automator --cli claude --project new --debug --task <description>   # capture mode
./automator --project list                                             # local, no backend
```

`--project` accepts exactly: `new`, `continue`, `fork`, `close`, `delete`, `list`. Anything else is a hard error.
`--id` is the exact project folder name (matches registry). Required for `continue`, `fork`, `close`, and targeted `delete`.

### Health check (needs backend)

```
./automator --cli claude --check-runtime
./automator --api --check-runtime
```

### Debug issue management (local, no backend)

```
./automator --debug open                                               # default
./automator --debug list [--status open|in_progress|fixed|regressed]
./automator --debug analyse [--status ...]
./automator --debug verify --id <issue-id> --verify-command <cmd> --summary <text>
```

`--debug` alone (no value) defaults to `open`. With `--project`, it enables capture mode instead.

### Local admin (no backend needed)

```
./automator --config setup|show|validate
./automator --skill list|check|catalog|fetch|rebuild-manifest
./automator --knowledge purge --id <project-id>
./automator --agent list|add
```

`./automator ...` is the equivalent repo-root launcher form and will use the repo-local `.venv` automatically when present.

The CLI parser is intentionally strict. Invalid action values fail fast with a clear error.

## Architecture: Agent-Driven Orchestration

The system follows an agent-driven orchestration model:

- **Engine is the Orchestrator:** The local engine runs the pipeline directly — no master routing agent.
- **Agents are Bounded Workers:** Each agent performs a specific task (implement, review, or research) within a bounded stage.
- **Infrastructure is Local:** Local Python scripts (the "Host") handle loading context, spawning agents, persisting artifacts, and enforcing safety guards.
- **No Local Reasoning Substitutes:** The local host must never substitute for agent reasoning or silently replace a failed spawned agent with local logic.

### Pipeline

```
user → [plan?] → worker → review → [pass: complete | fail: one rework → complete]
```

For complex tasks (multiple systems, credentials, sequential steps), the engine runs a lightweight planning step first:

```
user → plan(questions?) → [user answers] → worker(with plan) → review → complete
```

When the worker signals `needs_research: true`, the engine dispatches research and re-runs the worker (up to 2 research cycles):

```
user → worker(needs_research) → research → worker(with findings) → [still needs research?] → research → worker → review → complete
```

When the worker reports `blocked` with researchable issues (not credentials/permissions), the engine challenges the blockers with research:

```
user → worker(blocked: "unknown endpoint") → research(challenge) → worker(with findings) → review → complete
```

The engine runs a fixed lifecycle with no dynamic routing:

1. `worker` — implements the task; may signal `needs_research: true` if external facts are needed
2. `research` (conditional, up to 2 cycles) — runs when the worker flags `needs_research` or reports researchable blockers; answers the worker's specific questions, then the worker re-runs with the findings injected
3. `review` — verifies the output; one rework cycle is allowed if review fails
4. `complete` — engine presents delivery files for user acceptance

**Delivery assurance features:**

- **Task planning:** Complex tasks (multiple systems, credentials, sequential steps) trigger a lightweight planning step that decomposes the request into ordered steps. If critical information is missing, the engine returns questions to the user before starting implementation. Simple tasks skip planning entirely (zero extra tokens).
- **LLM planning classifier fallback:** When the regex heuristic is inconclusive (exactly one complexity signal in a substantive request), the engine asks the backend to classify the request as `needs_planning: true/false` via a minimal prompt. Clear cases stay on the fast path; only ambiguous requests pay the extra classification call. Classifier failure defaults to skip-planning (safe default).
- **Write-test-fix cycle:** The worker is required to run code after writing it, fix failures, and iterate — not just write and report success. Capability rounds are used for test-fix cycles.
- **Delivery verification:** After the worker reports success, the engine verifies that claimed files (artifacts, changes_made) actually exist on disk.
- **Review enforcement:** Reviews that pass without running any commands or tests are auto-demoted to fail, triggering a rework cycle with actual verification.
- **Transient retry:** Rate limits, timeouts, and provider errors are retried up to 2 times with exponential backoff (5s, 10s). Fatal errors (binary not found, permission denied) fail immediately.
- **Output validation:** Each stage's output is checked for minimum required fields (worker: `summary`; review: `status`). Empty or structureless output fails the stage instead of passing silently.
- **Review default:** If the review agent returns no `status` field (unparseable output), the engine defaults to "fail" and triggers a rework cycle rather than silently accepting broken work.
- **Stage resume:** On `--project continue` after a failed review, the engine detects the prior successful worker artifact and skips directly to review instead of re-running the full pipeline.
- **Project ID validation:** `--project continue --id <id>` and `--project fork --id <id>` fail fast with a clear message if the project ID does not exist in the registry.
- **Graceful error handling:** The top-level entrypoint catches common exceptions (corrupt JSON, permission denied, missing files) and prints one-line user-friendly messages instead of raw Python tracebacks. Set `PYTHONTRACEBACK=1` to see full tracebacks for development.
- **Adaptive capability rounds:** The engine assigns 5 (simple), 8 (medium), or 12 (planned/complex) capability rounds based on task complexity signals. Planning, research, and review keep the default 5.
- **Context compaction:** As capability rounds accumulate, older round results are compacted to one-line summaries (e.g., `Round 1 (compact): read_file=ok; run_command=ok`) while the 2 most recent rounds retain full detail. This prevents context window degradation on complex tasks.
- **Delivery context pre-injection:** On continue runs, the worker receives a compact snapshot of the delivery directory (file tree + contents of small text files) at prompt time, saving 1-2 capability rounds of discovery.
- **Blocker challenge:** When the worker reports `status: blocked`, the engine classifies each blocker as hard (credentials, permissions, user decisions) or researchable (API questions, implementation unknowns). Researchable blockers auto-dispatch research and re-run the worker instead of stopping.
- **Research retry:** The research loop runs up to 2 cycles. If the post-research worker still flags `needs_research` with new questions, the engine dispatches a second research cycle before proceeding to review.
- **Structured rework:** Review feedback is injected as a numbered checklist pairing each blocking issue with its required fix, so the worker can address items systematically.

**Project IDs** are sequential numbers: `001`, `002`, `003`, etc.

**Project Forking:** Use the keyword `fork` in your request, or the front-door CLI `./automator --cli <llm> --project fork --id <project-id> --task <description>`, to create a new project that inherits artifacts from an existing one. The engine copies selected artifacts from the source project and marks them as inherited.

## Worker Capabilities

The worker is a DevOps/DevSecOps engineer with access to the full capability set:

- **Read and search** — files, directories, code search, git history
- **Write and execute** — `write_file`, `run_command`, `run_tests`
- **HTTP and APIs** — `http_request_with_secret_binding` for Microsoft Graph, SharePoint, Power BI, Azure, Qualys, and any other REST API; all mutating methods trigger the operator guard
- **Platform capabilities** — `validate_logic_app_workflow`, `create_sharepoint_list_schema`, `create_powerbi_import_bundle`, `powerbi_trigger_refresh`, `powerbi_check_refresh_status`
- **Deployment** — `deploy_logic_app_definition`, `powerbi_import_artifact`; only executes when explicitly requested and operator approves the guard prompt
- **Secrets** — `load_secrets`, `save_secret`, `test_credentials`
- **Memory** — `load_memory`, `save_memory`
- **Artifacts and state** — `persist_artifact`, git query caps

The full capability reference and allowlists per role are in `docs/capability-requests.md`.

## Data Handling

To prevent context exhaustion:

- **512KB Truncation (Engine Guardrail):** The host script truncates agent output >512KB before injecting it into downstream prompts. Full data remains in artifact files on disk.
- **File read cap:** Agents may not read files larger than `MAX_FILE_READ_SIZE` through capabilities. Larger files must be accessed through targeted read-lines or search capabilities.

## Secrets Management

The system provides automatic secret detection, storage, and leak prevention:

- **Detection:** The engine scans user prompts for secrets (API keys, passwords, tokens, tenant IDs, AWS keys, etc.) using pattern-based detection. Detected secrets are automatically stored in the project's secrets vault and redacted from the prompt before it reaches any LLM.
- **Storage:** Secrets are stored in `projects/<project-id>/secrets/secrets.json`, which is git-ignored. Each entry includes key, value, type, label, source, and timestamp.
- **Retrieval:** Agents use the `load_secrets` capability to retrieve project secrets. The `save_secret` capability allows storing new secrets discovered during work.
- **Leak Prevention:** The engine guards `write_file`, `persist_artifact`, `create_sharepoint_list_schema`, and `create_powerbi_import_bundle` capabilities — if content contains a known secret value from the project vault, the write is blocked with an error. Secrets must be injected via environment variables or config references, never hardcoded.
- **Pre-project Secrets:** If secrets are detected before a project is bootstrapped, they are held in memory and flushed into the project vault once bootstrap completes.

## Destructive Action Guard

Every capability request from every agent passes through the destructive action guard before it executes. The guard is enforced at the engine level in `engine/work/destructive_guard.py` — it cannot be bypassed by agent instructions or prompt content.

### Protection layers (in evaluation order)

1. **Role-based capability allowlist** — each role is restricted to a specific set of capabilities. Requests outside the allowlist fail immediately with no prompt.

2. **Absolute HTTP blocks** — SharePoint sites and Entra ID users/service principals (DELETE, PATCH, PUT) are absolutely protected. The engine pauses and requires the operator to type the exact resource ID to proceed. No session-level allow is possible.

3. **Soft HTTP blocks** — any `DELETE`, `PATCH`, or `PUT` via `http_request_with_secret_binding` triggers a `[y/A/N]` operator prompt regardless of which API or domain is called (Qualys, Bitsight, internal systems, Microsoft 365, Azure — all covered). `[A]` adds a session-level allow for the same block category.

4. **Shell command blocklist** — `rm -r/rf`, `find -delete`, fork bombs, `shred`, PowerShell PnP bulk-removal cmdlets, and shell HTTP tools (`curl`/`wget`/`az rest`/`Invoke-RestMethod`) used with DELETE/PATCH/PUT are blocked with a `[y/A/N]` prompt.

5. **Write file protection** — writes to `engine/`, `agents/`, `docs/`, `config/`, `knowledge/`, or `skills/` are blocked. Overwrites of files not created by the engine are blocked. Scripts written to delivery directories are scanned for destructive patterns before saving.

6. **Script content scan** — when an agent writes or runs a script file (`.py`, `.sh`, `.ps1`, `.js`, etc.), the engine scans its content for destructive patterns (shell commands, HTTP mutation calls) and prompts the operator before the write or execution proceeds.

### Non-interactive mode

When stdin is not a TTY (CI/CD pipelines, piped input), all soft blocks default to blocked automatically. Absolute blocks always require explicit resource ID confirmation and cannot be bypassed non-interactively.

### What agents see

When a capability is blocked, the failure result is injected back into the agent's capability round with a `[destructive-guard] BLOCKED: ...` issue. Agents must report persistent blocks as blockers rather than retrying the same capability. Once the per-stage capability round budget (5/8/12 depending on task complexity) is exhausted, the stage fails with a `capability_loop` error enriched with the specific guard block reasons.

The full policy reference is in `docs/capability-requests.md`.

## Audit Expectations

When auditing this repository, focus on:
1. Integrity of the pipeline stage ordering (worker → [optional: research → worker] → review) and the single rework cycle cap.
2. Compliance with the "no local agent runners" rule — the host must not substitute its own reasoning for a failed spawned agent.
3. Visibility of agent execution and token usage in the timeline, including compact stage-start updates, sparse slow-run heartbeats, observable capability actions, and concise completion or next-stage messages.
4. Integrity of the destructive action guard — all capability requests must pass through `_guarded_execute_capability`; guard logic in `destructive_guard.py` must not be weakened without a corresponding policy decision.

## Test Contract

The test suites are part of the active engine contract. They are not auxiliary documentation checks.

- `engine/tests/test_progress_execution.py` — execution-layer behavior: capability loops, progress emission, and capability-result reinjection.
- `engine/tests/test_destructive_guard.py` — destructive action guard coverage across all protection layers.
- Other suites under `engine/tests/` cover CLI routing, backend config, project state, skills, and debug tooling.

Required maintenance rule:

- if you change orchestration semantics, the pipeline lifecycle, pending-resolution handling, session recovery, or capability-loop behavior, you must update the affected tests in the same change
- a failing test should be treated as either a real regression or an intentional contract change that has not yet been reflected in tests and docs
- do not weaken tests just to make the suite pass unless the underlying engine contract genuinely changed

## Project Structure

### Root

- `engine/ORCHESTRATION.md` — This file. The authoritative AI-facing guide for the repository.
- `ORCHESTRATION.md`, `CLAUDE.md`, `GEMINI.md`, `AGENTS.md` — Symlinks to `engine/ORCHESTRATION.md` so all AI platforms load the same guide.
- `README.md` — Human-facing project overview and quick start.
- `automator` — Tracked repo-root launcher for `engine/automator.py`. Prefers the repo-local `.venv` when present.
- `REQUIREMENTS.md` — Human-readable dependency and system prerequisites reference.
- `requirements.txt` — Machine-readable Python package list for pip.

### Engine

- `engine/` — Runtime entry surface plus the active work and test layers.
  - `automator.py` — Canonical Python entrypoint for the unified CLI. Routes by intent to project execution, debug supervision, skills management, or agent scaffolding.
  - `work/` — Real implementation modules. See `docs/engine-internals.md` for the full file-by-file inventory.
  - `tests/` — All tests and fixtures. Run with `python3 -m unittest discover -s engine/tests -v`.

### Agent Specifications

- `agents/` — Markdown files that define each agent role's behavior, responsibilities, and output format. Changes here affect LLM behavior, not Python code.

### Contracts and Schemas

- `docs/` — Shared contracts, policies, and reference documentation.
  - `capability-requests.md` — Capability request system available to agents.
  - `project-runtime-layout.md` — Per-project runtime directory structure.
  - `project-registry.md` — Project registry format and resolution policy.
  - `runtime-host.md` — Engine responsibilities and boundaries.
  - `credential-testing.md` — Credential validation reference.
  - `engine-internals.md` — Work layer file inventory, key function reference, import conventions, and extension point recipes.
  - `token-efficiency.md` — Prompt-reduction techniques and non-goals.
  - `backend-and-runtime.md` — Backend configuration schema, CLI vs API mode, resolution order, and runtime network requirements.
  - `data-and-secrets.md` — Secrets detection/storage, write guards, and the inputs/ drop zone.
  - `development-guide.md` — Agent supervisor mode, debug workflow, coding standards, audit expectations, source control, and operator/developer how-to.
  - `schemas/` — JSON schemas for capability requests and results.

### Data Directories

- `projects/` — All per-project data (git-ignored, structure preserved via `projects/.gitkeep`).
  - `registry.json` — Machine-readable project registry (git-ignored, created automatically).
  - `registry.csv` — Human-readable project index (git-ignored, created automatically).
  - `<NNN>/delivery/` — User-facing deliverables: scripts, outputs, reports. Project IDs are sequential numbers (001, 002, 003...).
  - `<NNN>/runtime/` — Orchestration internals: task state, artifacts, memory.
  - `<NNN>/secrets/` — Credentials vault: API keys, tokens.
- `inputs/` — Drop zone for user-provided files (content git-ignored, structure preserved via `.gitkeep`). Files are moved to `projects/<project-id>/runtime/inputs/` on engine start.
- `knowledge/` — Shared knowledge base. Contains `manifest.json` (entry index), `sources.json` (source-family routing hints), and tracked `<topic-slug>.json` files with reusable technical findings extracted from completed projects.
- `skills/` — Agent Skills directory. Contains `catalog.json` (all available vendor skills), `manifest.json` (cached skills), `sources.json` (vendor repo config), and `<vendor>--<skill-name>/SKILL.md` cached skill files.
- `config/` — Backend configuration (content git-ignored, structure preserved via `.gitkeep`). Contains `backends.json` (global mode, provider, default model, per-role overrides) and `secrets.json` (API keys). Created by `./automator --config setup` or manually from templates.
- `personal/` — User-specific configuration (git remote, SSH keys, tool preferences). Only `README.md` is tracked. AI agents read this directory to understand the user's environment.

## Build, Test, And Development Commands

- `./automator --cli claude --project new --task build a script`: New project in CLI mode.
- `./automator --api --project new --task build a script`: New project in API mode (provider from config).
- `./automator --cli claude --project continue --id demo --task add retries`: Continue an existing project.
- `./automator --cli claude --project fork --id demo --task store results in sharepoint`: Fork a project into a new one.
- `./automator --cli claude --check-runtime`: Probe configured backend reachability.
- `./automator --debug open`: List open debug issues with summaries.
- `./automator --skill list`: List cached skills.
- `./automator --agent list`: List current agent specifications.
- `./automator --config setup`: Interactive backend configuration wizard.
- `./automator --config show`: Display current backend configuration.
- `./automator --config validate`: Validate API keys for API-mode backends.
- `python3 -m unittest discover -s engine/tests -v`: Run all tests.
- `python3 -m unittest engine.tests.test_progress_execution -v`: Run the execution-layer suite.
- `python3 -m unittest engine.tests.test_destructive_guard -v`: Run the destructive guard suite.
- `python3 -m unittest engine.tests.test_automator.SomeTestClass -v`: Run a single test class.

GitHub Actions runs the full `unittest discover` suite on every push/PR to `master` under Python 3.10 / 3.11 / 3.12 (see `.github/workflows/tests.yml`).

## Runtime Requirements

The orchestration engine requires outbound HTTPS/WebSocket access for all supported AI CLIs: `claude`, `gemini`, and `codex`.

If Automator is launched under any parent sandboxed launcher or supervisor runtime, the outer runtime must use this policy:

- Keep filesystem sandboxing enabled.
- Enable network access for spawned subprocesses.
- Do not disable network access for spawned backends through environment flags or launcher policy.

This is a shared runtime requirement, not a per-user shell tweak. User-local wrappers can help for direct CLI usage, but they do not override an outer launcher that already blocked network access before Automator starts.

If the engine detects that the outer runtime blocked network access for spawned backends, it fails fast with a remediation message instead of letting agent runs fail later with transport errors.

Use `./automator --cli claude --check-runtime` to verify runtime reachability for a specific backend before starting a project. Use `./automator --api --check-runtime` to verify API-mode reachability.

## Reference Documentation

Detailed references for specific topics. Load the relevant doc when working on that area — do not load all of them unless you need all of them.

| Document | Read when... |
|---|---|
| `docs/engine-internals.md` | Modifying engine code, adding capabilities, adding agent roles, changing the orchestration loop |
| `docs/token-efficiency.md` | Touching prompt assembly, serialization, context management, or adding new prompt sections |
| `docs/backend-and-runtime.md` | Modifying backend selection, CLI vs API mode, config schema, or runtime network requirements |
| `docs/data-and-secrets.md` | Modifying secrets detection/storage, capability write guards, or the inputs/ drop zone |
| `docs/capability-requests.md` | Understanding or modifying the destructive action guard, capability allowlists, blocked patterns, or operator prompt behaviour |
| `docs/development-guide.md` | Operating the pipeline, supervising debug issues, developing the engine, or following coding/audit standards |

Agent contracts (always available to spawned agents):
- `docs/capability-requests.md` — Capability request system
- `docs/project-runtime-layout.md` — Per-project runtime directory structure
- `docs/project-registry.md` — Project registry format and resolution policy
- `docs/runtime-host.md` — Engine responsibilities and boundaries
- `docs/credential-testing.md` — Credential validation reference
