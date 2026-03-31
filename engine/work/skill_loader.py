"""Agent Skills loader: parse SKILL.md, manage catalog/manifest, match skills to roles."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from engine.work.repo_paths import (
    SKILLS_CACHE_DIR,
    SKILLS_CATALOG_PATH,
    SKILLS_DIR,
    SKILLS_MANIFEST_PATH,
    SKILLS_SOURCES_PATH,
)

try:
    import yaml  # type: ignore[import-untyped]

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# ---------------------------------------------------------------------------
# YAML frontmatter parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _coerce_string_list(value: Any) -> list[str]:
    """Normalize scalar-or-list frontmatter fields to a list of strings.

    Only string and numeric scalars are included. Dicts, lists, and None are
    silently dropped — calling str() on a nested object would produce garbage
    like "{'key': 'val'}" which pollutes role and tag lists.
    """
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                stripped = item.strip()
                if stripped:
                    result.append(stripped)
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                result.append(str(item))
            # dicts, lists, bool, None, and other complex types are intentionally dropped
        return result
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    return []


def _parse_frontmatter_fallback(raw: str) -> dict[str, Any]:
    """Regex-based parser for simple YAML frontmatter when pyyaml is absent."""
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item under a key
        if stripped.startswith("- ") and current_key is not None:
            if current_list is None:
                current_list = []
            val = stripped[2:].strip().strip("\"'")
            current_list.append(val)
            result[current_key] = current_list
            continue

        # Key: value pair
        if ":" in stripped:
            # Flush previous list
            current_list = None

            colon_idx = stripped.index(":")
            key = stripped[:colon_idx].strip()
            value = stripped[colon_idx + 1 :].strip()
            current_key = key

            if not value:
                # Next lines may be a list
                continue

            # Strip quotes
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]

            result[key] = value

    return result


def parse_skill_md(path: Path) -> dict[str, Any] | None:
    """Parse a SKILL.md file into {'frontmatter': {...}, 'body': '...', 'path': Path}.

    Returns None if the file cannot be parsed or has no frontmatter.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[skill_loader] parse_skill_md: cannot read {path}: {exc}", file=sys.stderr)
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        print(f"[skill_loader] parse_skill_md: no YAML frontmatter in {path}", file=sys.stderr)
        return None

    raw_fm = match.group(1)
    body = text[match.end() :]

    if _HAS_YAML:
        try:
            fm = yaml.safe_load(raw_fm)
            if not isinstance(fm, dict):
                return None
        except Exception as exc:
            print(f"[skill_loader] YAML parse failed for {path}: {exc}", file=sys.stderr)
            print(f"[skill_loader] using lossy fallback YAML parser for {path} — result may be incomplete", file=sys.stderr)
            fm = _parse_frontmatter_fallback(raw_fm)
    else:
        fm = _parse_frontmatter_fallback(raw_fm)

    return {"frontmatter": fm, "body": body.strip(), "path": path}


# ---------------------------------------------------------------------------
# Catalog & manifest I/O
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_skills_catalog() -> dict[str, Any]:
    """Load the skills catalog (all available vendor skills metadata)."""
    if not SKILLS_CATALOG_PATH.exists():
        return {"version": 1, "skills": []}
    return _load_json(SKILLS_CATALOG_PATH) or {"version": 1, "skills": []}


