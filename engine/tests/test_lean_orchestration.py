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
9. Transient error retry with backoff
10. Worker output validation gate
11. Review default to fail on missing status
12. Stage resume from prior worker output
13. Task planning heuristic
14. Review enforcement: pass without checks → demoted to fail
15. Delivery file verification
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

from engine.work.engine_runtime import _build_delivery_context
from engine.work.orchestrator import (
    MAX_RESEARCH_CYCLES,
    _capability_rounds_for_task,
    _classify_blockers,
    _needs_planning,
    _next_project_id,
    _project_name_from_request,
    _run_with_retry,
    _validate_agent_output,
    _verify_delivery_files,
    configure_orchestrator_environment,
    run_orchestration,
)

# Patch out retry sleep globally for all orchestration tests.
_original_sleep = None

def setUpModule():
    global _original_sleep
    import engine.work.orchestrator as _orch
    _original_sleep = _orch._time_module.sleep
    _orch._time_module.sleep = lambda _: None

def tearDownModule():
    import engine.work.orchestrator as _orch
    _orch._time_module.sleep = _original_sleep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent_result(output: dict[str, Any], status: str = "success") -> dict[str, Any]:
    # capability_rounds_used defaults to 1 so test mocks look like a real
    # agent that ran at least one capability before producing its result.
    # Tests that specifically need a zero-round mock can override the field.
    return {"status": status, "output": output, "capability_rounds_used": 1}


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
        "checks_run": [{"check": "syntax check", "command": "python3 -c 'import script'", "result": "passed", "output": ""}],
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


def _agent_failed(error: str = "timeout", category: str = "timeout") -> dict[str, Any]:
    return {"status": "failed", "error": error, "error_category": category, "output": {}}


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
        # Use a non-retriable error category so no retry delay.
        self.run_agent = MagicMock(return_value=_agent_failed("spawn error", category="binary_not_found"))
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


# ---------------------------------------------------------------------------
# Transient error retry
# ---------------------------------------------------------------------------

class TestTransientRetry(unittest.TestCase):
    """Retriable errors (timeout, rate_limited, provider_error) are retried up to 2 extra times."""

    def test_retry_succeeds_on_second_attempt(self):
        task_state = [_make_task_state()]
        run_agent = MagicMock(side_effect=[
            _agent_failed("rate limit hit", category="rate_limited"),
            _worker_pass("Done after retry."),
            _review_pass(),
        ])
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))
        rc = run_orchestration(
            request="build something",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=project,
            task_state=task_state[0],
            task_state_path=ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        # Worker called twice (1 fail + 1 success), then review once
        roles = [c[0][0] for c in run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "worker", "review"])

    def test_non_retriable_error_fails_immediately(self):
        task_state = [_make_task_state()]
        run_agent = MagicMock(return_value=_agent_failed("binary missing", category="binary_not_found"))
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))
        rc = run_orchestration(
            request="build something",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=project,
            task_state=task_state[0],
            task_state_path=ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 1)
        # Only called once — no retries for non-retriable errors
        self.assertEqual(run_agent.call_count, 1)


# ---------------------------------------------------------------------------
# Worker output validation gate
# ---------------------------------------------------------------------------

class TestWorkerOutputValidation(unittest.TestCase):
    """Worker returning empty output or missing summary should fail the pipeline."""

    def test_empty_worker_output_fails(self):
        task_state = [_make_task_state()]
        run_agent = MagicMock(return_value=_make_agent_result({}))
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))
        rc = run_orchestration(
            request="build something",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=project,
            task_state=task_state[0],
            task_state_path=ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 1)

    def test_worker_missing_summary_fails(self):
        task_state = [_make_task_state()]
        # Output has fields but no summary
        run_agent = MagicMock(return_value=_make_agent_result({
            "status": "success",
            "changes_made": ["something"],
        }))
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))
        rc = run_orchestration(
            request="build something",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=project,
            task_state=task_state[0],
            task_state_path=ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# Review defaults to fail when status field is missing
# ---------------------------------------------------------------------------

class TestReviewDefaultFail(unittest.TestCase):
    """If review output has no status field, it should default to fail (not pass)."""

    def test_missing_review_status_triggers_rework(self):
        task_state = [_make_task_state()]
        # Review output with no status field — should be treated as fail.
        review_no_status = _make_agent_result({
            "summary": "Review done but no status.",
            "findings": [],
            "blocking": ["Missing status"],
        })
        run_agent = MagicMock(side_effect=[
            _worker_pass(),
            review_no_status,
            _worker_pass("Fixed."),
            _review_pass(),
        ])
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))
        rc = run_orchestration(
            request="build something",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=project,
            task_state=task_state[0],
            task_state_path=ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        # Should have gone through: worker → review(fail) → rework → final review(pass)
        roles = [c[0][0] for c in run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "review", "worker", "review"])


