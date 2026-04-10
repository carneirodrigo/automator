# Development Guide

Read this when operating the pipeline, supervising debug issues, developing the engine, or following coding and audit standards.

## Agent Supervisor Mode (Interactive Improvement)

The system can be run under a **parent AI agent** (Claude, Gemini, or equivalent) acting as a supervisor that both operates and develops the system in the same session.

**Workflow:**
1.  **User Directive:** The user commands the supervisor: `Run ./automator --cli claude --project new --task start project X`.
2.  **Execution:** The supervisor executes the command.
3.  **Monitoring:** The supervisor watches the orchestration timeline in real-time.
4.  **Self-Healing:** If the `automator` fails (e.g., JSON error, logic bug, unclear instructions), the supervisor **must**:
    *   Diagnose the failure by reading the artifact or error log.
    *   **Patch the Agent Specification (`agents/*.md`) or runtime implementation under `engine/work/`** to fix the root cause.
    *   Run tests to verify the fix: `python3 -m unittest discover -s engine/tests -v`.
    *   Re-run the `automator` command to verify end-to-end.
5.  **Completion:** The supervisor only reports success when the `automator` finishes cleanly.

## Debug Mode

The engine supports a fail-fast debug mode for reproducing orchestration faults without letting the normal recovery loop hide them.

Invocation:

- `./automator debug run --claude --project my-project "..."`
- `./automator debug run --gemini --name smoke-project "..."`
- `./automator debug run --codex "..."`

Intent:

- Run the same normal project entry path as a start, continue, or fork request.
- Capture orchestration faults exactly where they happen.
- Stop immediately instead of routing the fault back through the normal recovery loop.

Current debug-mode capture points:

- startup/runtime configuration failures
- agent execution failures
- structurally invalid agent output
- invalid engine decisions
- orchestration circuit-breaker exhaustion

Storage:

- `debug/tracker.json` stores the issue tracker with issue id, title, short summary, criticality, status, backend, role, project id, occurrence count, and `detail_path`
- `debug/issues/<issue-id>.json` stores the full local detail record for the issue

The `debug/` directory is local-only and intentionally ignored by git.

## Debug Supervisor Workflow

The repair workflow for debug issues is supervisor-driven, not engine-driven. The engine captures faults into `debug/`; the supervising LLM reads those records, fixes the underlying problem, verifies the fix, and updates status.

`debug run` is capture-only. It must never trigger repair work, issue closure, or status updates by itself. Any analysis, retest, or fix workflow must start through `./automator debug ...`.

Expected division of labor:

- cheaper backends reproduce faults with `debug run`
- the engine captures the issue in `debug/tracker.json` and `debug/issues/<issue-id>.json`
- the current supervising LLM reads the tracker, selects issues, implements fixes, runs verification, and updates the tracker/detail files

Accepted supervisor intents:

- `analyse current bugs in debug`
  - read `debug/tracker.json`
  - inspect linked detail files for `open` and `regressed` issues
  - summarize likely root causes and impact
  - do not change code or tracker status

- `plan fixes for debug issues`
  - read `debug/tracker.json`
  - group related open/regressed issues
  - propose fix order, likely write scope, and verification plan
  - do not change code or tracker status

- `fix debug issues`
  - read `debug/tracker.json`
  - pick `open` or `regressed` issues
  - implement fixes in repo code/specs
  - run verification
  - update tracker and detail files based on outcome

- `fix debug issue <issue-id>`
  - work only the named issue
  - patch, verify, and update status for that issue only

- `retest debug issues`
  - rerun the relevant reproduction/tests for targeted issues
  - if a previously fixed issue fails again, mark it `regressed`

Tracker status lifecycle:

- `open`
  - issue captured and not yet resolved

- `in_progress`
  - supervisor is actively working the issue but has not verified a fix yet

- `fixed`
  - a fix was applied and verification passed

- `regressed`
  - the same issue fingerprint failed again after being marked fixed

Supervisor update rules:

- never mark an issue `fixed` without running verification
- if a fix attempt is incomplete or verification fails, leave the issue `open` or set `in_progress`
- if the same issue fingerprint reappears after `fixed`, reuse the existing issue and mark it `regressed` instead of creating a new issue
- update the linked detail file with:
  - what changed
  - what verification ran
  - whether the fix passed or failed
  - which backend/session acted as supervisor

Supervisor CLI:

- `./automator debug open`
- `./automator debug analyse`
- `./automator debug list --status open --status regressed`
- `./automator debug verify <issue-id> --verify-command "python3 -m unittest ..." --summary "What changed and what passed"`

Current limitation: `analyse` does not take an issue id yet. Use `open` or `list` to identify an issue, then inspect its detail file or use `verify <issue-id> ...` after repair.

This workflow lets any current supervising LLM handle repair work without requiring a separate Python repair tool or requiring the user to name a fixer backend each time.

