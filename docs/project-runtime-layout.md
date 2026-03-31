# Project Runtime Layout

## Purpose

This repository defines reusable agent behavior and stores centralized runtime data for each managed project.

## Required Layout

Create this structure inside this repository for every managed project:

```text
projects/
  <project-id>/
    delivery/
    runtime/
      config.json
      memory/
      state/
        active_task.json
      artifacts/
    secrets/
```

## Directory Roles

- `projects/registry.csv`: human-readable project index (auto-synced on bootstrap)
- `projects/<project-id>/delivery/`: project deliverables and end results for that project
- `projects/<project-id>/runtime/config.json`: project-level settings such as project id, source root, default constraints, per-role backend selection, allowed tools, `deliverables_dir` (where final output files live), and `security_policy` (per-category blocking overrides)
- `projects/<project-id>/runtime/memory/`: durable project knowledge, decisions, preferences, and lessons
- `projects/<project-id>/runtime/state/`: active and historical task execution state
- `projects/<project-id>/runtime/artifacts/`: generated outputs for that project
- `projects/<project-id>/secrets/`: project credential vault

## Initialization

Initialize project runtime files from:

- [task_state.template.json](../engine/work/task_state.template.json)
- [project_config.template.json](../engine/work/project_config.template.json)

The preferred way to do this is through `bootstrap_project()` in `engine/work/project_state.py`, which creates the directory structure and initializes files from these templates on first run.

## Rule

Project-specific memory, state, artifacts, secrets, and managed source trees must stay inside this repository. The preferred source root is `projects/<project-id>/delivery/`, while runtime data stays under `projects/<project-id>/runtime/`.

Temporary smoke-test runtime data should be cleaned after verification so the control-plane repository does not accumulate stale project state.
