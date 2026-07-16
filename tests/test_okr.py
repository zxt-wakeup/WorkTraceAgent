from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worktrace_agent import cli  # noqa: E402
from worktrace_agent.okr import load_okr  # noqa: E402


class QuarterlyOkrTests(unittest.TestCase):
    def test_cli_can_save_and_report_private_okr_status_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "settings.json"
            okr_path = root / "private" / "okr.md"
            config_path.write_text(
                '{"okr":{"path":"' + str(okr_path) + '"}}',
                encoding="utf-8",
            )
            supplied = "周期：2026-Q3\n- O1/KR1：可靠交付\n"
            output = StringIO()
            with mock.patch("sys.stdin", StringIO(supplied)), redirect_stdout(output):
                cli.main(["--config", str(config_path), "okr", "set", "--stdin"])

            self.assertTrue(okr_path.is_file())
            self.assertEqual(okr_path.read_text(encoding="utf-8"), supplied)
            self.assertEqual(okr_path.stat().st_mode & 0o777, 0o600)
            self.assertIn("OKR status: configured", output.getvalue())

            status = StringIO()
            with redirect_stdout(status):
                cli.main(
                    [
                        "--config",
                        str(config_path),
                        "okr",
                        "status",
                        "--day",
                        "2026-07-16",
                    ]
                )
            self.assertIn("OKR status: configured", status.getvalue())

    def test_quarter_is_planning_context_not_an_all_work_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            okr_path = Path(tmp) / "okr.md"
            okr_path.write_text(
                """# 2026 Q3 OKR

周期：2026-Q3

- O1/KR1：发布可靠版本
- O1/KR2：提高自动化覆盖（停用）
""",
                encoding="utf-8",
            )
            settings = {
                "okr": {"path": str(okr_path), "required": False, "max_chars": 20000}
            }
            current = load_okr(settings, report_day="2026-07-16")
            self.assertTrue(current.configured)
            self.assertEqual(current.refs, ("O1/KR1",))
            self.assertNotIn("O1/KR2", current.text)

            previous_quarter = load_okr(settings, report_day="2026-06-30")
            self.assertFalse(previous_quarter.configured)
            self.assertEqual(previous_quarter.status, "out_of_period")
            self.assertEqual(previous_quarter.refs, ())

    def test_invalid_or_ambiguous_period_disables_alignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            okr_path = Path(tmp) / "okr.md"
            settings = {
                "okr": {"path": str(okr_path), "required": False, "max_chars": 20000}
            }
            for period_block in (
                "周期：2026-Q5",
                "周期：2026-Q3\n周期：2026",
                "没有周期",
            ):
                with self.subTest(period=period_block):
                    okr_path.write_text(
                        "{}\n- O1/KR1：可靠交付\n".format(period_block),
                        encoding="utf-8",
                    )
                    result = load_okr(settings, report_day="2026-07-16")
                    self.assertEqual(result.status, "invalid_period")
                    self.assertEqual(result.refs, ())


if __name__ == "__main__":
    unittest.main()
