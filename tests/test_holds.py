"""Holding a work session (#15): agents interrupted, nothing delivered until it resumes."""

import unittest
from unittest import mock

import support

import holds
import hub
import runner
import swarm
import team


class HoldTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.ws = hub.create_workspace("demo", None, "task")
        self.conductor = f"w{self.ws}"
        self.agent = f"w{self.ws}-qa"
        hub.add_swarm_agent(self.agent, self.ws, "demo", "qa-engineer", "QA")
        hub.set_state(self.conductor, status="waiting_on_team")
        hub.set_state(self.agent, status="working")
        self.keys, self.pasted = [], []
        patches = [mock.patch.object(runner, "is_running", return_value=True),
                   mock.patch.object(runner, "keys", side_effect=lambda a, *k: self.keys.append((a, k))),
                   mock.patch.object(runner, "paste", side_effect=lambda a, text: self.pasted.append((a, text)))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_hold_interrupts_and_blocks_delivery(self):
        stopped = holds.hold(self.ws, "checking costs")
        self.assertEqual(set(stopped), {self.conductor, self.agent})
        self.assertEqual({a for a, _ in self.keys}, {self.conductor, self.agent})
        self.assertTrue(holds.held(self.agent))
        hub.queue_text(self.agent, "new work")
        team.pump(self.agent)
        self.assertEqual(self.pasted, [])
        with self.assertRaises(holds.HoldError):
            holds.hold(self.ws)

    def test_release_tells_each_to_carry_on(self):
        holds.hold(self.ws)
        hub.queue_text(self.agent, "new work")
        holds.release(self.ws)
        self.assertFalse(holds.held(self.agent))
        texts = dict(self.pasted)
        self.assertIn(holds.RESUME_TEXT, texts[self.agent])
        self.assertIn("new work", texts[self.agent])
        self.assertIn(holds.RESUME_TEXT, texts[self.conductor])

    def test_run_step_refuses_while_held(self):
        pid = hub.create_pipeline(self.ws, "demo", None, "Plan", "goal", [
            {"id": "a", "agent": "qa-engineer", "title": "A", "after": [], "status": "pending",
             "attempts": 0, "task_id": None, "result": ""}])
        hub.update_pipeline(pid, status="approved")
        holds.hold(self.ws)
        with self.assertRaisesRegex(team.TeamError, "on hold"):
            swarm.run_step(pid, "a")

    def test_agents_outside_sessions_are_never_held(self):
        self.assertFalse(holds.held("r3-web-researcher"))
        self.assertFalse(holds.held("dev-qa"))


if __name__ == "__main__":
    unittest.main()
