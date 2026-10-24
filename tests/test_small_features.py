"""Spend dashboard (#5), quiet sessions (#7) and failure diagnostics (#8)."""

import json
import time
import unittest
from unittest import mock

import support

import diagnostics
import hub
import notifications
import notify
import spending
import voice
import config_store


class SpendingTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_summary_groups_by_project_agent_and_day(self):
        ws = hub.create_workspace("demo", None, "task")
        hub.add_swarm_agent(f"w{ws}-implementer", ws, "demo", "implementer", "Implementer")
        hub.add_swarm_agent(f"w{ws}-implementer-2", ws, "demo", "implementer", "Implementer 2")
        hub.record_spend("s1", f"w{ws}-implementer", ws, 2.0)
        hub.record_spend("s2", f"w{ws}-implementer-2", ws, 1.0)
        hub.record_spend("s3", f"w{ws}", ws, 0.5)
        hub.record_spend("s4", "dev-qa", None, 0.25)
        out = spending.summary(days=7)
        self.assertAlmostEqual(out["total"], 3.75)
        agents = {r["key"]: r["cost"] for r in out["by_agent"]}
        self.assertEqual(agents, {"implementer": 3.0, "conductor": 0.5, "dev-qa": 0.25})
        projects = {r["key"]: r["cost"] for r in out["by_project"]}
        self.assertEqual(projects["demo"], 3.5)
        self.assertEqual(len(out["by_day"]), 7)
        self.assertAlmostEqual(out["by_day"][-1]["cost"], 3.75)

    def test_window_spent_filters(self):
        ws = hub.create_workspace("demo", None, "task")
        hub.add_swarm_agent(f"w{ws}-qa", ws, "demo", "qa-engineer", "QA")
        hub.record_spend("s1", f"w{ws}-qa", ws, 4.0)
        hub.record_spend("s2", "dev-lead", None, 1.0)
        self.assertEqual(spending.window_spent(0), 5.0)
        self.assertEqual(spending.window_spent(0, agent="qa-engineer"), 4.0)
        self.assertEqual(spending.window_spent(0, project="demo"), 4.0)
        self.assertEqual(spending.window_spent(time.time() + 10), 0)


class QuietSessionTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.ws = hub.create_workspace("demo", None, "task")
        hub.update_workspace(self.ws, quiet=1)

    def test_quiet_session_speaks_only_when_it_needs_you(self):
        with mock.patch.object(voice, "announce", return_value="said") as announce:
            notify.report(self.ws, "done", "a stage finished")
            notify.report(self.ws, "question", "which colour?")
        self.assertEqual([c.args[1] for c in announce.call_args_list], ["question"])

    def test_quiet_session_done_stays_out_of_the_bell(self):
        with mock.patch.object(voice, "announce", return_value=""):
            notify.report(self.ws, "done", "a stage finished")
            notify.report(self.ws, "question", "which colour?")
        items = notifications.build(config_store.load(), {})
        self.assertEqual([i["kind"] for i in items], ["question"])


class DiagnosticsTests(unittest.TestCase):
    def write_transcript(self, entries):
        path = support.TMP / f"t{time.time_ns()}.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        return path

    def test_explains_last_failed_tool(self):
        path = self.write_transcript([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "npm test"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                 "content": "3 tests failed"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Tests fail, giving up."}]}},
        ])
        d = diagnostics.explain(path=path)
        self.assertEqual(d["errors"][0]["error"], "3 tests failed")
        text = diagnostics.text(d)
        self.assertIn("npm test", text)
        self.assertIn("giving up", text)

    def test_nothing_to_explain(self):
        self.assertIsNone(diagnostics.explain(session_id=None))
        self.assertEqual(diagnostics.text(None), "")


if __name__ == "__main__":
    unittest.main()
