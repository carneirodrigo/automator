"""Repo bootstrap: ensure directory structure, config files, links, and .gitignore exist."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from engine.work.repo_paths import (
    CONFIG_DIR,
    DEBUG_DIR,
    DEBUG_ISSUES_DIR,
    DEBUG_TRACKER_PATH,
    INPUTS_DIR,
    REPO_ROOT,
    SKILLS_CATALOG_PATH,
    SKILLS_DIR,
    SKILLS_MANIFEST_PATH,
    SKILLS_SOURCES_PATH,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_SOURCES = {
    "version": 1,
    "repos": [
        {"id": "anthropic", "url": "https://github.com/anthropics/skills", "skills_path": "skills"},
        {"id": "openai", "url": "https://github.com/openai/skills", "skills_path": ".curated"},
        {"id": "google-gemini-skills", "url": "https://github.com/google-gemini/gemini-skills", "skills_path": "skills"},
        {"id": "google-gemini-cli", "url": "https://github.com/google-gemini/gemini-cli", "skills_path": ".gemini/skills"},
        {"id": "microsoft", "url": "https://github.com/microsoft/skills", "skills_path": "skills"},
    ],
}

_EMPTY_INDEX = {"version": 1, "skills": []}
_EMPTY_DEBUG_TRACKER = {"version": 1, "issues": []}

# Repo-root symlinks auto-created by the engine so they don't need to live in git.
_REPO_ROOT_SYMLINKS: dict[str, str] = {
    "ORCHESTRATION.md": "engine/ORCHESTRATION.md",
    "CLAUDE.md": "engine/ORCHESTRATION.md",
    "GEMINI.md": "engine/ORCHESTRATION.md",
    "AGENTS.md": "engine/ORCHESTRATION.md",
}

# Default .gitignore content — auto-created by the engine so it doesn't need to live in git.
_DEFAULT_GITIGNORE = """\
# Self-ignore (this file is auto-created by the engine)
.gitignore

# Python (noise)
__pycache__/
*.pyc
.venv/

# Auto-created symlinks (engine creates on run — root-level only)
/ORCHESTRATION.md
/AGENTS.md
/CLAUDE.md
/GEMINI.md

# Security (local config/secrets)
.env
.claude/
.codex/
.gemini/
.github/

# Projects — ignore per-project content but keep registry files
projects/*
projects/registry.json
projects/registry.csv

# Personal config — track README template, ignore user files
personal/*
!personal/README.md

# Inputs inbox
inputs/

# Skills — track indexes and config, ignore cached content
skills/.cache/
skills/*/
!skills/catalog.json
!skills/sources.json
!skills/manifest.json

# Knowledge base — track shared knowledge entries and indexes

# Debug captures
debug/

# Backend configuration (API keys, secrets)
config/
"""


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def ensure_repo_structure() -> None:
    """Create the skills directory, default config files, .gitignore, and repo-root links if missing."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_ISSUES_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    INPUTS_DIR.mkdir(parents=True, exist_ok=True)

    # Default JSON files
    for path, default in [
        (SKILLS_SOURCES_PATH, _DEFAULT_SOURCES),
        (SKILLS_CATALOG_PATH, _EMPTY_INDEX),
        (SKILLS_MANIFEST_PATH, _EMPTY_INDEX),
        (DEBUG_TRACKER_PATH, _EMPTY_DEBUG_TRACKER),
    ]:
        if not path.exists():
            path.write_text(
                json.dumps(default, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    # .gitignore — auto-created if missing, never overwritten
    gitignore_path = REPO_ROOT / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(_DEFAULT_GITIGNORE, encoding="utf-8")

    # Repo-root symlinks: CLAUDE.md -> ORCHESTRATION.md, etc.
    for link_name, target in _REPO_ROOT_SYMLINKS.items():
        link = REPO_ROOT / link_name
        needs_refresh = False
        if link.is_symlink():
            try:
                needs_refresh = str(link.readlink()) != target
            except OSError:
                needs_refresh = True
            # Also refresh if the symlink is broken (target doesn't exist)
            if not needs_refresh and not link.exists():
                needs_refresh = True
        if needs_refresh:
            link.unlink(missing_ok=True)
        if not link.exists():
            try:
                link.symlink_to(target)
            except OSError as exc:
                print(f"[repo_bootstrap] symlink {link} -> {target} failed: {exc}", file=sys.stderr)
