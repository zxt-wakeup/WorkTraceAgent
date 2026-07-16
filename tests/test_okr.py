from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worktrace_agent.okr import load_okr  # noqa: E402


class QuarterlyOkrTests(unittest.TestCase):
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
