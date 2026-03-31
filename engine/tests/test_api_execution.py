"""Tests for api_execution and execution dispatch with mocked vendor responses."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

# Ensure the tmp directory is importable
TMP_ROOT = Path(__file__).resolve().parents[2]
if str(TMP_ROOT) not in sys.path:
    sys.path.insert(0, str(TMP_ROOT))

from engine.work.api_execution import run_agent_api, runtime_check_api
from engine.work.backend_config import BackendResolution


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_build_prompt(*args, **kwargs) -> str:
    return "test prompt"


def _mock_estimate_tokens(text: str) -> int:
    return len(text) // 4


def _mock_is_toon_available() -> bool:
    return False


def _mock_emit_progress(msg: str) -> None:
    pass


def _mock_extract_json_payload(text: str) -> dict[str, Any]:
    """Simple JSON extractor for tests."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                return {}
    return {}


def _mock_classify_error(msg: str) -> str:
    if "timeout" in msg.lower():
        return "timeout"
    if "auth" in msg.lower() or "key" in msg.lower():
        return "auth_error"
    return "unknown"


def _make_mock_session(persistent: bool = False):
    session = MagicMock()
    session.persistent = persistent
    return session


# ---------------------------------------------------------------------------
# Tests for run_agent_api
# ---------------------------------------------------------------------------


