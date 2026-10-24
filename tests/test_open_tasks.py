import time
import unittest
from unittest import mock

import support

import accounts
import config_store
import hub
import open_tasks
import runner
import team

RUN = "r1-builder"


class RunCase(unittest.TestCase):
    """One run of a builder, with a task it's working on; nothing really starts or stops."""

    def setUp(self):
        support.reset()
        config = config_store.load()
        config["agents"][RUN] = {"name": "Builder · run 1", "direct": True, "swarm": True}
        config_store.save(config)
        hub.add_swarm_agent(RUN, None, "", "builder", "Builder · run 1")
        self.task = hub.create_task(RUN, "Build it", "body", from_agent=config_store.ORCHESTRATOR_ID)
        hub.update_task(self.task, status="working")
        self.running = set()
        for p in [mock.patch.object(runner, "running_agents", side_effect=lambda: self.running),
                  mock.patch.object(runner, "is_running", side_effect=lambda a: a in self.running),
                  mock.patch.object(accounts, "room_again_at", return_value=None),
                  mock.patch.object(accounts, "with_room_now", return_value="main"),
                  mock.patch.object(team, "start"), mock.patch.object(team, "pump"),
                  mock.patch.object(team, "stop")]:
            p.start()
            self.addCleanup(p.stop)



class OpenTaskTests(RunCase):
    """An open run says where it honestly stands, and can be carried on or abandoned."""

    def state(self):
        return open_tasks.runs()[0]["state"]

    def test_a_stopped_run_says_nobody_is_on_it(self):
        hub.set_state(RUN, status="stopped")
        row = open_tasks.runs()[0]
        self.assertEqual((row["state"], row["can_continue"]), ("stopped", True))
        self.assertIn("Nobody is working on it", row["says"])

    def test_waiting_on_usage_and_room_again(self):
        hub.set_state(RUN, status="stopped", usage_wait=time.time() + 3600)
        self.assertIn("carries on by itself", open_tasks.runs()[0]["says"])
        accounts.room_again_at.return_value = time.time()
        row = open_tasks.runs()[0]
        self.assertEqual(row["state"], "ready")
        self.assertIn("room again now", row["says"])

    def test_running_and_quiet(self):
        self.running.add(RUN)
        hub.set_state(RUN, status="working")
        self.assertEqual(self.state(), "working")
        with mock.patch.object(open_tasks, "_log_age", return_value=open_tasks.QUIET_SECONDS + 60):
            self.assertEqual(self.state(), "quiet")

    def test_continue_restarts_a_stopped_run_on_its_conversation(self):
        hub.set_state(RUN, status="stopped", usage_wait=time.time() + 3600)
        open_tasks.continue_run(self.task)
        team.start.assert_called_once_with(RUN, initial_text=open_tasks.CONTINUE_TEXT, keep_session=True)
        self.assertIsNone(hub.get_state(RUN).get("usage_wait"))

    def test_continue_refuses_while_every_account_is_out(self):
        accounts.with_room_now.return_value = None
        with self.assertRaises(team.TeamError):
            open_tasks.continue_run(self.task)
        team.start.assert_not_called()

    def test_abandon_stops_it_and_closes_the_task(self):
        self.running.add(RUN)
        open_tasks.abandon(self.task, "not needed any more")
        team.stop.assert_called_once_with(RUN)
        task = hub.get_task(self.task)
        self.assertEqual(task["status"], "cancelled")
        self.assertIn("not needed any more", task["result"])
        self.assertEqual(open_tasks.runs(), [])
        with self.assertRaises(team.TeamError):
            open_tasks.abandon(self.task)

    def test_lago_brief_lists_it(self):
        hub.set_state(RUN, status="stopped")
        self.assertIn(f"Task #{self.task} \"Build it\"", open_tasks.brief())


class ResumeAfterResetTests(RunCase):
    """A run stopped by a usage limit carries on once there's room, even if it isn't running."""

    def test_a_stopped_run_with_open_work_carries_on(self):
        hub.set_state(RUN, status="stopped", usage_wait=time.time() - 5)
        with mock.patch.object(accounts, "active", return_value="main"):
            team.resume_after_reset()
        team.start.assert_called_once_with(RUN, initial_text=team.USAGE_RESET_TEXT, keep_session=True)

    def test_newer_readings_resume_it_early(self):
        hub.set_state(RUN, status="stopped", usage_wait=time.time() + 6 * 3600)
        accounts.room_again_at.return_value = time.time()
        with mock.patch.object(accounts, "active", return_value="main"):
            team.resume_after_reset()
        team.start.assert_called_once()

    def test_still_waiting_does_nothing(self):
        hub.set_state(RUN, status="stopped", usage_wait=time.time() + 3600)
        team.resume_after_reset()
        team.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
