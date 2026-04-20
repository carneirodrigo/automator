# Automator

Automator is a multi-agent orchestration engine that takes natural language requests and delivers working, tested output through a pipeline of specialized AI agents. It works with multiple AI backends (Claude, Gemini, Codex) and manages the full lifecycle from requirements through delivery.

**A note on "agents":** these are not SDK agents or vendor agent runtimes. Each agent here is a role — a set of instructions and responsibilities — executed by spawning an AI CLI tool (or calling an API) as a subprocess. The local engine coordinates them: it builds the prompt, runs the backend, validates the output, persists artifacts, and decides what runs next. The agents themselves have no persistent process or memory between invocations; the engine holds all state.

It supports three delivery modes:

- **Code delivery** — working scripts, applications, and integrations with automated QA and security review
- **Guide delivery** — professional implementation documents, operator runbooks, and platform configuration guides
- **Platform build delivery** — deployable low-code or platform artifacts such as Logic Apps, SharePoint schemas, and Power BI automation bundles

## Getting Started

### 1. Clone and Install

```text
git clone <repo-url>
cd automator
pip install -r requirements.txt
```

*Optional: Create a virtual environment first (`python3 -m venv .venv && . .venv/bin/activate`).*

### 2. Install an AI Backend

You need at least one AI backend. Choose CLI mode or API mode:

**CLI mode** (default) — install one or more AI CLI tools:

```text
npm install -g @anthropic-ai/claude-code     # Claude
npm install -g @google/gemini-cli             # Gemini
npm install -g @openai/codex                  # Codex
```

**API mode** — use a vendor API key instead (no CLI tool needed):

```text
pip install anthropic      # for Anthropic
pip install google-genai   # for Google
pip install openai         # for OpenAI
```

### 3. Run the Setup Wizard

The setup wizard checks your environment, reports anything missing, and configures the backend:

```text
./automator --config setup
```

> **Note:** The `./automator` launcher automatically detects and uses a local `.venv` if present. Otherwise, it uses your active `python3` environment.

It checks Python, Git, Node.js, AI CLI tools, Python packages, and vendor SDKs. Then it walks you through choosing CLI or API mode, selecting a provider, and optionally configuring per-role model overrides.

### 4. Verify

```text
./automator --config show                    # Check your configuration
./automator --cli claude --check-runtime     # Test backend reachability
./automator --help                           # See all commands and examples
```

## Usage

Every action is a flag. Use `--cli <llm>` or `--api` whenever a language model is needed. Run `./automator --help` for the full flag reference and examples.

### Start a New Project

```text
# CLI mode
./automator --cli claude --project new --task create a Python script that fetches GitHub issues to CSV
./automator --cli gemini --project new --task write a Power Automate approval flow guide

# API mode — provider comes from config, no backend flag needed
./automator --api --project new --task create a Python script that fetches GitHub issues to CSV
```

### Continue or Fork a Project

```text
# --id is the exact project folder name (shown in projects/ and the registry)
./automator --cli claude --project continue --id github-issues-export --task add retry logic
./automator --cli claude --project fork     --id github-issues-export --task store results in SharePoint
```

The `--id` must match an existing project. If it doesn't, the engine fails fast with a clear message and a hint to run `--project list`.

### Accept or Reject Results

When the orchestration completes, the engine prints the result and waits. The project stays open with a **pending** status until you respond explicitly — it will never auto-attach to a new request.

```text
# Accept the result
./automator --cli claude --project continue --id my-project --task yes

# Reject and give feedback for rework
./automator --cli claude --project continue --id my-project --task no, auth should use client credentials

# Close manually and extract knowledge (backend required for extraction)
./automator --cli claude --project close --id my-project
# Close without extraction (no backend needed)
./automator --project close --id my-project
```

### Provide Input Files

Drop files into the `inputs/` directory before running a project. The engine automatically moves them into the project, scans for secrets, and makes them available to agents.

## How It Works

```text
User Request
    |
  Engine (orchestrator)
    |
  spawns worker  ──────────────────────────────────────────────┐
    |                                                           │
  [optional] worker signals needs_research                      │
    |                                                           │
  spawns research, injects findings, re-runs worker            │
    |                                                           │
  spawns review ◄──────────────────────────────────────────────┘
    |
  [pass] complete  /  [fail] one rework cycle → complete
    |
  Delivery (projects/<project-id>/delivery/)
```

The engine orchestrates the pipeline directly. Agents do the work. The engine handles infrastructure: prompt building, artifact persistence, rework loops, and safety guards.

### Agent Roles

