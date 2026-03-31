# Engine Internals & Extension Points

Read this when modifying engine code, adding capabilities, adding agent roles, or changing the orchestration loop. For runtime architecture overview see engine/ORCHESTRATION.md.

## Work Layer

- `engine/work/` — The active implementation area for runtime code.
  - `cli.py` — Unified CLI routing logic that dispatches to project execution, debug supervision, skills management, agent scaffolding, or backend configuration based on explicit subcommands or legacy compatibility flags.
  - `agent_admin.py` — Agent-spec administration helpers for listing current roles and scaffolding new `agents/*.md` files.
  - `orchestration_state.py` — Shared orchestration state models and run-control constants, including `StageContext`, stage limits, prompt-size caps, and deterministic forward-route definitions.
  - `runtime_entry.py` — Post-parse runtime assembly for the main entrypoint: repo bootstrap, backend/runtime checks, request preprocessing, project resolution, secret/input preparation, task-state loading, and delegation into the orchestration runner.
  - `prompts.py` — Prompt text helpers, prompt-context builders, knowledge/skills prompt injection, and input summarization.
  - `capabilities.py` — Runtime capability request validation, capability handlers, and the dispatch table for file/task/artifact/command/secret operations, including bounded platform-build and deployment helpers.
  - `destructive_guard.py` — Six-layer destructive action guard applied to every capability request before execution. Layers: role-based allowlist, absolute HTTP blocks (SharePoint sites and Entra users/SPs — require exact resource ID confirmation), soft HTTP blocks for named resource classes (`[y/A/N]` prompt), catch-all action-based block for DELETE/PATCH/PUT on any domain, shell command blocklist (`rm -rf`, `shred`, PnP bulk cmdlets), shell HTTP tool bypass detection (`curl`/`wget`/`az rest` with mutating methods), write-file protected-directory and overwrite protection, and script content scanning at write time and run time. Blocked results carry `[destructive-guard] BLOCKED` in `issues` and the `absolute` flag for hard blocks. When a capability loop exhausts due to guard blocks, `engine_runtime` upgrades `error_category` to `destructive_guard_block`.
  - `orchestrator.py` — The main orchestration runner: pending-resolution handling, stage loop execution, research/worker/review transitions, and completion/blocking control flow.
  - `sessions.py` — `AgentSession` dataclass (`mode`, `conversation_id`, `persistent`) and `PERSISTENT_SESSION_ROLES`.
  - `task_state.py` — `TaskState` TypedDict defining the schema for `projects/<id>/runtime/state/active_task.json`.
  - `runtime_helpers.py` — Request/project-resolution heuristics, feedback classification, session-ID extraction, runtime network-block detection, and `runtime_check_output_has_success`. Re-exports `load_json`, `write_json`, `load_json_safe`, `extract_json_payload`, `estimate_tokens`, `classify_error` from the focused modules below.
  - `json_io.py` — `load_json`, `write_json`, `load_json_safe`, `extract_json_payload`. Imported directly by modules that only need file I/O.
  - `tokenization.py` — `estimate_tokens` with tiktoken and character-count fallback. Falls back to `len(text) // 3` when tiktoken is not installed.
  - `error_classifier.py` — `classify_error` maps error strings to stable orchestration error categories (`binary_not_found`, `timeout`, `rate_limited`, etc.).
  - `project_state.py` — Project bootstrap/fork logic, registry persistence, secret-vault helpers, input inbox ingestion, and project-path inference.
  - `orchestrator.py` — Also handles rework-packet construction and review-artifact injection for the one rework cycle.
  - `execution.py` — Backend execution for agent runs: command building, runtime probes, prompt invocation, subprocess execution or API dispatch, output/session parsing, failure classification, execution summaries, result persistence, and the capability re-invocation loop.
  - `backend_config.py` — Backend configuration loader and resolver. Reads `config/backends.json` and `config/secrets.json`, resolves global CLI vs API mode, provider selection, and per-role overrides. Provides the `BackendResolution` dataclass.
  - `api_execution.py` — API execution path for vendor backends (Anthropic, Google, OpenAI). Lazy-imports vendor SDKs and returns the same result envelope as the CLI path.
  - `config_wizard.py` — Interactive config setup wizard, config display, and validation for `./automator config` commands.
  - `repo_paths.py` — Path constants (`REPO_ROOT`, `REGISTRY_PATH`, `CONFIG_DIR`, etc.) and safety helpers (`ensure_within_repo`).
  - `toon_adapter.py` — TOON (Token-Oriented Object Notation) encoder for prompt serialization. Reduces structured-data tokens by 30-45% vs JSON. Exposes `serialize_artifact_for_prompt(data, source_role)` — the entry point for artifact re-injection using lossless TOON encoding.
  - `secret_detector.py` — Secret detection, redaction, and leak scanning.
  - `credential_tester.py` — Format-only credential validation (Azure, AWS, API keys).
