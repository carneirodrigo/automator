# Backend Configuration & Runtime Requirements

Read this when modifying backend selection, CLI vs API mode, config schema, or runtime network requirements. For architecture overview see engine/ORCHESTRATION.md.

## Backend Configuration (CLI vs API)

The system supports two execution modes: **CLI** (subprocess, the original model) and **API** (direct HTTP calls to vendor APIs). A single global switch in `config/backends.json` selects which mode the engine uses. Configuration lives in `config/` at the repo root (git-ignored).

### Configuration Files

- `config/backends.json` — Global execution mode (`cli` or `api`), provider, default model, and optional per-role overrides.
- `config/secrets.json` — API keys and auth credentials (separate file for tighter access control).

Templates are tracked at `engine/work/backends_config.template.json` and `engine/work/secrets_config.template.json`.

### Backward Compatibility

If `config/` does not exist or `config/backends.json` is missing, every backend defaults to CLI mode — behavior is identical to the original system. No existing CLI workflows are affected.

### Configuration Schema

`config/backends.json`:
```json
{
  "version": 2,
  "mode": "cli",
  "provider": "anthropic",
  "default_model": "claude-sonnet-4-20250514",
  "role_overrides": {
    "worker": { "model": "claude-opus-4-20250514" },
    "research": { "provider": "google", "model": "gemini-2.5-pro" }
  }
}
```

- `mode` — Global switch: `"cli"` or `"api"`. When `"cli"`, all other fields are ignored and `--cli claude|gemini|codex` drives execution. When `"api"`, the config drives everything.
- `provider` — Which vendor API to use: `"anthropic"`, `"google"`, or `"openai"`.
- `default_model` — The model used for all agent roles unless overridden.
- `role_overrides` — Optional per-role overrides. Each role can specify a different `model` and optionally a different `provider`.

`config/secrets.json`:
```json
{
  "version": 1,
  "anthropic_api_key": "sk-ant-...",
  "google_api_key": "AIza...",
  "openai_api_key": "sk-..."
}
```

### Resolution Order

When the engine executes an agent for a given role:
1. If `mode` is `"cli"`: return CLI mode. The `--cli claude|gemini|codex` flag drives execution; config is irrelevant.
2. If `mode` is `"api"`: use the global `provider` and `default_model`.
3. Check `role_overrides[role]` — if present, override `provider` and/or `model`.
4. Look up the API key for the resolved provider in `secrets.json`.

Provider-to-backend mapping: `anthropic` -> `claude`, `google` -> `gemini`, `openai` -> `codex`.

### CLI Management

- `./automator config setup` — Interactive wizard to configure mode, provider, API keys, models, and per-role overrides. Also runnable standalone: `python3 engine/work/config_wizard.py`.
- `./automator config show` — Display current configuration with redacted API keys.
- `./automator config validate` — Check that all API providers have keys configured.

### API Execution Path

When the global mode is `"api"`:
- The `--cli` flag is ignored; the engine uses the provider from config.
- The engine calls the vendor HTTP API directly instead of spawning a CLI subprocess.
- Prompts are built identically to the CLI path.
- The result envelope is identical: `{"status": "success|failed|capability_requested", "output": {...}, "duration": float}`.
- The capability re-invocation loop works identically.
- Session persistence uses portable handoff (backend-neutral continuity).

Vendor SDKs (`anthropic`, `google-genai`, `openai`) are optional dependencies — only needed when API mode is configured. They are lazy-imported so CLI-only usage never triggers an ImportError.

### Implementation Files

- `engine/work/backend_config.py` — Config loader, resolver, `BackendResolution` dataclass.
- `engine/work/api_execution.py` — API execution path for Anthropic, Google, and OpenAI.
- `engine/work/config_wizard.py` — Interactive config setup, show, and validate commands. Runnable standalone.
- `engine/work/backends_config.template.json` — Template for `config/backends.json`.
- `engine/work/secrets_config.template.json` — Template for `config/secrets.json`.

## Runtime Requirements

The orchestration engine requires outbound HTTPS/WebSocket access for all supported AI CLIs: `claude`, `gemini`, and `codex`.

If Automator is launched under any parent sandboxed launcher or supervisor runtime, the outer runtime must use this policy:

- Keep filesystem sandboxing enabled.
- Enable network access for spawned subprocesses.
- Do not disable network access for spawned backends through environment flags or launcher policy.

This is a shared runtime requirement, not a per-user shell tweak. User-local wrappers can help for direct CLI usage, but they do not override an outer launcher that already blocked network access before Automator starts.

If the engine detects that the outer runtime blocked network access for spawned backends, it fails fast with a remediation message instead of letting agent runs fail later with transport errors.

Use `./automator project check-runtime` to verify runtime reachability for installed backends before starting a project. You can also scope it to one backend, for example `./automator project check-runtime --claude`.

Example failure mode:

- a parent sandbox launcher started Automator with network disabled for child processes
- that block affected every backend spawned by Automator, not just the parent runtime's own CLI
- the same backend commands succeeded once run under an outer runtime that allowed outbound network access
