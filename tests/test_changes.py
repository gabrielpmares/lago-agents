"""What a work session changed (changes.py): files since it opened, diffs, steps' branches."""

import shutil
import subprocess
import unittest

import support

import changes
import hub


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


class ChangesTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.repo = support.TMP / "demo"
        shutil.rmtree(self.repo, ignore_errors=True)
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "app.py").write_text("print('one')\n")
        (self.repo / "old.txt").write_text("bye\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "start")
        self.ws = hub.create_workspace("demo", None, "Fix it")
        changes.record_base(self.ws)

    def test_committed_uncommitted_and_new_files_since_it_opened(self):
        (self.repo / "app.py").write_text("print('two')\nprint('three')\n")
        git(self.repo, "commit", "-qam", "edit")
        (self.repo / "old.txt").unlink()
        (self.repo / "new.py").write_text("x = 1\n")
        c = changes.summary(self.ws)
        self.assertTrue(c["full"])
        files = {f["path"]: f for f in c["files"]}
        self.assertEqual((files["app.py"]["added"], files["app.py"]["removed"]), (2, 1))
        self.assertEqual(files["old.txt"]["status"], "deleted")
        self.assertEqual(files["new.py"]["status"], "added")
        self.assertIn("+print('three')", changes.diff(self.ws, "app.py"))
        self.assertIn("+x = 1", changes.diff(self.ws, "new.py"))

    def test_secrets_in_diffs_are_hidden_and_paths_stay_inside(self):
        (self.repo / "app.py").write_text("KEY = 'ghp_" + "a" * 36 + "'\n")
        self.assertIn("[hidden]", changes.diff(self.ws, "app.py"))
        with self.assertRaises(changes.ChangesError):
            changes.diff(self.ws, "../../etc/passwd")

    def test_steps_branches(self):
        pid = hub.create_pipeline(self.ws, "demo", None, "Plan", "goal", [])
        git(self.repo, "checkout", "-qb", f"lago/p{pid}-build")
        (self.repo / "feature.py").write_text("y = 2\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "feature")
        git(self.repo, "checkout", "-q", "main")
        branches = changes.summary(self.ws)["branches"]
        self.assertEqual([b["branch"] for b in branches], [f"lago/p{pid}-build"])
        self.assertFalse(branches[0]["merged"])
        self.assertIn("+y = 2", changes.diff(self.ws, "feature.py", f"lago/p{pid}-build"))

    def test_not_a_repository(self):
        shutil.rmtree(self.repo / ".git")
        self.assertEqual(changes.summary(self.ws)["files"], [])


if __name__ == "__main__":
    unittest.main()
