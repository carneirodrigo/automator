# Project Registry

## Purpose

The project registry lets the engine map conversational project references to both the managed project source location and the centralized runtime location in this repository.

## Rule

The engine may infer which project the user means from the request, but it must resolve that project through the registry before reading or writing project-specific files.

## Registry Shape

```json
{
  "projects": [
    {
      "project_id": "project-x",
      "project_name": "Project X",
      "aliases": ["x", "project x"],
      "project_home": "/path/to/this-repo/projects/project-x",
      "project_root": "/path/to/this-repo/projects/project-x/delivery",
      "runtime_dir": "/path/to/this-repo/projects/project-x/runtime"
    }
  ]
}
```

Use `projects/registry.json` as the live registry file. The shape is defined by `bootstrap_project()` in `engine/work/project_state.py`.

## Resolution Policy

Use this order:

1. explicit project name in the current request
2. explicit alias in the current request
3. existing `active_project` if the conversation clearly continues the same work
4. a single unambiguous registry match from the conversation context

Matching should use normalized project names and aliases as whole phrases so near matches do not resolve to the wrong project.

If multiple projects match, the engine reports the ambiguity and requires the user to specify `--id`.

If no project matches, the engine starts a new project.

## Active Project Rule

Only one `active_project` may be bound to a session at a time.

When the user targets a different project via `--id`, the engine resolves and activates it before running.
