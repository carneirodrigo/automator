# Data Handling: Secrets & Input Inbox

Read this when modifying secrets detection/storage, capability write guards, or the inputs/ drop zone. For big-data boundaries see engine/ORCHESTRATION.md.

## Secrets Management

The system provides automatic secret detection, storage, and leak prevention:

- **Detection:** The engine scans user prompts for secrets (API keys, passwords, tokens, tenant IDs, AWS keys, etc.) using pattern-based detection. Detected secrets are automatically stored in the project's secrets vault and redacted from the prompt before it reaches any LLM.
- **Storage:** Secrets are stored in `projects/<project-id>/secrets/secrets.json`, which is git-ignored. Each entry includes key, value, type, label, source, and timestamp.
- **Retrieval:** Agents use the `load_secrets` capability to retrieve project secrets. The `save_secret` capability allows storing new secrets discovered during work.
- **Leak Prevention:** The engine guards `write_file` and `persist_artifact` capabilities — if content contains a known secret value from the project vault, the write is blocked with an error. Secrets must be injected via environment variables or config references, never hardcoded.
- **Pre-project Secrets:** If secrets are detected before a project is bootstrapped, they are held in memory and flushed into the project vault once bootstrap completes.

## Input Inbox

The `inputs/` directory at the repo root is a drop zone for files the user wants to provide to a project (examples, configs, credentials, data files). The engine processes this directory automatically on every run:

- **On start:** The engine checks `inputs/` for files. If an active project exists, files are moved immediately. If no project exists yet (new project), files are buffered and moved after bootstrap or fork completes.
- **Move, not copy:** Files are moved from `inputs/` to `projects/<project-id>/runtime/inputs/`. The inbox is always empty after a run.
- **Secret scanning:** Text file contents are scanned for secrets before moving. Detected secrets are stored in the project vault.
- **Prompt injection:** Text input files are automatically included in agent prompts via `StageContext.inputs`. Binary files are moved but excluded from prompts.
- **Manifest:** An `inputs_manifest.json` is written to the project inputs directory recording what was ingested, file types, sizes, and secret counts.
- **Engine visibility:** `_build_project_inventory()` lists input files so the engine can include them in worker context (e.g., avoiding redundant discovery when sufficient context was provided).
