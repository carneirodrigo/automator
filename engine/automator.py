#!/usr/bin/env python3
"""Unified Automator CLI wrapper."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.work import cli as _impl

if __name__ == "__main__":
    try:
        raise SystemExit(_impl.main())
    except KeyboardInterrupt:
        print("\n[engine] Interrupted. Project state has been preserved.", file=sys.stderr)
        raise SystemExit(1)

# Replace this module in sys.modules with the cli implementation module so that
# "engine.automator.X" resolves to "engine.work.cli.X".  This is load-bearing:
# the test suite patches engine.automator.engine_runtime (and similar) via this
# alias — removing it would break all test mocks that use that dotted path.
# Trade-off: IDE navigation and debugger module identity do not work for this
# thin wrapper; the real implementation lives in engine/work/cli.py.
sys.modules[__name__] = _impl
