"""Skills from a git repository (#23): fetched, pinned, checked, installed only on approval."""

import os
import subprocess
import unittest
from unittest import mock

import support

import skill_sources


def make_repo(root, skills):
    root.mkdir(parents=True)
    for name, text in skills.items():
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"], check=True)
    return str(root)


GOOD = "---\nname: sql-style\ndescription: How we write SQL.\n---\n# SQL style\n"


class SkillSourceTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        base = support.TMP / f"skills-{self._testMethodName}"
        patches = [mock.patch.object(skill_sources, "DRAFTS_DIR", base / "drafts"),
                   mock.patch.object(skill_sources, "SKILLS_DIR", base / "claude-skills"),
                   mock.patch.object(skill_sources, "LINK_DIR", base / "links")]
        (base / "claude-skills").mkdir(parents=True)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.base = base

    def test_fetch_then_approve(self):
        repo = make_repo(self.base / "remote", {"sql-style": GOOD})
        draft = skill_sources.fetch(repo)
        self.assertEqual((draft["name"], draft["status"], len(draft["commit"])), ("sql-style", "ready", 40))
        self.assertFalse((self.base / "claude-skills/sql-style").exists())  # nothing yet
        self.assertIn("# SQL style", skill_sources.read(draft["id"])["texts"]["SKILL.md"])
        skill_sources.decide(draft["id"], True)
        self.assertTrue((self.base / "claude-skills/sql-style/.source.json").exists())
        self.assertTrue((self.base / "links/sql-style").is_symlink())

    def test_refusals(self):
        bad = make_repo(self.base / "bad", {"Bad_Name": "---\nname: Bad_Name\ndescription: x\n---\n"})
        with self.assertRaises(skill_sources.SkillSourceError):
            skill_sources.fetch(bad)
        two = make_repo(self.base / "two", {"a-skill": GOOD.replace("sql-style", "a-skill"),
                                            "b-skill": GOOD.replace("sql-style", "b-skill")})
        with self.assertRaisesRegex(skill_sources.SkillSourceError, "several skills"):
            skill_sources.fetch(two)
        self.assertEqual(skill_sources.fetch(two, path="b-skill")["name"], "b-skill")
        with self.assertRaisesRegex(skill_sources.SkillSourceError, "inside the repository"):
            skill_sources.fetch(two, path="../..")
        with self.assertRaises(skill_sources.SkillSourceError):
            skill_sources.fetch("not a url; rm -rf")

    def test_skill_at_repository_root(self):
        root = self.base / "rootskill"
        root.mkdir()
        (root / "SKILL.md").write_text(GOOD)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"], check=True)
        self.assertEqual(skill_sources.fetch(str(root))["name"], "sql-style")

    def test_executables_refused(self):
        root = self.base / "exe"
        make_repo(root, {"sql-style": GOOD})
        script = root / "sql-style" / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        os.chmod(script, 0o755)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "y"], check=True)
        with self.assertRaisesRegex(skill_sources.SkillSourceError, "executable"):
            skill_sources.fetch(str(root))


if __name__ == "__main__":
    unittest.main()
