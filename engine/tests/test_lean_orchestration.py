"""Lean orchestration lifecycle tests — zero LLM tokens.

Covers:
1. New project: worker → review → pending_resolution(user_acceptance)
2. Continue: user accept → project closed
3. Continue: user feedback → rework cycle
4. Review fail → one rework cycle → final review pass
5. Review fail → rework → final review still fails → review_blocked
6. Worker needs_research → research → worker re-run → review
7. Worker failure → return 1
8. execute_agents=False → manual mode, no agents called
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

from engine.work.orchestrator import (
    _next_project_id,
    _project_name_from_request,
    configure_orchestrator_environment,
    run_orchestration,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent_result(output: dict[str, Any], status: str = "success") -> dict[str, Any]:
    return {"status": status, "output": output}


def _worker_pass(summary: str = "Done.") -> dict[str, Any]:
    return _make_agent_result({
        "status": "success",
        "summary": summary,
        "changes_made": [],
        "checks_run": [],
        "artifacts": [],
        "open_issues": [],
        "needs_research": False,
        "needs_user_input": False,
    })


def _worker_needs_research(questions: list[str]) -> dict[str, Any]:
    return _make_agent_result({
        "status": "success",
        "summary": "Need external facts before proceeding.",
        "changes_made": [],
        "checks_run": [],
        "artifacts": [],
        "open_issues": questions,
        "needs_research": True,
        "needs_user_input": False,
    })


def _review_pass(summary: str = "Looks good.") -> dict[str, Any]:
    return _make_agent_result({
        "status": "pass",
        "summary": summary,
        "findings": [],
        "checks_run": [],
        "blocking": [],
    })


def _review_fail(rework: list[str] | None = None) -> dict[str, Any]:
    rework = rework or ["Fix the obvious error."]
    return _make_agent_result({
        "status": "fail",
        "summary": "Issues found.",
        "findings": ["Bad output"],
        "checks_run": [],
        "blocking": rework,
    })


def _research_result() -> dict[str, Any]:
    return _make_agent_result({
        "status": "success",
        "summary": "Found the relevant facts.",
        "facts": ["API uses OAuth2 (source: docs)"],
        "sources": ["https://example.com/api"],
        "open_risks": [],
        "implementation_notes": ["Use client_credentials grant"],
    })


def _agent_failed(error: str = "timeout") -> dict[str, Any]:
    return {"status": "failed", "error": error, "error_category": "timeout", "output": {}}


def _make_project(pid: str = "001") -> dict[str, Any]:
    return {
        "project_id": pid,
        "project_name": "Test Project",
        "project_root": f"/tmp/projects/{pid}",
        "runtime_dir": f"/tmp/projects/{pid}/runtime",
    }


def _make_task_state() -> dict[str, Any]:
    return {}


def _configure(
    run_agent_fn: Any,
    task_state_holder: list[dict],
    *,
    project: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Wire up orchestrator _ENV with mocks. Returns (active_project, task_state_path)."""
    active_project = project or _make_project()
    task_state_path = Path(f"/tmp/task_{id(task_state_holder)}.json")

    def load_json_mock(path: Any) -> Any:
        if path == Path("/fake/registry.json"):
            return registry or {"projects": []}
        # Return the latest task_state from holder
        return task_state_holder[0] if task_state_holder else {}

    def write_json_mock(path: Any, data: Any) -> None:
        task_state_holder[0] = data

    def persist_result_mock(proj: Any, role: str, output: Any) -> str:
        return f"/tmp/artifacts/{role}_result.json"

    def bootstrap_mock(decision: dict) -> dict:
        return active_project

    configure_orchestrator_environment(
        emit_progress=MagicMock(),
        run_agent_with_capabilities=run_agent_fn,
        persist_result=persist_result_mock,
        write_json=write_json_mock,
        load_json=load_json_mock,
        now_iso=lambda: "2026-01-01T00:00:00+00:00",
        bootstrap_project=bootstrap_mock,
        fork_project=bootstrap_mock,
        store_secrets=MagicMock(),
        ingest_input_files=MagicMock(return_value=[]),
        save_last_active_project=MagicMock(),
        _get_project_input_paths=MagicMock(return_value=[]),
        REGISTRY_PATH=Path("/fake/registry.json"),
        extract_project_knowledge=MagicMock(),
    )

    return active_project, task_state_path


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------

class TestNextProjectId(unittest.TestCase):
    def test_empty_registry(self):
        self.assertEqual(_next_project_id({}), "001")

    def test_increments_from_existing(self):
        registry = {"projects": [{"project_id": "001"}, {"project_id": "003"}]}
        self.assertEqual(_next_project_id(registry), "004")

    def test_ignores_non_numeric_ids(self):
        registry = {"projects": [{"project_id": "abc"}, {"project_id": "002"}]}
        self.assertEqual(_next_project_id(registry), "003")

    def test_zero_pads_to_three_digits(self):
        registry = {"projects": [{"project_id": "009"}]}
        self.assertEqual(_next_project_id(registry), "010")


