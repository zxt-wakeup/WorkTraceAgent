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