- `repo_bootstrap.py` — Repo bootstrap: ensures directory structure, config files, `.gitignore`, and repo-root symlinks exist on engine startup.
  - `skill_loader.py` — Agent Skills: SKILL.md parser, catalog/manifest operations, cache/freshness checks, and on-demand fetch.
  - `skill_sync.py` — Agent Skills CLI implementation for building the skills catalog from vendor GitHub repos and managing cached skills.
  - `debug_store.py` — Engine-side debug issue capture, fingerprinting, summary/criticality inference, and tracker/detail-file writing.
  - `debug_supervisor.py` — Debug issue analysis and verification workflow implementation.

Refactor rule:
- Put runtime implementation under `engine/work/`.
- Keep `engine/` root files only for the stable entrypoints that are intentionally user-facing.
- Keep all verification code under `engine/tests/`.

## Key Files

The project-structure section above is the canonical file inventory. For runtime behavior, focus on these hotspots:

- `engine/automator.py` / `engine/work/cli.py` — Public CLI entrypoint and command routing.
- `engine/work/runtime_entry.py` — Startup bootstrap, backend checks, project resolution, input/secret preparation, and handoff into orchestration.
- `engine/work/orchestrator.py` — Main stage loop, research/worker/review transitions, rework-packet construction, completion handling, and deterministic handoffs.
- `engine/work/execution.py` / `engine/work/api_execution.py` — CLI/API agent execution, capability loop, and compact progress heartbeats.
- `engine/work/prompts.py` — Prompt assembly, knowledge injection, skills injection, and input summarization.
- `engine/work/sessions.py` — Provider-native resume, portable handoffs, and persisted-role continuity.
- `engine/work/capabilities.py` — Capability dispatch, validation, and local host actions, including bounded low-code and platform-build operations.
- `engine/work/destructive_guard.py` — Destructive action guard. Every capability request passes through `check_capability()` before execution. Contains the role allowlist, HTTP block patterns (absolute and soft), shell command blocklist, shell HTTP bypass detection, write-file protection, and script content scanner. `is_absolute_block()` distinguishes hard blocks (resource ID required) from soft blocks (`[y/A/N]`). `register_created_path()` / `is_engine_created()` track engine-created files to allow safe overwrite.
- `engine/work/backend_config.py` — CLI/API mode resolution and per-role backend overrides.
- `engine/work/skill_loader.py` / `engine/work/skill_sync.py` — Agent Skills catalog, cache, and fetch flow.
- `engine/work/debug_supervisor.py` / `engine/work/debug_store.py` — Debug capture, verification, and issue supervision.
- `engine/tests/test_progress_execution.py` — Execution-layer coverage for capability loops, progress behavior, warning propagation, and capability-result reinjection.
- `engine/tests/test_destructive_guard.py` — Coverage for all six guard layers.

### Engine Internals