class TestProjectNameFromRequest(unittest.TestCase):
    def test_capitalises_first_words(self):
        self.assertEqual(
            _project_name_from_request("build a script to fetch github issues"),
            "Build A Script To Fetch Github",
        )

    def test_strips_new_project_framing(self):
        self.assertEqual(
            _project_name_from_request("start new project. Task: write a fibonacci script"),
            "Write A Fibonacci Script",
        )

    def test_strips_fork_framing(self):
        name = _project_name_from_request("fork my-project into a new project. Task: add retry logic")
        self.assertNotIn("fork", name.lower())
        self.assertIn("Retry", name)

    def test_strips_punctuation(self):
        name = _project_name_from_request("create: a hello-world script!")
        self.assertNotIn(":", name)

    def test_empty_request(self):
        self.assertEqual(_project_name_from_request(""), "Untitled Project")


# ---------------------------------------------------------------------------
# Lifecycle: new project → worker → review → user_acceptance
# ---------------------------------------------------------------------------

class TestNewProjectHappyPath(unittest.TestCase):
    def setUp(self):
        self.task_state = [_make_task_state()]
        self.run_agent = MagicMock(side_effect=[_worker_pass(), _review_pass()])
        self.project, self.ts_path = _configure(
            self.run_agent, self.task_state, project=_make_project("001"),
        )

    def test_returns_zero(self):
        rc = run_orchestration(
            request="write hello world in python",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 0)

    def test_worker_then_review_called(self):
        run_orchestration(
            request="write hello world in python",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        calls = self.run_agent.call_args_list
        self.assertEqual(calls[0][0][0], "worker")
        self.assertEqual(calls[1][0][0], "review")
        self.assertEqual(len(calls), 2)

    def test_pending_resolution_set_to_user_acceptance(self):
        run_orchestration(
            request="write hello world in python",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        state = self.task_state[0]
        self.assertIn("pending_resolution", state)
        self.assertEqual(state["pending_resolution"]["type"], "user_acceptance")

    def test_completed_steps_recorded(self):
        run_orchestration(
            request="write hello world in python",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        roles = [s["agent"] for s in self.task_state[0].get("completed_steps", [])]
        self.assertIn("worker", roles)
        self.assertIn("review", roles)


# ---------------------------------------------------------------------------
# Lifecycle: continue with user acceptance → returns 0
# ---------------------------------------------------------------------------

class TestUserAcceptance(unittest.TestCase):
    def setUp(self):
        self.task_state = [{
            "pending_resolution": {
                "type": "user_acceptance",
                "message": "Work delivered.",
                "original_request": "write hello world",
            }
        }]
        self.run_agent = MagicMock()
        self.project, self.ts_path = _configure(
            self.run_agent, self.task_state, project=_make_project("001"),
        )

    def _run(self, user_response: str) -> int:
        return run_orchestration(
            request=user_response,
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )

    def test_yes_returns_zero_no_agents(self):
        rc = self._run("yes")
        self.assertEqual(rc, 0)
        self.run_agent.assert_not_called()

    def test_lgtm_accepted(self):
        self.assertEqual(self._run("lgtm"), 0)

    def test_ship_it_accepted(self):
        self.assertEqual(self._run("ship it"), 0)

    def test_rejection_triggers_rework(self):
        self.task_state = [{
            "pending_resolution": {
                "type": "user_acceptance",
                "message": "Work delivered.",
                "original_request": "write hello world",
            }
        }]
        self.run_agent = MagicMock(side_effect=[_worker_pass(), _review_pass()])
        self.project, self.ts_path = _configure(
            self.run_agent, self.task_state, project=_make_project("001"),
        )
        rc = self._run("fix the indentation please")
        self.assertEqual(rc, 0)
        self.assertEqual(self.run_agent.call_args_list[0][0][0], "worker")


# ---------------------------------------------------------------------------
# Lifecycle: review fail → one rework cycle → pass
# ---------------------------------------------------------------------------

class TestReworkCycle(unittest.TestCase):
    def setUp(self):
        self.task_state = [_make_task_state()]
        # worker → review(fail) → worker(rework) → review(pass)
        self.run_agent = MagicMock(side_effect=[
            _worker_pass("Initial attempt."),
            _review_fail(["Fix the obvious error."]),
            _worker_pass("Fixed."),
            _review_pass("All good now."),
        ])
        self.project, self.ts_path = _configure(
            self.run_agent, self.task_state, project=_make_project("001"),
        )

    def test_returns_zero_after_rework(self):
        rc = run_orchestration(
            request="write hello world in python",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 0)

    def test_four_agent_calls(self):
        run_orchestration(
            request="write hello world in python",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        roles = [c[0][0] for c in self.run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "review", "worker", "review"])

    def test_rework_task_contains_feedback(self):
        run_orchestration(
            request="write hello world in python",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        rework_task = self.run_agent.call_args_list[2][0][1]
        self.assertIn("Fix the obvious error", rework_task)

    def test_pending_resolution_user_acceptance_set(self):
        run_orchestration(
            request="write hello world in python",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(
            self.task_state[0]["pending_resolution"]["type"], "user_acceptance"
        )


# ---------------------------------------------------------------------------
# Lifecycle: review fail → rework → final review still fails → blocked
# ---------------------------------------------------------------------------

class TestReworkStillFails(unittest.TestCase):
    def setUp(self):
        self.task_state = [_make_task_state()]
        self.run_agent = MagicMock(side_effect=[
            _worker_pass(),
            _review_fail(["Critical bug remains."]),
            _worker_pass("Attempted fix."),
            _review_fail(["Still broken."]),
        ])
        self.project, self.ts_path = _configure(
            self.run_agent, self.task_state, project=_make_project("001"),
        )

    def test_returns_one(self):
        rc = run_orchestration(
            request="write hello world in python",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 1)

    def test_pending_resolution_review_blocked(self):
        run_orchestration(
            request="write hello world in python",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(
            self.task_state[0]["pending_resolution"]["type"], "review_blocked"
        )


# ---------------------------------------------------------------------------
# Worker failure propagation
# ---------------------------------------------------------------------------

class TestWorkerFailure(unittest.TestCase):
    def setUp(self):
        self.task_state = [_make_task_state()]
        self.run_agent = MagicMock(return_value=_agent_failed("spawn error"))
        self.project, self.ts_path = _configure(
            self.run_agent, self.task_state, project=_make_project("001"),
        )

    def test_returns_one_on_worker_failure(self):
        rc = run_orchestration(
            request="do something",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 1)

    def test_review_not_called_on_worker_failure(self):
        run_orchestration(
            request="do something",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        roles = [c[0][0] for c in self.run_agent.call_args_list]
        self.assertNotIn("review", roles)


# ---------------------------------------------------------------------------
# execute_agents=False → manual mode
# ---------------------------------------------------------------------------

class TestManualMode(unittest.TestCase):
    def setUp(self):
        self.task_state = [_make_task_state()]
        self.run_agent = MagicMock()
        self.project, self.ts_path = _configure(
            self.run_agent, self.task_state, project=_make_project("001"),
        )

    def test_no_agents_called_in_manual_mode(self):
        run_orchestration(
            request="write hello world",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=False,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.run_agent.assert_not_called()

    def test_returns_zero_in_manual_mode(self):
        rc = run_orchestration(
            request="write hello world",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=False,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# New project bootstrap (active_project is None)
# ---------------------------------------------------------------------------

class TestNewProjectBootstrap(unittest.TestCase):
    def setUp(self):
        self.task_state = [_make_task_state()]
        self.run_agent = MagicMock(side_effect=[_worker_pass(), _review_pass()])
        self.bootstrap_mock = MagicMock(return_value=_make_project("001"))
        self.project, self.ts_path = _configure(
            self.run_agent, self.task_state, project=_make_project("001"),
        )
        # Override bootstrap in _ENV
        configure_orchestrator_environment(bootstrap_project=self.bootstrap_mock)

    def test_bootstrap_called_when_no_active_project(self):
        run_orchestration(
            request="write hello world",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=None,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.bootstrap_mock.assert_called_once()
        decision = self.bootstrap_mock.call_args[0][0]
        self.assertIn("project_id", decision)
        self.assertIn("project_name", decision)
        self.assertIn("description", decision)

    def test_secrets_stored_when_pending(self):
        secrets_mock = MagicMock()
        configure_orchestrator_environment(store_secrets=secrets_mock)
        run_orchestration(
            request="write hello world",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=None,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[{"key": "API_KEY", "value": "secret"}],
            pending_input_files=False,
        )
        secrets_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Research branch (needs_research=True)
# ---------------------------------------------------------------------------

class TestResearchBranch(unittest.TestCase):
    """When worker signals needs_research, orchestrator should run research
    then re-run worker with the research artifact in inputs."""

    def setUp(self):
        self.task_state = [_make_task_state()]
        # worker(needs_research) → research → worker(pass) → review(pass)
        self.run_agent = MagicMock(side_effect=[
            _worker_needs_research(["How does the Qualys API authenticate?"]),
            _research_result(),
            _worker_pass("Implemented with OAuth2."),
            _review_pass("All good."),
        ])
        self.project, self.ts_path = _configure(
            self.run_agent, self.task_state, project=_make_project("001"),
        )

    def test_returns_zero(self):
        rc = run_orchestration(
            request="fetch Qualys vulnerabilities",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 0)

    def test_research_called_between_workers(self):
        run_orchestration(
            request="fetch Qualys vulnerabilities",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        roles = [c[0][0] for c in self.run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "research", "worker", "review"])

    def test_research_artifact_in_second_worker_inputs(self):
        run_orchestration(
            request="fetch Qualys vulnerabilities",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=self.project,
            task_state=self.task_state[0],
            task_state_path=self.ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        # Third call is the second worker call; inputs arg is index 3 (positional)
        second_worker_inputs = self.run_agent.call_args_list[2][0][3]
        research_artifact = "/tmp/artifacts/research_result.json"
        self.assertIn(research_artifact, second_worker_inputs)


if __name__ == "__main__":
    unittest.main()