def load_skills_manifest() -> dict[str, Any]:
    """Load the skills manifest (locally cached/installed skills)."""
    if not SKILLS_MANIFEST_PATH.exists():
        return {"version": 1, "skills": []}
    return _load_json(SKILLS_MANIFEST_PATH) or {"version": 1, "skills": []}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _file_hash(path: Path) -> str:
    """SHA-256 hash of file contents."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return f"sha256:{h.hexdigest()}"


def rebuild_skills_manifest() -> dict[str, Any]:
    """Scan skills/ for cached SKILL.md files and rebuild manifest.json."""
    manifest: dict[str, Any] = {"version": 1, "skills": []}
    if not SKILLS_DIR.exists():
        return manifest

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        parsed = parse_skill_md(skill_md)
        if not parsed:
            continue
        fm = parsed["frontmatter"]
        entry = {
            "id": skill_dir.name,
            "name": fm.get("name", skill_dir.name),
            "description": fm.get("description", ""),
            "roles": _coerce_string_list(fm.get("roles", [])),
            "tags": _coerce_string_list(fm.get("tags", [])),
            "version": fm.get("version", ""),
            "source": fm.get("source", ""),
            "path": f"{skill_dir.name}/SKILL.md",
            "file_hash": _file_hash(skill_md),
            "char_count": len(parsed["body"]),
        }
        manifest["skills"].append(entry)

    _write_json(SKILLS_MANIFEST_PATH, manifest)
    return manifest


# ---------------------------------------------------------------------------
# Role heuristics
# ---------------------------------------------------------------------------

def role_heuristic(skill_metadata: dict[str, Any]) -> list[str]:
    """Infer which orchestration roles a skill applies to from its metadata.

    Skills without explicit role declarations default to the worker role,
    which is the DevOps/DevSecOps agent that handles all implementation work.
    """
    explicit = _coerce_string_list(skill_metadata.get("roles", []))
    if explicit:
        return explicit
    return ["worker"]


# ---------------------------------------------------------------------------
# Skill matching for agent prompts
# ---------------------------------------------------------------------------

MAX_MATCHED_SKILLS = 3


def match_skills_for_role(
    role: str, task: str, reason: str, project_desc: str = ""
) -> list[dict[str, Any]]:
    """Match cached skills to a role and task keywords. Returns top MAX_MATCHED_SKILLS."""
    manifest = load_skills_manifest()
    skills = manifest.get("skills", [])
    if not skills:
        return []

    query_tokens = set(re.findall(r"[a-z0-9]{3,}", f"{task} {reason} {project_desc}".lower()))
    if not query_tokens:
        return []

    candidates: list[tuple[int, dict[str, Any]]] = []
    for entry in skills:
        # Filter by role
        inferred_roles = role_heuristic(entry)
        if role not in inferred_roles:
            continue

        # Token-match against name, description, tags
        name = str(entry.get("name", "")).lower()
        desc = str(entry.get("description", "")).lower()
        tags = [str(t).lower() for t in entry.get("tags", [])]
        entry_id = str(entry.get("id", "")).lower()
        haystack_tokens = set(re.findall(r"[a-z0-9]{3,}", f"{name} {desc} {entry_id} {' '.join(tags)}"))

        score = len(query_tokens & haystack_tokens)
        if score <= 0:
            continue
        candidates.append((score, entry))

    if not candidates:
        return []

    candidates.sort(key=lambda item: (-item[0], item[1].get("id", "")))
    return [entry for _, entry in candidates[:MAX_MATCHED_SKILLS]]


# ---------------------------------------------------------------------------
# Skill body loading
# ---------------------------------------------------------------------------

MAX_SKILL_CHARS = 8000


def load_skill_body(path: Path, max_chars: int = MAX_SKILL_CHARS) -> str:
    """Load the markdown body of a SKILL.md, excluding frontmatter, truncated."""
    parsed = parse_skill_md(path)
    if not parsed:
        return ""
    body = parsed["body"]
    if len(body) > max_chars:
        body = body[:max_chars] + "\n... [TRUNCATED]"
    return body


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def is_skill_stale(manifest_entry: dict[str, Any], catalog: dict[str, Any] | None = None) -> bool:
    """Check if a cached skill is stale compared to the catalog version."""
    if catalog is None:
        catalog = load_skills_catalog()

    skill_id = manifest_entry.get("id", "")
    catalog_skills = {s["id"]: s for s in catalog.get("skills", [])}
    catalog_entry = catalog_skills.get(skill_id)

    if not catalog_entry:
        # Not in catalog — can't determine staleness
        return False

    local_hash = manifest_entry.get("file_hash", "")
    remote_hash = catalog_entry.get("file_hash", "")

    if local_hash and remote_hash and local_hash != remote_hash:
        return True

    local_version = str(manifest_entry.get("version", ""))
    remote_version = str(catalog_entry.get("version", ""))
    if local_version and remote_version and local_version != remote_version:
        return True

    return False


# ---------------------------------------------------------------------------
# Skill fetching (on-demand download)
# ---------------------------------------------------------------------------


def _ensure_repo_cached(repo_config: dict[str, Any]) -> Path | None:
    """Ensure a vendor repo is shallow-cloned in the cache. Returns cache path."""
    repo_id = repo_config["id"]
    url = repo_config["url"]
    cache_path = SKILLS_CACHE_DIR / repo_id

    SKILLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if cache_path.exists() and (cache_path / ".git").exists():
        # Pull latest
        try:
            pull = subprocess.run(
                ["git", "-C", str(cache_path), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if pull.returncode != 0:
                import sys as _sys
                print(f"[skill-loader] git pull failed for '{repo_id}' (using stale cache): {pull.stderr.strip()}", file=_sys.stderr)
        except (subprocess.TimeoutExpired, OSError):
            pass  # non-fatal; work continues with stale local cache
        return cache_path

    # Fresh clone
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(cache_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, OSError):
        return None

    return cache_path


def _find_repo_config(repo_id: str) -> dict[str, Any] | None:
    """Look up a repo config from sources.json by ID."""
    if not SKILLS_SOURCES_PATH.exists():
        return None
    sources = _load_json(SKILLS_SOURCES_PATH)
    for repo in sources.get("repos", []):
        if repo.get("id") == repo_id:
            return repo
    return None


def fetch_skill(skill_id: str) -> Path | None:
    """Fetch a single skill by ID (e.g., 'openai--playwright') to local cache.

    Returns the path to the cached SKILL.md, or None on failure.
    """
    # Parse vendor and skill name from ID
    parts = skill_id.split("--", 1)
    if len(parts) != 2:
        return None
    repo_id, skill_name = parts

    # Check if already cached and fresh
    skill_dir = SKILLS_DIR / skill_id
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        manifest = load_skills_manifest()
        manifest_entry = next((s for s in manifest.get("skills", []) if s["id"] == skill_id), None)
        if manifest_entry and not is_skill_stale(manifest_entry):
            return skill_md

    # Find repo config
    repo_config = _find_repo_config(repo_id)
    if not repo_config:
        return None

    # Ensure repo is cloned
    cache_path = _ensure_repo_cached(repo_config)
    if not cache_path:
        return None

    # Find the skill in the cached repo
    skills_path = repo_config.get("skills_path", "skills")
    source_skill_dir = cache_path / skills_path / skill_name
    source_skill_md = source_skill_dir / "SKILL.md"

    if not source_skill_md.exists():
        return None

    # Copy to skills/<vendor>--<skill-name>/
    skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_skill_md, skill_md)

    # Copy optional subdirectories (scripts/, references/, assets/)
    for subdir_name in ("scripts", "references", "assets"):
        source_sub = source_skill_dir / subdir_name
        dest_sub = skill_dir / subdir_name
        if source_sub.is_dir():
            if dest_sub.exists():
                shutil.rmtree(dest_sub)
            shutil.copytree(source_sub, dest_sub)

    # Update manifest
    _update_manifest_entry(skill_id, skill_md)

    return skill_md


def _update_manifest_entry(skill_id: str, skill_md: Path) -> None:
    """Add or update a single entry in the manifest after fetching."""
    manifest = load_skills_manifest()
    parsed = parse_skill_md(skill_md)
    if not parsed:
        return

    fm = parsed["frontmatter"]
    new_entry = {
        "id": skill_id,
        "name": fm.get("name", skill_id),
        "description": fm.get("description", ""),
        "roles": _coerce_string_list(fm.get("roles", [])),
        "tags": _coerce_string_list(fm.get("tags", [])),
        "version": fm.get("version", ""),
        "source": fm.get("source", ""),
        "path": f"{skill_id}/SKILL.md",
        "file_hash": _file_hash(skill_md),
        "char_count": len(parsed["body"]),
    }

    # Replace existing or append
    skills = manifest.get("skills", [])
    replaced = False
    for i, entry in enumerate(skills):
        if entry.get("id") == skill_id:
            skills[i] = new_entry
            replaced = True
            break
    if not replaced:
        skills.append(new_entry)

    manifest["skills"] = skills
    _write_json(SKILLS_MANIFEST_PATH, manifest)
