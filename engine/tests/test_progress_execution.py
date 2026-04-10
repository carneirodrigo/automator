"""Tests for compact progress updates in execution paths."""

from __future__ import annotations

import json
import subprocess
import time
import unittest
from unittest.mock import MagicMock, patch

from engine.work.api_execution import run_agent_api
from engine.work.execution import run_agent, run_agent_with_capabilities


def _build_prompt(*args, **kwargs) -> str:
    return "test prompt"


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _is_toon_available() -> bool:
    return False


def _extract_json_payload(text: str) -> dict:
    return json.loads(text)


def _classify_error(msg: str) -> str:
    return "timeout" if "timed out" in msg.lower() else "unknown"


def _session() -> MagicMock:
    session = MagicMock()
    session.persistent = False
    session.conversation_id = None
    return session


class _FakePopen:
    """Fake Popen compatible with the streaming _stream_process implementation."""

    def __init__(self, *args, **kwargs):
        import io
        self.returncode = 0
        self.stdin = None
        self.stderr = io.StringIO("")

    @property
    def stdout(self):
        return self._gen()

    def _gen(self):
        # Sleep so the queue.Empty heartbeat path fires at least once.
        time.sleep(1.1)
        payload = {
            "summary": "Implemented auth retry",
            "technical_data": {"result": {"status": "pass"}},
        }
        yield json.dumps({"type": "result", "result": json.dumps(payload)})

    def wait(self, timeout=None):
        pass

    def kill(self):
        self.returncode = -9


class _FakeCodexToolPopen:
    """Fake Popen that emits Codex-style JSONL command events before the final result."""

    def __init__(self, *args, **kwargs):
        import io
        self.returncode = 0
        self.stdin = None
        self.stderr = io.StringIO("")

    @property
    def stdout(self):
        return self._gen()

    def _gen(self):
        yield json.dumps({
            "type": "item.started",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "/bin/bash -lc 'ls -1'",
                "status": "in_progress",
            },
        })
        payload = {
            "summary": "Implemented auth retry",
            "technical_data": {"result": {"status": "pass"}},
        }
        yield json.dumps({"type": "result", "result": json.dumps(payload)})

    def wait(self, timeout=None):
        pass

    def kill(self):
        self.returncode = -9


class _FakeCodexSessionPopen:
    """Fake Popen that emits a Codex thread.started event before the final message."""

    def __init__(self, *args, **kwargs):
        import io
        self.returncode = 0
        self.stdin = None
        self.stderr = io.StringIO("")

    @property
    def stdout(self):
        return self._gen()

    def _gen(self):
        yield json.dumps({"type": "thread.started", "thread_id": "codex-thread-123"})
        yield json.dumps({
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "agent_message",
                "text": json.dumps({
                    "summary": "Implemented auth retry",
                    "technical_data": {"result": {"status": "pass"}},
                }),
            },
        })

    def wait(self, timeout=None):
        pass

    def kill(self):
        self.returncode = -9


