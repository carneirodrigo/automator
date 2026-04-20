"""Backend configuration loader and resolver for CLI/API execution modes."""

from __future__ import annotations

import json
import logging
import shlex
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend name mapping: maps CLI binary names and config keys to canonical names
# ---------------------------------------------------------------------------

_BACKEND_ALIASES: dict[str, str] = {
    "claude": "claude",
    "gemini": "gemini",
    "codex": "openai",
    "openai": "openai",
}

# Maps provider names (user-friendly) to canonical backend names
_PROVIDER_TO_BACKEND: dict[str, str] = {
    "anthropic": "claude",
    "google": "gemini",
    "openai": "openai",
}

# Maps canonical backend name to provider name
_BACKEND_TO_PROVIDER: dict[str, str] = {
    "claude": "anthropic",
    "gemini": "google",
    "openai": "openai",
}

# Maps canonical backend name to the equivalent CLI binary
_BACKEND_TO_AGENT_BIN: dict[str, str] = {
    "claude": "claude",
    "gemini": "gemini",
    "openai": "codex",
}

# Maps canonical backend name to the API key field in secrets.json
_API_KEY_FIELDS: dict[str, str] = {
    "claude": "anthropic_api_key",
    "gemini": "google_api_key",
    "openai": "openai_api_key",
}

# Valid execution modes
VALID_MODES = {"cli", "api"}

# Valid provider names
VALID_PROVIDERS = {"anthropic", "google", "openai"}


# ---------------------------------------------------------------------------
# Resolution result
# ---------------------------------------------------------------------------


@dataclass
class BackendResolution:
    """Resolved execution parameters for a backend+role combination."""

    mode: str = "cli"
    backend_name: str = ""
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: int | None = None


# ---------------------------------------------------------------------------
# Config cache
# ---------------------------------------------------------------------------

_cached_backends_config: dict[str, Any] | None = None
_cached_secrets_config: dict[str, Any] | None = None
_config_dir: Path | None = None
_cache_lock = threading.Lock()


def set_config_dir(path: Path) -> None:
    """Override the config directory (useful for testing)."""
    global _config_dir, _cached_backends_config, _cached_secrets_config
    with _cache_lock:
        _config_dir = path
        _cached_backends_config = None
        _cached_secrets_config = None


def _get_config_dir() -> Path:
    if _config_dir is not None:
        return _config_dir
    # Default: repo root / config
    return Path(__file__).resolve().parents[2] / "config"


def reset_cache() -> None:
    """Clear cached config (useful for testing)."""
    global _cached_backends_config, _cached_secrets_config
    with _cache_lock:
        _cached_backends_config = None
        _cached_secrets_config = None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_backend_config(config_dir: Path | None = None) -> dict[str, Any]:
    """Load config/backends.json. Returns empty defaults if missing."""
    global _cached_backends_config
    import copy  # noqa: PLC0415
    with _cache_lock:
        if _cached_backends_config is not None and config_dir is None:
            return copy.deepcopy(_cached_backends_config)

        path = (config_dir or _get_config_dir()) / "backends.json"
        if not path.exists():
            result: dict[str, Any] = {"version": 2, "mode": "cli"}
            if config_dir is None:
                _cached_backends_config = result
            return copy.deepcopy(result)

        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load %s: %s. Using defaults.", path, exc)
            result = {"version": 2, "mode": "cli"}
            if config_dir is None:
                _cached_backends_config = result
            return copy.deepcopy(result)

        if not isinstance(data, dict):
            logger.warning("backends.json is not a JSON object. Using defaults.")
            result = {"version": 2, "mode": "cli"}
            if config_dir is None:
                _cached_backends_config = result
            return copy.deepcopy(result)

        if config_dir is None:
            _cached_backends_config = data
        return copy.deepcopy(data)


def load_api_secrets(config_dir: Path | None = None) -> dict[str, Any]:
    """Load config/secrets.json. Returns empty dict if missing."""
    import copy  # noqa: PLC0415
    global _cached_secrets_config
    with _cache_lock:
        if _cached_secrets_config is not None and config_dir is None:
            return copy.deepcopy(_cached_secrets_config)

        path = (config_dir or _get_config_dir()) / "secrets.json"
        if not path.exists():
            result: dict[str, Any] = {}
            if config_dir is None:
                _cached_secrets_config = result
            return copy.deepcopy(result)

        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load %s: %s. Using empty secrets.", path, exc)
            result = {}
            if config_dir is None:
                _cached_secrets_config = result
            return copy.deepcopy(result)

        if not isinstance(data, dict):
            logger.warning("secrets.json is not a JSON object. Using empty secrets.")
            result = {}
            if config_dir is None:
                _cached_secrets_config = result
            return copy.deepcopy(result)

        if config_dir is None:
            _cached_secrets_config = data
        return copy.deepcopy(data)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _canonical_backend_name(agent_bin: str) -> str:
    """Map an agent binary name (e.g., 'claude', '/usr/bin/gemini') to a canonical config key."""
    try:
        parts = shlex.split(agent_bin)
    except ValueError:
        parts = [agent_bin]
    binary_name = parts[0].lower() if parts else ""
    for alias, canonical in _BACKEND_ALIASES.items():
        if alias in binary_name:
            return canonical
    return binary_name


