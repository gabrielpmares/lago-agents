"""A git worktree per step (#22)."""

import subprocess
import unittest
from unittest import mock

import support

import agentdefs
import config_store
import hub
import swarm
import team
import worktrees

CATALOG = {"implementer": {"id": "implementer", "name": "Implementer", "inputs": [], "outputs": [], "model": None}}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.repo = support.TMP / f"repo-{self._testMethodName}"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "a.txt").write_text("1")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "one")

    def test_ensure_is_idempotent_and_ignored(self):
        info = worktrees.ensure(self.repo, 7, "API work")
        self.assertTrue(info["new"])
        self.assertEqual(info["branch"], "lago/p7-api-work")
        self.assertTrue((self.repo / ".worktrees/p7-api-work/a.txt").exists())
        self.assertFalse(worktrees.ensure(self.repo, 7, "API work")["new"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_remove_deletes_merged_branch_only(self):
        info = worktrees.ensure(self.repo, 1, "s")
        (support.TMP / info["path"] / "b.txt").write_text("2")
        git(info["path"], "add", ".")
        git(info["path"], "commit", "-qm", "two")
        out = worktrees.remove(self.repo, 1, "s")
        self.assertFalse(out["branch_deleted"])  # not merged yet
        git(self.repo, "merge", "-q", info["branch"])
        worktrees.ensure(self.repo, 1, "s")
        self.assertTrue(worktrees.remove(self.repo, 1, "s")["branch_deleted"])

    def test_not_a_repo(self):
        with self.assertRaises(worktrees.WorktreeError):
            worktrees.ensure(support.TMP, 1, "s")

    def test_run_step_puts_the_worktree_in_the_task(self):
        config = config_store.load()
        config["projects"]["demo"]["cwd"] = str(self.repo)
        config_store.save(config)
        ws = hub.create_workspace("demo", None, "task")
        with mock.patch.object(agentdefs, "catalog", return_value=CATALOG), \
                mock.patch.object(agentdefs, "skills", return_value={}), \
                mock.patch.object(swarm, "enabled_agents", return_value=CATALOG), \
                mock.patch.object(team, "pump"):
            steps = swarm._normalize_steps(config_store.load(), config_store.load()["projects"]["demo"], [
                {"id": "api", "agent": "implementer", "title": "API", "task": "Build it", "worktree": True}])
            pid = hub.create_pipeline(ws, "demo", None, "Plan", "goal", steps)
            hub.update_pipeline(pid, status="approved")
            task_id, _ = swarm.run_step(pid, "api")
        body = hub.get_task(task_id)["body"]
        self.assertIn(".worktrees/p%d-api" % pid, body)
        self.assertIn("lago/p%d-api" % pid, body)
        self.assertEqual(swarm._get(pid)["steps"][0]["branch"], "lago/p%d-api" % pid)


if __name__ == "__main__":
    unittest.main()
