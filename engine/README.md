# Engine Directory

This directory is the Python engine for the automator system. It contains the public entrypoint, the active implementation layer, and the test suites.

For the full file-by-file inventory, key function reference, and extension point recipes, see [`docs/engine-internals.md`](../docs/engine-internals.md).

---

## Top-Level Files

| File | Role |
|---|---|
| `automator.py` | Canonical Python entrypoint. Parses CLI flags and delegates to `engine/work/cli.py`. Run as `./automator` from repo root. |
| `__init__.py` | Makes `engine` a Python package so `engine.work.*` imports work from any working directory. |
| `ORCHESTRATION.md` | Symlink to `engine/ORCHESTRATION.md` (the authoritative AI-facing guide). Auto-created at startup. |

---

## `work/` — Implementation Layer

All runtime logic lives here. Files are split by concern:

### Entry & Routing
| File | Responsibility |
|---|---|
| `cli.py` | Unified CLI dispatch — routes `--project`, `--debug`, `--skill`, `--agent`, `--config` subcommands. |
| `runtime_entry.py` | Post-parse startup flow: bootstrap, backend checks, project resolution, secret/input prep, handoff to orchestrator. |
| `orchestrator.py` | Main pipeline loop: pending-resolution handling, worker → review lifecycle, one rework cycle, completion and blocking control flow. |

### Execution
| File | Responsibility |
|---|---|
| `execution.py` | CLI agent execution: subprocess dispatch, output parsing, session ID capture, capability re-invocation loop. |
| `api_execution.py` | API execution path for Anthropic, Google, OpenAI — returns the same result envelope as `execution.py`. |
| `backend_config.py` | Loads `config/backends.json`, resolves CLI vs API mode, provider, model, and per-role overrides. |

### Prompt Assembly
| File | Responsibility |
|---|---|
| `engine_runtime.py` | `build_prompt()` for all roles. Applies minification, section stripping, TOON serialization, recall anchors, proactive compaction, and the compact-followup path for active sessions. |
| `prompts.py` | Prompt text helpers, knowledge/skills injection, condensed research handoff, input summarization and sampling. |

### Capabilities
| File | Responsibility |
|---|---|
| `capabilities.py` | Capability dispatch table (`_CAPABILITY_DISPATCH`), all handler functions, and `_CAPABILITY_QUICK_REFERENCE` for prompt injection. Includes file ops, git helpers, test runner, secrets, platform-build, and deployment handlers. |
| `destructive_guard.py` | Destructive action guard — six-layer safety policy applied to every capability request before execution. Covers role allowlist, absolute HTTP blocks (SharePoint/Entra, require resource ID confirmation), soft HTTP blocks (`[y/A/N]`), catch-all action-based detection for DELETE/PATCH/PUT on any domain, shell command blocklist, shell HTTP bypass detection, write-file protected-directory and overwrite protection, and script content scanning at write and run time. |

### Sessions & State
| File | Responsibility |
|---|---|
| `sessions.py` | `AgentSession` dataclass, `PERSISTENT_SESSION_ROLES`, and provider resume modes. |
| `task_state.py` | `TaskState` TypedDict — schema for `projects/<id>/runtime/state/active_task.json`. |
| `project_state.py` | Project bootstrap/fork, registry persistence, secrets vault, input inbox ingestion. |
| `orchestration_state.py` | Stage/capability/inspect limits and `CMD_OUTPUT_INLINE_LIMIT`. |

### Infrastructure
| File | Responsibility |
|---|---|
| `runtime_helpers.py` | Session-ID extraction, project resolution helpers, feedback classification, network-block detection; re-exports `json_io`, `tokenization`, `error_classifier`. |
| `json_io.py` | `load_json`, `write_json`, `load_json_safe`, `extract_json_payload`. |
| `tokenization.py` | `estimate_tokens` with tiktoken and character-count fallback. |
| `error_classifier.py` | `classify_error` — maps error strings to orchestration error categories. |
| `repo_paths.py` | `REPO_ROOT`, `REGISTRY_PATH`, `CONFIG_DIR` and `ensure_within_repo()` safety helper. |
| `repo_bootstrap.py` | Ensures directory structure, config templates, `.gitignore`, and repo-root symlinks exist on startup. |
| `toon_adapter.py` | TOON encoder (`serialize_for_prompt()`), `serialize_artifact_for_prompt()`. |
| `secret_detector.py` | Secret detection, redaction, and leak scanning for prompts and file writes. |
| `credential_tester.py` | Format-only credential validation (Azure, AWS, API keys). |
| `progress.py` | Stage-start messages, heartbeat messages, capability action messages, token-estimate annotation. |

### Skills & Config
| File | Responsibility |
|---|---|
| `skill_loader.py` | Agent Skills: SKILL.md parser, catalog/manifest operations, cache and freshness checks, on-demand fetch. |
| `skill_sync.py` | `./automator --skill` CLI: builds skills catalog from vendor GitHub repos, manages cached skills. |
| `config_wizard.py` | Interactive config setup, display, and validation for `./automator --config`. |
| `agent_admin.py` | `./automator --agent list/add` — lists roles and scaffolds new `agents/*.md` files. |

### Debug
| File | Responsibility |
|---|---|
| `debug_store.py` | Captures debug issues, fingerprints them, infers criticality, writes tracker/detail files. |
| `debug_supervisor.py` | Debug analysis and verification workflow for `./automator --debug`. |

---

## `tests/` — Test Suites

| File | What it covers |
|---|---|
| `test_lean_orchestration.py` | End-to-end pipeline: new project, continue, research branch, rework loop, close — scripted agent outputs, zero LLM usage. |
| `test_progress_execution.py` | Execution-layer: capability loops, progress emission, warning propagation, capability-result reinjection, API heartbeats. |
| `test_destructive_guard.py` | Destructive action guard: all six protection layers — role allowlist, absolute and soft HTTP blocks, catch-all action-based detection, delivery mode gate, shell blocklist, shell HTTP bypass, write-file protection, script content scanning at write and run time. |
| `test_automator.py` | CLI routing, flag parsing, project resolution, registry fallback, intent classification. |
| `test_backend_config.py` | Backend configuration loading, CLI vs API mode resolution, per-role overrides. |
| `test_api_execution.py` | API execution path: vendor callers, timeout handling, JSON extraction, error classification. |
| `test_engine_runtime.py` | Prompt building, section compaction, knowledge/skills injection, TOON encoding. |
| `test_project_state.py` | Project bootstrap, fork, registry persistence. |
| `test_skill_loader.py` | Skills catalog parsing, cache freshness, on-demand fetch. |
| `test_skill_sync.py` | Skills sync from vendor repos. |
| `test_agent_admin.py` | Agent scaffolding and listing. |
| `test_config_wizard.py` | Config setup and validation. |
| `test_credential_tester.py` | Credential format validation. |
| `test_debug_supervisor.py` | Debug analysis and verification workflow. |
| `test_repo_bootstrap.py` | Directory structure and symlink bootstrap. |

Run all tests:
```
python3 -m unittest discover -s engine/tests -v
```

---

## Request Flow

```
./automator (flags)
  └─ engine/automator.py
       └─ engine/work/cli.py              (command routing)
            └─ engine/work/runtime_entry.py    (bootstrap, project resolution)
                 └─ engine/work/orchestrator.py   (pipeline: worker → review → complete)
                      ├─ engine/work/engine_runtime.py  (build_prompt)
                      └─ engine/work/execution.py       (run agent, capability loop)
```

Each stage: `build_prompt → spawn agent (CLI or API) → parse output → execute capability requests → return result → route to next stage or complete`.
