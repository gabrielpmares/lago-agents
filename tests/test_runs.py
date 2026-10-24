import time
import unittest
from unittest import mock

import support

import api
import config_store
import hub
import lago_hook
import team


class DirectRunEndingTests(unittest.TestCase):
    """A run Lago started must not sit open after its turn ended without a result."""

    def setUp(self):
        support.reset()
        config = config_store.load()
        config["agents"]["r1-builder"] = {"name": "Builder · run 1", "direct": True}
        config_store.save(config)
        hub.add_swarm_agent("r1-builder", None, "", "builder", "Builder · run 1")
        self.task = hub.create_task("r1-builder", "Build it", "body",
                                    from_agent=config_store.ORCHESTRATOR_ID)
        hub.update_task(self.task, status="working")
        for p in [mock.patch.object(lago_hook.transcripts, "find", return_value=None),
                  mock.patch.object(team, "pump")]:
            p.start()
            self.addCleanup(p.stop)

    def test_an_api_error_fails_the_task(self):
        lago_hook.on_event("r1-builder", {"hook_event_name": "StopFailure", "error": "invalid_request",
                                          "error_details": "safeguards flagged this session"})
        task = hub.get_task(self.task)
        self.assertEqual(task["status"], "failed")
        self.assertIn("safeguards flagged", task["result"])

    def test_an_interrupted_turn_fails_the_task_once_idle(self):
        lago_hook.on_idle("r1-builder", {})
        task = hub.get_task(self.task)
        self.assertEqual(task["status"], "failed")
        self.assertIn("interrupted", task["result"])

    def test_a_team_member_task_stays_open(self):
        config = config_store.load()
        config["agents"]["r1-builder"]["direct"] = False
        config_store.save(config)
        lago_hook.on_idle("r1-builder", {})
        self.assertEqual(hub.get_task(self.task)["status"], "working")


class CloseRunsTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        hub.add_swarm_agent("r1-web", None, "", "web", "Web · run 1")
        hub.add_swarm_agent("r2-web", None, "", "web", "Web · run 2")
        self.done = hub.create_task("r1-web", "Search", "b")
        hub.update_task(self.done, status="done", result="found it")
        self.open = hub.create_task("r2-web", "Search more", "b")
        hub.update_task(self.open, status="working")

    def shown(self):
        return [r["task_id"] for r in api._agent_runs({}, set(), time.time())]

    def test_closed_runs_leave_the_panel_and_open_ones_cannot_close(self):
        self.assertEqual(sorted(self.shown()), sorted([self.done, self.open]))
        api.Api.close_runs(None, [self.done, self.open])
        self.assertEqual(self.shown(), [self.open])


if __name__ == "__main__":
    unittest.main()