# ---------------------------------------------------------------------------
# Task planning heuristic
# ---------------------------------------------------------------------------

class TestNeedsPlanning(unittest.TestCase):
    """Heuristic correctly separates simple tasks from complex ones."""

    def test_simple_task_skips_planning(self):
        self.assertFalse(_needs_planning("write a hello world script"))

    def test_short_task_skips_planning(self):
        self.assertFalse(_needs_planning("add retry logic"))

    def test_rework_skips_planning(self):
        self.assertFalse(_needs_planning("Rework required. Fix the indentation."))

    def test_complex_task_triggers_planning(self):
        self.assertTrue(_needs_planning(
            "build a script that authenticates to Microsoft Graph API "
            "and then fetches all SharePoint sites and stores the results"
        ))

    def test_multi_system_triggers_planning(self):
        self.assertTrue(_needs_planning(
            "connect to Azure Defender and Qualys, pull vulnerability data, "
            "and deploy the results to a SharePoint list"
        ))


# ---------------------------------------------------------------------------
# Delivery file verification
# ---------------------------------------------------------------------------

class TestVerifyDeliveryFiles(unittest.TestCase):
    def test_empty_output_returns_no_missing(self):
        self.assertEqual(_verify_delivery_files({}, "/tmp"), [])

    def test_existing_file_not_flagged(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as f:
            f.write(b"print('hello')")
            path = f.name
        try:
            result = _verify_delivery_files({"artifacts": [path]}, "/tmp")
            self.assertEqual(result, [])
        finally:
            os.unlink(path)

    def test_relative_path_from_cwd(self):
        """Relative paths should resolve from cwd (repo root), not project_root."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".py", dir=os.getcwd()
        ) as f:
            f.write(b"print('hello')")
            relpath = os.path.basename(f.name)
        try:
            result = _verify_delivery_files(
                {"artifacts": [relpath]}, "/tmp/not_repo_root"
            )
            self.assertEqual(result, [])
        finally:
            os.unlink(f.name)

    def test_missing_file_flagged(self):
        result = _verify_delivery_files(
            {"artifacts": ["/tmp/nonexistent_12345.py"]}, "/tmp"
        )
        self.assertEqual(len(result), 1)
        self.assertIn("not found", result[0])

    def test_empty_init_py_not_flagged(self):
        """Empty __init__.py is a conventional Python marker — do not flag."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            init = os.path.join(tmpdir, "__init__.py")
            open(init, "w").close()  # 0 bytes
            result = _verify_delivery_files({"artifacts": [init]}, tmpdir)
            self.assertEqual(result, [])

    def test_empty_gitkeep_not_flagged(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            keep = os.path.join(tmpdir, ".gitkeep")
            open(keep, "w").close()
            result = _verify_delivery_files({"artifacts": [keep]}, tmpdir)
            self.assertEqual(result, [])

    def test_empty_non_marker_still_flagged(self):
        """Empty .py files (not __init__.py) should still be flagged."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            empty = os.path.join(tmpdir, "main.py")
            open(empty, "w").close()
            result = _verify_delivery_files({"artifacts": [empty]}, tmpdir)
            self.assertEqual(len(result), 1)
            self.assertIn("empty", result[0])


# ---------------------------------------------------------------------------
# Review enforcement: pass without checks → demoted to fail
# ---------------------------------------------------------------------------

class TestReviewEnforcement(unittest.TestCase):
    """Review that passes with empty checks_run should be demoted to fail."""

    def test_review_pass_no_checks_triggers_rework(self):
        task_state = [_make_task_state()]
        # Review passes but never ran any capability — should be demoted to fail.
        # capability_rounds_used=0 is the hard signal; it's what the engine trusts.
        review_no_checks = _make_agent_result({
            "status": "pass",
            "summary": "Looks fine.",
            "findings": [],
            "checks_run": [],
            "blocking": [],
        })
        review_no_checks["capability_rounds_used"] = 0
        run_agent = MagicMock(side_effect=[
            _worker_pass(),
            review_no_checks,       # first review: pass but no checks → demoted to fail
            _worker_pass("Fixed."), # rework
            _review_pass(),         # final review with proper checks
        ])
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))
        rc = run_orchestration(
            request="build something",
            agent_bin="claude",
            debug_mode=False,
            execute_agents=True,
            active_project=project,
            task_state=task_state[0],
            task_state_path=ts_path,
            fork_hint=None,
            pending_secrets=[],
            pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        # Should have 4 calls: worker → review(demoted) → rework → final review
        roles = [c[0][0] for c in run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "review", "worker", "review"])


# ---------------------------------------------------------------------------
# Edge case robustness: _needs_planning
# ---------------------------------------------------------------------------

class TestNeedsPlanningEdgeCases(unittest.TestCase):
    def test_none_input(self):
        self.assertFalse(_needs_planning(None))

    def test_empty_string(self):
        self.assertFalse(_needs_planning(""))

    def test_whitespace_only(self):
        self.assertFalse(_needs_planning("   "))

    def test_single_repeated_signal(self):
        # Same signal repeated should count as 1, not many.
        self.assertFalse(_needs_planning("authenticate " * 20))


# ---------------------------------------------------------------------------
# Edge case robustness: _validate_agent_output
# ---------------------------------------------------------------------------

class TestValidateAgentOutputEdgeCases(unittest.TestCase):
    def test_string_output(self):
        result = _validate_agent_output("not a dict", "worker")
        self.assertIsNotNone(result)
        self.assertIn("empty output", result)

    def test_list_output(self):
        result = _validate_agent_output(["a", "b"], "worker")
        self.assertIsNotNone(result)

    def test_none_output(self):
        result = _validate_agent_output(None, "worker")
        self.assertIsNotNone(result)

    def test_review_empty_status(self):
        result = _validate_agent_output({"status": ""}, "review")
        self.assertIsNotNone(result)
        self.assertIn("missing", result)

    def test_review_valid_status(self):
        self.assertIsNone(_validate_agent_output({"status": "pass"}, "review"))

    def test_worker_valid(self):
        self.assertIsNone(_validate_agent_output({"summary": "did stuff"}, "worker"))


# ---------------------------------------------------------------------------
# Edge case robustness: _run_with_retry
# ---------------------------------------------------------------------------

class TestRunWithRetryEdgeCases(unittest.TestCase):
    def test_none_return_from_run_fn(self):
        """run_fn returning None should not crash."""
        result = _run_with_retry(lambda: None, "worker", lambda msg: None)
        self.assertEqual(result, {})

    def test_missing_status_key(self):
        """run_fn returning dict without 'status' should not crash."""
        result = _run_with_retry(lambda: {"output": "hi"}, "worker", lambda msg: None)
        self.assertEqual(result, {"output": "hi"})

    def test_success_on_first_try(self):
        result = _run_with_retry(
            lambda: {"status": "completed", "output": {"summary": "ok"}},
            "worker", lambda msg: None,
        )
        self.assertEqual(result["status"], "completed")


# ---------------------------------------------------------------------------
# Edge case robustness: _verify_delivery_files
# ---------------------------------------------------------------------------

class TestVerifyDeliveryFilesEdgeCases(unittest.TestCase):
    def test_artifacts_is_none(self):
        """artifacts: None should not crash."""
        result = _verify_delivery_files({"artifacts": None}, "/tmp")
        self.assertEqual(result, [])

    def test_changes_made_is_none(self):
        result = _verify_delivery_files({"changes_made": None}, "/tmp")
        self.assertEqual(result, [])

    def test_mixed_garbage_in_artifacts(self):
        result = _verify_delivery_files(
            {"artifacts": [None, 123, "", "  ", True]}, "/tmp"
        )
        self.assertEqual(result, [])

    def test_mixed_garbage_in_changes_made(self):
        result = _verify_delivery_files(
            {"changes_made": [None, 123, True]}, "/tmp"
        )
        self.assertEqual(result, [])

    def test_changes_made_empty_path_before_colon(self):
        result = _verify_delivery_files(
            {"changes_made": [": just description"]}, "/tmp"
        )
        self.assertEqual(result, [])

    def test_both_none(self):
        result = _verify_delivery_files(
            {"artifacts": None, "changes_made": None}, "/tmp"
        )
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Edge case robustness: _normalize_review_status
# ---------------------------------------------------------------------------

from engine.work.orchestrator import _normalize_review_status

class TestNormalizeReviewStatusEdgeCases(unittest.TestCase):
    def test_none(self):
        self.assertEqual(_normalize_review_status(None), "fail")

    def test_empty_string(self):
        self.assertEqual(_normalize_review_status(""), "fail")

    def test_integer(self):
        self.assertEqual(_normalize_review_status(123), "fail")

    def test_pass_variants(self):
        self.assertEqual(_normalize_review_status("pass"), "pass")
        self.assertEqual(_normalize_review_status("PASS"), "pass")
        self.assertEqual(_normalize_review_status("  pass  "), "pass")


# ---------------------------------------------------------------------------
# Edge case robustness: _project_name_from_request
# ---------------------------------------------------------------------------

class TestProjectNameEdgeCases(unittest.TestCase):
    def test_none(self):
        self.assertEqual(_project_name_from_request(None), "Untitled Project")

    def test_empty(self):
        self.assertEqual(_project_name_from_request(""), "Untitled Project")

    def test_only_punctuation(self):
        self.assertEqual(_project_name_from_request("!!!@@@###"), "Untitled Project")


# ---------------------------------------------------------------------------
# Edge case robustness: _next_project_id
# ---------------------------------------------------------------------------

class TestNextProjectIdEdgeCases(unittest.TestCase):
    def test_none_registry(self):
        self.assertEqual(_next_project_id(None), "001")

    def test_empty_dict(self):
        self.assertEqual(_next_project_id({}), "001")

    def test_projects_is_none(self):
        self.assertEqual(_next_project_id({"projects": None}), "001")

    def test_projects_with_non_dict_entries(self):
        self.assertEqual(_next_project_id({"projects": ["garbage", 123, None]}), "001")

    def test_normal_increment(self):
        registry = {"projects": [{"project_id": "003"}, {"project_id": "001"}]}
        self.assertEqual(_next_project_id(registry), "004")


class TestCapabilityRoundsForTask(unittest.TestCase):
    """Adaptive capability round budget based on task complexity."""

    def test_simple_task_gets_5_rounds(self):
        self.assertEqual(_capability_rounds_for_task("write a hello world script", False), 5)

    def test_empty_task_gets_5_rounds(self):
        self.assertEqual(_capability_rounds_for_task("", False), 5)

    def test_none_task_gets_5_rounds(self):
        self.assertEqual(_capability_rounds_for_task(None, False), 5)

    def test_medium_task_gets_8_rounds(self):
        task = "authenticate to the Graph API and then deploy the Logic App"
        result = _capability_rounds_for_task(task, False)
        self.assertEqual(result, 8)

    def test_planned_task_gets_12_rounds(self):
        result = _capability_rounds_for_task("simple task", True)
        self.assertEqual(result, 12)

    def test_complex_task_with_plan_gets_12(self):
        task = "authenticate to Graph API and deploy to Azure and connect SharePoint"
        result = _capability_rounds_for_task(task, True)
        self.assertEqual(result, 12)

    def test_plan_flag_overrides_simplicity(self):
        result = _capability_rounds_for_task("write a script", True)
        self.assertEqual(result, 12)


class TestBuildDeliveryContext(unittest.TestCase):
    """Delivery context pre-injection for worker prompts."""

    def _make_project(self, tmp: Path) -> dict[str, Any]:
        delivery = tmp / "delivery"
        delivery.mkdir(parents=True, exist_ok=True)
        return {"project_root": str(delivery)}

    def test_empty_delivery_returns_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(Path(tmp))
            result = _build_delivery_context(project)
            self.assertEqual(result, [])

    def test_missing_dir_returns_empty(self):
        result = _build_delivery_context({"project_root": "/nonexistent/path/xyz"})
        self.assertEqual(result, [])

    def test_small_text_file_inlined(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(Path(tmp))
            delivery = Path(project["project_root"])
            (delivery / "main.py").write_text("print('hello')\n", encoding="utf-8")
            result = _build_delivery_context(project)
            self.assertTrue(any("Existing delivery files" in line for line in result))
            self.assertTrue(any("main.py" in line for line in result))
            self.assertTrue(any("print('hello')" in line for line in result))

    def test_large_file_listed_not_inlined(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(Path(tmp))
            delivery = Path(project["project_root"])
            # Write a file exceeding _DELIVERY_INLINE_MAX_BYTES
            (delivery / "big.py").write_text("x = 1\n" * 2000, encoding="utf-8")
            result = _build_delivery_context(project)
            # Should appear in file tree
            self.assertTrue(any("big.py" in line for line in result))
            # Content should NOT be inlined (no --- big.py --- marker)
            self.assertFalse(any("--- big.py ---" in line for line in result))

    def test_binary_file_not_inlined(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(Path(tmp))
            delivery = Path(project["project_root"])
            (delivery / "image.png").write_bytes(b"\x89PNG\r\n")
            result = _build_delivery_context(project)
            self.assertTrue(any("image.png" in line for line in result))
            self.assertFalse(any("--- image.png ---" in line for line in result))

    def test_subdirectory_files_included(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_project(Path(tmp))
            delivery = Path(project["project_root"])
            sub = delivery / "lib"
            sub.mkdir()
            (sub / "util.py").write_text("def helper(): pass\n", encoding="utf-8")
            result = _build_delivery_context(project)
            self.assertTrue(any("lib/util.py" in line or "lib\\util.py" in line for line in result))


class TestClassifyBlockers(unittest.TestCase):
    """Blocker classification: hard vs researchable."""

    def test_credential_blocker_is_hard(self):
        hard, researchable = _classify_blockers(["Missing API key for Graph API"])
        self.assertEqual(hard, ["Missing API key for Graph API"])
        self.assertEqual(researchable, [])

    def test_permission_denied_is_hard(self):
        hard, researchable = _classify_blockers(["Access denied when calling /users endpoint"])
        self.assertEqual(len(hard), 1)
        self.assertEqual(len(researchable), 0)

    def test_api_question_is_researchable(self):
        hard, researchable = _classify_blockers(["Unknown endpoint for listing Logic App runs"])
        self.assertEqual(hard, [])
        self.assertEqual(researchable, ["Unknown endpoint for listing Logic App runs"])

    def test_mixed_blockers_split(self):
        blockers = [
            "Need credentials to authenticate",
            "Cannot find the correct endpoint for Qualys scan results",
            "Waiting for user confirmation on output format",
        ]
        hard, researchable = _classify_blockers(blockers)
        self.assertEqual(len(hard), 2)  # credentials + user confirmation
        self.assertEqual(len(researchable), 1)  # endpoint question

    def test_empty_list(self):
        hard, researchable = _classify_blockers([])
        self.assertEqual(hard, [])
        self.assertEqual(researchable, [])

    def test_non_string_items_skipped(self):
        hard, researchable = _classify_blockers([None, 123, "", "Real issue here"])
        self.assertEqual(len(researchable), 1)
        self.assertEqual(hard, [])

    def test_401_error_is_hard(self):
        hard, _ = _classify_blockers(["API returned 401 Unauthorized"])
        self.assertEqual(len(hard), 1)

    def test_403_forbidden_is_hard(self):
        hard, _ = _classify_blockers(["403 Forbidden when calling endpoint"])
        self.assertEqual(len(hard), 1)

    def test_max_research_cycles_constant(self):
        self.assertEqual(MAX_RESEARCH_CYCLES, 2)


def _worker_blocked(issues: list[str], summary: str = "Blocked.") -> dict[str, Any]:
    return _make_agent_result({
        "status": "blocked",
        "summary": summary,
        "changes_made": [],
        "checks_run": [],
        "artifacts": [],
        "open_issues": issues,
        "needs_research": False,
        "needs_user_input": False,
    })


class TestBlockerChallenge(unittest.TestCase):
    """Integration: worker reports researchable blocker → engine challenges with research."""

    def test_researchable_blocker_triggers_challenge(self):
        """Worker blocked with researchable issue → research runs → worker re-runs."""
        run_agent = MagicMock(side_effect=[
            _worker_blocked(["Unknown endpoint for listing Qualys vulnerability data"]),
            _research_result(),
            _worker_pass("Script completed"),
            _review_pass(),
        ])
        task_state = [_make_task_state()]
        project, ts_path = _configure(run_agent, task_state)

        rc = run_orchestration(
            request="build a script that queries the Qualys API",
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        roles = [c.args[0] for c in run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "research", "worker", "review"])

    def test_hard_blocker_stops_immediately(self):
        """Worker blocked with credential issue → engine stops, no research."""
        run_agent = MagicMock(side_effect=[
            _worker_blocked(["Missing API key for Qualys"]),
        ])
        task_state = [_make_task_state()]
        project, ts_path = _configure(run_agent, task_state)

        rc = run_orchestration(
            request="build a script",
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 1)
        self.assertEqual(len(run_agent.call_args_list), 1)

    def test_mixed_blockers_with_researchable_triggers_research(self):
        """If at least one blocker is researchable, research is dispatched."""
        run_agent = MagicMock(side_effect=[
            _worker_blocked([
                "Unknown API response format for scan results",  # researchable
            ]),
            _research_result(),
            _worker_pass("Done"),
            _review_pass(),
        ])
        task_state = [_make_task_state()]
        project, ts_path = _configure(run_agent, task_state)

        rc = run_orchestration(
            request="build a script",
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        roles = [c.args[0] for c in run_agent.call_args_list]
        self.assertIn("research", roles)


class TestResearchRetry(unittest.TestCase):
    """Integration: worker still needs research after first cycle → second research dispatched."""

    def test_second_research_cycle_runs(self):
        """Worker needs research twice → both cycles run before review."""
        run_agent = MagicMock(side_effect=[
            _worker_needs_research(["What is the auth flow for API X?"]),
            _research_result(),
            _worker_needs_research(["How does pagination work for API X?"]),
            _research_result(),
            _worker_pass("Script completed"),
            _review_pass(),
        ])
        task_state = [_make_task_state()]
        project, ts_path = _configure(run_agent, task_state)

        rc = run_orchestration(
            request="build a complex integration",
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        roles = [c.args[0] for c in run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "research", "worker", "research", "worker", "review"])

    def test_research_capped_at_max_cycles(self):
        """After MAX_RESEARCH_CYCLES, worker proceeds to review even if still flagging needs_research."""
        run_agent = MagicMock(side_effect=[
            _worker_needs_research(["Q1?"]),
            _research_result(),
            _worker_needs_research(["Q2?"]),
            _research_result(),
            # Worker 3 still flags needs_research but cycles exhausted
            _worker_needs_research(["Q3?"]),
            _review_pass(),
        ])
        task_state = [_make_task_state()]
        project, ts_path = _configure(run_agent, task_state)

        rc = run_orchestration(
            request="build a complex integration",
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        roles = [c.args[0] for c in run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "research", "worker", "research", "worker", "review"])


if __name__ == "__main__":
    unittest.main()
