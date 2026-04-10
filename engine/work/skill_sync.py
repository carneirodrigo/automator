#!/usr/bin/env python3
"""Agent Skills sync CLI: build catalog from vendor repos, fetch individual skills, check freshness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure engine.* imports work from any working directory
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from engine.work.repo_paths import (
    SKILLS_CACHE_DIR,
    SKILLS_CATALOG_PATH,
    SKILLS_DIR,
    SKILLS_SOURCES_PATH,
)
from engine.work.skill_loader import (
    _coerce_string_list,
    _ensure_repo_cached,
    _load_json,
    _write_json,
    fetch_skill,
    is_skill_stale,
    load_skills_catalog,
    load_skills_manifest,
    parse_skill_md,
    rebuild_skills_manifest,
    _file_hash,
    role_heuristic,
)


# ---------------------------------------------------------------------------
# Catalog building
# ---------------------------------------------------------------------------


def build_catalog(repo_filter: str | None = None, *, write_catalog: bool = True) -> dict[str, list[dict]]:
    """Build catalog.json by scanning vendor repos for SKILL.md frontmatter.

    Returns {"added": [...], "updated": [...], "unchanged": [...]}.
    """
    if not SKILLS_SOURCES_PATH.exists():
        print(f"Error: {SKILLS_SOURCES_PATH} not found.", file=sys.stderr)
        return {"added": [], "updated": [], "unchanged": []}

    sources = _load_json(SKILLS_SOURCES_PATH)
    repos = sources.get("repos", [])

    if repo_filter:
        repos = [r for r in repos if r.get("id") == repo_filter]
        if not repos:
            print(f"Error: repo '{repo_filter}' not found in sources.json.", file=sys.stderr)
            return {"added": [], "updated": [], "unchanged": []}

    # Load existing catalog to merge
    existing_catalog = load_skills_catalog()
    existing_by_id: dict[str, dict] = {s["id"]: s for s in existing_catalog.get("skills", [])}

    stats: dict[str, list[dict]] = {"added": [], "updated": [], "unchanged": []}

    for repo_config in repos:
        repo_id = repo_config["id"]
        print(f"Syncing catalog from {repo_config['url']} ...", file=sys.stderr)

        cache_path = _ensure_repo_cached(repo_config)
        if not cache_path:
            print(f"  Failed to clone/pull {repo_id}.", file=sys.stderr)
            continue

        skills_path = repo_config.get("skills_path", "skills")
        skills_root = cache_path / skills_path

        if not skills_root.is_dir():
            print(f"  Skills path '{skills_path}' not found in {repo_id}.", file=sys.stderr)
            continue

        count = 0
        for skill_dir in sorted(skills_root.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue

            parsed = parse_skill_md(skill_md)
            if not parsed:
                continue

            fm = parsed["frontmatter"]
            skill_id = f"{repo_id}--{skill_dir.name}"

            entry = {
                "id": skill_id,
                "name": fm.get("name", skill_dir.name),
                "description": fm.get("description", ""),
                "tags": _coerce_string_list(fm.get("tags", [])),
                "roles": role_heuristic({
                    "name": fm.get("name", skill_dir.name),
                    "description": fm.get("description", ""),
                    "tags": fm.get("tags", []),
                    "roles": fm.get("roles", []),
                }),
                "version": fm.get("version", ""),
                "repo_id": repo_id,
                "repo_url": repo_config["url"],
                "skill_path": f"{skills_path}/{skill_dir.name}",
                "file_hash": _file_hash(skill_md),
            }

            old = existing_by_id.get(skill_id)
            if old and old.get("file_hash") == entry["file_hash"]:
                stats["unchanged"].append(entry)
            elif old:
                stats["updated"].append(entry)
            else:
                stats["added"].append(entry)

            existing_by_id[skill_id] = entry
            count += 1

        print(f"  Found {count} skills in {repo_id}.", file=sys.stderr)

    # Write merged catalog unless this is a dry run.
    catalog = {"version": 1, "skills": list(existing_by_id.values())}
    if write_catalog:
        _write_json(SKILLS_CATALOG_PATH, catalog)
    print(
        f"Catalog: {len(stats['added'])} added, {len(stats['updated'])} updated, "
        f"{len(stats['unchanged'])} unchanged. Total: {len(catalog['skills'])}.",
        file=sys.stderr,
    )
    return stats


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------


def check_freshness() -> list[dict]:
    """Report which cached skills are stale compared to the catalog."""
    manifest = load_skills_manifest()
    catalog = load_skills_catalog()
    stale: list[dict] = []

    for entry in manifest.get("skills", []):
        if is_skill_stale(entry, catalog):
            stale.append(entry)

    return stale


# ---------------------------------------------------------------------------
# List cached skills
# ---------------------------------------------------------------------------


def list_cached() -> list[dict]:
    """List all locally cached skills."""
    manifest = load_skills_manifest()
    return manifest.get("skills", [])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent Skills sync: catalog refresh, skill fetch, freshness check."
    )
    parser.add_argument("--catalog", action="store_true", help="Refresh catalog from vendor repos")
    parser.add_argument("--repo", type=str, default=None, help="Filter to a single repo ID (with --catalog)")
    parser.add_argument("--skill", type=str, default=None, help="Fetch a single skill by ID (e.g., openai--playwright)")
    parser.add_argument("--check", action="store_true", help="Check freshness of cached skills")
    parser.add_argument("--list", action="store_true", help="List cached skills")
    parser.add_argument("--rebuild-manifest", action="store_true", help="Rebuild manifest from local cache")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without writing (with --catalog)")

    args = parser.parse_args(argv)

    if not any([args.catalog, args.skill, args.check, args.list, args.rebuild_manifest]):
        parser.print_help()
        return 1

    if args.catalog:
        if args.dry_run:
            print("Dry-run mode: would refresh catalog.", file=sys.stderr)
        stats = build_catalog(repo_filter=args.repo, write_catalog=not args.dry_run)
        if args.dry_run:
            # Show what would change without writing
            for action in ("added", "updated"):
                for entry in stats.get(action, []):
                    print(f"  {action}: {entry['id']} — {entry.get('description', '')[:80]}")

    elif args.skill:
        print(f"Fetching skill: {args.skill} ...", file=sys.stderr)
        result = fetch_skill(args.skill)
        if result:
            print(f"OK: {result}", file=sys.stderr)
        else:
            print(f"Failed to fetch skill: {args.skill}", file=sys.stderr)
            return 1

    elif args.check:
        stale = check_freshness()
        if not stale:
            print("All cached skills are up to date.", file=sys.stderr)
        else:
            print(f"{len(stale)} stale skill(s):", file=sys.stderr)
            for entry in stale:
                print(f"  {entry['id']} (version: {entry.get('version', '?')})")

    elif args.list:
        cached = list_cached()
        if not cached:
            print("No cached skills.", file=sys.stderr)
        else:
            print(f"{len(cached)} cached skill(s):", file=sys.stderr)
            for entry in cached:
                roles = ", ".join(entry.get("roles", [])) or "(inferred)"
                print(f"  {entry['id']}  roles={roles}  v={entry.get('version', '?')}", file=sys.stderr)

    elif args.rebuild_manifest:
        manifest = rebuild_skills_manifest()
        count = len(manifest.get("skills", []))
        print(f"Manifest rebuilt: {count} skill(s).", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
