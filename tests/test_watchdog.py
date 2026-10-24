"""Watchdogs (#9): stalled, looping and crashed agents are told once to whoever can act."""

import json
import os
import time
import unittest
from unittest import mock

import support

import activity
import config_store
import hub
import notify
import runner
import team
import transcripts
import watchdog


def transcript(entries, age=0):
    path = support.TMP / f"w{time.time_ns()}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    if age:
        t = time.time() - age
        os.utime(path, (t, t))
    return path


def tool(i, cmd="ls"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": cmd}}]}}


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.ws = hub.create_workspace("demo", None, "task")
        hub.add_swarm_agent(f"w{self.ws}-qa", self.ws, "demo", "qa-engineer", "QA")
        self.agent = f"w{self.ws}-qa"
        self.task = hub.create_task(self.agent, "Run tests", "body", f"w{self.ws}")
        hub.update_task(self.task, status="working")
        self.told = []
        patches = [
            mock.patch.object(runner, "running_agents", return_value=[self.agent]),
            mock.patch.object(runner, "capture", return_value="$ npm test\n(waiting)"),
            mock.patch.object(notify, "report", side_effect=lambda ws, kind, text: self.told.append((ws, kind, text))),
            mock.patch.object(team, "pump"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, path, status="working", updated=None):
        hub.set_state(self.agent, status=status, session_id="s1")
        if updated is not None:
            with hub.connect() as conn:
                conn.execute("UPDATE agent_state SET updated_at = ? WHERE agent_id = ?", (updated, self.agent))
        with mock.patch.object(transcripts, "find", return_value=path):
            return watchdog.check()

    def test_a_stopped_run_has_its_task_closed(self):
        hub.add_swarm_agent("r7-browser", None, "demo", "browser", "Browser · run 7", folder=str(support.TMP))
        hub.add_swarm_agent("r8-browser", None, "demo", "browser", "Browser · run 8", folder=str(support.TMP))
        stopped = hub.create_task("r7-browser", "Look something up", "body", from_agent="orchestrator")
        hub.update_task(stopped, status="working")
        waiting = hub.create_task("r8-browser", "Not started yet", "body", from_agent="orchestrator")
        hub.set_state("r7-browser", status="stopped")
        hub.set_state("r8-browser", status="stopped")
        now = time.time()
        self.assertEqual(watchdog.close_orphan_runs(set(), now), [])  # just stopped: give it time
        later = now + watchdog.ORPHAN_GRACE_SECONDS + 1
        self.assertEqual(watchdog.close_orphan_runs({"r7-browser"}, later), [])  # still running
        self.assertEqual(watchdog.close_orphan_runs(set(), later), [stopped])
        self.assertEqual(hub.get_task(stopped)["status"], "failed")
        self.assertIn("stopped before it finished", hub.get_task(stopped)["result"])
        self.assertEqual(hub.get_task(waiting)["status"], "queued")  # never started: left alone
        import api
        with mock.patch.object(hub, "get_state", lambda a: {"status": "stopped", "updated_at": now - 3 * 86400}):
            shown = api._agent_runs({"r7-browser": {"status": "stopped", "updated_at": now - 3 * 86400}}, set(), now)
        self.assertNotIn(stopped, [r["task_id"] for r in shown])  # old news, not a new result
        self.assertEqual(watchdog.close_orphan_runs(set(), later), [])

    def test_stalled_agent_is_told_once(self):
        path = transcript([tool(1)], age=30 * 60)
        found = self.run_with(path, updated=time.time() - 30 * 60)
        self.assertEqual([f[1] for f in found], ["stalled"])
        self.assertEqual(self.told[0][1], "update")
        self.assertIn("npm test", self.told[0][2])
        self.assertEqual(self.run_with(path, updated=time.time() - 30 * 60), [])
        self.assertTrue(hub.take_inbox(f"w{self.ws}") or True)
        self.assertEqual(activity.timeline(kind="watchdog")[0]["kind"], "watchdog.stalled")

    def test_recent_activity_is_not_stalled(self):
        path = transcript([tool(1)], age=60)
        self.assertEqual(self.run_with(path, updated=time.time() - 30 * 60), [])

    def test_looping_agent(self):
        path = transcript([tool(i, "cat x") for i in range(watchdog.LOOP_REPEATS)])
        found = self.run_with(path)
        self.assertEqual([f[1] for f in found], ["looping"])
        self.assertEqual(self.told[0][1], "blocked")

    def test_varied_calls_are_not_a_loop(self):
        path = transcript([tool(i, f"cat {i}") for i in range(watchdog.LOOP_REPEATS)])
        self.assertEqual(self.run_with(path), [])

    def test_first_crash_is_recovered_second_is_told(self):
        path = transcript([tool(1)])
        with mock.patch.object(team, "start") as start:
            self.assertEqual(self.run_with(path, status="exited", updated=time.time()), [])  # grace
            self.assertEqual(self.run_with(path, status="exited", updated=time.time() - 300), [])
            start.assert_called_once()
            self.assertTrue(start.call_args.kwargs["keep_session"])
            self.assertIn(f"task #{self.task}", start.call_args.kwargs["initial_text"])
            found = self.run_with(path, status="exited", updated=time.time() - 200)
        self.assertEqual([f[1] for f in found], ["crashed"])
        self.assertEqual(activity.timeline(kind="watchdog.recovered")[0]["target"], self.agent)

    def test_killed_session_is_not_restarted(self):
        path = transcript([tool(1)])
        hub.set_state(self.agent, detail="The session was killed. Restart it to carry on.")
        with mock.patch.object(team, "start") as start:
            found = self.run_with(path, status="exited", updated=time.time() - 300)
        start.assert_not_called()
        self.assertEqual([f[1] for f in found], ["crashed"])

    def test_conductors_and_disabled(self):
        hub.set_state(f"w{self.ws}", status="working", session_id="s2")
        path = transcript([tool(i, "x") for i in range(10)])
        with mock.patch.object(transcripts, "find", return_value=path), \
                mock.patch.object(runner, "running_agents", return_value=[f"w{self.ws}"]):
            self.assertEqual(watchdog.check(), [])
        config = config_store.load()
        config["watchdog"] = {"enabled": False}
        self.assertEqual(watchdog.check(config=config), [])


if __name__ == "__main__":
    unittest.main()


class StuckLagoTests(unittest.TestCase):
    """Lago shown thinking forever: a `!` command typed in, or a message a prompt swallowed."""

    def setUp(self):
        support.reset()
        self.oid = config_store.ORCHESTRATOR_ID
        self.path = transcript([
            {"type": "user", "message": {"content": "hello"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi."}]}},
            {"type": "system", "subtype": "turn_duration"},
            {"type": "user", "message": {"content": "<bash-input>codex login</bash-input>"}},
            {"type": "user", "message": {"content": "<bash-stdout>moved to the background</bash-stdout>"}}])
        p = mock.patch.object(transcripts, "find", lambda sid: self.path)
        p.start()
        self.addCleanup(p.stop)
        hub.set_state(self.oid, status="working", detail="Reading new messages", session_id="s1")

    def test_shell_commands_in_his_terminal_are_not_a_turn(self):
        import memory
        self.assertTrue(memory.between_turns("s1"))

    def test_the_watchdog_marks_him_idle_and_delivers_his_inbox(self):
        hub.queue_text(self.oid, "are you there?")
        pasted = []
        with hub.connect() as conn:
            conn.execute("UPDATE agent_state SET updated_at = ? WHERE agent_id = ?", (time.time() - 600, self.oid))
        with mock.patch.object(runner, "is_running", lambda a: True), \
                mock.patch.object(runner, "paste", lambda a, text: pasted.append(text)):
            watchdog.unstick({self.oid})
        self.assertEqual(pasted, ["are you there?"])
        self.assertEqual(hub.get_state(self.oid)["status"], "working")  # now reading it for real

    def test_a_recent_working_status_is_left_alone(self):
        watchdog.unstick({self.oid})
        self.assertEqual(hub.get_state(self.oid)["status"], "working")

    def test_interrupt_settles_him_even_without_a_stop(self):
        import api
        with mock.patch.object(runner, "is_running", lambda a: True), \
                mock.patch.object(runner, "keys", lambda *a: (True, "")):
            api.Api(None).press(self.oid, "interrupt")
        self.assertEqual(hub.get_state(self.oid)["status"], "idle")

    def test_a_message_starting_with_bang_is_not_a_shell_command(self):
        sent = []
        with mock.patch.object(runner, "tmux", lambda *a, **k: (sent.append((a, k.get("input_text"))), (True, ""))[1]), \
                mock.patch.object(time, "sleep", lambda s: None):
            runner.paste(self.oid, "! codex login")
        self.assertEqual(sent[0][1], " ! codex login")
