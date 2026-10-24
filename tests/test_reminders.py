import time
import unittest
from datetime import datetime
from unittest import mock

import support

import api
import schedules


class ReminderEditingTests(unittest.TestCase):
    """The Routines page changes, runs and deletes Lago's reminders."""

    def setUp(self):
        support.reset()
        with mock.patch("background.start_loops"):  # their first query would race this test's
            self.api = api.Api(None)
        self.row = self.api.save_reminder(text="Daily MCP news", when="09:00", repeat="daily")

    def test_creates_a_daily_reminder(self):
        self.assertEqual(self.row["kind"], "daily")
        self.assertTrue(self.row["repeat"].startswith("every day at 09:00"))
        self.assertEqual([r["id"] for r in self.api.reminders()], [self.row["id"]])

    def test_a_new_one_needs_a_time(self):
        with self.assertRaises(api.ApiError):
            self.api.save_reminder(text="No time", when="", repeat="daily")

    def test_edits_text_and_time(self):
        row = self.api.save_reminder(schedule_id=self.row["id"], text="  MCP   news ", when="07:30", repeat="weekdays")
        self.assertEqual(row["text"], "MCP news")
        self.assertEqual(row["kind"], "weekdays")
        moment = datetime.fromtimestamp(row["next_at"])
        self.assertEqual((moment.hour, moment.minute), (7, 30))
        self.assertLess(moment.weekday(), 5)

    def test_editing_only_the_text_keeps_its_time(self):
        row = self.api.save_reminder(schedule_id=self.row["id"], text="Renamed", repeat="daily")
        self.assertEqual(row["next_at"], self.row["next_at"])

    def test_every_n_minutes_starts_one_interval_from_now(self):
        row = self.api.save_reminder(schedule_id=self.row["id"], repeat="minutes", every_minutes=30)
        self.assertAlmostEqual(row["next_at"], time.time() + 1800, delta=5)
        with self.assertRaises(api.ApiError):
            self.api.save_reminder(schedule_id=self.row["id"], repeat="minutes", every_minutes=1)

    def test_run_now_delivers_and_keeps_the_next_time(self):
        sent = []
        schedules.run_now(self.row["id"], sent.append)
        self.assertIn("Daily MCP news", sent[0])
        self.assertIn(f"Reminder #{self.row['id']}", sent[0])
        self.assertEqual(schedules.get(self.row["id"])["next_at"], self.row["next_at"])

    def test_deleted_reminders_cant_be_edited(self):
        self.api.cancel_schedule(self.row["id"])
        self.assertEqual(self.api.reminders(), [])
        with self.assertRaises(api.ApiError):
            self.api.save_reminder(schedule_id=self.row["id"], text="Back")


if __name__ == "__main__":
    unittest.main()
