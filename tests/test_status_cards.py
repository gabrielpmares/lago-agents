"""Status cards (#17) and project summaries (#19): rewritten only when what they watch changes."""

import unittest

import support

import activity
import hub
import statuscards


class StatusCardTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.prompts = []
        self.ask = lambda p: (self.prompts.append(p), f"written #{len(self.prompts)}")[1]
        self.ws = hub.create_workspace("demo", None, "Checkout")

    def test_rewrites_only_on_change(self):
        hub.add_event(self.ws, "demo", "done", "Checkout fixed")
        card = statuscards.add("Demo launch", project="demo")
        card, rewritten = statuscards.refresh(card["id"], ask=self.ask)
        self.assertTrue(rewritten)
        self.assertEqual(card["text"], "written #1")
        self.assertIn("Checkout fixed", self.prompts[0])
        self.assertFalse(statuscards.refresh(card["id"], ask=self.ask)[1])  # nothing new: no model call
        hub.add_event(self.ws, "demo", "question", "Which payment provider?")
        self.assertTrue(statuscards.refresh(card["id"], ask=self.ask)[1])
        self.assertEqual(len(self.prompts), 2)

    def test_topic_card_and_refresh_all(self):
        hub.create_task("dev-qa", "Payments: add Stripe", "body")
        activity.log("you", "task.assigned", "nothing about it")
        card = statuscards.add("Payments", query="stripe")
        self.assertEqual(statuscards.refresh_all(ask=self.ask), [card["id"]])
        self.assertIn("Stripe", self.prompts[0])
        self.assertNotIn("nothing about it", self.prompts[0])
        self.assertEqual(statuscards.refresh_all(ask=self.ask), [])

    def test_empty_material_needs_no_model(self):
        card = statuscards.add("Quiet", project="nothing-here")
        card, _ = statuscards.refresh(card["id"], ask=self.ask)
        self.assertEqual(self.prompts, [])
        self.assertIn("Nothing recorded", card["text"])

    def test_model_failure_keeps_old_text(self):
        hub.add_event(self.ws, "demo", "done", "x")
        card = statuscards.add("Demo", project="demo")
        statuscards.refresh(card["id"], ask=self.ask)
        hub.add_event(self.ws, "demo", "done", "y")

        def broken(prompt):
            raise RuntimeError("claude -p failed")
        card, rewritten = statuscards.refresh(card["id"], ask=broken)
        self.assertFalse(rewritten)
        self.assertEqual((card["text"], card["error"]), ("written #1", "claude -p failed"))

    def test_project_summary_is_one_card(self):
        first = statuscards.project_summary("demo")
        self.assertEqual(statuscards.project_summary("demo")["id"], first["id"])
        self.assertTrue(first["summary"])
        with self.assertRaises(statuscards.CardError):
            statuscards.add("Nothing to watch")


if __name__ == "__main__":
    unittest.main()