def resolve_backend(
    agent_bin: str,
    role: str,
    *,
    config_dir: Path | None = None,
) -> BackendResolution:
    """Resolve execution parameters for a given agent binary and role.

    Resolution logic:
    - If config mode is "cli" or missing: returns CLI mode (agent_bin drives everything).
    - If config mode is "api": uses provider + default_model from config,
      with optional per-role overrides for provider and model.
      The agent_bin parameter is ignored in API mode.
    """
    config = load_backend_config(config_dir)
    secrets = load_api_secrets(config_dir)

    global_mode = config.get("mode", "cli")
    if global_mode not in VALID_MODES:
        logger.warning("Invalid global mode '%s'. Falling back to cli.", global_mode)
        global_mode = "cli"

    # --- CLI mode: agent_bin drives everything, config is irrelevant ---
    if global_mode == "cli":
        canonical = _canonical_backend_name(agent_bin)
        return BackendResolution(mode="cli", backend_name=canonical)

    # --- API mode: config drives everything, agent_bin is ignored ---
    provider = config.get("provider", "")
    if provider not in VALID_PROVIDERS:
        logger.warning("Invalid or missing provider '%s' in config. Falling back to cli.", provider)
        canonical = _canonical_backend_name(agent_bin)
        return BackendResolution(mode="cli", backend_name=canonical)

    backend_name = _PROVIDER_TO_BACKEND[provider]
    default_model = config.get("default_model")
    global_base_url = config.get("base_url")
    if global_base_url is not None and not isinstance(global_base_url, str):
        logger.warning("base_url in config is not a string; ignoring.")
        global_base_url = None
    if isinstance(global_base_url, str):
        global_base_url = global_base_url.strip() or None

    resolution = BackendResolution(
        mode="api",
        backend_name=backend_name,
        model=default_model,
        base_url=global_base_url,
    )

    # Apply role overrides if present
    role_overrides = config.get("role_overrides", {})
    role_override = role_overrides.get(role, {})
    if role_override and not isinstance(role_override, dict):
        logger.warning(
            "Role override for '%s' is not a dict (got %s): ignored.",
            role, type(role_override).__name__,
        )
        role_override = {}
    if isinstance(role_override, dict) and role_override:
        override_provider = role_override.get("provider")
        if override_provider and override_provider in VALID_PROVIDERS:
            resolution.backend_name = _PROVIDER_TO_BACKEND[override_provider]
            # Switching provider invalidates a global base_url aimed at the previous provider.
            # The override may restate base_url explicitly below; otherwise clear it.
            resolution.base_url = None
        override_model = role_override.get("model")
        if override_model is not None:
            resolution.model = override_model
        if "base_url" in role_override:
            override_base_url = role_override.get("base_url")
            if override_base_url is None:
                resolution.base_url = None
            elif isinstance(override_base_url, str):
                resolution.base_url = override_base_url.strip() or None
            else:
                logger.warning(
                    "base_url override for role '%s' is not a string; ignored.", role,
                )

    # Resolve API key for the resolved backend
    key_field = _API_KEY_FIELDS.get(resolution.backend_name, "")
    api_key = secrets.get(key_field, "")
    if api_key and isinstance(api_key, str) and api_key.strip():
        resolution.api_key = api_key.strip()
    else:
        logger.warning(
            "API mode: no API key found for provider '%s' (field: %s in secrets.json).",
            _BACKEND_TO_PROVIDER.get(resolution.backend_name, resolution.backend_name),
            key_field,
        )

    return resolution


def is_api_mode(*, config_dir: Path | None = None) -> bool:
    """Quick check: is the global config set to API mode?"""
    config = load_backend_config(config_dir)
    return config.get("mode") == "api"


def get_api_agent_bin(*, config_dir: Path | None = None) -> str | None:
    """When mode=api, return the equivalent agent_bin for the configured provider.

    Returns None if mode is not api or provider is invalid.
    """
    config = load_backend_config(config_dir)
    if config.get("mode") != "api":
        return None
    provider = config.get("provider", "")
    if provider not in VALID_PROVIDERS:
        return None
    backend = _PROVIDER_TO_BACKEND[provider]
    return _BACKEND_TO_AGENT_BIN.get(backend)


def has_any_api_config(*, config_dir: Path | None = None) -> bool:
    """Check if global mode is set to API."""
    return is_api_mode(config_dir=config_dir)
