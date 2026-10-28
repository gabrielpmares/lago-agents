"""Clearing Lago's side panel and the bell: reports, reminders, finished tasks, notifications."""

import unittest
from unittest import mock

import support

import api
import hub
import notifications
import schedules


class ClearingTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.api = api.Api(mock.Mock())
        self.ws = hub.create_workspace("demo", None, "Checkout fix")
        self.first = hub.add_event(self.ws, "demo", "done", "stage one")
        self.second = hub.add_event(self.ws, "demo", "question", "which colour?")

    def test_one_report_leaves_the_list_and_counts_as_seen(self):
        self.api.clear_reports(event_ids=[self.first])
        self.assertEqual([e["id"] for e in hub.events("cleared = 0")], [self.second])
        self.assertEqual(hub.events("id = ?", (self.first,))[0]["seen"], 1)

    def test_clear_all_keeps_the_reports_for_lago(self):
        self.api.clear_reports()
        self.assertEqual(hub.events("cleared = 0"), [])
        self.assertEqual(len(hub.events()), 2)
        self.assertEqual(hub.events("seen = 0"), [])

    def test_cancel_all_reminders(self):
        schedules.add("water the plants", in_minutes=30)
        schedules.add("stand up", in_minutes=60, repeat="daily")
        self.api.cancel_all_schedules()
        self.assertEqual(schedules.active(), [])

    def test_close_finished_closes_only_solved_sessions(self):
        busy = hub.create_workspace("demo", None, "Still going")
        hub.add_event(self.ws, "demo", "solved", "all delivered")
        with mock.patch.object(api.swarm, "close_workspace") as close:
            self.api.close_finished_workspaces()
        close.assert_called_once_with(self.ws)
        self.assertNotIn(mock.call(busy), close.call_args_list)

    def test_bell_clear_all_dismisses_every_key(self):
        self.api.dismiss_notifications(["event:1", "pipeline:2"])
        self.assertEqual(notifications._dismissed(), {"event:1", "pipeline:2"})


if __name__ == "__main__":
    unittest.main()
