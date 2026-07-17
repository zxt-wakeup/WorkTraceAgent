from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worktrace_agent import cli  # noqa: E402
from worktrace_agent.render import build_weekly_report_prompt  # noqa: E402


class WeeklyReferenceTests(unittest.TestCase):
    def test_cli_saves_previous_reports_privately_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "settings.json"
            reference_path = root / "private" / "weekly-reference.md"
            config_path.write_text(
                json.dumps(
                    {"weekly_report_reference": {"path": str(reference_path)}}
                ),
                encoding="utf-8",
            )
            supplied = "# 往届周报\n\n## 本周亮点\n- 先结论后证据\n"
            output = StringIO()
            with mock.patch("sys.stdin", StringIO(supplied)), redirect_stdout(output):
                cli.main(
                    [
                        "--config",
                        str(config_path),
                        "weekly-reference",
                        "set",
                        "--stdin",
                    ]
                )

            self.assertEqual(reference_path.read_text(encoding="utf-8"), supplied)
            self.assertEqual(reference_path.stat().st_mode & 0o777, 0o600)
            self.assertIn("Weekly reference status: configured", output.getvalue())

            status = StringIO()
            with redirect_stdout(status):
                cli.main(
                    [
                        "--config",
                        str(config_path),
                        "weekly-reference",
                        "status",
                    ]
                )
            self.assertIn("Weekly reference status: configured", status.getvalue())

    def test_weekly_prompt_treats_previous_report_as_style_only(self):
        prompt = build_weekly_report_prompt(
            iso_week="2026-W29",
            period_start="2026-07-13",
            period_end="2026-07-19",
            timezone="Asia/Singapore",
            partial_period=True,
            context_text="#### E-123456789abc / USER / message / 2026-07-15T10:00:00+08:00\n> DATA: test\n\"本周工作\"",
            okr_text="周期：2026-Q3\n- O1/KR1：可靠交付",
            weekly_reference_text="上周完成了不可复制的旧项目事实",
        )

        self.assertIn("<weekly-report-style-reference>", prompt)
        self.assertIn("上周完成了不可复制的旧项目事实", prompt)
        self.assertIn("不是本周工作证据", prompt)
        self.assertIn("不得复制其中的事实", prompt)


if __name__ == "__main__":
    unittest.main()