- `StageContext` (`engine/work/orchestration_state.py`) — Dataclass tracking the current pipeline stage (role, task, reason, inputs, etc.). State transitions create new instances.
- `execute_main_flow()` (`engine/work/runtime_entry.py`) — Post-parse main-entry flow used by `main()`. Handles startup bootstrap, backend/runtime checks, project resolution, secret/input handling, task-state preparation, and orchestration launch.
- `_CAPABILITY_DISPATCH` (`engine/work/capabilities.py`) — Dict mapping capability names to handler functions. Add new capabilities here. Current platform-build examples include `http_request_with_secret_binding`, `validate_logic_app_workflow`, `deploy_logic_app_definition`, `create_sharepoint_list_schema`, `create_powerbi_import_bundle`, `powerbi_import_artifact`, `powerbi_trigger_refresh`, and `powerbi_check_refresh_status`.
- `check_capability()` (`engine/work/destructive_guard.py`) — Guard entry point called by `_guarded_execute_capability` in `engine_runtime.py` for every capability request. Returns `None` to allow or a blocked-result dict to deny. All six protection layers run here in evaluation order.
- `ROLE_ALLOWED_CAPABILITIES` (`engine/work/destructive_guard.py`) — Dict mapping role name to the frozenset of capabilities that role may invoke. Requests outside this set are hard-blocked with no prompt. `None` key is the permissive fallback for unknown roles.
- `_ABSOLUTE_HTTP_BLOCK_COUNT` (`engine/work/destructive_guard.py`) — Integer constant (currently 6) that splits `_HARD_BLOCKED_HTTP` into absolute entries (first N) and soft entries (rest). Increment this to promote a soft HTTP block to absolute.
- `is_absolute_block()` (`engine/work/destructive_guard.py`) — Returns `True` if a blocked result carries `absolute=True`, meaning it requires explicit resource ID confirmation rather than a `[y/A/N]` prompt.
- `register_created_path()` / `is_engine_created()` (`engine/work/destructive_guard.py`) — In-memory registry of files the engine has written. Used by the write-file overwrite guard to allow agents to update their own outputs while blocking overwrites of pre-existing user files.
- `AgentSession` / `PERSISTENT_SESSION_ROLES` (`engine/work/sessions.py`) — The session continuity model for persisted roles and the backend-specific resume modes they use. API session modes: `anthropic_api`, `google_api`, `openai_api`.
- `BackendResolution` (`engine/work/backend_config.py`) — Dataclass with resolved execution parameters (mode, backend_name, model, api_key, base_url, timeout) for a given backend+role.
- `resolve_backend()` (`engine/work/backend_config.py`) — Resolves config for a backend+role pair: checks global mode first (CLI returns immediately), then applies provider and per-role overrides in API mode.
- `is_api_mode()` (`engine/work/backend_config.py`) — Returns whether the global config mode is `"api"`.
- `get_api_agent_bin()` (`engine/work/backend_config.py`) — Returns the CLI-equivalent binary name for the configured API provider (e.g., anthropic→"claude"), or `None` if not in API mode.
- `run_agent_api()` (`engine/work/api_execution.py`) — API equivalent of the CLI `run_agent()`. Calls vendor HTTP APIs and returns the same result envelope.
- `_get_api_caller()` (`engine/work/api_execution.py`) — Late-binding lookup for vendor-specific API callers (Anthropic, Google, OpenAI).
- `now_iso()` (`engine/work/runtime_helpers.py`) — UTC ISO-8601 timestamp.
- `load_json()` / `write_json()` / `load_json_safe()` (`engine/work/json_io.py`) — JSON file helpers (re-exported from `runtime_helpers`).
- `extract_json_payload()` (`engine/work/json_io.py`) — Extracts and parses the first complete JSON object from agent output text, including markdown fence stripping and nested response/result unwrapping.
- `extract_session_id_from_text()` (`engine/work/runtime_helpers.py`) — Recovers backend session/conversation IDs from CLI output.
- `detect_runtime_network_block()` (`engine/work/runtime_helpers.py`) — Fail-fast preflight: detects when an outer sandbox has disabled network for spawned backends.
- `estimate_tokens()` (`engine/work/tokenization.py`) — Estimates token count for a prompt string. Uses `tiktoken` if available, otherwise `len(text) // 3`. Re-exported from `runtime_helpers`.
- `classify_error()` (`engine/work/error_classifier.py`) — Maps error strings to stable orchestration error categories (`binary_not_found`, `timeout`, `rate_limited`, etc.). Re-exported from `runtime_helpers`.
- `bootstrap_project()` / `fork_project()` / `save_last_active_project()` (`engine/work/project_state.py`) — Project creation/forking and registry state persistence.
- `store_secrets()` / `load_secrets()` / `ingest_input_files()` (`engine/work/project_state.py`) — Secret-vault and input-inbox lifecycle helpers.
- `run_agent()` / `run_agent_with_capabilities()` (`engine/work/execution.py`) — Single-agent execution plus the capability re-invocation loop. This layer builds prompts, dispatches to CLI subprocess or API execution based on `resolve_backend()`, parses output, recovers session IDs, executes requested capabilities, and returns the normalized execution envelope. The API dispatch is opt-in via the `resolve_backend` and `run_agent_api` keyword parameters (both default to `None`, preserving CLI-only behavior when not wired).
- `build_agent_command()` / `run_runtime_check()` / `run_runtime_checks()` (`engine/work/execution.py`) — Backend CLI adapter and reachability probe helpers used by both normal execution and `--check-runtime`.
- `persist_result()` (`engine/work/execution.py`) — Artifact persistence for completed agent outputs.
- `_strip_execution_prompt_template()` — Removes the "Execution Prompt Template" section from agent specs at runtime (duplicates the spec body above it).
- `_strip_sections()` — Removes named `##` sections from text. Used to strip sections that duplicate content already injected separately (e.g., "Required Output", "Runtime Capabilities" from agent specs).
- `_build_project_inventory()` — Builds a compact project inventory (task state summary, artifact listing, project description) for agent prompts.
- `_effective_context_tokens()` (`engine/work/engine_runtime.py`) — Returns the effective context window for the configured model by prefix-matching against `_MODEL_EFFECTIVE_CONTEXT_TOKENS`. Used to scale compaction and recall-anchor thresholds per backend.
- `_compact_prompt_sections()` (`engine/work/engine_runtime.py`) — Drops low-priority sections when the prompt exceeds 70% of the model's effective window.
- `build_prompt()` — Constructs the full prompt for an agent. Injects capability reference, agent spec, output template, and dynamic context (project, task, inputs). Applies `minify_text`, strips redundant sections, and serializes structured data via `serialize_for_prompt()` (TOON format).
- `fork_project()` (`engine/work/project_state.py`) — Creates a new project inheriting selected artifacts from a source project. Copies latest role artifacts with `_inherited_from` provenance metadata.
- `run_orchestration()` (`engine/work/orchestrator.py`) — Lean lifecycle runner: handles pending user acceptance/feedback, bootstraps projects, runs worker → optional research → review → one rework cycle, and sets pending_resolution on completion.
- `_is_binary_file()` / `_get_project_input_paths()` (`engine/work/project_state.py`) — Input-file classification and project input discovery helpers.
- `ingest_input_files()` (`engine/work/project_state.py`) — Moves files from `inputs/` inbox to `projects/<project-id>/runtime/inputs/`, scans text files for secrets, writes an `inputs_manifest.json`.
- `MAX_CAPABILITY_ROUNDS` (`engine/work/orchestration_state.py`) — Cap on capability request/response cycles per agent stage.
- `MAX_FILE_READ_SIZE` (`engine/work/orchestration_state.py`) — Hard cap on file size that agents may read through capabilities.
- `_append_skills_catalog_context()` — Appends the compact Agent Skills catalog to the knowledge context for `research` only.
- `_build_skills_context()` — Injects downstream skill bodies selected for the current role into the agent prompt.
- `_cap_fetch_skill()` — Capability handler that downloads a skill from the catalog and returns its content for research evaluation.

