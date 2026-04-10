"""JSON file I/O helpers."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# Emit extra parse-failure context when set in the environment.
_DEBUG_JSON: bool = os.environ.get("AUTOMATOR_DEBUG_JSON") == "1"


def load_json(path: Path) -> Any:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to a temp file in the same directory, then rename.
    # os.replace is atomic on POSIX, preventing corruption on crash.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(tmp_path), str(path))


def load_json_safe(path: Path) -> Any:
    """Like load_json but also swallows JSONDecodeError, returning {} for corrupt files."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}


def extract_json_payload(text: str, _depth: int = 0) -> dict[str, Any]:
    """Extract and parse the first complete JSON object from the text.

    *_depth* is an internal recursion guard — callers must not pass it.
    Nested extraction (response/result unwrapping) is capped at 2 levels
    so a deeply nested or circular payload cannot cause unbounded recursion.
    """
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    start = -1
    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidate = text[start:i + 1]
                try:
                    payload = json.loads(candidate)
                except json.JSONDecodeError:
                    # LLMs often produce trailing commas — strip them and retry
                    cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
                    try:
                        payload = json.loads(cleaned)
                    except json.JSONDecodeError:
                        start = -1
                        continue
                if not isinstance(payload, dict):
                    start = -1
                    continue
                if _depth < 2:
                    # Only unwrap response/result if they are the sole key —
                    # otherwise other sibling keys (e.g. capability_requests)
                    # would be silently discarded.
                    if len(payload) == 1:
                        if "response" in payload and isinstance(payload["response"], str):
                            inner = extract_json_payload(payload["response"], _depth + 1)
                            if inner:
                                return inner
                        if "result" in payload and isinstance(payload["result"], str):
                            inner = extract_json_payload(payload["result"], _depth + 1)
                            if inner:
                                return inner
                return payload

    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else {}
    except json.JSONDecodeError:
        # Last resort: strip trailing commas and retry
        cleaned = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            result = json.loads(cleaned)
            return result if isinstance(result, dict) else {}
        except json.JSONDecodeError:
            pass
        if _DEBUG_JSON:
            print(
                f"[json_io] extract_json_payload: full-text parse failed on {len(text)} chars",
                file=sys.stderr,
            )
        return {}
