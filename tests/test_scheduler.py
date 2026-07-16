from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from worktrace_agent import cli, scheduler  # noqa: E402


class SchedulerPortabilityTests(unittest.TestCase):
    def test_build_launch_agent_uses_module_for_installed_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            value = scheduler.build_launch_agent(
                "07:05",
                project_dir=project,
                python_executable=Path(sys.executable),
                log_dir=project / "logs",
            )

        self.assertEqual(
            value["ProgramArguments"],
            [
                str(Path(sys.executable).absolute()),
                "-m",
                "worktrace_agent.cli",
                "run",
                "--day",
                "today",
            ],
        )
        self.assertEqual(value["WorkingDirectory"], str(project.resolve()))

    def test_install_schedule_uses_module_without_source_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "working"
            project.mkdir()
            plist_path = root / "agent.plist"
            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                mock.patch.object(scheduler, "_require_macos"),
                mock.patch.object(scheduler, "_bootout"),
                mock.patch.object(scheduler, "_module_available", return_value=True),
                mock.patch.object(scheduler.subprocess, "run", return_value=completed),
            ):
                status = scheduler.install_schedule(
                    "19:00",
                    project_dir=project,
                    python_executable=Path(sys.executable),
                    plist_path=plist_path,
                    log_dir=root / "logs",
                )

            value = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(
                value["ProgramArguments"][:3],
                [
                    str(Path(sys.executable).absolute()),
                    "-m",
                    "worktrace_agent.cli",
                ],
            )
            self.assertFalse(status.stale)

    def test_source_script_remains_a_supported_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "source"
            entry = project / "scripts" / "worktrace.py"
            entry.parent.mkdir(parents=True)
            entry.write_text("from worktrace_agent.cli import main\nmain()\n")
            plist_path = root / "agent.plist"
            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                mock.patch.object(scheduler, "_require_macos"),
                mock.patch.object(scheduler, "_bootout"),
                mock.patch.object(scheduler, "_module_available", return_value=False),
                mock.patch.object(scheduler.subprocess, "run", return_value=completed),
            ):
                status = scheduler.install_schedule(
                    "19:00",
                    project_dir=project,
                    python_executable=Path(sys.executable),
                    plist_path=plist_path,
                    log_dir=root / "logs",
                )

            value = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(
                value["ProgramArguments"][:2],
                [str(Path(sys.executable).absolute()), str(entry.resolve())],
            )
            self.assertFalse(status.stale)

    def test_installed_module_is_preferred_even_in_source_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "source"
            entry = project / "scripts" / "worktrace.py"
            entry.parent.mkdir(parents=True)
            entry.write_text("from worktrace_agent.cli import main\nmain()\n")
            plist_path = root / "agent.plist"
            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                mock.patch.object(scheduler, "_require_macos"),
                mock.patch.object(scheduler, "_bootout"),
                mock.patch.object(scheduler, "_module_available", return_value=True),
                mock.patch.object(scheduler.subprocess, "run", return_value=completed),
            ):
                scheduler.install_schedule(
                    "19:00",
                    project_dir=project,
                    python_executable=Path(sys.executable),
                    plist_path=plist_path,
                    log_dir=root / "logs",
                )

            value = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(
                value["ProgramArguments"][:3],
                [
                    str(Path(sys.executable).absolute()),
                    "-m",
                    "worktrace_agent.cli",
                ],
            )


class DoctorPortabilityTests(unittest.TestCase):
    def test_runtime_summary_recognizes_installed_distribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_skill = Path(tmp) / "SKILL.md"
            with (
                mock.patch.object(cli, "PROJECT_SKILL", missing_skill),
                mock.patch.object(cli.metadata, "version", return_value="1.2.3"),
            ):
                summary = cli._runtime_installation_summary()

        self.assertIn("installed package worktrace-agent 1.2.3", summary)
        self.assertNotIn("missing", summary)

    def test_runtime_summary_keeps_source_skill_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text("---\nname: worktrace\n---\n")
            with mock.patch.object(cli, "PROJECT_SKILL", skill):
                summary = cli._runtime_installation_summary()

        self.assertIn("source skill", summary)
        self.assertIn("found", summary)


if __name__ == "__main__":
    unittest.main()
