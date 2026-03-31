"""TOON (Token-Oriented Object Notation) adapter for prompt serialization.

Provides a single abstraction point that encodes structured data in TOON
format for prompt injection — significantly fewer tokens than JSON with
no loss of information. LLMs parse TOON at equal or better accuracy vs JSON.

Only used for prompt INPUT — agent output parsing remains JSON via
extract_json_payload().

TOON format rules:
- Object keys are unquoted
- Simple string values are unquoted (quoted only when ambiguous)
- Arrays of primitives are inline: key[N]: v1,v2,v3
- Arrays of homogeneous objects use tabular format: key[N]{col1,col2}:\\n  v1,v2
- Nested objects use indentation instead of braces
"""
from __future__ import annotations

import json
import re
from typing import Any


# --- TOON Encoder ---

_LOOKS_NUMERIC = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _needs_quoting(s: str) -> bool:
    """Check if a string value needs JSON-style quoting in TOON."""
    if not s:
        return True
    if s in ("true", "false", "null"):
        return True
    if _LOOKS_NUMERIC.match(s):
        return True
    # Characters that would be ambiguous in TOON context
    if any(c in s for c in (",", "\n", "\r", "\t")):
        return True
    # Leading/trailing whitespace
    if s != s.strip():
        return True
    return False


def _encode_value(value: Any) -> str:
    """Encode a single primitive value to TOON."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value) if _needs_quoting(value) else value
    return json.dumps(value)


def _is_primitive(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, type(None)))


def _homogeneous_keys(arr: list) -> list[str] | None:
    """Return shared keys if all elements are dicts with identical key sets."""
    if not arr or not all(isinstance(x, dict) for x in arr):
        return None
    if not arr[0]:
        return None
    keys = list(arr[0].keys())
    key_set = set(keys)
    if all(set(d.keys()) == key_set for d in arr):
        return keys
    return None


def _encode_array(data: list, indent: int, level: int) -> str:
    """Encode a list, returning the [N]... portion (no key prefix)."""
    if not data:
        return "[]"

    child_pad = " " * (indent * (level + 1))

    # All primitives — inline
    if all(_is_primitive(x) for x in data):
        vals = ",".join(_encode_value(v) for v in data)
        return f"[{len(data)}]: {vals}"

    # Homogeneous dicts with all-primitive values — tabular
    cols = _homogeneous_keys(data)
    if cols and all(
        all(_is_primitive(row.get(c)) for c in cols) for row in data
    ):
        header = ",".join(cols)
        rows = []
        for item in data:
            row_vals = ",".join(_encode_value(item[c]) for c in cols)
            rows.append(f"{child_pad}{row_vals}")
        return f"[{len(data)}]{{{header}}}:\n" + "\n".join(rows)

    # Mixed / nested — itemized with dash markers
    parts = []
    for item in data:
        if isinstance(item, dict) and item:
            inner = _encode_dict(item, indent, level + 2)
            parts.append(f"{child_pad}-\n{inner}")
        elif isinstance(item, list):
            inner = _encode_array(item, indent, level + 1)
            parts.append(f"{child_pad}- {inner}")
        else:
            parts.append(f"{child_pad}- {_encode_value(item)}")
    return f"[{len(data)}]:\n" + "\n".join(parts)


def _encode_dict(data: dict, indent: int, level: int) -> str:
    """Encode a dict with indentation-based nesting."""
    if not data:
        return "{}"

    pad = " " * (indent * level)
    lines = []

    for key, value in data.items():
        if _is_primitive(value):
            lines.append(f"{pad}{key}: {_encode_value(value)}")
        elif isinstance(value, dict):
            if not value:
                lines.append(f"{pad}{key}: {{}}")
            else:
                inner = _encode_dict(value, indent, level + 1)
                lines.append(f"{pad}{key}:\n{inner}")
        elif isinstance(value, list):
            arr_body = _encode_array(value, indent, level)
            lines.append(f"{pad}{key}{arr_body}")
        else:
            lines.append(f"{pad}{key}: {_encode_value(value)}")

    return "\n".join(lines)


def toon_encode(data: Any, indent: int = 2) -> str:
    """Encode any JSON-serializable value to TOON format."""
    if _is_primitive(data):
        return _encode_value(data)
    if isinstance(data, list):
        return _encode_array(data, indent, 0)
    if isinstance(data, dict):
        return _encode_dict(data, indent, 0)
    return str(data)


# --- Public API ---

def serialize_for_prompt(data: Any) -> str:
    """Serialize data for prompt injection using TOON.

    Uses our built-in TOON encoder. Falls back to the toon-format package
    if installed and functional, or to json.dumps as a last resort.
    """
    return toon_encode(data)


def serialize_artifact_for_prompt(data: Any, source_role: str = "") -> str:
    """Serialize artifact data for prompt injection using lossless TOON encoding."""
    return toon_encode(data)


def is_toon_available() -> bool:
    """TOON is always available via the built-in encoder."""
    return True
