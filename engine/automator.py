#!/usr/bin/env python3
"""Unified Automator CLI wrapper."""

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.work import cli as _impl

# Map exception types to user-friendly messages.
_FRIENDLY_ERRORS: dict[type, str] = {
    json.JSONDecodeError: "A project data file is corrupted (invalid JSON). {msg}",
    PermissionError:      "Permission denied: {filename}",
    FileNotFoundError:    "Required file not found: {filename}",
    IsADirectoryError:    "Expected a file but found a directory: {filename}",
}


def _friendly_message(exc: Exception) -> str | None:
    """Return a one-line user-facing message for known error types, or None."""
    for exc_type, template in _FRIENDLY_ERRORS.items():
        if isinstance(exc, exc_type):
            ctx: dict[str, str] = {}
            if hasattr(exc, "filename") and exc.filename:
                ctx["filename"] = str(exc.filename)
            if isinstance(exc, json.JSONDecodeError):
                ctx["msg"] = exc.msg
            return template.format_map({k: ctx.get(k, "unknown") for k in ("msg", "filename")})
    return None


if __name__ == "__main__":
    try:
        raise SystemExit(_impl.main())
    except KeyboardInterrupt:
        print("\n[engine] Interrupted. Project state has been preserved.", file=sys.stderr)
        raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as exc:
        friendly = _friendly_message(exc)
        if friendly:
            print(f"[engine] Error: {friendly}", file=sys.stderr)
            print("[engine] If this persists, check project files under projects/<id>/runtime/.", file=sys.stderr)
        else:
            print(f"[engine] Unexpected error: {exc}", file=sys.stderr)
            print("[engine] Run with PYTHONTRACEBACK=1 for the full traceback.", file=sys.stderr)
        if sys.flags.dev_mode or "PYTHONTRACEBACK" in __import__("os").environ:
            raise
        raise SystemExit(1)

# Replace this module in sys.modules with the cli implementation module so that
# "engine.automator.X" resolves to "engine.work.cli.X".  This is load-bearing:
# the test suite patches engine.automator.engine_runtime (and similar) via this
# alias — removing it would break all test mocks that use that dotted path.
# Trade-off: IDE navigation and debugger module identity do not work for this
# thin wrapper; the real implementation lives in engine/work/cli.py.
sys.modules[__name__] = _impl
