"""Budget windows (#11): weekly and monthly caps for everything, a project or an agent."""

import time
import unittest
from datetime import datetime

import support

import budgets
import config_store
import hub


class BudgetWindowTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.ws = hub.create_workspace("demo", None, "task")
        self.agent = f"w{self.ws}-implementer"
        hub.add_swarm_agent(self.agent, self.ws, "demo", "implementer", "Implementer")
        config = config_store.load()
        config["budgets"] = {"agent": 0, "session": 0}
        config_store.save(config)

    def set_windows(self, windows):
        config = config_store.load()
        config["budget_windows"] = windows
        config_store.save(config)

    def test_window_start(self):
        thursday = datetime(2026, 10, 22, 15, 30).timestamp()
        self.assertEqual(datetime.fromtimestamp(budgets.window_start("week", thursday)), datetime(2026, 10, 19))
        self.assertEqual(datetime.fromtimestamp(budgets.window_start("month", thursday)), datetime(2026, 10, 1))

    def test_invalid_windows_are_ignored(self):
        self.set_windows([{"scope": "all", "window": "year", "limit": 5}, {"scope": "team:x", "window": "week", "limit": 5},
                          {"scope": "all", "window": "week", "limit": 0}, {"scope": "all", "window": "week", "limit": 5}])
        self.assertEqual(len(budgets.windows(config_store.load())), 1)

    def test_project_window_stops_and_extend_lets_go(self):
        self.set_windows([{"scope": "project:demo", "window": "month", "limit": 5}])
        hub.record_spend("s1", self.agent, self.ws, 6.0)
        hub.set_state(self.agent, session_id="s1", status="working")
        why = budgets.check(self.agent, "s1", None)
        self.assertIn("monthly budget for project demo", why)
        self.assertEqual(hub.get_state(self.agent)["status"], "over_budget")
        self.assertIn("Extended", budgets.extend(self.agent))
        self.assertIsNone(budgets.blocked(self.agent, "s1"))
        window = [b for b in budgets.state(self.agent, "s1") if b["kind"] == "window"][0]
        self.assertAlmostEqual(window["limit"], 6.0 + 5 * budgets.WINDOW_EXTEND)

    def test_window_for_another_agent_does_not_apply(self):
        self.set_windows([{"scope": "agent:qa-engineer", "window": "week", "limit": 1}])
        hub.record_spend("s1", self.agent, self.ws, 6.0)
        self.assertIsNone(budgets.check(self.agent, "s1", None))
        self.set_windows([{"scope": "agent:implementer", "window": "week", "limit": 1}])
        self.assertIsNotNone(budgets.check(self.agent, "s1", None))

    def test_window_counts_all_conversations_of_the_scope(self):
        self.set_windows([{"scope": "all", "window": "week", "limit": 10}])
        other = f"w{self.ws}-qa"
        hub.add_swarm_agent(other, self.ws, "demo", "qa-engineer", "QA")
        hub.record_spend("s1", self.agent, self.ws, 6.0)
        hub.record_spend("s2", other, self.ws, 5.0)
        hub.set_state(other, session_id="s2", status="over_budget")
        self.assertIsNotNone(budgets.check(self.agent, "s1", None))
        # Extending from one agent resumes every agent the window stopped.
        stopped = budgets._stopped_agents(self.agent, [b for b in budgets.state(self.agent, "s1") if budgets._over(b)])
        self.assertIn(other, stopped)

    def test_orchestrator_is_exempt(self):
        self.set_windows([{"scope": "all", "window": "week", "limit": 1}])
        self.assertEqual(budgets.state(config_store.ORCHESTRATOR_ID), [])


if __name__ == "__main__":
    unittest.main()