class TestExecutionProgress(unittest.TestCase):
    @patch("engine.work.execution.subprocess.Popen", side_effect=_FakeCodexToolPopen)
    def test_cli_run_agent_emits_codex_command_progress(self, mock_popen):
        messages: list[str] = []

        result = run_agent(
            "worker",
            "implement auth retry",
            "test",
            None,
            [],
            None,
            "codex",
            force_full_artifacts=None,
            expected_result_shape=None,
            session=_session(),
            build_prompt=_build_prompt,
            estimate_tokens=_estimate_tokens,
            build_agent_command=MagicMock(return_value=(["codex"], "prompt")),
            is_toon_available=_is_toon_available,
            emit_progress=messages.append,
            repo_root="/tmp",
            spawn_timeout_seconds=5,
            classify_error=_classify_error,
            extract_session_id_from_text=MagicMock(return_value=None),
            extract_json_payload=_extract_json_payload,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(messages[0], "worker: starting")
        self.assertTrue(any(msg.startswith("worker: running ") for msg in messages))

    @patch("engine.work.execution.subprocess.Popen", side_effect=_FakeCodexSessionPopen)
    def test_cli_run_agent_does_not_persist_codex_thread_id_for_portable_mode(self, mock_popen):
        session = _session()
        session.persistent = True
        session.mode = "codex_portable"
        extracted_inputs: list[str] = []

        def _extract(text: str) -> str | None:
            extracted_inputs.append(text)
            for line in text.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "thread.started":
                    return event.get("thread_id")
            return None

        result = run_agent(
            "worker",
            "implement auth retry",
            "test",
            None,
            [],
            None,
            "codex",
            force_full_artifacts=None,
            expected_result_shape=None,
            session=session,
            build_prompt=_build_prompt,
            estimate_tokens=_estimate_tokens,
            build_agent_command=MagicMock(return_value=(["codex"], "prompt")),
            is_toon_available=_is_toon_available,
            emit_progress=lambda msg: None,
            repo_root="/tmp",
            spawn_timeout_seconds=5,
            classify_error=_classify_error,
            extract_session_id_from_text=_extract,
            extract_json_payload=_extract_json_payload,
        )

        self.assertEqual(result["status"], "success")
        self.assertIsNone(session.conversation_id)
        self.assertTrue(any("thread.started" in text for text in extracted_inputs))

    @patch("engine.work.execution.heartbeat_message", return_value="worker: still running (0m 30s)")
    @patch("engine.work.execution.should_emit_heartbeat", return_value=True)
    @patch("engine.work.execution.subprocess.Popen", side_effect=_FakePopen)
    def test_cli_run_agent_emits_sparse_heartbeat(self, mock_popen, mock_should_emit, mock_heartbeat):
        messages: list[str] = []

        result = run_agent(
            "worker",
            "implement auth retry",
            "test",
            None,
            [],
            None,
            "codex",
            force_full_artifacts=None,
            expected_result_shape=None,
            session=_session(),
            build_prompt=_build_prompt,
            estimate_tokens=_estimate_tokens,
            build_agent_command=MagicMock(return_value=(["codex"], "prompt")),
            is_toon_available=_is_toon_available,
            emit_progress=messages.append,
            repo_root="/tmp",
            spawn_timeout_seconds=5,
            classify_error=_classify_error,
            extract_session_id_from_text=MagicMock(return_value=None),
            extract_json_payload=_extract_json_payload,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(messages[0], "worker: starting")
        self.assertTrue(any(msg.startswith("worker: still running") for msg in messages))

    def test_capability_requests_emit_compact_message(self):
        messages: list[str] = []
        mock_run_agent = MagicMock(side_effect=[
            {
                "status": "capability_requested",
                "capability_requests": [{"capability": "read_file", "arguments": {"path": "x.py"}}],
            },
            {
                "status": "success",
                "output": {"summary": "done", "technical_data": {"result": {}}},
            },
        ])

        result = run_agent_with_capabilities(
            "worker",
            "implement auth retry",
            "test",
            None,
            [],
            None,
            "codex",
            force_full_artifacts=None,
            expected_result_shape=None,
            session=_session(),
            run_agent=mock_run_agent,
            max_capability_rounds=2,
            validate_capability_request=lambda req: [],
            emit_progress=messages.append,
            execute_capability=lambda req: {"ok": True},
            serialize_for_prompt=lambda data: json.dumps(data),
        )

        self.assertEqual(result["status"], "success")
        self.assertIn("worker: requested read_file", messages)

    def test_capability_loop_limit_becomes_failure(self):
        messages: list[str] = []
        mock_run_agent = MagicMock(return_value={
            "status": "capability_requested",
            "capability_requests": [{"capability": "read_file", "arguments": {"path": "x.py"}}],
        })

        result = run_agent_with_capabilities(
            "worker",
            "implement auth retry",
            "test",
            None,
            [],
            None,
            "codex",
            force_full_artifacts=None,
            expected_result_shape=None,
            session=_session(),
            run_agent=mock_run_agent,
            max_capability_rounds=2,
            validate_capability_request=lambda req: [],
            emit_progress=messages.append,
            execute_capability=lambda req: {"ok": True},
            serialize_for_prompt=lambda data: json.dumps(data),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_category"], "capability_loop")
        self.assertIn("Capability re-invocation limit reached", "\n".join(messages))

    def test_capability_validation_warnings_are_emitted(self):
        messages: list[str] = []
        mock_run_agent = MagicMock(side_effect=[
            {
                "status": "capability_requested",
                "capability_requests": [{"capability": "write_file", "arguments": {"path": "x.py"}}],
            },
            {
                "status": "success",
                "output": {"summary": "done", "technical_data": {"result": {}}},
            },
        ])

        run_agent_with_capabilities(
            "worker",
            "implement auth retry",
            "test",
            None,
            [],
            None,
            "codex",
            force_full_artifacts=None,
            expected_result_shape=None,
            session=_session(),
            run_agent=mock_run_agent,
            max_capability_rounds=2,
            validate_capability_request=lambda req: ["suspicious path"],
            emit_progress=messages.append,
            execute_capability=lambda req: {"ok": True},
            serialize_for_prompt=lambda data: json.dumps(data),
        )

        self.assertTrue(any("Capability request warning: suspicious path" in msg for msg in messages))

    def test_capability_results_are_injected_into_followup_prompt(self):
        first = {
            "status": "capability_requested",
            "capability_requests": [{"capability": "read_file", "arguments": {"path": "x.py"}}],
        }
        second = {
            "status": "success",
            "output": {"summary": "done", "technical_data": {"result": {}}},
        }
        mock_run_agent = MagicMock(side_effect=[first, second])

        run_agent_with_capabilities(
            "worker",
            "implement auth retry",
            "test",
            None,
            [],
            None,
            "codex",
            force_full_artifacts=None,
            expected_result_shape=None,
            session=_session(),
            run_agent=mock_run_agent,
            max_capability_rounds=2,
            validate_capability_request=lambda req: [],
            emit_progress=lambda msg: None,
            execute_capability=lambda req: {"capability": req["capability"], "status": "failed", "issues": ["missing file"]},
            serialize_for_prompt=lambda data: json.dumps(data),
        )

        followup_task = mock_run_agent.call_args_list[1].args[1]
        self.assertIn("Runtime Capability Results (1 round(s) so far)", followup_task)
        self.assertIn("missing file", followup_task)

    @patch("engine.work.api_execution.heartbeat_message", return_value="worker: still running (0m 30s)")
    @patch("engine.work.api_execution.should_emit_heartbeat", return_value=True)
    @patch("engine.work.api_execution._call_anthropic")
    def test_api_run_agent_emits_sparse_heartbeat(self, mock_call, mock_should_emit, mock_heartbeat):
        messages: list[str] = []

        def slow_call(*args, **kwargs):
            time.sleep(1.1)
            payload = {
                "summary": "Implemented auth retry",
                "technical_data": {"result": {"status": "pass"}},
            }
            return {"ok": True, "text": json.dumps(payload), "error": ""}

        mock_call.side_effect = slow_call

        result = run_agent_api(
            "worker",
            "implement auth retry",
            "test",
            None,
            [],
            None,
            backend_name="claude",
            model=None,
            api_key="test-key",
            base_url=None,
            timeout_seconds=5,
            session=_session(),
            force_full_artifacts=None,
            expected_result_shape=None,
            build_prompt=_build_prompt,
            estimate_tokens=_estimate_tokens,
            is_toon_available=_is_toon_available,
            emit_progress=messages.append,
            extract_json_payload=_extract_json_payload,
            classify_error=_classify_error,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(messages[0], "worker: starting")
        self.assertTrue(any(msg.startswith("worker: still running") for msg in messages))


if __name__ == "__main__":
    unittest.main()
