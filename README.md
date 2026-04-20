# Automator

**Plain-English task → working output.** Automator runs your request through a pipeline of AI agents that plan, write, test, and review. Results land in `projects/<id>/delivery/` — ready to inspect, run, or hand off.

Works with Claude, GPT, Gemini (CLI or API), plus DeepSeek, Qwen, Grok, Mistral, Llama, and local models (Ollama, LM Studio) via any OpenAI-compatible endpoint.

---

## TL;DR

**1. Install**

```text
git clone <repo-url> && cd automator
pip install -r requirements.txt
```

**2. Pick an AI backend** (just one):

| Option | How to set up | Why pick this |
|---|---|---|
| **Claude Code CLI** | `npm install -g @anthropic-ai/claude-code` then `claude` to log in with a Pro/Max subscription | Easiest if you already subscribe to Claude |
| **Anthropic API key** | `./automator --config setup` → `api` → `anthropic` → paste key | Pay-per-token, no CLI install |
| **DeepSeek via OpenRouter** | `./automator --config setup` → `api` → `openai` → endpoint `https://openrouter.ai/api/v1` → model `deepseek/deepseek-chat` | 10–20× cheaper than Claude; one key unlocks 100+ models |
| **Local Ollama** | Install [Ollama](https://ollama.com), run `ollama serve`, then `./automator --config setup` → `api` → `openai` → endpoint `http://localhost:11434/v1` | Free, fully local, no network |

**3. Run a task**

```text
# CLI mode
./automator --cli claude --project new --task create a Python script that fetches GitHub issues to CSV

# API mode — no backend flag needed; provider comes from config
./automator --api --project new --task write a Power Automate approval flow guide
```

**4. Accept or refine the result**

When the pipeline finishes it waits for your call:

```text
./automator --cli claude --project continue --id <id> --task yes
./automator --cli claude --project continue --id <id> --task no, use client credentials instead of delegated
```

Run `./automator --help` for the full flag reference.

---

## What Automator Delivers

- **Code** — working scripts, applications, and integrations, with automated tests and security review.
- **Guides** — implementation documents, operator runbooks, and platform configuration guides.
- **Platform builds** — deployable low-code and platform artifacts (Logic Apps, SharePoint schemas, Power BI bundles).

---

## How It Works

```text
User Request
    │
  Engine (orchestrator)
    │
  spawns worker  ──────────────────────────────────────────────┐
    │                                                           │
  [optional] worker signals needs_research                      │
    │                                                           │
  spawns research, injects findings, re-runs worker             │
    │                                                           │
  spawns review ◄───────────────────────────────────────────────┘
    │
  [pass] complete  /  [fail] one rework cycle → complete
    │
  Delivery → projects/<project-id>/delivery/
```

The local engine owns the pipeline, prompts, artifacts, rework loops, and safety guards. Each "agent" is a role — a Markdown spec + a subprocess call to an AI CLI or API — with no persistent process or memory between invocations. The engine holds all state.

### Agent Roles

| Role | Purpose |
|------|---------|
| `worker` | DevOps/DevSecOps engineer. Implements the task, produces artifacts, writes and runs tests, makes API calls, and deploys when explicitly asked. |
| `review` | Verifies the worker's output. One rework cycle is allowed if review finds issues. |
| `research` | Answers specific external questions the worker cannot verify locally. Dispatched automatically when the worker signals `needs_research: true`. |

### Typical Flows

- **Standard:** `worker → review → complete`
- **With research:** `worker(needs_research) → research → worker(with findings) → review → complete`
- **With rework:** `worker → review (fail) → worker rework → review → complete`

For low-code/platform requests:

- `build_only` — produce deployable artifacts without mutating the tenant.
- `build_and_deploy` — produce artifacts and perform bounded deployment when credentials and permissions pass preflight.

---

## Setup (Detailed)

### Clone and install

```text
git clone <repo-url>
cd automator
pip install -r requirements.txt
```

*Optional: `python3 -m venv .venv && . .venv/bin/activate` first. The `./automator` launcher auto-detects a local `.venv` if present; otherwise it uses your active `python3`.*

### Install a backend

Automator runs against at least one AI backend. You can mix and match — install a CLI and also configure an API key later.

**CLI mode** (default — spawns an AI CLI as a subprocess):

```text
npm install -g @anthropic-ai/claude-code     # Claude
npm install -g @google/gemini-cli            # Gemini
npm install -g @openai/codex                 # Codex
```

**API mode** (calls vendor HTTP APIs directly — no CLI needed):

```text
pip install anthropic        # for Anthropic
pip install google-genai     # for Google
pip install openai           # for OpenAI (also used for OpenRouter, DeepSeek, Ollama, LM Studio)
```

### Run the setup wizard

```text
./automator --config setup
```

The wizard checks Python, Git, Node.js, CLI tools, Python packages, and vendor SDKs, reports what's missing, then walks you through mode, provider, API key, optional custom endpoint, default model, and per-role overrides. Every configuration shown in the [Backend Configuration](#backend-configuration) examples can be produced via the wizard alone — no JSON editing required.

### Verify

```text
./automator --config show                    # Show config (keys redacted)
./automator --cli claude --check-runtime     # Or --api --check-runtime
./automator --help                           # Full flag reference
```

---

## Backend Configuration

| Mode | How it works | How to select |
|------|-------------|------|
| **CLI** (default) | Spawns AI CLI tools as subprocesses | `--cli claude` / `--cli gemini` / `--cli codex` |
| **API** | Calls vendor HTTP APIs directly | `--api` (provider from `config/backends.json`) |

Config lives in `config/` (git-ignored):

- `config/backends.json` — mode, provider, default model, optional `base_url`, per-role overrides.
- `config/secrets.json` — API keys.

```text
./automator --config setup      # Interactive wizard (recommended)
./automator --config show       # Display current config
./automator --config validate   # Check API keys are reachable
```

### Examples

Each example shows both `backends.json` and `secrets.json`. Copy-paste or produce the same via the wizard.

**Claude via API** — pay-per-token Anthropic key (separate from a Claude Max subscription, which only covers the Claude Code CLI):

```json
// config/backends.json
{ "version": 2, "mode": "api", "provider": "anthropic",
  "default_model": "claude-sonnet-4-6" }

// config/secrets.json
{ "version": 1, "anthropic_api_key": "sk-ant-..." }
```

**OpenAI / GPT:**

```json
// config/backends.json
{ "version": 2, "mode": "api", "provider": "openai",
  "default_model": "gpt-4.1" }

// config/secrets.json
{ "version": 1, "openai_api_key": "sk-..." }
```

**DeepSeek via OpenRouter** — one key unlocks DeepSeek, Qwen, Grok, Mistral, Llama, and still Claude/GPT/Gemini:

```json
// config/backends.json
{ "version": 2, "mode": "api", "provider": "openai",
  "default_model": "deepseek/deepseek-chat",
  "base_url": "https://openrouter.ai/api/v1" }

// config/secrets.json
{ "version": 1, "openai_api_key": "sk-or-v1-..." }
```

**DeepSeek direct** (cheapest, via platform.deepseek.com):

```json
// config/backends.json
{ "version": 2, "mode": "api", "provider": "openai",
  "default_model": "deepseek-chat",
  "base_url": "https://api.deepseek.com/v1" }

// config/secrets.json
{ "version": 1, "openai_api_key": "sk-..." }
```

**Fully local via Ollama** — free, no network. Expect a capability drop on hard tasks. Start the server first (`ollama serve`):

```json
// config/backends.json
{ "version": 2, "mode": "api", "provider": "openai",
  "default_model": "qwen3-coder",
  "base_url": "http://localhost:11434/v1" }

// config/secrets.json
{ "version": 1, "openai_api_key": "ollama" }
```

**Per-role overrides** — e.g., strong model for coding, cheap model for research:

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
./automator --api --check-runtime
./automator --api --project new --task <your task>
```

If no config exists, the engine defaults to CLI mode. See [docs/backend-and-runtime.md](docs/backend-and-runtime.md) for the full schema, resolution order, and endpoint reference.

---

## Using Automator

Every action is a flag. `--cli <llm>` or `--api` is required whenever a backend is needed.

### Start a new project

```text
./automator --cli claude --project new --task create a Python script that fetches GitHub issues to CSV
./automator --cli gemini --project new --task write a Power Automate approval flow guide
./automator --api        --project new --task create a Python script that fetches GitHub issues to CSV
```

### Continue or fork a project

`--id` is the exact project folder name (shown in `projects/` and the registry). The engine fails fast with a hint to `--project list` if the id doesn't exist.

```text
./automator --cli claude --project continue --id github-issues --task add retry logic
./automator --cli claude --project fork     --id github-issues --task store results in SharePoint
```

### Accept or reject the result

When the orchestration completes, the project stays in **pending** status until you respond — it will never auto-attach to a new request.

```text
./automator --cli claude --project continue --id my-project --task yes                       # accept
./automator --cli claude --project continue --id my-project --task no, auth should use client credentials   # reject + feedback
./automator --cli claude --project close --id my-project                                     # close + extract knowledge
./automator --project close --id my-project                                                  # close only (no extraction)
```

### Provide input files

Drop files into `inputs/` before running. The engine moves them into the project, scans for secrets, and makes them available to agents.

---

## Safety Guards

Every action an agent requests passes through a destructive action guard enforced at the engine level — agents cannot bypass it via prompt content.

### Protection layers

| Layer | What it stops | Your control |
|---|---|---|
| **Role allowlist** | An agent requesting a capability outside its permitted set (`review` cannot write files; `research` cannot run shell) | Hard-blocked automatically |
| **Absolute HTTP blocks** | DELETE/PATCH/PUT against SharePoint sites or Entra ID users/service principals | Must type exact resource ID; no session-wide approval |
| **Soft HTTP blocks** | DELETE against named classes: Azure resource groups, Logic Apps, Teams, Power BI workspaces/datasets, SharePoint lists | `[y]` once / `[A]` all similar / `[N]` block |
| **Catch-all HTTP** | DELETE/PATCH/PUT to **any** domain | Same `[y/A/N]` prompt |
| **Shell blocklist** | `rm -rf`, `find -delete`, `shred`, `rd /s`, PnP bulk-removal cmdlets | `[y/A/N]` prompt |
| **Shell HTTP bypass** | `curl`, `wget`, `az rest`, `Invoke-RestMethod` with DELETE/PATCH/PUT | `[y/A/N]` prompt |
| **Write protection** | Writes to `engine/`, `agents/`, `docs/`, `config/`, `knowledge/`, `skills/`; overwrites of files the engine did not create | Hard-blocked automatically |
| **Script content scan** | Agent-written scripts containing destructive patterns — scanned at write time and again at run time | `[y/A/N]` at each point |

### Non-interactive mode

When stdin is not a TTY (CI pipelines, piped input), all prompts default to blocked. Absolute blocks always require explicit resource ID confirmation.

### What agents see when blocked

A blocked capability returns `[destructive-guard] BLOCKED` to the agent. Persistent blocks propagate as blockers in the agent's output. If an agent loops on blocked capabilities past the retry budget, the failure carries `error_category: destructive_guard_block` — signalling policy, not engine defect.

### POST is unguarded by design

POST (resource creation) isn't blocked. To prevent creation of new resources, use `delivery_mode: build_only` on platform-build tasks — this blocks all mutating HTTP regardless of method.

---

## Command Reference

Run `./automator --help` for the full reference. Quick overview:

```text
./automator --api | --cli <llm>                    backend selection
./automator --project new|continue|fork            project work (needs backend)
./automator --project close --id <id>              close project (add --cli/--api to extract knowledge)
./automator --project delete --id <id>             delete project (repeat --id, or --all)
./automator --project list                         list projects (local)
./automator --check-runtime                        probe backend (needs backend)
./automator --debug open|list|analyse|verify       debug management (local)
./automator --config setup|show|validate           configuration (local)
./automator --skill  list|check|catalog|fetch|rebuild-manifest   skills (local)
./automator --agent  list|add                      agents (local)
```

---

## Debug Mode

Capture orchestration faults without letting the engine self-heal:

```text
./automator --cli claude --project new --debug --task add OAuth support
./automator --debug open          # List captured issues
./automator --debug analyse       # Summarize root causes
```

---

## Project Structure

```text
engine/                   All Python code
  ORCHESTRATION.md        Architecture guide and AI agent instructions
  automator.py            Canonical Python entrypoint
  work/                   Runtime implementation modules
  tests/                  Test suite

agents/                   Agent role specifications (Markdown)
docs/                     Shared contracts, schemas, policies
config/                   Backend configuration (structure tracked, content git-ignored)
knowledge/                Shared knowledge base (reusable entries and indexes)
skills/                   Cached vendor skills and catalog
projects/                 Per-project data (structure tracked, content git-ignored)
inputs/                   Drop zone for user-provided files
personal/                 User-specific config (README tracked, content git-ignored)
REQUIREMENTS.md           Human-readable prerequisites
requirements.txt          Python dependencies
```

---

## Development

```text
# Run all tests
python3 -m unittest discover -s engine/tests -v

# Lean orchestration suite (end-to-end pipeline, zero LLM usage)
python3 -m unittest engine.tests.test_lean_orchestration -v

# Execution and destructive guard suites
python3 -m unittest engine.tests.test_progress_execution engine.tests.test_destructive_guard -v

# Single test class
python3 -m unittest engine.tests.test_automator.ProjectResolutionTest -v
```

The orchestration and guard suites are part of the engine contract, not optional extras. If pipeline behavior, capability allowlists, rework handling, or destructive guard semantics change, update these tests in the same change.

### Troubleshooting

The engine prints one-line messages for common errors (corrupt config, missing files, permission errors). To see full Python tracebacks:

```text
PYTHONTRACEBACK=1 ./automator --cli claude --project new --task ...
```

See [engine/ORCHESTRATION.md](engine/ORCHESTRATION.md) for the full architecture, extension points, and development workflows.
