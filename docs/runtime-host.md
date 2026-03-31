# Runtime Host

## Purpose

`engine/work/engine_runtime.py` is the internal orchestration runtime.

`engine/automator.py` is the canonical Python entrypoint for humans. `./automator` is the equivalent repo-root launcher.

The runtime host is not an agent. It is the local execution layer that:

- loads project context from registry
- spawns worker, review, and research agents in sequence
- persists task state and artifacts
- enforces filesystem and command boundaries
- executes bounded local capabilities

## Command

```bash
./automator --cli claude --project new --task integrate with the security API
```

## Current Responsibilities

- load `projects/registry.json` (includes last active project tracking)
- resolve the active project
- load runtime config, memory, and task state
- spawn agents (worker, research, review) in pipeline order
- enforce runtime guardrails and bounded local execution
- print a structured JSON response
- print compact live progress lines for each spawned agent
- include a response `timeline` with stage handoffs, completions, and per-agent token usage

## Non-Responsibilities

The runtime host is not the orchestration brain. It must not:

- decide the next stage on its own beyond the fixed pipeline (research → worker → review)
- reinterpret review findings semantically
- act as a hidden fallback agent
- replace a required spawned agent with local reasoning

## Closed Loop

Current best-practice usage:

```bash
./automator --cli claude --project continue --id my-project --task help me decide how to add X
```

## Related Documents

- [project-runtime-layout.md](project-runtime-layout.md)