### Import Conventions

- Runtime implementation should live under `engine/work/`.
- Human-facing command entry should go through `./automator` or the direct Python entrypoint `engine/automator.py`.
- Tests should import the implementation surface they are verifying. Example: `from engine.work.engine_runtime import ...`.
- Engine adds `REPO_ROOT` to `sys.path` so `engine.*` imports work from any working directory.

## Extension Points

### Adding a New Capability

1. Create a handler function: `def _cap_my_thing(capability: str, arguments: dict[str, Any]) -> dict[str, Any]:`
2. Return the standard envelope: `{"capability": capability, "status": "ok"|"failed", "result": ..., "issues": [...]}`
3. Add to `_CAPABILITY_DISPATCH` in `engine/work/capabilities.py`: `"my_thing": _cap_my_thing,`
4. Document in `docs/capability-requests.md`.
5. Add a test in `engine/tests/test_automator.py` or the most relevant existing suite.

### Adding a New Agent Role

1. Create `agents/new-role.md` with purpose, responsibilities, and execution prompt template.
2. Add the role to `ROLE_ALLOWED_CAPABILITIES` in `engine/work/destructive_guard.py` with the appropriate capability frozenset.
3. Add the role to `_AGENT_OUTPUT_TEMPLATES` in `engine/work/engine_runtime.py` with the expected output structure.
4. Wire the role into `run_orchestration()` in `engine/work/orchestrator.py` at the appropriate pipeline position.
5. Update the pipeline lifecycle and role list in `engine/ORCHESTRATION.md` and `README.md`.
6. Add at least one execution-path and one failure-path test.
7. If the role changes front-door delivery behavior or stage transitions, update the affected test suites.