| Role | Purpose |
|------|---------|
| `worker` | DevOps/DevSecOps engineer. Implements the task, produces delivery artifacts, writes and runs tests, makes API calls, creates documents, and deploys when explicitly asked. |
| `review` | Verifies the worker's output. One rework cycle is allowed if review finds issues. |
| `research` | Answers specific external questions (API behaviour, third-party docs) that the worker cannot verify locally. Dispatched automatically when the worker signals `needs_research: true`. |

### Typical Flows

**Standard:** `worker → review → complete`

**With research:** `worker(needs_research) → research → worker(with findings) → review → complete`

**With rework:** `worker → review (fail) → worker rework → review → complete`

For low-code and platform requests, the engine also distinguishes:

- `build_only` — produce deployable artifacts without mutating the tenant
- `build_and_deploy` — produce artifacts and perform bounded deployment when credentials and permissions pass preflight

## Safety Guards

Every action an agent requests passes through a destructive action guard before the engine executes it. Agents cannot bypass it — it runs at the engine level, not inside the agent prompt.

### Protection layers

| Layer | What it stops | Your control |
|---|---|---|
| **Role allowlist** | An agent requesting a capability outside its permitted set (e.g. `review` cannot write files, `research` cannot run shell commands) | None needed — hard-blocked automatically |
| **Absolute HTTP blocks** | DELETE, PATCH, or PUT against SharePoint sites or Entra ID users and service principals | Must type the exact resource ID to allow; no session-wide approval possible |
| **Soft HTTP blocks** | DELETE against named resource classes: Azure resource groups, Logic Apps, Teams, Power BI workspaces and datasets, SharePoint lists | `[y]` allow once / `[A]` allow all similar for this session / `[N]` block |
| **Catch-all HTTP** | DELETE, PATCH, or PUT to **any** domain — Qualys, Bitsight, internal APIs, anything | Same `[y/A/N]` prompt |
| **Shell blocklist** | `rm -rf`, `find -delete`, `shred`, Windows `rd /s`, PnP bulk-removal cmdlets | `[y/A/N]` prompt |
| **Shell HTTP bypass** | `curl`, `wget`, `az rest`, `Invoke-RestMethod` used with DELETE, PATCH, or PUT — attempts to sidestep the HTTP guard | `[y/A/N]` prompt |
| **Write protection** | Writes to `engine/`, `agents/`, `docs/`, `config/`, `knowledge/`, or `skills/`; overwrites of files the engine did not create | Hard-blocked automatically |
| **Script content scan** | Scripts written by agents that contain shell destruction or HTTP mutation patterns — scanned at write time and again at run time | `[y/A/N]` prompt at each point |

### Non-interactive mode

When stdin is not a TTY (CI/CD pipelines, piped input), all prompts default to blocked. Absolute blocks always require explicit resource ID confirmation and cannot be approved non-interactively.

### What agents see when blocked

A blocked capability returns a `[destructive-guard] BLOCKED` result to the agent. Agents are instructed to report persistent blocks as blockers in their output rather than retrying. If an agent loops on blocked capabilities until the retry limit is hit, the failure carries `error_category: destructive_guard_block` — making it clear the failure is an intentional policy, not an engine defect.

### POST is unguarded by design

POST (resource creation) is not blocked. If you want to prevent agents from creating new resources, use `delivery_mode: build_only` on platform-build tasks — this prevents all mutating HTTP methods regardless of method.

## Backend Configuration

| Mode | How it works | Flag |
|------|-------------|------|
| **CLI** (default) | Spawns AI CLI tools as subprocesses | `--cli claude` / `--cli gemini` / `--cli codex` |
| **API** | Calls vendor HTTP APIs directly | `--api` (provider from `config/backends.json`) |

```text
./automator --config setup      # Interactive wizard — recommended
./automator --config show       # Display current config (keys redacted)
./automator --config validate   # Check API keys are reachable
```

The wizard handles everything interactively. If you prefer editing the JSON files yourself, configuration lives in `config/` (git-ignored):

- `config/backends.json` — mode, provider, default model, optional `base_url`, per-role overrides
- `config/secrets.json` — API keys

### Examples

**Claude via API** (pay-per-token, uses your Anthropic key — separate from a Claude Max subscription, which only covers the Claude Code CLI):

```json
// config/backends.json
{ "version": 2, "mode": "api", "provider": "anthropic",
  "default_model": "claude-sonnet-4-6" }

// config/secrets.json
{ "version": 1, "anthropic_api_key": "sk-ant-..." }
```

**OpenAI / GPT**:

```json
// config/backends.json
{ "version": 2, "mode": "api", "provider": "openai",
  "default_model": "gpt-4.1" }

// config/secrets.json
{ "version": 1, "openai_api_key": "sk-..." }
```

