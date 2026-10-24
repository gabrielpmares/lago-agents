"""Routines (#10): policies, triggers, run history and revisions."""

import subprocess
import time
import unittest
from unittest import mock

import support

import hub
import routines
import swarm
import team


class RoutineTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.started = []

        def fake_run(catalog_id, task, project_id="", folder="", from_agent=None, intro=None, **_):
            task_id = hub.create_task(f"r{len(self.started) + 1}-{catalog_id}", task[:40], task, from_agent)
            hub.update_task(task_id, status="working")
            self.started.append({"agent": catalog_id, "task": task, "from": from_agent, "task_id": task_id})
            return task_id, f"r{len(self.started)}-{catalog_id}", "inherit"
        patches = [mock.patch.object(swarm, "enabled_agents", return_value={"web-researcher": {}, "qa": {}}),
                   mock.patch.object(swarm, "run_agent", side_effect=fake_run)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def make(self, **kw):
        args = dict(title="Prices", agent="web-researcher", instructions="Check competitor prices",
                    repeat="daily", when="09:00")
        args.update(kw)
        return routines.create(**args)

    def test_create_validates(self):
        with self.assertRaises(routines.RoutineError):
            self.make(agent="nobody")
        with self.assertRaises(routines.RoutineError):
            self.make(repeat="once")
        with self.assertRaises(routines.RoutineError):
            self.make(concurrency="sometimes")
        with self.assertRaises(routines.RoutineError):
            self.make(only_if_changed="git")  # needs a project
        r = self.make()
        self.assertEqual(r["status"], "active")
        self.assertGreater(r["next_at"], time.time())
        self.assertEqual(routines.revisions(r["id"])[0]["revision"], 1)

    def test_due_routine_fires_and_reports_to_lago(self):
        r = self.make()
        runs = routines.fire_due(now=r["next_at"] + 1)
        self.assertEqual([x["status"] for x in runs], ["running"])
        self.assertEqual(self.started[0]["from"], "orchestrator")
        self.assertGreater(routines.get(r["id"])["next_at"], r["next_at"])
        self.assertEqual(routines.fire_due(now=r["next_at"] + 2), [])  # claimed once

    def test_nothing_fires_with_routines_switched_off(self):
        import features
        r = self.make()
        features.switch("routines", False)
        self.assertEqual(routines.fire_due(now=r["next_at"] + 1), [])
        self.assertEqual(routines.get(r["id"])["next_at"], r["next_at"])  # still due, not skipped
        features.switch("routines", True)
        self.assertEqual([x["status"] for x in routines.fire_due(now=r["next_at"] + 1)], ["running"])

    def test_skip_while_running_then_done(self):
        r = self.make()
        first = routines.fire(r["id"])
        second = routines.fire(r["id"], "webhook")
        self.assertEqual(second["status"], "coalesced")
        self.assertEqual(routines.get_run(first["id"])["coalesced"], 1)
        team.finish(first["task_id"], "found 3 changes")
        self.assertEqual(routines.runs(r["id"])[-1]["status"], "done")
        self.assertEqual(routines.fire(r["id"])["status"], "running")

    def test_parallel_allows_overlap(self):
        r = self.make(concurrency="parallel")
        routines.fire(r["id"])
        self.assertEqual(routines.fire(r["id"])["status"], "running")

    def test_catch_up_skip_after_app_was_closed(self):
        r = self.make(catch_up="skip")
        runs = routines.fire_due(now=r["next_at"] + 3600)
        self.assertEqual((runs[0]["status"], runs[0]["reason"]), ("skipped", "missed while Lago Agents was closed"))
        self.assertEqual(self.started, [])

    def test_only_if_git_changed(self):
        repo = support.TMP / "demo"
        repo.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a.txt").write_text("1")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "one"], check=True)
        r = self.make(project="demo", only_if_changed="git", concurrency="parallel")
        self.assertEqual(routines.fire(r["id"], "schedule")["status"], "running")
        self.assertEqual(routines.fire(r["id"], "schedule")["status"], "skipped")
        self.assertEqual(routines.fire(r["id"], "manual", force=True)["status"], "running")
        (repo / "a.txt").write_text("2")
        self.assertEqual(routines.fire(r["id"], "schedule")["status"], "running")

    def test_failures_only_reports_failures(self):
        r = self.make(report="failures")
        run = routines.fire(r["id"])
        self.assertIsNone(self.started[0]["from"])
        with mock.patch.object(routines, "_tell_failure") as tell:
            team.finish(run["task_id"], "site down", status="failed")
            routines.sync_runs()
        tell.assert_called_once()

    def test_pause_resume_archive_and_restore(self):
        r = self.make()
        routines.set_status(r["id"], "paused")
        self.assertEqual(routines.fire_due(now=r["next_at"] + 1), [])
        routines.set_status(r["id"], "active")
        routines.update(r["id"], instructions="Check prices and stock")
        routines.restore(r["id"], 1)
        self.assertEqual(routines.get(r["id"])["instructions"], "Check competitor prices")
        self.assertEqual(routines.get(r["id"])["revision"], 3)
        routines.set_status(r["id"], "archived")
        with self.assertRaises(routines.RoutineError):
            routines.set_status(r["id"], "active")

    def test_webhook_secret(self):
        r = self.make(repeat="never", when=None)
        self.assertIsNone(r["next_at"])
        secret = routines.new_hook(r["id"])
        self.assertEqual(routines.by_hook(secret)["id"], r["id"])
        self.assertIsNone(routines.by_hook("wrong"))
        self.assertNotIn(secret, str(routines.get(r["id"])))  # only the hash is kept


if __name__ == "__main__":
    unittest.main()
