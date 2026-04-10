"""Agent specification administration helpers and CLI."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.work.repo_paths import REPO_ROOT

AGENTS_DIR = REPO_ROOT / "agents"


def _slug_to_title(role: str) -> str:
    return " ".join(part.capitalize() for part in role.split("-"))


def _safe_role_filename(role: str) -> str:
    normalized = role.strip().lower()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
        raise SystemExit("Role names must be lowercase slug strings like 'platform-implementation'.")
    return normalized


def list_agents() -> list[dict[str, str]]:
    agents: list[dict[str, str]] = []
    for path in sorted(AGENTS_DIR.glob("*.md")):
        title = path.stem
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            first_line = lines[0].strip() if lines else ""
            if first_line.startswith("# "):
                title = first_line[2:].strip()
        except OSError as exc:
            print(f"[agent_admin] cannot read {path}: {exc}", file=sys.stderr)
        role = path.stem
        agents.append({
            "role": role,
            "title": title,
            "path": str(path.relative_to(REPO_ROOT)),
        })
    return agents


def scaffold_agent_spec(
    *,
    role: str,
    title: str | None,
    purpose: str,
    force: bool = False,
) -> Path:
    safe_role = _safe_role_filename(role)
    path = AGENTS_DIR / f"{safe_role}.md"
    if path.exists() and not force:
        raise SystemExit(f"Agent spec already exists: {path}")

    agent_title = title.strip() if title else f"{_slug_to_title(safe_role)} Agent"
    content = f"""# {agent_title} Specification

## Purpose

{purpose.strip()}

## Core Responsibilities

- describe the role's primary responsibilities here
- explain what this agent should produce for downstream stages
- define the types of tasks this agent owns

## Scope Rules

- stay within the assigned role boundary
- do not perform other agent roles unless the user explicitly authorizes bypass
- report blockers instead of silently guessing
- call out uncertainty, missing research, or unresolved dependencies explicitly

## Input Context You May Receive

The engine may provide:

- validated requirements or prior agent artifacts
- user-provided input files from the project inbox
- matched knowledge-base or skill context selected upstream

Use the provided context as working evidence instead of falling back to generic assumptions.

## Runtime Capabilities

Document which engine capabilities this agent is expected to use. See [capability-requests.md](../docs/capability-requests.md).

Useful capabilities:
- `read_file` to inspect relevant project files, input files, or prior artifacts
- `load_artifact` when this role depends on upstream agent outputs
- `persist_artifact` if this role needs to save a generated artifact for downstream use

## Required Output

Output must be a JSON object with at minimum a `summary` field (one sentence) and a `status` field (`"success"` or `"fail"`).

- populate `summary` with a one-sentence description of what was done
- populate `technical_data.result` with the role-specific result shape once it is added
- keep total JSON output under 512KB and persist large artifacts to disk instead of inlining them

## Execution Prompt Template

```text
You are the {safe_role} agent in a specialized multi-agent orchestration system.

Your job is to:
- perform the role-specific work assigned to {safe_role}
- stay within scope
- report blockers honestly
- return output using the shared message schema
```
"""
    path.write_text(content, encoding="utf-8")
    return path


def cmd_list(_: argparse.Namespace) -> int:
    for entry in list_agents():
        print(f"{entry['role']}\t{entry['title']}\t{entry['path']}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    path = scaffold_agent_spec(
        role=args.role,
        title=args.title,
        purpose=args.purpose,
        force=args.force,
    )
    print(path.relative_to(REPO_ROOT))
    print("Next steps: add the role to _AGENT_OUTPUT_TEMPLATES in engine_runtime.py and add tests.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent specification administration.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List available agent specs.")
    list_parser.set_defaults(func=cmd_list)

    add_parser = subparsers.add_parser("add", help="Create a new agent spec skeleton.")
    add_parser.add_argument("role", help="New agent role slug, e.g. platform-implementation")
    add_parser.add_argument("--title", help="Optional human-readable title for the spec heading")
    add_parser.add_argument("--purpose", required=True, help="Short purpose statement for the new role")
    add_parser.add_argument("--force", action="store_true", help="Overwrite an existing spec file")
    add_parser.set_defaults(func=cmd_add)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
