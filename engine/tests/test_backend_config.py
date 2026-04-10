"""Tests for backend_config: loading, resolution, fallback, and validation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.work.backend_config import (
    BackendResolution,
    _canonical_backend_name,
    has_any_api_config,
    is_api_mode,
    get_api_agent_bin,
    load_api_secrets,
    load_backend_config,
    resolve_backend,
    reset_cache,
    set_config_dir,
)


class TestCanonicalBackendName(unittest.TestCase):
    def test_claude_binary(self):
        self.assertEqual(_canonical_backend_name("claude"), "claude")

    def test_gemini_binary(self):
        self.assertEqual(_canonical_backend_name("gemini"), "gemini")

    def test_codex_maps_to_openai(self):
        self.assertEqual(_canonical_backend_name("codex"), "openai")

    def test_openai_maps_to_openai(self):
        self.assertEqual(_canonical_backend_name("openai"), "openai")

    def test_path_with_claude(self):
        self.assertEqual(_canonical_backend_name("/usr/bin/claude"), "claude")

    def test_binary_with_args(self):
        self.assertEqual(_canonical_backend_name("claude --flag"), "claude")

    def test_unknown_binary(self):
        self.assertEqual(_canonical_backend_name("unknown_tool"), "unknown_tool")

    def test_empty_string(self):
        self.assertEqual(_canonical_backend_name(""), "")


class TestLoadBackendConfig(unittest.TestCase):
    def test_missing_config_returns_defaults(self):
        config = load_backend_config(Path("/nonexistent"))
        self.assertEqual(config["mode"], "cli")

    def test_valid_config_loads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            data = {"version": 2, "mode": "api", "provider": "anthropic", "default_model": "claude-sonnet-4-20250514"}
            (config_dir / "backends.json").write_text(json.dumps(data))
            config = load_backend_config(config_dir)
            self.assertEqual(config["mode"], "api")
            self.assertEqual(config["provider"], "anthropic")

    def test_invalid_json_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "backends.json").write_text("not json")
            config = load_backend_config(config_dir)
            self.assertEqual(config["mode"], "cli")

    def test_non_dict_json_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "backends.json").write_text('"just a string"')
            config = load_backend_config(config_dir)
            self.assertEqual(config["mode"], "cli")


class TestLoadApiSecrets(unittest.TestCase):
    def test_missing_secrets_returns_empty(self):
        result = load_api_secrets(Path("/nonexistent"))
        self.assertEqual(result, {})

    def test_valid_secrets_loads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            data = {"anthropic_api_key": "sk-test"}
            (config_dir / "secrets.json").write_text(json.dumps(data))
            secrets = load_api_secrets(config_dir)
            self.assertEqual(secrets["anthropic_api_key"], "sk-test")

    def test_invalid_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "secrets.json").write_text("bad json")
            result = load_api_secrets(config_dir)
            self.assertEqual(result, {})


class TestResolveBackendCliMode(unittest.TestCase):
    """Tests for CLI mode resolution."""

    def test_no_config_defaults_to_cli(self):
        res = resolve_backend("claude", "worker", config_dir=Path("/nonexistent"))
        self.assertEqual(res.mode, "cli")
        self.assertEqual(res.backend_name, "claude")
        self.assertIsNone(res.api_key)

    def test_explicit_cli_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "backends.json").write_text(json.dumps({"version": 2, "mode": "cli"}))
            res = resolve_backend("gemini", "worker", config_dir=config_dir)
            self.assertEqual(res.mode, "cli")
            self.assertEqual(res.backend_name, "gemini")

    def test_cli_mode_ignores_provider(self):
        """In CLI mode, provider field is irrelevant."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            data = {"version": 2, "mode": "cli", "provider": "anthropic", "default_model": "test-model"}
            (config_dir / "backends.json").write_text(json.dumps(data))
            res = resolve_backend("gemini", "worker", config_dir=config_dir)
            self.assertEqual(res.mode, "cli")
            self.assertEqual(res.backend_name, "gemini")
            self.assertIsNone(res.model)

    def test_codex_maps_to_openai_in_cli(self):
        res = resolve_backend("codex", "worker", config_dir=Path("/nonexistent"))
        self.assertEqual(res.mode, "cli")
        self.assertEqual(res.backend_name, "openai")


