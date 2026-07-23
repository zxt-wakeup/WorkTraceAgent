from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worktrace_agent.feishu import (  # noqa: E402
    enable_feishu_config,
    publish_report,
    publishing_status,
    setup_feishu,
)
from worktrace_agent.settings import load_settings  # noqa: E402


class FakeFeishuClient:
    def __init__(self):
        self.command = "/test/bin/lark-cli"
        self.files = {"": []}
        self.documents = {}
        self.folder_creates = 0
        self.document_creates = 0
        self.document_updates = 0
        self.fetches = 0

    def verify_auth(self):
        return {"authenticated": True}

    def list_files(self, folder_token):
        return list(self.files.setdefault(folder_token, []))

    def create_folder(self, name, parent_token):
        self.folder_creates += 1
        token = "folder-{}".format(self.folder_creates)
        value = {
            "name": name,
            "token": token,
            "type": "folder",
            "url": "https://example.test/folder/{}".format(token),
        }
        self.files.setdefault(parent_token, []).append(value)
        self.files[token] = []
        return value

    def create_document(self, title, body, parent_token):
        self.document_creates += 1
        token = "doc-{}".format(self.document_creates)
        value = {
            "name": title,
            "document_id": token,
            "token": token,
            "type": "docx",
            "url": "https://example.test/doc/{}".format(token),
        }
        self.files.setdefault(parent_token, []).append(value)
        self.documents[token] = body
        return {"document": value}

    def overwrite_document(self, token, body):
        if token not in self.documents:
            raise AssertionError("unknown document")
        self.document_updates += 1
        self.documents[token] = body
        return {"document_id": token}

    def fetch_document(self, token):
        if token not in self.documents:
            raise AssertionError("unknown document")
        self.fetches += 1
        return {"document_id": token, "content": self.documents[token]}


def settings_for(root: Path):
    return {
        "publishing": {
            "feishu": {
                "enabled": True,
                "auto_publish": True,
                "command": "/test/bin/lark-cli",
                "state_path": str(root / "state" / "feishu.json"),
                "root_folder_name": "WorkTrace",
                "daily_folder_name": "日报",
                "weekly_folder_name": "周报",
                "failure_policy": "warn",
                "timeout_seconds": 120,
            }
        }
    }


class FeishuPublishingTests(unittest.TestCase):
    def test_setup_is_idempotent_and_state_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            client = FakeFeishuClient()

            first = setup_feishu(settings, client=client)
            second = setup_feishu(settings, client=client)

            self.assertEqual(client.folder_creates, 3)
            self.assertEqual(first["root_url"], second["root_url"])
            state_path = Path(first["state_path"])
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
            state = json.loads(state_path.read_text())
            self.assertEqual(state["schema"], "worktrace-agent/feishu-publishing-v1")
            self.assertEqual(set(state["folders"]), {"root", "daily", "weekly"})
            self.assertEqual(state["documents"], {})

    def test_publish_creates_then_reuses_token_and_updates_only_on_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            client = FakeFeishuClient()
            setup_feishu(settings, client=client)
            report = root / "daily-report.md"
            report.write_text("# 2026-07-17 日报\n\n## 工作内容\n\n完成 A。\n")

            created = publish_report(
                settings, report, "daily", "2026-07-17", client=client
            )
            unchanged = publish_report(
                settings, report, "daily", "2026-07-17", client=client
            )
            report.write_text("# 2026-07-17 日报\n\n## 工作内容\n\n完成 B。\n")
            updated = publish_report(
                settings, report, "daily", "2026-07-17", client=client
            )

            self.assertEqual(created.action, "created")
            self.assertEqual(unchanged.action, "unchanged")
            self.assertEqual(updated.action, "updated")
            self.assertEqual(client.document_creates, 1)
            self.assertEqual(client.document_updates, 1)
            state = json.loads(
                Path(settings["publishing"]["feishu"]["state_path"]).read_text()
            )
            document = state["documents"]["daily:2026-07-17"]
            self.assertEqual(document["token"], "doc-1")
            self.assertNotIn("# 2026-07-17 日报", client.documents["doc-1"])
            self.assertIn("完成 B", client.documents["doc-1"])

    def test_publish_reuses_a_unique_existing_title_when_state_was_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            client = FakeFeishuClient()
            setup_feishu(settings, client=client)
            daily_token = json.loads(
                Path(settings["publishing"]["feishu"]["state_path"]).read_text()
            )["folders"]["daily"]["token"]
            client.create_document("2026-07-17 日报", "旧内容", daily_token)
            report = root / "daily-report.md"
            report.write_text("# 2026-07-17 日报\n\n新内容\n")

            result = publish_report(
                settings, report, "daily", "2026-07-17", client=client
            )

            self.assertEqual(result.action, "updated")
            self.assertEqual(client.document_creates, 1)
            self.assertEqual(client.document_updates, 1)
            self.assertEqual(client.documents["doc-1"], "新内容\n")

    def test_enable_config_preserves_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "settings.json"
            config.write_text(json.dumps({"research": {"mode": "off"}}))
            enable_feishu_config(config, "/opt/lark-cli")
            value = json.loads(config.read_text())
            self.assertEqual(value["research"]["mode"], "off")
            self.assertTrue(value["publishing"]["feishu"]["enabled"])
            self.assertTrue(value["publishing"]["feishu"]["auto_publish"])
            self.assertEqual(
                value["publishing"]["feishu"]["command"], "/opt/lark-cli"
            )
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_status_does_not_return_resource_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = settings_for(Path(tmp))
            client = FakeFeishuClient()
            setup_feishu(settings, client=client)
            status = publishing_status(settings, client=client)
            self.assertTrue(status["authenticated"])
            self.assertTrue(status["folders_ready"])
            self.assertNotIn("folders", status)
            self.assertNotIn("token", json.dumps(status))

    def test_settings_validate_feishu_fields_and_bind_relative_state_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "settings.json"
            config.write_text(
                json.dumps(
                    {
                        "publishing": {
                            "feishu": {
                                "state_path": "private/feishu.json",
                                "failure_policy": "stop",
                            }
                        }
                    }
                )
            )
            loaded = load_settings(config)
            self.assertEqual(
                loaded["publishing"]["feishu"]["state_path"],
                str((root / "private" / "feishu.json").resolve()),
            )
            self.assertEqual(
                loaded["publishing"]["feishu"]["failure_policy"], "stop"
            )


if __name__ == "__main__":
    unittest.main()
