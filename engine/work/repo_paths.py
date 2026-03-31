"""Helpers for keeping all managed paths inside the current repository tree."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECTS_DIR = REPO_ROOT / "projects"
RUNTIME_PROJECTS_DIR = PROJECTS_DIR / "runtime"
DELIVERY_DIR = PROJECTS_DIR / "delivery"
SECRETS_PROJECTS_DIR = PROJECTS_DIR / "secrets"
REGISTRY_PATH = PROJECTS_DIR / "registry.json"
INPUTS_DIR = REPO_ROOT / "inputs"
SKILLS_DIR = REPO_ROOT / "skills"
SKILLS_CATALOG_PATH = SKILLS_DIR / "catalog.json"
SKILLS_MANIFEST_PATH = SKILLS_DIR / "manifest.json"
SKILLS_SOURCES_PATH = SKILLS_DIR / "sources.json"
SKILLS_CACHE_DIR = SKILLS_DIR / ".cache"
DEBUG_DIR = REPO_ROOT / "debug"
DEBUG_TRACKER_PATH = DEBUG_DIR / "tracker.json"
DEBUG_ISSUES_DIR = DEBUG_DIR / "issues"
CONFIG_DIR = REPO_ROOT / "config"
BACKENDS_CONFIG_PATH = CONFIG_DIR / "backends.json"
SECRETS_CONFIG_PATH = CONFIG_DIR / "secrets.json"


def resolve_repo_path(path_value: str | Path) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def ensure_within_repo(path_value: str | Path, label: str) -> Path:
    path = resolve_repo_path(path_value)
    try:
        path.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the repository tree: {path}") from exc
    return path


def managed_project_root(project_id: str) -> Path:
    return (PROJECTS_DIR / project_id / "delivery").resolve()


def managed_project_runtime_dir(project_id: str) -> Path:
    return (PROJECTS_DIR / project_id / "runtime").resolve()


def managed_project_secrets_dir(project_id: str) -> Path:
    return (PROJECTS_DIR / project_id / "secrets").resolve()


def project_secrets_path(project_id: str) -> Path:
    return managed_project_secrets_dir(project_id) / "secrets.json"


def project_inputs_path(project_id: str) -> Path:
    return managed_project_runtime_dir(project_id) / "inputs"


def validate_project_paths(config: dict[str, object]) -> tuple[Path, Path]:
    project_root = ensure_within_repo(str(config["project_root"]), "project_root")
    runtime_dir = ensure_within_repo(str(config["runtime_dir"]), "runtime_dir")
    return project_root, runtime_dir