class TestResolveBackendApiMode(unittest.TestCase):
    """Tests for API mode resolution."""

    def _make_config(self, tmpdir, config, secrets=None):
        config_dir = Path(tmpdir)
        (config_dir / "backends.json").write_text(json.dumps(config))
        if secrets:
            (config_dir / "secrets.json").write_text(json.dumps(secrets))
        return config_dir

    def test_api_mode_uses_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = self._make_config(tmpdir,
                {"version": 2, "mode": "api", "provider": "anthropic", "default_model": "claude-sonnet-4-20250514"},
                {"anthropic_api_key": "sk-test-key"})
            res = resolve_backend("gemini", "worker", config_dir=config_dir)
            self.assertEqual(res.mode, "api")
            self.assertEqual(res.backend_name, "claude")
            self.assertEqual(res.model, "claude-sonnet-4-20250514")
            self.assertEqual(res.api_key, "sk-test-key")

    def test_api_mode_ignores_agent_bin(self):
        """In API mode, agent_bin is irrelevant."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = self._make_config(tmpdir,
                {"version": 2, "mode": "api", "provider": "google"},
                {"google_api_key": "AIza-test"})
            res = resolve_backend("claude", "worker", config_dir=config_dir)
            self.assertEqual(res.mode, "api")
            self.assertEqual(res.backend_name, "gemini")

    def test_api_mode_missing_key_warns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = self._make_config(tmpdir,
                {"version": 2, "mode": "api", "provider": "anthropic"})
            res = resolve_backend("claude", "worker", config_dir=config_dir)
            self.assertEqual(res.mode, "api")
            self.assertIsNone(res.api_key)

    def test_role_override_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = self._make_config(tmpdir,
                {"version": 2, "mode": "api", "provider": "anthropic", "default_model": "claude-sonnet-4-20250514",
                 "role_overrides": {"worker": {"model": "claude-opus-4-20250514"}}},
                {"anthropic_api_key": "sk-test"})
            res = resolve_backend("claude", "worker", config_dir=config_dir)
            self.assertEqual(res.model, "claude-opus-4-20250514")
            res2 = resolve_backend("claude", "review", config_dir=config_dir)
            self.assertEqual(res2.model, "claude-sonnet-4-20250514")

    def test_role_override_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = self._make_config(tmpdir,
                {"version": 2, "mode": "api", "provider": "anthropic", "default_model": "claude-sonnet-4-20250514",
                 "role_overrides": {"worker": {"provider": "openai", "model": "gpt-4.1"}}},
                {"anthropic_api_key": "sk-ant", "openai_api_key": "sk-oai"})
            res_master = resolve_backend("x", "review", config_dir=config_dir)
            self.assertEqual(res_master.backend_name, "claude")
            self.assertEqual(res_master.api_key, "sk-ant")
            res_coding = resolve_backend("x", "worker", config_dir=config_dir)
            self.assertEqual(res_coding.backend_name, "openai")
            self.assertEqual(res_coding.model, "gpt-4.1")
            self.assertEqual(res_coding.api_key, "sk-oai")

    def test_invalid_provider_falls_back_to_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = self._make_config(tmpdir,
                {"version": 2, "mode": "api", "provider": "invalid_provider"})
            res = resolve_backend("claude", "worker", config_dir=config_dir)
            self.assertEqual(res.mode, "cli")

    def test_invalid_global_mode_falls_back_to_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = self._make_config(tmpdir,
                {"version": 2, "mode": "invalid_mode"})
            res = resolve_backend("claude", "worker", config_dir=config_dir)
            self.assertEqual(res.mode, "cli")


class TestIsApiMode(unittest.TestCase):
    def test_no_config_is_not_api(self):
        self.assertFalse(is_api_mode(config_dir=Path("/nonexistent")))

    def test_cli_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "backends.json").write_text(json.dumps({"version": 2, "mode": "cli"}))
            self.assertFalse(is_api_mode(config_dir=Path(tmpdir)))

    def test_api_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "backends.json").write_text(json.dumps({"version": 2, "mode": "api", "provider": "anthropic"}))
            self.assertTrue(is_api_mode(config_dir=Path(tmpdir)))


class TestGetApiAgentBin(unittest.TestCase):
    def test_cli_returns_none(self):
        self.assertIsNone(get_api_agent_bin(config_dir=Path("/nonexistent")))

    def test_anthropic_returns_claude(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "backends.json").write_text(json.dumps({"version": 2, "mode": "api", "provider": "anthropic"}))
            self.assertEqual(get_api_agent_bin(config_dir=Path(tmpdir)), "claude")

    def test_google_returns_gemini(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "backends.json").write_text(json.dumps({"version": 2, "mode": "api", "provider": "google"}))
            self.assertEqual(get_api_agent_bin(config_dir=Path(tmpdir)), "gemini")

    def test_openai_returns_codex(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "backends.json").write_text(json.dumps({"version": 2, "mode": "api", "provider": "openai"}))
            self.assertEqual(get_api_agent_bin(config_dir=Path(tmpdir)), "codex")

    def test_invalid_provider_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "backends.json").write_text(json.dumps({"version": 2, "mode": "api", "provider": "invalid"}))
            self.assertIsNone(get_api_agent_bin(config_dir=Path(tmpdir)))


class TestHasAnyApiConfig(unittest.TestCase):
    def test_no_config(self):
        self.assertFalse(has_any_api_config(config_dir=Path("/nonexistent")))

    def test_cli_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "backends.json").write_text(json.dumps({"version": 2, "mode": "cli"}))
            self.assertFalse(has_any_api_config(config_dir=Path(tmpdir)))

    def test_api_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "backends.json").write_text(json.dumps({"version": 2, "mode": "api", "provider": "anthropic"}))
            self.assertTrue(has_any_api_config(config_dir=Path(tmpdir)))


class TestConfigCaching(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_dir = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        reset_cache()
        set_config_dir(Path(__file__).resolve().parents[2] / "config")

    def test_config_is_cached(self):
        data = {"version": 2, "mode": "api", "provider": "anthropic"}
        (self.config_dir / "backends.json").write_text(json.dumps(data))
        set_config_dir(self.config_dir)
        c1 = load_backend_config()
        c2 = load_backend_config()
        self.assertEqual(c1, c2)
        # Mutations to a returned copy must not corrupt the cached value.
        c1["mutated"] = True
        c3 = load_backend_config()
        self.assertNotIn("mutated", c3)

    def test_reset_cache_forces_reload(self):
        data = {"version": 2, "mode": "cli"}
        (self.config_dir / "backends.json").write_text(json.dumps(data))
        set_config_dir(self.config_dir)
        c1 = load_backend_config()
        reset_cache()
        data2 = {"version": 2, "mode": "api", "provider": "google"}
        (self.config_dir / "backends.json").write_text(json.dumps(data2))
        c2 = load_backend_config()
        self.assertNotEqual(c1.get("mode"), c2.get("mode"))


if __name__ == "__main__":
    unittest.main()
