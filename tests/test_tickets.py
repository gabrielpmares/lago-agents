"""Tasks as tickets (#27): blocked-by links, comment threads, labels."""

import unittest
from unittest import mock

import support

import hub
import team
import tickets


class TicketTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        p = mock.patch.object(team, "pump")
        p.start()
        self.addCleanup(p.stop)

    def inbox(self, agent):
        return hub.take_inbox(agent)

    def test_blocked_task_waits_then_goes_out(self):
        a = team.assign("dev-qa", "Write tests", "body", from_agent="dev-lead")
        b = team.assign("dev-qa", "Run tests", "body", from_agent="dev-lead", blocked_by=[a], labels=["QA", "qa", "Release 1"])
        self.assertEqual(hub.get_task(b)["status"], "blocked")
        self.assertEqual(tickets.labels(hub.get_task(b)), ["qa", "release-1"])
        texts = self.inbox("dev-qa")
        self.assertEqual(len(texts), 1)  # only the first went out
        team.finish(a, "done")
        self.assertEqual(hub.get_task(b)["status"], "sent")
        self.assertIn("Run tests", self.inbox("dev-qa")[0])

    def test_failed_blocker_tells_the_lead(self):
        a = team.assign("dev-qa", "A", "body", from_agent="dev-lead")
        b = team.assign("dev-qa", "B", "body", from_agent="dev-lead", blocked_by=[a])
        self.inbox("dev-lead")
        team.finish(a, "broke", status="failed")
        self.assertEqual(hub.get_task(b)["status"], "blocked")
        self.assertTrue(any("stays blocked" in t for t in self.inbox("dev-lead")))

    def test_already_done_blocker_does_not_block(self):
        a = team.assign("dev-qa", "A", "body", from_agent="dev-lead")
        team.finish(a, "ok")
        b = team.assign("dev-qa", "B", "body", from_agent="dev-lead", blocked_by=[a])
        self.assertEqual(hub.get_task(b)["status"], "sent")
        with self.assertRaises(team.TeamError):
            team.assign("dev-qa", "C", "body", blocked_by=[999])

    def test_comments_go_to_the_other_side(self):
        t = team.assign("dev-qa", "A", "body", from_agent="dev-lead")
        self.inbox("dev-qa")
        self.assertEqual(tickets.comment(t, "Also check Safari", author="you"), "dev-qa")
        self.assertIn("Also check Safari", self.inbox("dev-qa")[0])
        self.assertEqual(tickets.comment(t, "Safari has no WebUSB", author="dev-qa"), "dev-lead")
        self.assertIn("Safari has no WebUSB", self.inbox("dev-lead")[0])
        self.assertEqual([c["author"] for c in tickets.comments(t)], ["you", "dev-qa"])

    def test_reopen_with_comment(self):
        t = team.assign("dev-qa", "A", "body", from_agent="dev-lead")
        team.finish(t, "first try")
        self.inbox("dev-qa")
        self.assertIsNone(tickets.comment(t, "just a note", author="you"))
        tickets.comment(t, "Cover the empty cart too", author="you", reopen=True)
        self.assertEqual(hub.get_task(t)["status"], "sent")
        text = self.inbox("dev-qa")[0]
        self.assertIn("reopened", text)
        self.assertIn("first try", text)


if __name__ == "__main__":
    unittest.main()