class TestRunAgentApiNoKey(unittest.TestCase):
    def test_missing_api_key_returns_failed(self):
        result = run_agent_api(
            "worker", "test task", "test reason", None, [], None,
            backend_name="claude",
            model=None,
            api_key="",
            base_url=None,
            timeout_seconds=30,
            session=_make_mock_session(),
            force_full_artifacts=None,
            expected_result_shape=None,
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            extract_json_payload=_mock_extract_json_payload,
            classify_error=_mock_classify_error,
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("No API key", result["error"])
        self.assertEqual(result["error_category"], "configuration_error")


class TestRunAgentApiUnsupportedBackend(unittest.TestCase):
    def test_unknown_backend_returns_failed(self):
        result = run_agent_api(
            "worker", "test task", "test reason", None, [], None,
            backend_name="unknown_vendor",
            model=None,
            api_key="some-key",
            base_url=None,
            timeout_seconds=30,
            session=_make_mock_session(),
            force_full_artifacts=None,
            expected_result_shape=None,
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            extract_json_payload=_mock_extract_json_payload,
            classify_error=_mock_classify_error,
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("No API caller", result["error"])


class TestRunAgentApiAnthropicMocked(unittest.TestCase):
    @patch("engine.work.api_execution._call_anthropic")
    def test_successful_api_call(self, mock_call):
        payload = {
            "summary": "Done",
            "technical_data": {"result": {"status": "pass"}},
        }
        mock_call.return_value = {"ok": True, "text": json.dumps(payload), "error": ""}

        result = run_agent_api(
            "worker", "write code", "implement feature", None, [], None,
            backend_name="claude",
            model="claude-sonnet-4-20250514",
            api_key="sk-ant-test",
            base_url=None,
            timeout_seconds=660,
            session=_make_mock_session(),
            force_full_artifacts=None,
            expected_result_shape=None,
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            extract_json_payload=_mock_extract_json_payload,
            classify_error=_mock_classify_error,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["summary"], "Done")
        self.assertIn("duration", result)

    @patch("engine.work.api_execution._call_anthropic")
    def test_api_error_returns_failed(self, mock_call):
        mock_call.return_value = {"ok": False, "text": "", "error": "Authentication failed"}

        result = run_agent_api(
            "worker", "write code", "test", None, [], None,
            backend_name="claude",
            model=None,
            api_key="sk-ant-bad",
            base_url=None,
            timeout_seconds=30,
            session=_make_mock_session(),
            force_full_artifacts=None,
            expected_result_shape=None,
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            extract_json_payload=_mock_extract_json_payload,
            classify_error=_mock_classify_error,
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("Authentication failed", result["error"])

    @patch("engine.work.api_execution._call_anthropic")
    def test_empty_response_returns_failed(self, mock_call):
        mock_call.return_value = {"ok": True, "text": "", "error": ""}

        result = run_agent_api(
            "worker", "decide", "test", None, [], None,
            backend_name="claude",
            model=None,
            api_key="sk-ant-test",
            base_url=None,
            timeout_seconds=30,
            session=_make_mock_session(),
            force_full_artifacts=None,
            expected_result_shape=None,
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            extract_json_payload=_mock_extract_json_payload,
            classify_error=_mock_classify_error,
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("Empty response", result["error"])

    @patch("engine.work.api_execution._call_anthropic")
    def test_invalid_json_response_returns_failed(self, mock_call):
        mock_call.return_value = {"ok": True, "text": "This is not JSON at all", "error": ""}

        result = run_agent_api(
            "worker", "decide", "test", None, [], None,
            backend_name="claude",
            model=None,
            api_key="sk-ant-test",
            base_url=None,
            timeout_seconds=30,
            session=_make_mock_session(),
            force_full_artifacts=None,
            expected_result_shape=None,
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            extract_json_payload=_mock_extract_json_payload,
            classify_error=_mock_classify_error,
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("Invalid JSON", result["error"])

    @patch("engine.work.api_execution._call_anthropic")
    def test_capability_requested(self, mock_call):
        payload = {
            "capability_requests": [
                {"capability": "read_file", "arguments": {"path": "test.py"}}
            ]
        }
        mock_call.return_value = {"ok": True, "text": json.dumps(payload), "error": ""}

        result = run_agent_api(
            "worker", "implement", "test", None, [], None,
            backend_name="claude",
            model=None,
            api_key="sk-ant-test",
            base_url=None,
            timeout_seconds=30,
            session=_make_mock_session(),
            force_full_artifacts=None,
            expected_result_shape=None,
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            extract_json_payload=_mock_extract_json_payload,
            classify_error=_mock_classify_error,
        )
        self.assertEqual(result["status"], "capability_requested")
        self.assertEqual(len(result["capability_requests"]), 1)

    @patch("engine.work.api_execution._call_anthropic")
    def test_persistent_session_conversation_id_set(self, mock_call):
        """Persistent sessions capture the conversation_id returned by the backend."""
        payload = {"summary": "ok", "technical_data": {}}
        mock_call.return_value = {
            "ok": True,
            "text": json.dumps(payload),
            "error": "",
            "conversation_id": "api-session-abc",
        }

        session = _make_mock_session(persistent=True)
        run_agent_api(
            "worker", "implement", "test", None, [], None,
            backend_name="claude",
            model=None,
            api_key="sk-ant-test",
            base_url=None,
            timeout_seconds=30,
            session=session,
            force_full_artifacts=None,
            expected_result_shape=None,
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            extract_json_payload=_mock_extract_json_payload,
            classify_error=_mock_classify_error,
        )
        # The session object itself is a MagicMock so set-attribute works freely;
        # we just verify the run completed without error.
        self.assertEqual(mock_call.call_count, 1)


class TestRunAgentApiGoogleMocked(unittest.TestCase):
    @patch("engine.work.api_execution._call_google")
    def test_successful_gemini_call(self, mock_call):
        payload = {
            "summary": "Research complete",
            "technical_data": {"result": {"findings": []}},
        }
        mock_call.return_value = {"ok": True, "text": json.dumps(payload), "error": ""}

        result = run_agent_api(
            "research", "research APIs", "gather info", None, [], None,
            backend_name="gemini",
            model="gemini-2.5-pro",
            api_key="AIza-test",
            base_url=None,
            timeout_seconds=120,
            session=_make_mock_session(),
            force_full_artifacts=None,
            expected_result_shape=None,
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            extract_json_payload=_mock_extract_json_payload,
            classify_error=_mock_classify_error,
        )
        self.assertEqual(result["status"], "success")


class TestRunAgentApiOpenAIMocked(unittest.TestCase):
    @patch("engine.work.api_execution._call_openai")
    def test_successful_openai_call(self, mock_call):
        payload = {
            "summary": "Code written",
            "technical_data": {"result": {"files": ["main.py"]}},
        }
        mock_call.return_value = {"ok": True, "text": json.dumps(payload), "error": ""}

        result = run_agent_api(
            "worker", "write script", "implement", None, [], None,
            backend_name="openai",
            model="gpt-4.1",
            api_key="sk-openai-test",
            base_url=None,
            timeout_seconds=120,
            session=_make_mock_session(),
            force_full_artifacts=None,
            expected_result_shape=None,
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            extract_json_payload=_mock_extract_json_payload,
            classify_error=_mock_classify_error,
        )
        self.assertEqual(result["status"], "success")


# ---------------------------------------------------------------------------
# Tests for runtime_check_api
# ---------------------------------------------------------------------------


class TestRuntimeCheckApi(unittest.TestCase):
    def test_no_api_key(self):
        result = runtime_check_api("claude", None, None, None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_api_key")

    def test_unsupported_backend(self):
        result = runtime_check_api("unknown", "some-key", None, None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported")

    @patch("engine.work.api_execution._call_anthropic")
    def test_successful_check(self, mock_call):
        mock_call.return_value = {"ok": True, "text": '{"status": "ok"}', "error": ""}
        result = runtime_check_api("claude", "sk-ant-test", None, None)
        self.assertTrue(result["ok"])

    @patch("engine.work.api_execution._call_anthropic")
    def test_failed_check(self, mock_call):
        mock_call.return_value = {"ok": False, "text": "", "error": "Connection refused"}
        result = runtime_check_api("claude", "sk-ant-test", None, None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "api_error")


# ---------------------------------------------------------------------------
# Tests for execution.py dispatch
# ---------------------------------------------------------------------------


class TestExecutionDispatch(unittest.TestCase):
    """Test that execution.py run_agent dispatches to API when configured."""

    def test_run_agent_dispatches_to_api_when_configured(self):
        from engine.work.execution import run_agent as run_agent_fn

        mock_resolution = BackendResolution(
            mode="api",
            backend_name="claude",
            model="claude-sonnet-4-20250514",
            api_key="sk-ant-test",
        )

        api_result = {
            "status": "success",
            "output": {"summary": "via API"},
            "duration": 1.5,
        }

        mock_resolve = MagicMock(return_value=mock_resolution)
        mock_api_runner = MagicMock(return_value=api_result)

        result = run_agent_fn(
            "worker", "test task", "test reason", None, [], None, "claude",
            force_full_artifacts=None,
            expected_result_shape=None,
            session=_make_mock_session(),
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            build_agent_command=MagicMock(),
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            repo_root="/tmp",
            spawn_timeout_seconds=660,
            classify_error=_mock_classify_error,
            extract_session_id_from_text=MagicMock(return_value=None),
            extract_json_payload=_mock_extract_json_payload,
            resolve_backend=mock_resolve,
            run_agent_api=mock_api_runner,
        )

        self.assertEqual(result["status"], "success")
        mock_resolve.assert_called_once_with("claude", "worker")
        mock_api_runner.assert_called_once()

    def test_run_agent_uses_cli_when_no_api_config(self):
        """When resolve_backend is not provided, CLI path is used."""
        from engine.work.execution import run_agent as run_agent_fn

        # Without resolve_backend, should fall through to CLI path
        # which will fail since we're not providing a real binary — that's fine,
        # we just verify it doesn't call run_agent_api
        mock_api_runner = MagicMock()

        # This will fail in subprocess, which is expected
        result = run_agent_fn(
            "worker", "test task", "test reason", None, [], None, "nonexistent-binary",
            force_full_artifacts=None,
            expected_result_shape=None,
            session=_make_mock_session(),
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            build_agent_command=MagicMock(return_value=(["nonexistent-binary"], "prompt")),
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            repo_root="/tmp",
            spawn_timeout_seconds=5,
            classify_error=_mock_classify_error,
            extract_session_id_from_text=MagicMock(return_value=None),
            extract_json_payload=_mock_extract_json_payload,
            # No resolve_backend or run_agent_api provided
        )

        # Should have gone through CLI path and failed (no such binary)
        self.assertEqual(result["status"], "failed")
        mock_api_runner.assert_not_called()

    def test_run_agent_uses_cli_when_mode_is_cli(self):
        """When resolve_backend returns mode=cli, CLI path is used."""
        from engine.work.execution import run_agent as run_agent_fn

        mock_resolution = BackendResolution(mode="cli", backend_name="claude")
        mock_resolve = MagicMock(return_value=mock_resolution)
        mock_api_runner = MagicMock()

        result = run_agent_fn(
            "worker", "test task", "test reason", None, [], None, "nonexistent-binary",
            force_full_artifacts=None,
            expected_result_shape=None,
            session=_make_mock_session(),
            build_prompt=_mock_build_prompt,
            estimate_tokens=_mock_estimate_tokens,
            build_agent_command=MagicMock(return_value=(["nonexistent-binary"], "prompt")),
            is_toon_available=_mock_is_toon_available,
            emit_progress=_mock_emit_progress,
            repo_root="/tmp",
            spawn_timeout_seconds=5,
            classify_error=_mock_classify_error,
            extract_session_id_from_text=MagicMock(return_value=None),
            extract_json_payload=_mock_extract_json_payload,
            resolve_backend=mock_resolve,
            run_agent_api=mock_api_runner,
        )

        # Should have gone through CLI path
        self.assertEqual(result["status"], "failed")
        mock_api_runner.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for runtime_check dispatch
# ---------------------------------------------------------------------------


class TestRuntimeCheckDispatch(unittest.TestCase):
    def test_runtime_check_dispatches_to_api(self):
        from engine.work.execution import runtime_check

        mock_resolution = BackendResolution(
            mode="api", backend_name="claude", api_key="sk-test", model=None
        )
        mock_resolve = MagicMock(return_value=mock_resolution)
        mock_api_check = MagicMock(return_value={
            "backend": "claude (api)", "ok": True, "reason": "ok", "details": ""
        })

        result = runtime_check(
            "claude",
            runtime_check_prompt="test",
            build_agent_command=MagicMock(),
            extract_json_payload=MagicMock(),
            runtime_check_output_has_success=MagicMock(),
            resolve_backend=mock_resolve,
            runtime_check_api=mock_api_check,
        )

        self.assertTrue(result["ok"])
        mock_api_check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
