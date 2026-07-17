from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import unittest
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worktrace_agent.connectors import build_connectors  # noqa: E402
from worktrace_agent.connectors.cursor import CursorConnector  # noqa: E402
from worktrace_agent.settings import DEFAULT_SETTINGS  # noqa: E402
from worktrace_agent.window import build_week_window  # noqa: E402


class CursorConnectorTests(unittest.TestCase):
    def test_cursor_disk_kv_bubbles_are_merged_by_composer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Cursor"
            storage = root / "User" / "globalStorage"
            storage.mkdir(parents=True)
            db_path = storage / "state.vscdb"
            rows = [
                (
                    "bubbleId:user-1",
                    {
                        "bubbleId": "user-1",
                        "composerId": "composer-private",
                        "type": "user",
                        "text": "继续",
                        "createdAt": "2026-07-16T09:00:00+08:00",
                    },
                ),
                (
                    "bubbleId:assistant-1",
                    {
                        "bubbleId": "assistant-1",
                        "composerId": "composer-private",
                        "type": "assistant",
                        "text": "Cursor 工作已完成",
                        "createdAt": "2026-07-16T09:05:00+08:00",
                    },
                ),
            ]
            with sqlite3.connect(str(db_path)) as connection:
                connection.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
                connection.executemany(
                    "INSERT INTO cursorDiskKV(key, value) VALUES (?, ?)",
                    [
                        (key, json.dumps(value, ensure_ascii=False))
                        for key, value in rows
                    ],
                )
            mtime = datetime(2026, 7, 16, 2, tzinfo=timezone.utc).timestamp()
            os.utime(db_path, (mtime, mtime))

            result = CursorConnector([root]).scan(
                build_week_window("2026-W29", "Asia/Singapore")
            )

            self.assertEqual(len(result.conversations), 1)
            self.assertEqual(
                [item.content for item in result.conversations[0].messages],
                ["继续", "Cursor 工作已完成"],
            )

    def test_plaintext_chat_state_becomes_timestamped_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Cursor"
            workspace = root / "User" / "workspaceStorage" / "workspace-a"
            workspace.mkdir(parents=True)
            (workspace / "workspace.json").write_text(
                json.dumps({"folder": "file:///private/project-alpha"}),
                encoding="utf-8",
            )
            db_path = workspace / "state.vscdb"
            payload = {
                "chatId": "chat-private-id",
                "title": "实现 Cursor 采集",
                "messages": [
                    {
                        "id": "message-private-1",
                        "role": "user",
                        "content": "接入 Cursor 工作内容",
                        "createdAt": "2026-07-15T10:00:00+08:00",
                    },
                    {
                        "id": "message-private-2",
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "text": "private reasoning"},
                            {"type": "text", "text": "已实现只读解析并完成测试"},
                        ],
                        "createdAt": "2026-07-15T10:10:00+08:00",
                    },
                ],
            }
            with sqlite3.connect(str(db_path)) as connection:
                connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
                connection.execute(
                    "INSERT INTO ItemTable(key, value) VALUES (?, ?)",
                    ("cursor.chat.private", json.dumps(payload, ensure_ascii=False)),
                )
            mtime = datetime(2026, 7, 15, 3, tzinfo=timezone.utc).timestamp()
            os.utime(db_path, (mtime, mtime))
            os.utime(workspace / "workspace.json", (mtime, mtime))

            result = CursorConnector([root]).scan(
                build_week_window("2026-W29", "Asia/Singapore")
            )

            self.assertEqual(result.coverage[0].status, "partial")
            self.assertEqual(result.coverage[0].conversations, 1)
            self.assertEqual(result.coverage[0].messages, 2)
            messages = result.conversations[0].messages
            self.assertEqual([item.role for item in messages], ["user", "assistant"])
            self.assertEqual(messages[0].content, "接入 Cursor 工作内容")
            self.assertEqual(messages[1].content, "已实现只读解析并完成测试")
            self.assertNotIn("private reasoning", messages[1].content)
            self.assertFalse(
                any("private reasoning" in signal.note for signal in result.signals)
            )

    def test_cursor_auto_enables_only_when_detected_or_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            settings["connectors"]["cursor"] = {
                "enabled": "auto",
                "roots": [str(root / "missing")],
            }
            self.assertFalse(
                any(
                    isinstance(item, CursorConnector)
                    for item in build_connectors(settings)
                )
            )

            (root / "detected").mkdir()
            settings["connectors"]["cursor"]["roots"] = [str(root / "detected")]
            self.assertTrue(
                any(
                    isinstance(item, CursorConnector)
                    for item in build_connectors(settings)
                )
            )

            settings["connectors"]["cursor"]["roots"] = [str(root / "missing")]
            requested = build_connectors(settings, ["cursor"])
            self.assertEqual(len(requested), 1)
            self.assertIsInstance(requested[0], CursorConnector)


if __name__ == "__main__":
    unittest.main()
