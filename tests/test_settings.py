from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worktrace_agent import settings as settings_module  # noqa: E402
from worktrace_agent.connectors import build_connectors  # noqa: E402
from worktrace_agent.okr import resolve_okr_path  # noqa: E402


class InstalledSettingsBoundaryTests(unittest.TestCase):
    def test_generation_chunk_parallelism_is_configurable_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "settings.json"
            config.write_text(
                json.dumps({"generation": {"max_parallel_chunks": 8}})
            )
            settings = settings_module.load_settings(config)
            self.assertEqual(settings["generation"]["max_parallel_chunks"], 8)

            for value in (True, 0, 9, "4"):
                with self.subTest(value=value):
                    config.write_text(
                        json.dumps({"generation": {"max_parallel_chunks": value}})
                    )
                    with self.assertRaises(ValueError):
                        settings_module.load_settings(config)

    def test_legacy_context_truncation_defaults_migrate_to_lossless_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "settings.json"
            config.write_text(
                json.dumps(
                    {"context": {"max_chars": 400000, "per_message_chars": 12000}}
                )
            )
            settings = settings_module.load_settings(config)
            self.assertEqual(settings["context"]["max_chars"], 0)
            self.assertNotIn("per_message_chars", settings["context"])

    def test_codex_jsonl_limits_are_validated_and_passed_to_connector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "settings.json"
            config.write_text(
                json.dumps(
                    {
                        "connectors": {
                            "codex_cli": {
                                "root": "codex-home",
                                "max_jsonl_files": 7,
                                "max_file_mb": 3,
                                "max_messages": 11,
                                "max_thread_rows": 13,
                            }
                        }
                    }
                )
            )
            settings = settings_module.load_settings(config)
            connectors = build_connectors(settings, ["codex_cli"])
            self.assertEqual(len(connectors), 1)
            connector = connectors[0]
            self.assertEqual(connector.max_jsonl_files, 7)
            self.assertEqual(connector.max_file_bytes, 3 * 1024 * 1024)
            self.assertEqual(connector.max_messages, 11)
            self.assertEqual(connector.max_thread_rows, 13)

    def test_codex_jsonl_limits_reject_bool_negative_and_excessive_values(self):
        invalid_values = {
            "max_jsonl_files": [True, -1, 100_001],
            "max_file_mb": [False, -1, 1_025],
            "max_messages": ["100", -1, 1_000_001],
            "max_thread_rows": [True, -1, 1_000_001],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for field, values in invalid_values.items():
                for index, value in enumerate(values):
                    with self.subTest(field=field, value=value):
                        config = root / "{}-{}.json".format(field, index)
                        config.write_text(
                            json.dumps({"connectors": {"codex_cli": {field: value}}})
                        )
                        with self.assertRaises(ValueError):
                            settings_module.load_settings(config)

    def test_relative_path_binding_preserves_empty_path_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "settings.json"
            config.write_text(json.dumps({"artifacts": {"directory": ""}}))
            with self.assertRaises(ValueError):
                settings_module.load_settings(config)

    def test_installed_mode_ignores_malicious_cwd_and_binds_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted-config"
            trusted.mkdir()
            user_config = trusted / "settings.json"
            user_config.write_text(
                json.dumps(
                    {
                        "artifacts": {"directory": "reports"},
                        "okr": {"path": "planning/okr.md"},
                        "connectors": {
                            "codex_cli": {"root": "sessions/codex"},
                            "claude_code": {"root": "sessions/claude"},
                            "cursor": {"roots": ["sessions/cursor"]},
                            "codex_web": {"browser_profiles": ["browsers/codex"]},
                            "chatgpt_web": {
                                "browser_profiles": ["browsers/chatgpt"],
                                "export_paths": ["exports/chatgpt"],
                            },
                            "agent_sessions": {
                                "profiles": {"custom": {"roots": ["sessions/custom"]}}
                            },
                        },
                    }
                )
            )
            malicious = root / "malicious-cwd"
            malicious.mkdir()
            (malicious / "worktrace.settings.json").write_text(
                json.dumps(
                    {
                        "artifacts": {
                            "directory": "attacker-output",
                            "retention_days": "invalid-if-loaded",
                        }
                    }
                )
            )
            stable_user_base = root / "stable-user-base"

            original_cwd = Path.cwd()
            try:
                os.chdir(malicious)
                with (
                    mock.patch.object(settings_module, "PROJECT_CONFIG_PATH", None),
                    mock.patch.object(
                        settings_module, "DEFAULT_CONFIG_PATH", user_config
                    ),
                    mock.patch.object(settings_module, "SKILL_ROOT", stable_user_base),
                ):
                    settings = settings_module.load_settings()
            finally:
                os.chdir(original_cwd)

            self.assertEqual(
                settings_module.artifact_dir(settings), (trusted / "reports").resolve()
            )
            self.assertEqual(
                resolve_okr_path(settings), (trusted / "planning" / "okr.md").resolve()
            )
            connectors = settings["connectors"]
            self.assertEqual(
                connectors["codex_cli"]["root"],
                str((trusted / "sessions" / "codex").resolve()),
            )
            self.assertEqual(
                connectors["claude_code"]["root"],
                str((trusted / "sessions" / "claude").resolve()),
            )
            self.assertEqual(
                connectors["cursor"]["roots"],
                [str((trusted / "sessions" / "cursor").resolve())],
            )
            self.assertEqual(
                connectors["codex_web"]["browser_profiles"],
                [str((trusted / "browsers" / "codex").resolve())],
            )
            self.assertEqual(
                connectors["chatgpt_web"]["browser_profiles"],
                [str((trusted / "browsers" / "chatgpt").resolve())],
            )
            self.assertEqual(
                connectors["chatgpt_web"]["export_paths"],
                [str((trusted / "exports" / "chatgpt").resolve())],
            )
            self.assertEqual(
                connectors["agent_sessions"]["profiles"]["custom"]["roots"],
                [str((trusted / "sessions" / "custom").resolve())],
            )

    def test_installed_mode_without_user_config_still_ignores_cwd_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malicious = root / "malicious-cwd"
            malicious.mkdir()
            (malicious / "worktrace.settings.json").write_text(
                json.dumps({"schedule": {"default_time": "attacker-value"}})
            )
            missing_user_config = root / "user" / "settings.json"
            stable_user_base = missing_user_config.parent

            original_cwd = Path.cwd()
            try:
                os.chdir(malicious)
                with (
                    mock.patch.object(settings_module, "PROJECT_CONFIG_PATH", None),
                    mock.patch.object(
                        settings_module,
                        "DEFAULT_CONFIG_PATH",
                        missing_user_config,
                    ),
                    mock.patch.object(settings_module, "SKILL_ROOT", stable_user_base),
                ):
                    settings = settings_module.load_settings()
                    resolved = settings_module.expand_path("relative-artifacts")
            finally:
                os.chdir(original_cwd)

            self.assertEqual(settings["schedule"]["default_time"], "19:00")
            self.assertEqual(
                resolved, (stable_user_base / "relative-artifacts").resolve()
            )


if __name__ == "__main__":
    unittest.main()
