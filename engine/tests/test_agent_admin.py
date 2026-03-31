"""Tests for agent specification scaffolding."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.work import agent_admin


class AgentAdminScaffoldTest(unittest.TestCase):
    def test_scaffold_agent_spec_creates_current_style_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            agents_dir = Path(tmpdir)
            with mock.patch.object(agent_admin, "AGENTS_DIR", agents_dir):
                path = agent_admin.scaffold_agent_spec(
                    role="platform-implementation",
                    title=None,
                    purpose="Produce low-code implementation guides.",
                )

            self.assertEqual(path, agents_dir / "platform-implementation.md")
            content = path.read_text(encoding="utf-8")
            self.assertIn("# Platform Implementation Agent Specification", content)
            self.assertIn("## Input Context You May Receive", content)
            self.assertIn("Useful capabilities:", content)
            self.assertIn("keep total JSON output under 512KB", content)
            self.assertIn("You are the platform-implementation agent", content)

    def test_scaffold_agent_spec_rejects_invalid_role_slug(self) -> None:
        with self.assertRaises(SystemExit):
            agent_admin.scaffold_agent_spec(
                role="Platform Implementation",
                title=None,
                purpose="Invalid role test.",
            )


if __name__ == "__main__":
    unittest.main()
