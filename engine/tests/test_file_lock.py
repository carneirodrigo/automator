"""Tests for engine.work.file_lock advisory locking."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from engine.work.file_lock import LockUnavailable, locked


class TestFileLock(unittest.TestCase):
    def test_lock_creates_sidecar(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "registry.json"
            path.write_text("{}")
            with locked(path):
                self.assertTrue(path.with_suffix(".json.lock").exists())

    def test_sequential_acquire_releases(self):
        """Two sequential acquires on the same path should both succeed."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "registry.json"
            path.write_text("{}")
            with locked(path):
                pass
            with locked(path):
                pass  # reached => prior lock was released

    def test_non_blocking_raises_on_contention(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "registry.json"
            path.write_text("{}")
            with locked(path):
                with self.assertRaises(LockUnavailable):
                    with locked(path, non_blocking=True):
                        pass

    def test_blocking_waits_for_release(self):
        """A blocking lock should wait for the prior holder to release."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "active_task.json"
            path.write_text("{}")

            acquired_at: list[float] = []
            release_event = threading.Event()

            def holder() -> None:
                with locked(path):
                    release_event.wait(timeout=2.0)

            t = threading.Thread(target=holder)
            t.start()
            time.sleep(0.05)  # let holder acquire first

            def waiter() -> None:
                with locked(path):
                    acquired_at.append(time.time())

            w = threading.Thread(target=waiter)
            w.start()
            time.sleep(0.05)
            self.assertEqual(acquired_at, [])  # blocked
            release_event.set()
            t.join(timeout=2.0)
            w.join(timeout=2.0)
            self.assertEqual(len(acquired_at), 1)

    def test_parent_dir_created_lazily(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nested" / "dir" / "registry.json"
            # file does not exist yet — lock should still work
            with locked(path):
                self.assertTrue(path.parent.exists())


if __name__ == "__main__":
    unittest.main()
