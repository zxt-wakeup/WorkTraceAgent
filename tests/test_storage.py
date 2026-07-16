from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worktrace_agent.storage import (  # noqa: E402
    ARTIFACT_MARKER_NAME,
    ARTIFACT_MARKER_SCHEMA,
    initialize_artifact_period,
    prune_artifacts,
)


class ArtifactRetentionTests(unittest.TestCase):
    def test_command_scan_atomically_initializes_period_ownership(self):
        from worktrace_agent import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifacts"
            today = datetime.now(timezone.utc).date().isoformat()
            settings = {
                "artifacts": {
                    "directory": str(root),
                    "timezone": "UTC",
                    "retention_days": 30,
                }
            }
            args = argparse.Namespace(
                day=today,
                week=None,
                connectors="all",
                output=None,
            )
            with mock.patch.object(cli, "build_connectors", return_value=[]):
                signals = cli.command_scan(args, settings)

            marker = root.resolve() / today / ARTIFACT_MARKER_NAME
            self.assertTrue(signals.is_file())
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8")),
                {
                    "schema": ARTIFACT_MARKER_SCHEMA,
                    "owner": "worktrace-agent",
                    "period_type": "daily",
                    "period_key": today,
                },
            )
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)

    def test_unmarked_old_daily_is_preserved_and_marked_old_daily_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifacts"
            unmarked = root / "2025-01-01"
            unmarked.mkdir(parents=True)
            (unmarked / "foreign.txt").write_text("keep", encoding="utf-8")
            marked = root / "2025-01-02"
            initialize_artifact_period(root, marked, "daily", marked.name)
            (marked / "signals.json").write_text("{}", encoding="utf-8")

            removed = prune_artifacts(root, 30, date(2026, 7, 15))

            self.assertEqual(removed, 1)
            self.assertTrue(unmarked.is_dir())
            self.assertFalse(marked.exists())

    def test_weekly_pruning_requires_a_matching_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "artifacts"
            marked = root / "weekly" / "2025-W01"
            initialize_artifact_period(root, marked, "weekly", marked.name)
            unmarked = root / "weekly" / "2025-W02"
            unmarked.mkdir(parents=True)
            mismatched = root / "weekly" / "2025-W03"
            mismatched.mkdir()
            marker = marked / ARTIFACT_MARKER_NAME
            copied = json.loads(marker.read_text(encoding="utf-8"))
            (mismatched / ARTIFACT_MARKER_NAME).write_text(
                json.dumps(copied), encoding="utf-8"
            )

            removed = prune_artifacts(root, 30, date(2026, 7, 15))

            self.assertEqual(removed, 1)
            self.assertFalse(marked.exists())
            self.assertTrue(unmarked.is_dir())
            self.assertTrue(mismatched.is_dir())

    def test_dangerous_roots_are_rejected_without_deleting(self):
        with mock.patch("worktrace_agent.storage.shutil.rmtree") as rmtree:
            for dangerous in (Path(Path.cwd().anchor), Path.home()):
                with self.subTest(root=dangerous):
                    with self.assertRaises(ValueError):
                        prune_artifacts(dangerous, 30, date(2026, 7, 15))
            rmtree.assert_not_called()

    def test_symlinked_period_is_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            root = temp / "artifacts"
            root.mkdir()
            outside_root = temp / "outside-root"
            outside = outside_root / "2025-01-01"
            initialize_artifact_period(outside_root, outside, "daily", outside.name)
            linked = root / outside.name
            try:
                linked.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("directory symlinks are unavailable")

            removed = prune_artifacts(root, 30, date(2026, 7, 15))

            self.assertEqual(removed, 0)
            self.assertTrue(outside.is_dir())


if __name__ == "__main__":
    unittest.main()