**DeepSeek via OpenRouter** (one key unlocks DeepSeek, Qwen, Grok, Mistral, Llama, and still Claude/GPT/Gemini — roughly 10–20× cheaper than Claude Sonnet):

```json
// config/backends.json
{ "version": 2, "mode": "api", "provider": "openai",
  "default_model": "deepseek/deepseek-chat",
  "base_url": "https://openrouter.ai/api/v1" }

// config/secrets.json
{ "version": 1, "openai_api_key": "sk-or-v1-..." }
```

**DeepSeek direct** (cheapest; get a key from platform.deepseek.com):

```json
// config/backends.json
{ "version": 2, "mode": "api", "provider": "openai",
  "default_model": "deepseek-chat",
  "base_url": "https://api.deepseek.com/v1" }

// config/secrets.json
{ "version": 1, "openai_api_key": "sk-..." }
```

**Fully local via Ollama** (free, no network — expect a capability drop on hard tasks; start Ollama first with `ollama serve`):

```json
// config/backends.json
{ "version": 2, "mode": "api", "provider": "openai",
  "default_model": "qwen3-coder",
  "base_url": "http://localhost:11434/v1" }

// config/secrets.json
{ "version": 1, "openai_api_key": "ollama" }
```

**Per-role overrides** (e.g., cheap model for review, strong model for coding):

```json
{ "version": 2, "mode": "api", "provider": "openai",
  "default_model": "gpt-4.1",
  "role_overrides": {
    "worker":   { "model": "claude-opus-4-6", "provider": "anthropic" },
    "research": { "model": "deepseek/deepseek-chat",
                  "base_url": "https://openrouter.ai/api/v1" }
  }
}
```

After editing, run:

```text
./automator --api --check-runtime    # Verify reachability
./automator --api --project new --task <your task>
```

If no config exists, the engine defaults to CLI mode. See [docs/backend-and-runtime.md](docs/backend-and-runtime.md) for full schema, resolution order, and the endpoints table.

## Command Reference

Run `./automator --help` for the full reference with all flag combinations and examples.

Quick overview:

```text
./automator --api | --cli <llm>                    backend selection
./automator --project new|continue|fork            project work (needs backend)
./automator --project close --id <id>              close project; add --cli/--api to extract knowledge
./automator --project delete --id <id>             delete project (repeat --id for bulk, or --all)
./automator --project list                         list projects (local)
./automator --check-runtime                        probe backend (needs backend)
./automator --debug open|list|analyse|verify       debug management (local)
./automator --config setup|show|validate           configuration (local)
./automator --skill  list|check|catalog|fetch|rebuild-manifest   skills (local)
./automator --agent  list|add                      agents (local)
```

## Debug Mode

Capture orchestration faults without letting the engine self-heal:

```text
./automator --cli claude --project new --debug --task add OAuth support
./automator --debug open          # List captured issues
./automator --debug analyse       # Summarize root causes
```

## Project Structure

```text
engine/                   All Python code
  ORCHESTRATION.md        Full architecture guide and AI agent instructions
  automator.py            Canonical Python entrypoint
  work/                   Runtime implementation modules
  tests/                  Test suite

agents/                   Agent role specifications (Markdown)
docs/                     Shared contracts, schemas, and policies
config/                   Backend configuration (structure tracked, content git-ignored)
knowledge/                Shared knowledge base (tracked reusable entries and indexes)
skills/                   Cached vendor skills and catalog
projects/                 Per-project data (structure tracked, content git-ignored)
inputs/                   Drop zone for user-provided files (structure tracked, content git-ignored)
personal/                 User-specific config (README tracked, content git-ignored)
REQUIREMENTS.md           Human-readable prerequisites and guide
requirements.txt          Python dependencies for pip
```

## Development

```text
# Run all tests
python3 -m unittest discover -s engine/tests -v

# Run the lean orchestration suite (end-to-end pipeline, zero LLM usage)
python3 -m unittest engine.tests.test_lean_orchestration -v

# Run the execution and destructive guard suites
python3 -m unittest engine.tests.test_progress_execution engine.tests.test_destructive_guard -v

# Run a single test class
python3 -m unittest engine.tests.test_automator.ProjectResolutionTest -v
```

The orchestration and guard suites are part of the engine contract, not optional extras. If pipeline behavior, capability allowlists, rework handling, or destructive guard semantics change, these tests must be updated in the same change.

### Troubleshooting

The engine shows one-line error messages for common problems (corrupt config, missing files, permission errors). To see full Python tracebacks for debugging, set the environment variable:

```text
PYTHONTRACEBACK=1 ./automator --cli claude --project new --task ...
```

See [engine/ORCHESTRATION.md](engine/ORCHESTRATION.md) for the full architecture, extension points, engine internals, and development workflows.
