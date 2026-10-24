"""Structured questions (#16): a form on a question report, answered back to the Conductor."""

import json
import unittest
from unittest import mock

import support

import hub
import notify
import questions
import team


class QuestionTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.ws = hub.create_workspace("demo", None, "task")
        self.form = questions.clean_form([
            {"id": "db", "prompt": "Which database?", "choices": ["Postgres", "SQLite"], "recommended": "Postgres"},
            {"id": "extras", "prompt": "Also add?", "choices": ["Auth", "Billing", "Search"], "multi": True},
            {"prompt": "Anything else?"}])
        self.messages = []
        p = mock.patch.object(team, "message", side_effect=lambda to, text, from_agent=None: self.messages.append((to, text)))
        p.start()
        self.addCleanup(p.stop)

    def test_clean_form(self):
        qs = json.loads(self.form)["questions"]
        self.assertEqual([q["id"] for q in qs], ["db", "extras", "q3"])
        self.assertTrue(qs[1]["multi"])
        self.assertEqual(qs[2]["choices"], [])
        with self.assertRaises(questions.QuestionError):
            questions.clean_form([{"prompt": ""}])
        with self.assertRaises(questions.QuestionError):
            questions.clean_form([{"id": "a", "prompt": "x"}, {"id": "a", "prompt": "y"}])
        self.assertIsNone(questions.clean_form(None))

    def test_answer_goes_to_conductor_once(self):
        event = notify.report(self.ws, "question", "Some choices to make", self.form)
        questions.answer(event, {"db": "SQLite", "extras": ["Auth", "Nope", "Search"], "q3": "keep it small"}, "thanks")
        to, text = self.messages[0]
        self.assertEqual(to, f"w{self.ws}")
        self.assertIn("Which database? → SQLite", text)
        self.assertIn("Also add? → Auth, Search", text)
        self.assertIn("keep it small", text)
        self.assertIn("Their note: thanks", text)
        with self.assertRaises(questions.QuestionError):
            questions.answer(event, {"db": "SQLite", "extras": ["Auth"], "q3": "x"})

    def test_missing_answers_are_refused(self):
        event = notify.report(self.ws, "question", "?", self.form)
        with self.assertRaisesRegex(questions.QuestionError, "Which database"):
            questions.answer(event, {"db": "MySQL", "extras": ["Auth"], "q3": "x"})
        with self.assertRaisesRegex(questions.QuestionError, "Anything else"):
            questions.answer(event, {"db": "SQLite", "extras": ["Auth"]})
        self.assertEqual(self.messages, [])

    def test_lago_relay_includes_the_form(self):
        hub.update_workspace(self.ws, orchestrated=1)
        with mock.patch.object(notify, "tell_orchestrator") as tell:
            event = notify.report(self.ws, "question", "Pick one", self.form)
        relayed = tell.call_args.args[0]
        self.assertIn(f"report #{event}", relayed)
        self.assertIn("Postgres (recommended) / SQLite", relayed)


if __name__ == "__main__":
    unittest.main()
