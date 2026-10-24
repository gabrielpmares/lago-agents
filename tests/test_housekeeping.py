"""Backups (#1), config revisions (#2), the activity log (#3) and search (#4)."""

import time
import unittest

import support

import activity
import backups
import config_store
import hub
import revisions


class BackupTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        for b in backups.backups():
            __import__("os").unlink(b["path"])

    def test_backup_copies_rows_and_is_restorable(self):
        task = hub.create_task("dev-qa", "Check login", "body")
        path = backups.backup()
        self.assertTrue(path.exists())
        hub.update_task(task, title="changed after backup")
        backups.restore(path.name)
        self.assertEqual(hub.get_task(task)["title"], "Check login")

    def test_run_if_due_once_a_day_and_keeps_seven(self):
        now = time.time()
        self.assertIsNotNone(backups.run_if_due(now))
        self.assertIsNone(backups.run_if_due(now + 60))
        for day in range(1, 10):
            path = backups.backup(now + day * 86400)
            t = now + day * 86400
            __import__("os").utime(path, (t, t))
        self.assertEqual(len(backups.backups()), backups.KEEP)

    def test_restore_unknown_name(self):
        with self.assertRaises(backups.BackupError):
            backups.restore("hub-nope.db")


class RevisionTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_save_records_changed_paths_and_restore_undoes(self):
        config = config_store.load()
        config["agents"]["dev-qa"]["name"] = "Tester"
        config_store.save(config)
        rev = revisions.recent()[0]
        self.assertEqual(rev["changed"], ["agents.dev-qa"])
        self.assertEqual(revisions.diff(rev["id"])[0]["before"]["name"], "QA")
        revisions.restore(rev["id"])
        self.assertEqual(config_store.load()["agents"]["dev-qa"]["name"], "QA")
        newest = revisions.recent()[0]
        self.assertEqual((newest["source"], newest["restored_from"]), ("restore", rev["id"]))

    def test_unchanged_save_keeps_no_revision(self):
        config_store.save(config_store.load())
        config_store.save(config_store.load())
        # The first save may normalise formatting only; neither changes content.
        self.assertEqual(revisions.recent(), [])

    def test_swarm_entries_never_reach_revisions(self):
        hub.create_workspace("demo", None, "a task")
        config = config_store.load()
        config["voice"] = {"on": True}
        config_store.save(config)
        rev = revisions.get(revisions.recent()[0]["id"])
        self.assertNotIn("orchestrator", rev["after"]["agents"])
        self.assertEqual(rev["changed"], ["voice"])

    def test_changed_paths_depth(self):
        self.assertEqual(revisions.changed_paths({"a": {"b": {"c": 1}}}, {"a": {"b": {"c": 2}}}),
                         ["a.b"])
        self.assertEqual(revisions.changed_paths({"a": 1}, {"b": 1}), ["a", "b"])


class ActivityAndSearchTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_timeline_filters(self):
        activity.log("you", "task.assigned", "Gave QA a task", workspace=3)
        activity.log("lago", "decision.made", "Approved plan", workspace=4)
        self.assertEqual(len(activity.timeline()), 2)
        self.assertEqual(activity.timeline(kind="task")[0]["actor"], "you")
        self.assertEqual(activity.timeline(workspace=4)[0]["kind"], "decision.made")

    def test_prune_drops_old_lines(self):
        activity.log("you", "x", "old")
        with hub.connect() as conn:
            conn.execute("UPDATE activity SET created_at = 0")
        activity.log("you", "x", "new")
        self.assertEqual(activity.prune(), 1)

    def test_search_finds_all_words_across_records(self):
        hub.create_task("dev-qa", "Fix checkout bug", "The cart total is wrong")
        hub.add_event(1, "demo", "done", "Checkout fixed and deployed")
        hub.create_task("dev-qa", "Unrelated", "nothing here")
        found = activity.search("checkout")
        self.assertEqual({r["kind"] for r in found}, {"task", "report"})
        self.assertEqual(len(activity.search("cart total")), 1)
        self.assertEqual(activity.search("checkout nonsense"), [])
        self.assertEqual([r["kind"] for r in activity.search("checkout", workspace=1)], ["report"])

    def test_config_change_is_logged(self):
        config = config_store.load()
        config["voice"] = {"on": False}
        config_store.save(config)
        self.assertEqual(activity.timeline(kind="config")[0]["actor"], "app")


if __name__ == "__main__":
    unittest.main()
