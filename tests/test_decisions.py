"""Decision outcomes (#18): verdicts on Lago's decisions go back into his prompt."""

import unittest

import support

import config_store
import decisions
import hub
import memory


class DecisionTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.ws = hub.create_workspace("demo", None, "task")
        self.good = hub.add_event(self.ws, "demo", "decided", "Approved plan #3: small refactor\nWhy: routine")
        self.bad = hub.add_event(self.ws, "demo", "decided", "Raised the price to 12 EUR\nWhy: margins")

    def test_judge_and_read_back(self):
        decisions.judge(self.good, 1)
        decisions.judge(self.bad, -1, "ask me before changing prices")
        events = decisions.with_verdicts(hub.events(limit=10))
        verdicts = {e["id"]: e.get("verdict") for e in events}
        self.assertEqual((verdicts[self.good], verdicts[self.bad]), (1, -1))
        lines = decisions.lessons()
        self.assertTrue(lines[0].startswith("- Not what they'd have done: Raised the price"))
        self.assertIn("ask me before changing prices", lines[0])

    def test_changing_a_verdict_replaces_it(self):
        decisions.judge(self.bad, 1)
        decisions.judge(self.bad, -1, "no")
        self.assertEqual(len(decisions.lessons()), 1)

    def test_only_decisions_can_be_judged(self):
        other = hub.add_event(self.ws, "demo", "done", "stage done")
        with self.assertRaises(decisions.DecisionError):
            decisions.judge(other, 1)
        with self.assertRaises(decisions.DecisionError):
            decisions.judge(self.good, 0)

    def test_prompt_section_only_in_decide_mode(self):
        decisions.judge(self.bad, -1, "ask me first")
        config = config_store.load()
        config["orchestrator"] = {"mode": "check"}
        self.assertNotIn("How your past decisions went", memory.prompt_section(config))
        config["orchestrator"] = {"mode": "decide", "model": "opus"}
        self.assertIn("ask me first", memory.prompt_section(config))


if __name__ == "__main__":
    unittest.main()
