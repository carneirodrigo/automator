"""Shared orchestration state models and run-control constants."""

from __future__ import annotations

SPAWN_TIMEOUT_SECONDS = 660
# Max size for files read via the read_file capability.
MAX_FILE_READ_SIZE = 1024 * 1024  # 1MB
MAX_STAGE_OUTPUT_BYTES = 1024 * 512  # 512KB
# Max size for non-artifact files injected into agent prompts.
MAX_INPUT_FILE_SIZE = 50_000  # ~50KB
DATA_FILE_EXTENSIONS = {".csv", ".tsv", ".jsonl", ".ndjson", ".log", ".xml"}
MAX_CAPABILITY_ROUNDS = 5        # simple tasks (default)
MAX_CAPABILITY_ROUNDS_MEDIUM = 8  # medium complexity
MAX_CAPABILITY_ROUNDS_COMPLEX = 12  # planned / high complexity
MAX_CAPABILITY_WRITE_SIZE = 10 * 1024 * 1024  # 10MB
# Max bytes of run_command stdout/stderr injected inline into the next prompt.
CMD_OUTPUT_INLINE_LIMIT = 8_000
RUNTIME_CHECK_PROMPT = 'Return exactly {"ok":true}.'
DEBUG_TRACKER_VERSION = 1
