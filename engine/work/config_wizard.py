#!/usr/bin/env python3
"""Backend configuration wizard for the Automator engine.

Run directly:   python3 engine/work/config_wizard.py
Via automator:   ./automator --config setup
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.work.json_io import load_json_safe as _load_json, write_json as _write_json


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]

def _default_config_dir() -> Path:
    return REPO_ROOT / "config"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prompt_choice(prompt: str, choices: list[str], default: str | None = None) -> str:
    """Prompt user to pick from a list of choices."""
    while True:
        hint = "/".join(choices)
        if default:
            hint += f" [default: {default}]"
        answer = input(f"{prompt} ({hint}): ").strip().lower()
        if not answer and default:
            return default
        if answer in choices:
            return answer
        print(f"  Invalid choice. Pick one of: {', '.join(choices)}")


def _prompt_string(prompt: str, default: str = "", secret: bool = False) -> str:
    """Prompt user for a string value."""
    hint = f" [default: {default}]" if default else ""
    if secret:
        try:
            import getpass
            answer = getpass.getpass(f"{prompt}{hint}: ").strip()
        except (ImportError, EOFError):
            answer = input(f"{prompt}{hint}: ").strip()
    else:
        answer = input(f"{prompt}{hint}: ").strip()
    return answer if answer else default


def _redact_key(key: str) -> str:
    """Redact an API key for display."""
    if not key or len(key) < 8:
        return "***"
    return key[:4] + "..." + key[-4:]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROVIDERS = ["anthropic", "google", "openai"]
_PROVIDER_LABELS = {
    "anthropic": "Anthropic (Claude)",
    "google": "Google (Gemini)",
    "openai": "OpenAI (GPT / Codex)",
}
_API_KEY_LABELS = {
    "anthropic": "Anthropic API key",
    "google": "Google API key",
    "openai": "OpenAI API key",
}
_API_KEY_FIELDS = {
    "anthropic": "anthropic_api_key",
    "google": "google_api_key",
    "openai": "openai_api_key",
}
_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "google": "gemini-2.5-pro",
    "openai": "gpt-4.1",
}
_KNOWN_ROLES = ["worker", "review", "research"]

# CLI binary names for each provider
_CLI_TOOLS = {
    "anthropic": "claude",
    "google": "gemini",
    "openai": "codex",
}

# Python SDK module names for each provider (for import check)
_API_SDK_MODULES = {
    "anthropic": "anthropic",
    "google": "google.genai",
    "openai": "openai",
}
_API_SDK_PACKAGES = {
    "anthropic": "anthropic>=0.39.0",
    "google": "google-genai>=1.0.0",
    "openai": "openai>=1.50.0",
}

# Core Python packages from requirements.txt
_CORE_PACKAGES = [
    ("tiktoken", "tiktoken>=0.5.0", False),
    ("requests", "requests>=2.28.0", False),
    ("yaml", "pyyaml>=6.0", False),
    ("docx", "python-docx>=1.1.0", False),
    ("openpyxl", "openpyxl>=3.1.0", False),
    ("pandas", "pandas>=2.0.0", False),
    ("reportlab", "reportlab>=4.0.0", False),
    ("pdfplumber", "pdfplumber>=0.11.0", False),
    ("pypdf", "pypdf>=4.0.0", False),
    ("pdf2image", "pdf2image>=1.17.0", False),
]


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    """Result of a single environment check."""
    name: str
    status: str  # "ok", "warn", "fail"
    message: str
    fix: str = ""


def check_python_version() -> CheckResult:
    """Check Python >= 3.10."""
    v = sys.version_info
    version_str = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 10):
        return CheckResult("Python", "ok", f"{version_str}")
    return CheckResult(
        "Python", "fail", f"{version_str} (need >= 3.10)",
        fix="Install Python 3.10 or later: https://www.python.org/downloads/",
    )


def check_git() -> CheckResult:
    """Check git is available."""
    path = shutil.which("git")
    if path:
        try:
            out = subprocess.run(
                ["git", "--version"], capture_output=True, text=True, timeout=5,
            )
            version = out.stdout.strip() if out.returncode == 0 else "found"
            return CheckResult("Git", "ok", version)
        except (OSError, subprocess.TimeoutExpired):
            return CheckResult("Git", "ok", f"found at {path}")
    return CheckResult(
        "Git", "fail", "not found",
        fix="Install git: https://git-scm.com/downloads",
    )


def check_node() -> CheckResult:
    """Check Node.js / npm (needed to install AI CLI tools)."""
    npm_path = shutil.which("npm")
    node_path = shutil.which("node")
    if node_path and npm_path:
        try:
            out = subprocess.run(
                ["node", "--version"], capture_output=True, text=True, timeout=5,
            )
            version = out.stdout.strip() if out.returncode == 0 else "found"
            return CheckResult("Node.js", "ok", version)
        except (OSError, subprocess.TimeoutExpired):
            return CheckResult("Node.js", "ok", "found")
    if node_path:
        return CheckResult(
            "Node.js", "warn", "node found but npm missing",
            fix="Install npm (usually bundled with Node.js): https://nodejs.org/",
        )
    return CheckResult(
        "Node.js", "warn", "not found (needed to install AI CLI tools)",
        fix="Install Node.js (LTS): https://nodejs.org/",
    )


def check_cli_tools() -> list[CheckResult]:
    """Check which AI CLI tools are installed."""
    results = []
    for provider, binary in _CLI_TOOLS.items():
        label = _PROVIDER_LABELS[provider]
        path = shutil.which(binary)
        if path:
            results.append(CheckResult(f"CLI: {binary}", "ok", f"found ({label})"))
        else:
            results.append(CheckResult(
                f"CLI: {binary}", "info", f"not installed ({label})",
            ))
    return results


def check_python_packages() -> list[CheckResult]:
    """Check which core Python packages are installed."""
    results = []
    for module_name, pip_name, _optional in _CORE_PACKAGES:
        try:
            __import__(module_name)
            results.append(CheckResult(f"pkg: {pip_name.split('>=')[0]}", "ok", "installed"))
        except ImportError:
            results.append(CheckResult(
                f"pkg: {pip_name.split('>=')[0]}", "fail",
                "not installed",
                fix=f"pip install {pip_name}",
            ))
    return results


def check_api_sdk(provider: str) -> CheckResult:
    """Check if the vendor SDK for a specific provider is installed."""
    module_name = _API_SDK_MODULES.get(provider, "")
    pip_name = _API_SDK_PACKAGES.get(provider, "")
    label = _PROVIDER_LABELS.get(provider, provider)
    if not module_name:
        return CheckResult(f"SDK: {provider}", "fail", "unknown provider")
    try:
        __import__(module_name)
        return CheckResult(f"SDK: {label}", "ok", "installed")
    except ImportError:
        return CheckResult(
            f"SDK: {label}", "fail",
            "not installed",
            fix=f"pip install {pip_name}",
        )


def check_venv() -> CheckResult:
    """Check if running inside the repo-local .venv."""
    venv_dir = REPO_ROOT / ".venv"
    if not venv_dir.is_dir():
        return CheckResult(
            "Virtualenv", "warn",
            "no .venv/ found (recommended but optional)",
            fix=f"python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt",
        )
    # Check if the current Python is inside the venv
    python_path = Path(sys.executable).resolve()
    try:
        python_path.relative_to(venv_dir.resolve())
        return CheckResult("Virtualenv", "ok", ".venv active")
    except ValueError:
        return CheckResult(
            "Virtualenv", "warn",
            ".venv/ exists but not active",
            fix=f". {venv_dir}/bin/activate",
        )


def check_optional_system_tools() -> list[CheckResult]:
    """Check optional system tools (poppler-utils, qpdf)."""
    results = []
    for binary, pkg, purpose in [
        ("pdftotext", "poppler-utils", "PDF text extraction"),
        ("pdfinfo", "poppler-utils", "PDF inspection"),
        ("qpdf", "qpdf", "PDF structural operations"),
    ]:
        if shutil.which(binary):
            results.append(CheckResult(f"sys: {binary}", "ok", f"found ({purpose})"))
        else:
            results.append(CheckResult(
                f"sys: {binary}", "info",
                f"not found ({purpose})",
                fix=f"sudo apt-get install -y {pkg}",
            ))
    return results


def run_all_checks(mode: str | None = None, provider: str | None = None) -> list[CheckResult]:
    """Run all environment checks and return results.

    Args:
        mode: If "api", also checks the vendor SDK for the provider.
        provider: The API provider to check SDK for (only used when mode="api").
    """
    results: list[CheckResult] = []

    # Core requirements
    results.append(check_python_version())
    results.append(check_git())
    results.append(check_venv())
    results.append(check_node())

    # AI CLI tools
    results.extend(check_cli_tools())

    # Core Python packages
    results.extend(check_python_packages())

    # Optional system tools
    results.extend(check_optional_system_tools())

    # API SDK check if relevant
    if mode == "api" and provider:
        results.append(check_api_sdk(provider))

    return results


def _print_check_results(results: list[CheckResult]) -> tuple[int, int, int]:
    """Print check results and return (ok_count, warn_count, fail_count)."""
    _STATUS_SYMBOLS = {
        "ok": "  [OK]  ",
        "warn": "  [!!]  ",
        "fail": "  [FAIL]",
        "info": "  [--]  ",
    }

    ok_count = 0
    warn_count = 0
    fail_count = 0

    for r in results:
        symbol = _STATUS_SYMBOLS.get(r.status, "  [??]  ")
        print(f"{symbol} {r.name:<28} {r.message}")
        if r.status == "ok":
            ok_count += 1
        elif r.status == "warn":
            warn_count += 1
        elif r.status == "fail":
            fail_count += 1

    return ok_count, warn_count, fail_count


def _print_fixes(results: list[CheckResult]) -> None:
    """Print fix instructions for failed and warned checks."""
    fixes = [(r.name, r.fix) for r in results if r.fix and r.status in ("fail", "warn")]
    if not fixes:
        return
    print()
    print("  " + "-" * 40)
    print("  How to fix:")
    print("  " + "-" * 40)
    for name, fix in fixes:
        print(f"    {name}:")
        print(f"      {fix}")


# ---------------------------------------------------------------------------
# Setup command
# ---------------------------------------------------------------------------


def cmd_setup(config_dir: Path | None = None) -> int:
    """Interactive setup wizard for backend configuration."""
    config_dir = config_dir or _default_config_dir()
    backends_path = config_dir / "backends.json"
    secrets_path = config_dir / "secrets.json"

    # Ensure repo structure (symlinks, .gitignore, directories) exists
    try:
        from engine.work.repo_bootstrap import ensure_repo_structure
        ensure_repo_structure()
    except Exception as exc:
        print(f"  Warning: repo structure bootstrap skipped: {exc}", file=sys.stderr)

    # Load existing config if present
    existing = _load_json(backends_path)
    existing_secrets = _load_json(secrets_path)

    print()
    print("=" * 55)
    print("  Automator — Setup Wizard")
    print("=" * 55)

    # --- Step 1: Environment preflight ---
    print()
    print("  " + "-" * 40)
    print("  Step 1: Environment Check")
    print("  " + "-" * 40)
    print()

    preflight_results = run_all_checks()
    ok_count, warn_count, fail_count = _print_check_results(preflight_results)

    print()
    if fail_count == 0 and warn_count == 0:
        print("  All checks passed.")
    else:
        parts = []
        if fail_count:
            parts.append(f"{fail_count} failed")
        if warn_count:
            parts.append(f"{warn_count} warnings")
        print(f"  {ok_count} passed, {', '.join(parts)}.")
        _print_fixes(preflight_results)

    # Check if any CLI tools are available (relevant context for mode selection)
    any_cli = any(shutil.which(b) for b in _CLI_TOOLS.values())

    # --- Step 2: Backend mode ---
    print()
    print("  " + "-" * 40)
    print("  Step 2: Backend Configuration")
    print("  " + "-" * 40)
    print()
    print("  CLI mode  — spawn AI tools via subprocess (claude,")
    print("              gemini, codex). Requires the CLI tool")
    print("              installed. Use --claude/--gemini/--codex.")
    print()
    print("  API mode  — call vendor HTTP APIs directly. Requires")
    print("              an API key. The --LLM flag is ignored;")
    print("              provider and model come from this config.")
    if not any_cli:
        print()
        print("  Note: No AI CLI tools found. If you want CLI mode,")
        print("  install at least one first (see fix instructions above).")
    print()

    current_mode = existing.get("mode", "cli")
    mode = _prompt_choice(
        "  Execution mode",
        ["cli", "api"],
        default=current_mode,
    )

    backends_config: dict[str, Any] = {"version": 2, "mode": mode}
    secrets_config: dict[str, Any] = {"version": 1}

    # Preserve existing secrets
    for f in _API_KEY_FIELDS.values():
        if f in existing_secrets:
            secrets_config[f] = existing_secrets[f]

    if mode == "cli":
        print()
        print("  CLI mode selected.")
        print("  Use --claude, --gemini, or --codex to pick the backend at runtime.")
        print("  Example: ./automator project new --claude --name demo \"...\"")
        if not any_cli:
            print()
            print("  Warning: No AI CLI tools detected. Install at least one:")
            print("    npm install -g @anthropic-ai/claude-code     (Claude)")
            print("    npm install -g @google/gemini-cli             (Gemini)")
            print("    npm install -g @openai/codex                  (Codex)")
        config_dir.mkdir(parents=True, exist_ok=True)
        _write_json(backends_path, backends_config)
        _write_json(secrets_path, secrets_config)
        _print_done(backends_path, secrets_path)
        return 0

    # --- Step 3: Provider ---
    print()
    print("  " + "-" * 40)
    print("  Step 3: Provider Selection")
    print("  " + "-" * 40)
    print()
    for p in _PROVIDERS:
        default_tag = f"  (default model: {_DEFAULT_MODELS[p]})" if p in _DEFAULT_MODELS else ""
        print(f"    {p:<12} {_PROVIDER_LABELS[p]}{default_tag}")
    print()

    current_provider = existing.get("provider", "anthropic")
    if current_provider not in _PROVIDERS:
        current_provider = "anthropic"
    provider = _prompt_choice("  Default provider", _PROVIDERS, default=current_provider)
    backends_config["provider"] = provider

    # Check SDK availability for the chosen provider
    sdk_result = check_api_sdk(provider)
    if sdk_result.status == "fail":
        print()
        print(f"  Warning: {_PROVIDER_LABELS[provider]} SDK not installed.")
        print(f"  You will need it before running in API mode:")
        print(f"    {sdk_result.fix}")

    # --- Step 4: API key ---
    print()
    key_field = _API_KEY_FIELDS[provider]
    existing_key = secrets_config.get(key_field, "")
    if existing_key:
        print(f"  Current {_API_KEY_LABELS[provider]}: {_redact_key(existing_key)}")
        print(f"  Press Enter to keep the current key, or paste a new one.")
    api_key = _prompt_string(
        f"  {_API_KEY_LABELS[provider]}",
        default=existing_key,
        secret=True,
    )
    if api_key:
        secrets_config[key_field] = api_key

    # --- Optional: custom API endpoint (base_url) ---
    # OpenAI-compatible aggregators (OpenRouter), cheap/local providers (DeepSeek,
    # Ollama, LM Studio, vLLM) all expose an OpenAI-compatible HTTP endpoint.
    # Most useful with provider=openai, but vendor SDKs accept base_url too.
    print()
    existing_base_url = existing.get("base_url") or ""
    if existing_base_url:
        print(f"  Current API endpoint: {existing_base_url}")
        print("  Press Enter to keep, type 'default' to clear, or paste a new URL.")
    else:
        print("  Optional: custom API endpoint (base_url).")
        print("  Leave blank to use the vendor default. Examples:")
        print("    https://openrouter.ai/api/v1        (OpenRouter — 100+ models)")
        print("    https://api.deepseek.com/v1         (DeepSeek direct)")
        print("    http://localhost:11434/v1           (Ollama local)")
        print("    http://localhost:1234/v1            (LM Studio local)")
    base_url_input = _prompt_string("  API endpoint (optional)", default=existing_base_url)
    if base_url_input.strip().lower() == "default":
        backends_config["base_url"] = None
    elif base_url_input.strip():
        backends_config["base_url"] = base_url_input.strip()
    else:
        backends_config["base_url"] = None

    # --- Step 5: Default model ---
    print()
    print("  " + "-" * 40)
    print("  Step 4: Model Selection")
    print("  " + "-" * 40)
    print()
    suggested_model = _DEFAULT_MODELS.get(provider, "")
    current_model = existing.get("default_model") or ""
    print(f"  This model is used for ALL agent roles unless overridden below.")
    if suggested_model:
        print(f"  Suggested default for {provider}: {suggested_model}")
    print()
    default_model = _prompt_string(
        "  Default model",
        default=current_model or suggested_model,
    )
    backends_config["default_model"] = default_model if default_model else None

    # --- Step 6: Per-role overrides ---
    print()
    print("  " + "-" * 40)
    print("  Step 5: Per-Role Overrides (optional)")
    print("  " + "-" * 40)
    print()
    print("  You can override the model (or even the provider) for")
    print("  specific agent roles. This is useful for example to use")
    print("  a stronger model for coding and a cheaper one for QA.")
    print()
    print(f"  Available roles: {', '.join(_KNOWN_ROLES)}")
    print()
    add_overrides = _prompt_choice("  Add role overrides?", ["yes", "no"], default="no")

    role_overrides: dict[str, Any] = {}
    if add_overrides == "yes":
        print()
        print("  Enter a role name to configure, or 'done' to finish.")
        _override_count = 0
        while True:
            _override_count += 1
            if _override_count > 20:
                print("  Warning: override limit (20) reached. Stopping.", file=sys.stderr)
                break
            print()
            role = _prompt_string("  Role (or 'done')").strip().lower()
            if role == "done" or not role:
                break
            if role not in _KNOWN_ROLES:
                print(f"  Warning: '{role}' is not a known role. Continuing anyway.")

            override: dict[str, Any] = {}

            # Provider override
            override_provider = _prompt_choice(
                f"    Provider for {role}",
                _PROVIDERS + ["same"],
                default="same",
            )
            if override_provider != "same":
                override["provider"] = override_provider
                # Ensure we have an API key for this provider
                ovr_key_field = _API_KEY_FIELDS[override_provider]
                ovr_existing_key = secrets_config.get(ovr_key_field, "")
                if not ovr_existing_key:
                    print(f"    No API key set for {_PROVIDER_LABELS[override_provider]}.")
                    ovr_api_key = _prompt_string(
                        f"    {_API_KEY_LABELS[override_provider]}",
                        secret=True,
                    )
                    if ovr_api_key:
                        secrets_config[ovr_key_field] = ovr_api_key

                # Check SDK for override provider too
                ovr_sdk = check_api_sdk(override_provider)
                if ovr_sdk.status == "fail":
                    print(f"    Warning: {_PROVIDER_LABELS[override_provider]} SDK not installed.")
                    print(f"    You will need it: {ovr_sdk.fix}")

            # Model override
            effective_provider = override.get("provider", provider)
            suggested = _DEFAULT_MODELS.get(effective_provider, "")
            override_model = _prompt_string(
                f"    Model for {role}",
                default=suggested,
            )
            if override_model and override_model != (default_model or ""):
                override["model"] = override_model

            if override:
                role_overrides[role] = override
                print(f"    -> {role}: {override}")

    if role_overrides:
        backends_config["role_overrides"] = role_overrides

    # --- Write ---
    config_dir.mkdir(parents=True, exist_ok=True)
    _write_json(backends_path, backends_config)
    _write_json(secrets_path, secrets_config)

    # --- Final summary ---
    print()
    print("  " + "-" * 40)
    print("  Setup Complete")
    print("  " + "-" * 40)
    print()
    print(f"  Config written to:  {backends_path}")
    print(f"  Secrets written to: {secrets_path}")
    print()
    print("  Verify with:   ./automator config show")
    print("  Validate with: ./automator config validate")
    print("  Test runtime:  ./automator project check-runtime")
    print()

    return 0


def _print_done(backends_path: Path, secrets_path: Path) -> None:
    print()
    print("  " + "-" * 40)
    print("  Setup Complete")
    print("  " + "-" * 40)
    print()
    print(f"  Config written to:  {backends_path}")
    print(f"  Secrets written to: {secrets_path}")
    print()
    print("  Verify with:   ./automator config show")
    print("  Validate with: ./automator config validate")
    print("  Test runtime:  ./automator project check-runtime")
    print()


# ---------------------------------------------------------------------------
# Show command
# ---------------------------------------------------------------------------


def cmd_show(config_dir: Path | None = None) -> int:
    """Display current backend configuration with redacted keys."""
    config_dir = config_dir or _default_config_dir()
    backends_path = config_dir / "backends.json"
    secrets_path = config_dir / "secrets.json"

    if not backends_path.exists():
        print("No backend configuration found. Run: ./automator --config setup")
        return 1

    config = _load_json(backends_path)
    secrets = _load_json(secrets_path)

    mode = config.get("mode", "cli")
    print("=== Backend Configuration ===\n")
    print(f"  mode: {mode}")

    if mode == "cli":
        print("  (CLI mode — use --claude/--gemini/--codex flags)")
        return 0

    provider = config.get("provider", "(not set)")
    default_model = config.get("default_model") or "(not set)"
    print(f"  provider: {provider}")
    print(f"  default_model: {default_model}")
    base_url = config.get("base_url")
    if base_url:
        print(f"  base_url: {base_url}")

    # Show API key status for the provider
    key_field = _API_KEY_FIELDS.get(provider, "")
    api_key = secrets.get(key_field, "")
    key_display = _redact_key(api_key) if api_key else "(not set)"
    print(f"  api_key: {key_display}")

    # Role overrides
    overrides = config.get("role_overrides", {})
    if overrides:
        print("\n  Role Overrides:")
        for role, override in overrides.items():
            parts = []
            if "provider" in override:
                parts.append(f"provider={override['provider']}")
                ovr_key_field = _API_KEY_FIELDS.get(override["provider"], "")
                ovr_key = secrets.get(ovr_key_field, "")
                ovr_display = _redact_key(ovr_key) if ovr_key else "(not set)"
                parts.append(f"api_key={ovr_display}")
            if "model" in override:
                parts.append(f"model={override['model']}")
            if "base_url" in override:
                parts.append(f"base_url={override['base_url']}")
            print(f"    {role}: {', '.join(parts)}")

    return 0


# ---------------------------------------------------------------------------
# Validate command
# ---------------------------------------------------------------------------


def cmd_validate(config_dir: Path | None = None) -> int:
    """Validate backend configuration: check keys are set for API mode."""
    config_dir = config_dir or _default_config_dir()
    backends_path = config_dir / "backends.json"
    secrets_path = config_dir / "secrets.json"

    if not backends_path.exists():
        print("No backend configuration found. Run: ./automator --config setup")
        return 1

    config = _load_json(backends_path)
    secrets = _load_json(secrets_path)

    mode = config.get("mode", "cli")
    if mode == "cli":
        print("Validation OK: CLI mode — no API keys needed.")
        return 0

    errors = []

    # Check main provider key
    provider = config.get("provider", "")
    if provider not in _API_KEY_FIELDS:
        errors.append(f"Invalid or missing provider: '{provider}'")
    else:
        key_field = _API_KEY_FIELDS[provider]
        api_key = secrets.get(key_field, "")
        if not api_key:
            errors.append(f"Default provider '{provider}': no API key set ({key_field} in secrets.json)")

    # Check role override providers
    overrides = config.get("role_overrides", {})
    for role, override in overrides.items():
        if not isinstance(override, dict):
            continue
        ovr_provider = override.get("provider")
        if ovr_provider and ovr_provider in _API_KEY_FIELDS:
            ovr_key_field = _API_KEY_FIELDS[ovr_provider]
            ovr_api_key = secrets.get(ovr_key_field, "")
            if not ovr_api_key:
                errors.append(
                    f"Role '{role}' uses provider '{ovr_provider}' "
                    f"but no API key set ({ovr_key_field} in secrets.json)"
                )

    if errors:
        print("Validation FAILED:\n")
        for error in errors:
            print(f"  ERROR: {error}")
        return 1

    print("Validation OK: All API providers have keys configured.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for config commands."""
    argv = argv if argv is not None else sys.argv[1:]

    if not argv:
        # Default to setup when run directly
        return cmd_setup()

    command = argv[0]
    if command == "setup":
        return cmd_setup()
    elif command == "show":
        return cmd_show()
    elif command == "validate":
        return cmd_validate()
    else:
        print(f"Unknown config command: {command}")
        print("Usage: ./automator config <setup|show|validate>")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