## Coding Style & Standards

- **ASCII Default:** Use ASCII. Keep Markdown concise.
- **Naming:** Uppercase snake case for repo docs (`AGENT_ECOSYSTEM_V1.md`), lowercase kebab case for agents (`agents/worker.md`).
- **Logic:** Keep modules narrowly scoped. Prefer descriptive Python names.
- **Testing:** New agents, capabilities, or routing rules require at least one execution-path and one failure-path test under `engine/tests/`. Always run tests after engine changes.
- **Test Suites:** Treat `engine/tests/test_progress_execution.py` and `engine/tests/test_destructive_guard.py` as executable control-plane contracts. If pipeline lifecycle, pending-resolution handling, session recovery, or capability-loop behavior change, update the affected tests in the same change.

## Audit Expectations

When auditing this repository, focus on:
1. Integrity of the pipeline stage ordering (worker → [optional: research → worker] → review) and the single rework cycle cap.
2. Compliance with the "no local agent runners" rule — the host must not substitute its own reasoning for a failed spawned agent.
3. Visibility of agent execution and token usage in the timeline, including compact stage-start updates, sparse slow-run heartbeats, observable capability actions, and concise completion or next-stage messages.

## Source Control & GitHub Integration

This repository is version-controlled using Git. Each user configures their own remote and credentials in `personal/git.md`. AI agents read that file to know how to push, authenticate, and handle history operations on behalf of the user.

- **Primary Branch:** `master`
- **Setup:** See `personal/README.md` for the template, then create `personal/git.md` with your git remote URL, SSH key path, and workflow preferences.

### Version Control Best Practices
- **Committing:** Stage targeted paths explicitly, then commit with a focused message. Avoid broad staging commands that can capture local-only or unrelated files.
- **Pushing:** Read `personal/git.md` for the user's remote and auth method before pushing.
- **Security:** All project content under `projects/<project-id>/` is git-ignored. Registry files (`registry.json`, `registry.csv`) are also git-ignored as they contain user-specific project data. The `personal/` directory is git-ignored to prevent leakage of user-specific configuration.

## How To Work In This Repository

### First-Time Setup

```bash
pip install -r requirements.txt    # Install dependencies
./automator config setup           # Check environment and configure backend
./automator project check-runtime  # Verify backend reachability
```

The setup wizard checks all prerequisites (Python, Git, Node.js, CLI tools, Python packages) and reports what is missing before configuring the backend. See README.md for the full getting-started walkthrough.

### As an Operator (running the pipeline)

In **CLI mode**, pass `--cli claude`, `--cli gemini`, or `--cli codex`. In **API mode**, the provider comes from configuration and no flag is needed.

**Start a new project**:
```
# CLI mode
./automator --cli claude --project new --task create a Python script that fetches GitHub issues to CSV

# API mode — no backend flag needed
./automator --api --project new --task create a Python script that fetches GitHub issues to CSV
```

**Continue an existing project** (mention the project name or ID):
```
./automator --cli claude --project continue --id my-project --task add retry logic to the fetch script
```

**Fork a project** (use the `fork` keyword + source project name/ID):
```
./automator --cli claude --project fork --id my-project --task store results in SharePoint
```

Legacy short backend flags are still accepted for backward compatibility, but `--cli claude|gemini|codex` is the preferred form.

The engine runs the pipeline: research (optional) → worker → review. Artifacts are written to `projects/<project-id>/delivery/`. You monitor the timeline on stderr and get the final decision on stdout. The normal timeline is intentionally compact: one short line when a stage starts, sparse `still running` heartbeats for longer waits, capability updates when the engine can observe them, and short completion or next-stage lines. If the pipeline stops for clarification, re-run with your answer — the engine resumes from where it left off (`pending_resolution` in task state).

**Accept or reject results** — when the orchestration completes, the engine asks for your acceptance. Use the normal project entry path again with your response:
```
./automator --cli claude --project continue --id 001 --task yes
./automator --cli claude --project continue --id 001 --task looks good
./automator --cli claude --project continue --id 001 --task no, the auth flow should use client credentials not delegated
```
Acceptance triggers knowledge extraction (if research was done) and closes the project. Rejection feeds your feedback back for rework.

### As a Developer (improving the system)
Read the code directly. The canonical Python entrypoint is under `engine/`, and the active implementation lives under `engine/work/`. Agent behavior is defined in `agents/*.md`. Contracts are in `docs/`. Tests are in `engine/tests/`. After any engine change, run `python3 -m unittest discover -s engine/tests -v`. The Extension Points section in `docs/engine-internals.md` has step-by-step recipes for common changes.

Recommended control-plane checks after orchestration or execution changes:

- `python3 -m unittest engine.tests.test_progress_execution -v`
- `python3 -m unittest engine.tests.test_destructive_guard -v`
- `python3 -m unittest discover -s engine/tests -v`
