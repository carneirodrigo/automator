"""Unit tests for knowledge_store extract/purge manifest operations."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.work import knowledge_store


def _silent(_: str) -> None:
    pass


class TestExtractProjectKnowledge(unittest.TestCase):
    def _setup(self, tmp: Path) -> tuple[Path, Path]:
        knowledge_dir = tmp / "knowledge"
        knowledge_dir.mkdir()
        manifest = knowledge_dir / "manifest.json"
        return knowledge_dir, manifest

    def _patch_paths(self, knowledge_dir: Path, manifest: Path):
        return mock.patch.multiple(
            knowledge_store,
            KNOWLEDGE_DIR=knowledge_dir,
            KNOWLEDGE_MANIFEST_PATH=manifest,
            REPO_ROOT=knowledge_dir.parent,
        )

    def _write_worker_artifact(self, runtime_dir: Path, output: dict) -> None:
        artifacts = runtime_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "worker_result_1.json").write_text(json.dumps(output))

    def test_extract_writes_manifest_entry(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            knowledge_dir, manifest = self._setup(tmp)
            runtime = tmp / "projects" / "001" / "runtime"
            runtime.mkdir(parents=True)
            self._write_worker_artifact(runtime, {
                "status": "success",
                "summary": "Built a thing.",
                "artifacts": ["delivery/thing.py"],
                "changes_made": [],
                "checks_run": [],
            })

            project = {"project_id": "001", "project_name": "test", "runtime_dir": str(runtime)}
            task_state = {"user_request": "build a thing"}

            with self._patch_paths(knowledge_dir, manifest):
                knowledge_store.extract_project_knowledge(
                    project, task_state, emit_progress=_silent,
                )

            self.assertTrue(manifest.exists())
            data = json.loads(manifest.read_text())
            self.assertEqual(len(data["entries"]), 1)
            self.assertEqual(data["entries"][0]["source_project_id"], "001")

    def test_extract_creates_lock_sidecar(self):
        """Lock should be acquired — a .lock sidecar is created in the knowledge dir."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            knowledge_dir, manifest = self._setup(tmp)
            runtime = tmp / "projects" / "001" / "runtime"
            runtime.mkdir(parents=True)
            self._write_worker_artifact(runtime, {
                "status": "success",
                "summary": "Built a thing.",
            })

            project = {"project_id": "001", "project_name": "test", "runtime_dir": str(runtime)}
            task_state = {"user_request": "build a thing"}

            with self._patch_paths(knowledge_dir, manifest):
                knowledge_store.extract_project_knowledge(
                    project, task_state, emit_progress=_silent,
                )

            lock_sidecar = manifest.with_suffix(manifest.suffix + ".lock")
            self.assertTrue(lock_sidecar.exists(), "lock sidecar should be created by locked()")


class TestPurgeProjectKnowledge(unittest.TestCase):
    def test_purge_removes_owned_entries(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            knowledge_dir = tmp / "knowledge"
            knowledge_dir.mkdir()
            manifest = knowledge_dir / "manifest.json"

            owned_file = tmp / "projects" / "001" / "runtime" / "project-knowledge.json"
            owned_file.parent.mkdir(parents=True)
            owned_file.write_text('{"id": "project-001"}')

            manifest.write_text(json.dumps({
                "version": 1,
                "entries": [
                    {
                        "id": "project-001",
                        "file": "projects/001/runtime/project-knowledge.json",
                        "source_project_id": "001",
                    },
                    {
                        "id": "shared-001",
                        "file": "shared.json",
                        "source_project_id": "shared",
                    },
                ],
            }))

            with mock.patch.multiple(
                knowledge_store,
                KNOWLEDGE_DIR=knowledge_dir,
                KNOWLEDGE_MANIFEST_PATH=manifest,
                REPO_ROOT=tmp,
            ):
                rc = knowledge_store.purge_project_knowledge("001", emit_progress=_silent)

            self.assertEqual(rc, 0)
            self.assertFalse(owned_file.exists())
            data = json.loads(manifest.read_text())
            self.assertEqual(len(data["entries"]), 1)
            self.assertEqual(data["entries"][0]["id"], "shared-001")

    def test_purge_no_manifest_returns_zero(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            knowledge_dir = tmp / "knowledge"
            knowledge_dir.mkdir()
            manifest = knowledge_dir / "manifest.json"

            with mock.patch.multiple(
                knowledge_store,
                KNOWLEDGE_DIR=knowledge_dir,
                KNOWLEDGE_MANIFEST_PATH=manifest,
                REPO_ROOT=tmp,
            ):
                rc = knowledge_store.purge_project_knowledge("001", emit_progress=_silent)

            self.assertEqual(rc, 0)


class TestManifestLockSerialization(unittest.TestCase):
    """Two sequential extract calls must produce two distinct entries (lock is re-entrant-safe)."""

    def test_two_extracts_both_persist(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            knowledge_dir = tmp / "knowledge"
            knowledge_dir.mkdir()
            manifest = knowledge_dir / "manifest.json"

            for pid in ("001", "002"):
                runtime = tmp / "projects" / pid / "runtime"
                runtime.mkdir(parents=True)
                (runtime / "artifacts").mkdir()
                (runtime / "artifacts" / "worker_result_1.json").write_text(json.dumps({
                    "status": "success",
                    "summary": f"project {pid}",
                }))

                with mock.patch.multiple(
                    knowledge_store,
                    KNOWLEDGE_DIR=knowledge_dir,
                    KNOWLEDGE_MANIFEST_PATH=manifest,
                    REPO_ROOT=tmp,
                ):
                    knowledge_store.extract_project_knowledge(
                        {"project_id": pid, "project_name": f"proj-{pid}", "runtime_dir": str(runtime)},
                        {"user_request": "r"},
                        emit_progress=_silent,
                    )

            data = json.loads(manifest.read_text())
            ids = sorted(e["source_project_id"] for e in data["entries"])
            self.assertEqual(ids, ["001", "002"])


if __name__ == "__main__":
    unittest.main()