### Adding a New Engine Decision Type

1. Add handling in `run_orchestration()` (`engine/work/orchestrator.py`) under the `if mode == "..."` chain.
2. Update `_AGENT_OUTPUT_TEMPLATES` in `engine/work/engine_runtime.py` if the new decision type has a distinct expected output shape.
3. Add handling in `progress.py` `run_summary_message()` if the decision needs a visible outcome label.
6. Add validation tests.
7. If the decision changes top-level routing or pending-resolution behavior, update the affected test suites.

### Modifying the Orchestration Loop

When you modify the pipeline lifecycle, pending-resolution semantics, session recovery, or capability-loop behavior:

- update the relevant runtime code under `engine/work/`
- update the contract docs in `engine/ORCHESTRATION.md`, `README.md`, and `docs/` as needed
- update the affected test suites in `engine/tests/` in the same change
- run at minimum:
  - `python3 -m unittest engine.tests.test_progress_execution -v`
  - `python3 -m unittest discover -s engine/tests -v`

The runtime entrypoint delegates to `run_orchestration()` and uses `StageContext` for state. Key patterns:
- State transitions create a new `StageContext(...)` instance (not mutation).
- Capability requests loop up to `MAX_CAPABILITY_ROUNDS` (5) before treating as failure.
- Rework is capped at one cycle (`MAX_REWORK_LOOPS`). Counter resets when review passes.
- Rework re-runs inject the latest failed review artifact into `required_inputs`.
- `needs_clarification` and `blocked` decisions persist a `pending_resolution` to task state so the next run can resume with context.
- `complete` decisions persist a `pending_resolution` with `type: user_acceptance` — the user must accept before the project closes. Rejection feeds back for rework. Before the acceptance prompt, the engine lists all files in `delivery/` so users can verify artifacts directly.
- The orchestration loop caps at `MAX_AUTOMATED_STAGE_EXECUTIONS` (20) as a circuit breaker.
- Data files (CSV, TSV, JSONL, logs) in agent inputs are **sampled**, not injected raw. CSV files get header + first 20 rows + last 5 rows + counts. This prevents prompt bloat from large outputs.
