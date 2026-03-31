"""Token estimation helpers."""

from __future__ import annotations

import glob
import sys
from pathlib import Path
from typing import Any

# Sentinels for tiktoken load state — distinct objects so None is never reused.
_TIKTOKEN_NOT_LOADED: object = object()
_TIKTOKEN_FAILED: object = object()
_tiktoken_encoding: Any = _TIKTOKEN_NOT_LOADED


def _try_load_tiktoken() -> Any:
    """Try to import tiktoken, falling back to the repo-local .venv if not on sys.path."""
    try:
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        pass
    # Not found in current environment — try the repo-local .venv site-packages.
    repo_root = Path(__file__).resolve().parents[2]
    patterns = str(repo_root / ".venv" / "lib" / "python*" / "site-packages")
    for site_pkg in glob.glob(patterns):
        if site_pkg not in sys.path:
            sys.path.insert(0, site_pkg)
        try:
            import tiktoken
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            if site_pkg in sys.path:
                sys.path.remove(site_pkg)
    return None


def estimate_tokens(text: str) -> int:
    """Estimate token count for a prompt string."""
    global _tiktoken_encoding
    if not text:
        return 0
    if _tiktoken_encoding is _TIKTOKEN_NOT_LOADED:
        encoding = _try_load_tiktoken()
        if encoding is not None:
            _tiktoken_encoding = encoding
        else:
            _tiktoken_encoding = _TIKTOKEN_FAILED
            print(
                "Warning: tiktoken not installed — token estimates will be imprecise "
                "(install with: pip install tiktoken>=0.5.0). "
                "Falling back to ~3 chars/token, which underestimates code-heavy content.",
                file=sys.stderr,
            )
    if _tiktoken_encoding is not _TIKTOKEN_FAILED and _tiktoken_encoding is not _TIKTOKEN_NOT_LOADED:
        return len(_tiktoken_encoding.encode(text))
    # Fallback: ~3 chars/token is safer than 4 for mixed code/JSON/text content.
    return len(text) // 3
