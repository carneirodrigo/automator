"""End-to-end pipeline tests with scripted agents.

These exercise multi-stage flows (planning, research, rework, classifier,
pending-resolution round-trips) by driving `run_orchestration` with
canned agent responses. They are designed to catch pipeline ordering
regressions that unit tests of individual helpers cannot see.

Fixtures are borrowed from ``test_lean_orchestration`` to avoid
duplicating the mock wiring.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from engine.work.orchestrator import run_orchestration

from engine.tests.test_lean_orchestration import (
    _configure,
    _make_agent_result,
    _make_project,
    _make_task_state,
    _research_result,
    _review_fail,
    _review_pass,
    _worker_blocked,
    _worker_needs_research,
    _worker_pass,
    setUpModule,  # noqa: F401 — patches sleep
    tearDownModule,  # noqa: F401
)


def _plan_result(steps: list[str], questions: list[str] | None = None) -> dict[str, Any]:
    """Scripted planner output."""
    return _make_agent_result({
        "plan": steps,
        "questions": questions or [],
        "reasoning": "scripted",
    })


def _classify_result(needs_planning: bool) -> dict[str, Any]:
    return _make_agent_result({"needs_planning": needs_planning, "reason": "scripted"})


# A request the regex heuristic returns "plan" for. Multi-system + deploy +
# credentials is unambiguous.
_COMPLEX_REQUEST = (
    "connect to Azure Defender and Qualys using credentials from inputs/, "
    "pull vulnerability data, and deploy the combined results to SharePoint"
)

# A request the regex heuristic returns "uncertain" for: exactly one
# complexity signal in a substantive sentence. "deploy-ready" is the single
# hit; the rest is benign.
_UNCERTAIN_REQUEST = (
    "please write a python program that reads a csv file and "
    "produces a deploy-ready static html report on disk"
)


# ---------------------------------------------------------------------------
# Planning-questions round-trip
# ---------------------------------------------------------------------------


class TestPlanningQuestionsRoundTrip(unittest.TestCase):
    """Planner returns questions on first run; continue run uses stored answers."""

    def test_first_run_sets_pending_questions(self):
        task_state = [_make_task_state()]
        run_agent = MagicMock(side_effect=[
            _plan_result([], ["Which subscription should host the alert?"]),
        ])
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))

        rc = run_orchestration(
            request=_COMPLEX_REQUEST,
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        pending = task_state[0].get("pending_resolution")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["type"], "planning_questions")
        self.assertIn("Which subscription", pending["message"])
        # Only the planner ran — no worker, no review yet.
        roles = [c.args[0] for c in run_agent.call_args_list]
        self.assertEqual(roles, ["worker"])  # planner is invoked under worker role

    def test_continue_with_answers_runs_full_pipeline(self):
        """Second run resolves planning_questions, reuses stored plan, skips re-planning."""
        # Realistic continue state: the planner already persisted a plan on
        # the first run alongside the questions; only the pending_resolution
        # is consumed here.
        stored_plan = ["step 1", "step 2", "step 3"]
        task_state = [{
            "plan": stored_plan,
            "user_request": _COMPLEX_REQUEST,
            "pending_resolution": {
                "type": "planning_questions",
                "message": "Before starting, the engine needs clarification:\n  1. Q?",
                "original_request": _COMPLEX_REQUEST,
            },
        }]
        run_agent = MagicMock(side_effect=[
            _worker_pass("Implemented per plan."),
            _review_pass(),
        ])
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))

        rc = run_orchestration(
            request="Subscription: prod-sec; notification: teams channel",
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        roles = [c.args[0] for c in run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "review"])  # plan reused, no re-planning
        # Worker prompt should include the stored plan and the user answers.
        worker_task = run_agent.call_args_list[0].args[1]
        self.assertIn("step 1", worker_task)
        self.assertIn("Subscription: prod-sec", worker_task)


# ---------------------------------------------------------------------------
# LLM classifier integration
# ---------------------------------------------------------------------------


class TestClassifierIntegration(unittest.TestCase):
    """Uncertain regex verdict defers to the LLM classifier."""

    def test_classifier_plan_triggers_planner(self):
        task_state = [_make_task_state()]
        run_agent = MagicMock(side_effect=[
            _classify_result(True),                      # classifier → plan
            _plan_result(["step a", "step b", "step c"]),  # planner
            _worker_pass(),
            _review_pass(),
        ])
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))

        rc = run_orchestration(
            request=_UNCERTAIN_REQUEST,
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(run_agent.call_count, 4)
        # Every call is under worker role in this scripted setup
        # (research/review have their own roles — planner uses worker).
        roles = [c.args[0] for c in run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "worker", "worker", "review"])

    def test_classifier_skip_bypasses_planner(self):
        task_state = [_make_task_state()]
        run_agent = MagicMock(side_effect=[
            _classify_result(False),  # classifier → skip
            _worker_pass(),
            _review_pass(),
        ])
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))

        rc = run_orchestration(
            request=_UNCERTAIN_REQUEST,
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        # classifier + worker + review — no planning step
        self.assertEqual(run_agent.call_count, 3)

    def test_classifier_unavailable_skips_planning(self):
        """Failed classifier call defaults to skip, pipeline still completes."""
        task_state = [_make_task_state()]
        run_agent = MagicMock(side_effect=[
            {"status": "failed", "error_category": "provider_error", "output": {}},
            {"status": "failed", "error_category": "provider_error", "output": {}},
            {"status": "failed", "error_category": "provider_error", "output": {}},
            _worker_pass(),
            _review_pass(),
        ])
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))

        rc = run_orchestration(
            request=_UNCERTAIN_REQUEST,
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        # classifier retries (3 attempts) + worker + review = 5
        self.assertEqual(run_agent.call_count, 5)


# ---------------------------------------------------------------------------
# Stacked multi-feature pipeline
# ---------------------------------------------------------------------------


class TestStackedPipeline(unittest.TestCase):
    """Planning + research + rework stacked in a single run."""

    def test_plan_then_research_then_rework(self):
        """plan → worker(needs_research) → research → worker(success)
        → review(fail) → worker(rework) → review(pass)."""
        task_state = [_make_task_state()]
        run_agent = MagicMock(side_effect=[
            _plan_result(["step 1", "step 2", "step 3"]),  # planner
            _worker_needs_research(["Q for docs?"]),       # worker #1
            _research_result(),                            # research
            _worker_pass("Implemented."),                  # worker #2
            _review_fail(["Add a docstring."]),            # review #1
            _worker_pass("Added docstring."),              # worker #3 (rework)
            _review_pass(),                                # review #2
        ])
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))

        rc = run_orchestration(
            request=_COMPLEX_REQUEST,
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        roles = [c.args[0] for c in run_agent.call_args_list]
        self.assertEqual(
            roles,
            ["worker", "worker", "research", "worker", "review", "worker", "review"],
        )

    def test_plan_then_blocker_then_research_then_success(self):
        """plan → worker(blocked: researchable) → research → worker(success) → review(pass)."""
        task_state = [_make_task_state()]
        run_agent = MagicMock(side_effect=[
            _plan_result(["step 1", "step 2"]),                     # planner
            _worker_blocked(["Unknown API endpoint for scan list"]),# researchable block
            _research_result(),                                     # research
            _worker_pass("Unblocked."),                             # worker #2
            _review_pass(),                                         # review
        ])
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))

        rc = run_orchestration(
            request=_COMPLEX_REQUEST,
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 0)
        roles = [c.args[0] for c in run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "worker", "research", "worker", "review"])

    def test_hard_blocker_after_plan_stops_pipeline(self):
        """plan → worker(blocked: hard) → pipeline halts without review."""
        task_state = [_make_task_state()]
        run_agent = MagicMock(side_effect=[
            _plan_result(["step 1"]),
            _worker_blocked(["Missing client_secret credential"]),
        ])
        project, ts_path = _configure(run_agent, task_state, project=_make_project("001"))

        rc = run_orchestration(
            request=_COMPLEX_REQUEST,
            agent_bin="claude", debug_mode=False, execute_agents=True,
            active_project=project, task_state=task_state[0],
            task_state_path=ts_path, fork_hint=None,
            pending_secrets=[], pending_input_files=False,
        )
        self.assertEqual(rc, 1)
        roles = [c.args[0] for c in run_agent.call_args_list]
        self.assertEqual(roles, ["worker", "worker"])  # planner + blocked worker only


if __name__ == "__main__":
    unittest.main()
