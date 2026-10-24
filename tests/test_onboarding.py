"""The first start (onboarding.py) and New Task (Api.new_task)."""

import json
import subprocess
import unittest
from unittest import mock

import support

import attachments
import background
import config_store
import notify
import onboarding
import swarm

background.start_loops = lambda: None
attachments.start_sweeping = lambda: None
import api  # noqa: E402


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_needed_only_for_new_setups(self):
        self.assertTrue(onboarding.needed({"projects": {}}))
        self.assertTrue(onboarding.needed({"user_name": "Alex", "projects": {}}))
        self.assertFalse(onboarding.needed({"user_name": "Sam", "projects": {"a": {}}}))
        self.assertFalse(onboarding.needed({"onboarded": True, "projects": {}}))

    def test_detect(self):
        folder = support.TMP / "shop-app"
        folder.mkdir()
        (folder / "package.json").write_text(json.dumps({"dependencies": {"react": "18"}}))
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
        subprocess.run(["git", "-C", str(folder), "remote", "add", "origin", "git@github.com:acme/shop.git"], check=True)
        found = onboarding.detect(str(folder))
        self.assertEqual((found["name"], found["git"], found["repo"]), ("Shop App", True, "acme/shop"))
        self.assertIn("React", found["stack"])
        self.assertFalse(onboarding.detect(str(support.TMP / "nope"))["exists"])
        self.assertFalse(onboarding.detect("relative/path")["ok"])

    def test_finish_saves_name_project_and_start_card(self):
        folder = support.TMP / "fresh-idea"
        project_id = onboarding.finish("  Alex ", {"folder": str(folder), "name": "Fresh Idea"})
        config = config_store.load()
        self.assertEqual((config["user_name"], config["onboarded"], config["start_card"]), ("Alex", True, True))
        self.assertEqual(config["projects"][project_id]["name"], "Fresh Idea")
        self.assertTrue((folder / ".git").exists())  # a new folder gets git
        onboarding.dismiss_start_card()
        self.assertNotIn("start_card", config_store.load())
        with self.assertRaises(ValueError):
            onboarding.finish("  ")


class NewTaskTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.api = api.Api(None)

    def test_routes(self):
        with mock.patch.object(notify, "tell_orchestrator") as tell:
            self.assertEqual(self.api.new_task("Compare note apps")["href"], "#orchestrator")
        self.assertIn("New task: Compare note apps", tell.call_args.args[0])
        with mock.patch.object(swarm, "open_workspace", return_value=7) as opened:
            self.assertEqual(self.api.new_task("Fix login", project="demo")["href"], "#work/7")
        self.assertTrue(opened.call_args.kwargs["orchestrated"])
        with mock.patch.object(swarm, "run_agent", return_value=(3, "r3-web", "inherit")) as ran:
            self.assertEqual(self.api.new_task("Find prices", agent="web")["href"], "#agent/r3-web")
        self.assertEqual(ran.call_args.kwargs["from_agent"], "orchestrator")
        with self.assertRaises(api.ApiError):
            self.api.new_task("   ")


if __name__ == "__main__":
    unittest.main()


class MyTasksTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.api = api.Api(None)

    def test_sessions_and_runs_as_one_list(self):
        import hub
        open_ws = hub.create_workspace("demo", None, "Fix login")
        old = hub.create_workspace("", None, "Old research")
        hub.update_workspace(old, status="closed")
        hub.add_event(open_ws, "demo", "question", "Which provider?\nmore")
        hub.add_swarm_agent("r1-web", None, "", "web", "Web Researcher · run 1")
        task = hub.create_task("r1-web", "Bitcoin price", "body", "orchestrator")
        hub.update_task(task, status="working")
        rows = self.api.my_tasks()
        self.assertEqual([r["title"] for r in rows if r["open"]][:2], ["Bitcoin price", "Fix login"])
        login = next(r for r in rows if r["title"] == "Fix login")
        self.assertEqual((login["where"], login["last"], login["href"]), ("Demo", "Which provider?", f"#work/{open_ws}"))
        self.assertEqual(next(r for r in rows if r["title"] == "Old research")["status"], "closed")
