import time
import unittest

import support

import config_store
import dashboard
import hub

DAY = 86400


def task(status, days_ago=0):
    task_id = hub.create_task("builder", f"Task {status}", "body")
    with hub.connect() as conn:
        conn.execute("UPDATE tasks SET status = ?, created_at = ? WHERE id = ?",
                     (status, time.time() - days_ago * DAY, task_id))
    return task_id


class DashboardTests(unittest.TestCase):
    """The Dashboard's charts: tasks by outcome, spend and success rate per day."""

    def setUp(self):
        support.reset()

    def test_counts_each_day_by_outcome(self):
        task("done"), task("done"), task("failed"), task("working"), task("done", days_ago=3)
        series = dashboard.days(14)
        self.assertEqual(len(series), 14)
        today, earlier = series[-1], series[-4]
        self.assertEqual((today["done"], today["failed"], today["other"]), (2, 1, 1))
        self.assertAlmostEqual(today["rate"], 2 / 3, places=3)
        self.assertEqual((earlier["done"], earlier["rate"]), (1, 1.0))
        self.assertIsNone(series[0]["rate"])  # nothing finished that day

    def test_leaves_out_older_tasks(self):
        task("done", days_ago=20)
        self.assertEqual(sum(d["done"] for d in dashboard.days(14)), 0)

    def test_spend_per_day_and_month(self):
        hub.record_spend("s1", "builder", None, 2.5)
        summary = dashboard.summary()
        self.assertEqual(summary["days"][-1]["cost"], 2.5)
        self.assertEqual(summary["month"], {"spent": 2.5, "budget": 0})

    def test_month_budget_for_everything(self):
        config = config_store.load()
        config["budget_windows"] = [{"scope": "all", "window": "month", "limit": 300},
                                    {"scope": "project:x", "window": "month", "limit": 5}]
        config_store.save(config)
        self.assertEqual(dashboard.month()["budget"], 300)

    def test_summary_totals_and_recent(self):
        task("done"), task("failed")
        summary = dashboard.summary()
        self.assertEqual((summary["done"], summary["failed"], summary["rate"]), (1, 1, 0.5))
        self.assertEqual(len(summary["recent_tasks"]), 2)


if __name__ == "__main__":
    unittest.main()
