"""Detect and redact secrets in free-form text (user prompts, agent output).

Detection uses two strategies:
1. Standalone high-confidence patterns — identifiable by prefix alone (AWS AKIA,
   GitHub PAT ghp_, Anthropic sk-ant-, OpenAI sk-).
2. Context-anchored patterns — a keyword (e.g. "password", "tenant_id") must
   appear near the value to avoid false positives.
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Standalone patterns: high-entropy prefixes that are secrets by themselves.
_STANDALONE_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("aws_access_key", "aws", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("github_pat", "github", re.compile(r"\b(ghp_[A-Za-z0-9]{36})\b")),
    ("github_pat_fine", "github", re.compile(r"\b(github_pat_[A-Za-z0-9_]{36,})\b")),
    ("anthropic_key", "anthropic", re.compile(r"\b(sk-ant-[A-Za-z0-9\-]{80,})\b")),
    ("openai_key", "openai", re.compile(r"\b(sk-[A-Za-z0-9]{48,})\b")),
    ("generic_sk_key", "api_key", re.compile(r"\b(sk-[A-Za-z0-9]{20,47})\b")),
    ("generic_pk_key", "api_key", re.compile(r"\b(pk-[A-Za-z0-9]{20,})\b")),
]

# Context-anchored patterns: keyword must appear within 100 chars upstream.
# Each entry: (key_name, type, keyword_regex, value_regex_after_separator)
_CONTEXT_PATTERNS: list[tuple[str, str, re.Pattern[str], re.Pattern[str]]] = [
    (
        "aws_secret_key", "aws",
        re.compile(r"(?:secret_access_key|aws_secret|secret[_ ]key)", re.IGNORECASE),
        re.compile(r"""[:=]\s*["']?([A-Za-z0-9/+=]{40,})["']?"""),
    ),
    (
        "azure_tenant_id", "azure",
        re.compile(r"(?:tenant[_ ]?id|tenant)", re.IGNORECASE),
        re.compile(r"""[:=]\s*["']?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})["']?""", re.IGNORECASE),
    ),
    (
        "azure_client_id", "azure",
        re.compile(r"(?:client[_ ]?id|app[_ ]?id|application[_ ]?id)", re.IGNORECASE),
        re.compile(r"""[:=]\s*["']?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})["']?""", re.IGNORECASE),
    ),
    (
        "azure_subscription_id", "azure",
        re.compile(r"(?:subscription[_ ]?id)", re.IGNORECASE),
        re.compile(r"""[:=]\s*["']?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})["']?""", re.IGNORECASE),
    ),
    (
        "azure_client_secret", "azure",
        re.compile(r"(?:client[_ ]?secret|client_secret_value)", re.IGNORECASE),
        re.compile(r"""[:=]\s*["']?([A-Za-z0-9~._\-]{20,})["']?"""),
    ),
    (
        "password", "credential",
        re.compile(r"(?:password|passwd|pass)", re.IGNORECASE),
        # Exclude common non-secret boolean/null tokens to reduce false positives.
        re.compile(r"""[:=]\s*["']?(?!(?:false|true|null|none|yes|no)\b)(\S{4,})["']?""", re.IGNORECASE),
    ),
    (
        "api_key", "api_key",
        re.compile(r"(?:api[_ ]?key|apikey|api_token)", re.IGNORECASE),
        re.compile(r"""[:=]\s*["']?([A-Za-z0-9_\-]{16,})["']?"""),
    ),
    (
        "bearer_token", "token",
        re.compile(r"(?:bearer[_ ]?token|auth[_ ]?token|access[_ ]?token)", re.IGNORECASE),
        re.compile(r"""[:=]\s*["']?([A-Za-z0-9_\-.]{20,})["']?"""),
    ),
    (
        "connection_string", "connection_string",
        re.compile(r"(?:connection[_ ]?string)", re.IGNORECASE),
        re.compile(r"""[:=]\s*["']?(\S{20,})["']?"""),
    ),
]


def detect_secrets(text: str) -> list[dict[str, Any]]:
    """Scan *text* for secrets. Returns a list of detection dicts.

    Each dict: {"key", "value", "type", "pattern", "span": (start, end)}.
    """
    detections: list[dict[str, Any]] = []
    seen_values: set[str] = set()

    # 1. Standalone patterns — checked in priority order.
    # seen_values deduplicates by value, so overlapping patterns (e.g. openai_key
    # and generic_sk_key both match sk- prefixes) are resolved by first-match wins:
    # the more-specific pattern (longer minimum length) is listed first and takes
    # precedence; the less-specific pattern skips the already-seen value.
    for key, stype, pattern in _STANDALONE_PATTERNS:
        for m in pattern.finditer(text):
            val = m.group(1)
            if val not in seen_values:
                seen_values.add(val)
                detections.append({
                    "key": key,
                    "value": val,
                    "type": stype,
                    "pattern": key,
                    "span": m.span(1),
                })

    # 2. Context-anchored patterns
    for key, stype, kw_re, val_re in _CONTEXT_PATTERNS:
        for kw_match in kw_re.finditer(text):
            # Look for the value pattern within 100 chars after the keyword
            search_start = kw_match.end()
            search_end = min(search_start + 100, len(text))
            val_match = val_re.search(text, search_start, search_end)
            if val_match:
                val = val_match.group(1)
                if val not in seen_values:
                    seen_values.add(val)
                    detections.append({
                        "key": key,
                        "value": val,
                        "type": stype,
                        "pattern": key,
                        "span": val_match.span(1),
                    })

    return detections


def redact_secrets(text: str, detections: list[dict[str, Any]]) -> str:
    """Replace detected secret values with <<SECRET:key>> placeholders.

    Processes replacements from end to start so span offsets stay valid.
    """
    if not detections:
        return text
    # Sort by span start descending so we replace from end to start
    sorted_dets = sorted(detections, key=lambda d: d["span"][0], reverse=True)
    result = text
    for det in sorted_dets:
        start, end = det["span"]
        placeholder = f"<<SECRET:{det['key']}>>"
        result = result[:start] + placeholder + result[end:]
    return result


def scan_for_leaked_values(text: str, secret_values: list[tuple[str, str]]) -> list[str]:
    """Check if any known secret values appear in *text*.

    *secret_values* is a list of (key, value) tuples from the vault.
    Returns the list of key names whose values were found in the text.
    """
    leaked: list[str] = []
    for key, value in secret_values:
        if len(value) >= 8 and value in text:
            leaked.append(key)
    return leaked
