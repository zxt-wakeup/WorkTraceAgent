from __future__ import annotations

import importlib
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
installer = importlib.import_module("install_skills")


class SkillInstallerTests(unittest.TestCase):
    def _fixture(self, root: Path):
        source_root = root / "source"
        source_root.mkdir()
        skills = {}
        for name in ("worktrace-collect", "worktrace-report"):
            skill = source_root / name
            skill.mkdir()
            (skill / "SKILL.md").write_text("---\nname: {}\n---\n".format(name))
            skills[name] = skill.resolve()
        return skills

    def test_link_is_created_without_copying_and_rerun_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = self._fixture(root)
            target = root / "home" / ".agents" / "skills"

            actions = installer.plan_actions(skills, skills, [target])
            installer.apply_actions(actions)

            for name, source in skills.items():
                destination = target / name
                self.assertTrue(destination.is_symlink())
                self.assertEqual(destination.resolve(), source)
            repeated = installer.plan_actions(skills, skills, [target])
            self.assertTrue(all(item.operation == "unchanged" for item in repeated))

    def test_dry_run_does_not_create_parent_or_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = self._fixture(root)
            target = root / "missing" / "skills"
            actions = installer.plan_actions(skills, skills, [target])

            installer.apply_actions(actions, dry_run=True)

            self.assertFalse(target.exists())

    def test_detection_selects_install_directories_not_generation_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)

            def which(command):
                return (
                    "/usr/bin/{}".format(command)
                    if command in {"codex", "claude"}
                    else None
                )

            with mock.patch.object(installer.shutil, "which", side_effect=which):
                self.assertEqual(
                    installer.detected_targets(home), ["universal", "claude"]
                )

    def test_next_steps_advertise_natural_language_report_commands(self):
        output = io.StringIO()
        with redirect_stdout(output):
            installer._print_next_steps()

        self.assertIn("生成日报", output.getvalue())
        self.assertIn("生成周报", output.getvalue())
        self.assertIn("automatically", output.getvalue())
        self.assertIn("previous report samples", output.getvalue())
        self.assertIn("do not move or delete", output.getvalue())

    def test_link_status_distinguishes_optional_missing_and_broken_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = self._fixture(root)
            target = root / "skills"
            target.mkdir()
            linked = target / "worktrace-report"
            linked.symlink_to(
                skills["worktrace-report"], target_is_directory=True
            )

            statuses = installer.inspect_links(skills, skills, [target])
            by_name = {item.destination.name: item for item in statuses}

            self.assertEqual(by_name["worktrace-report"].state, "linked")
            self.assertEqual(by_name["worktrace-collect"].state, "not-linked")

            linked.unlink()
            linked.symlink_to(root / "deleted-clone" / "worktrace-report")
            statuses = installer.inspect_links(
                skills, ["worktrace-report"], [target]
            )
            self.assertEqual(statuses[0].state, "broken")
            self.assertIn("deleted-clone", statuses[0].detail)

    def test_status_mode_is_read_only_and_reports_broken_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "skills"
            target.mkdir()
            broken = target / "worktrace-report"
            broken.symlink_to(root / "missing" / "worktrace-report")
            output = io.StringIO()
            errors = io.StringIO()
            with (
                mock.patch.object(
                    installer,
                    "target_directories",
                    return_value={"universal": target, "claude": root / "claude"},
                ),
                redirect_stdout(output),
                mock.patch("sys.stderr", errors),
            ):
                result = installer.main(
                    [
                        "--target",
                        "universal",
                        "--skill",
                        "worktrace-report",
                        "--status",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertTrue(broken.is_symlink())
            self.assertIn("broken", output.getvalue())
            self.assertIn("Broken development link", errors.getvalue())

    def test_link_status_does_not_call_a_plain_file_an_installed_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = self._fixture(root)
            target = root / "skills"
            target.mkdir()
            destination = target / "worktrace-report"
            destination.write_text("user-owned", encoding="utf-8")

            statuses = installer.inspect_links(
                skills, ["worktrace-report"], [target]
            )

            self.assertEqual(statuses[0].state, "unexpected-file")
            self.assertEqual(destination.read_text(encoding="utf-8"), "user-owned")

    def test_help_marks_links_as_optional_development_mode(self):
        help_text = installer.build_parser().format_help()

        self.assertIn("source checkout can be used directly", help_text)
        self.assertIn("--mode", help_text)
        self.assertIn("--status", help_text)

    def test_existing_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = self._fixture(root)
            target = root / "skills"
            conflict = target / "worktrace-report"
            conflict.mkdir(parents=True)
            (conflict / "keep.txt").write_text("user-owned")

            with self.assertRaises(installer.InstallConflict):
                installer.plan_actions(skills, skills, [target])

            self.assertEqual((conflict / "keep.txt").read_text(), "user-owned")

    def test_uninstall_removes_only_links_to_this_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = self._fixture(root)
            target = root / "skills"
            target.mkdir()
            own_link = target / "worktrace-report"
            own_link.symlink_to(skills["worktrace-report"], target_is_directory=True)
            unrelated = root / "unrelated"
            unrelated.mkdir()
            foreign_link = target / "worktrace-collect"
            foreign_link.symlink_to(unrelated, target_is_directory=True)

            own_actions = installer.plan_actions(
                skills, ["worktrace-report"], [target], uninstall=True
            )
            installer.apply_actions(own_actions)
            self.assertFalse(own_link.is_symlink())

            with self.assertRaises(installer.InstallConflict):
                installer.plan_actions(
                    skills, ["worktrace-collect"], [target], uninstall=True
                )
            self.assertTrue(foreign_link.is_symlink())


if __name__ == "__main__":
    unittest.main()
