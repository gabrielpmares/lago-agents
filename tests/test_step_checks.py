"""Acceptance criteria (#14) and reviewers (#13) on pipeline steps."""

import unittest
from unittest import mock

import support

import agentdefs
import hub
import stepchecks
import swarm
import team

CATALOG = {a: {"id": a, "name": a.title(), "inputs": [], "outputs": [], "model": None}
           for a in ("implementer", "code-reviewer", "security-auditor")}


class CriteriaParsingTests(unittest.TestCase):
    def test_all_met(self):
        ok, missing = stepchecks.check_criteria(["tests pass", "docs updated"],
                                                "Done.\nCriteria:\n1. met: 42 passed\n2. Met: README section added")
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_unmet_and_unanswered(self):
        ok, missing = stepchecks.check_criteria(["tests pass", "docs updated", "no lint errors"],
                                                "Criteria:\n- 1. not met: 2 failing\n- 3. met: clean")
        self.assertFalse(ok)
        self.assertEqual(missing, ["1. tests pass (not met)", "2. docs updated (not answered)"])

    def test_review_verdicts(self):
        self.assertEqual(stepchecks.parse_review("**APPROVED**, nice work")[0], "approved")
        self.assertEqual(stepchecks.parse_review("CHANGES\n1. rename x")[0], "changes")
        self.assertEqual(stepchecks.parse_review("Looks fine I guess")[0], "changes")


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.ws = hub.create_workspace("demo", None, "task")
        (support.TMP / "demo").mkdir(exist_ok=True)
        patches = [mock.patch.object(agentdefs, "catalog", return_value=CATALOG),
                   mock.patch.object(agentdefs, "skills", return_value={}),
                   mock.patch.object(swarm, "enabled_agents", return_value=CATALOG),
                   mock.patch.object(team, "pump"),
                   mock.patch.object(swarm, "_tell_conductor")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        import config_store
        steps = swarm._normalize_steps(config_store.load(), config_store.load()["projects"]["demo"], [
            {"id": "code", "agent": "implementer", "title": "Write it", "task": "Implement the thing",
             "criteria": ["tests pass"], "reviewers": ["code-reviewer", "security-auditor"]}])
        self.pid = hub.create_pipeline(self.ws, "demo", None, "Plan", "goal", steps)
        hub.update_pipeline(self.pid, status="approved")

    def finish_current(self, result, status="done"):
        p = swarm._get(self.pid)
        step = p["steps"][0]
        task_id = step["review"]["task_id"] if step["status"] == "reviewing" else step["task_id"]
        team.finish(task_id, result, status=status)
        return swarm.get(self.pid)["steps"][0]

    def test_normalize_rejects_unknown_reviewer(self):
        import config_store
        with self.assertRaisesRegex(team.TeamError, "no swarm agent `nobody`"):
            swarm._normalize_steps(config_store.load(), {"cwd": str(support.TMP)}, [
                {"id": "a", "agent": "implementer", "title": "A", "task": "t", "reviewers": ["nobody"]}])

    def test_task_body_lists_criteria(self):
        swarm.run_step(self.pid, "code")
        body = hub.get_task(swarm._get(self.pid)["steps"][0]["task_id"])["body"]
        self.assertIn("## Acceptance criteria", body)
        self.assertIn("1. tests pass", body)

    def test_unmet_criteria_fail_the_step(self):
        swarm.run_step(self.pid, "code")
        step = self.finish_current("Done.\nCriteria:\n1. not met: 3 failing")
        self.assertEqual(step["status"], "failed")
        self.assertIn("aren't met", step["result"])

    def test_reviewers_in_order_then_done(self):
        swarm.run_step(self.pid, "code")
        step = self.finish_current("Done.\nCriteria:\n1. met: green")
        self.assertEqual((step["status"], step["review"]["index"]), ("reviewing", 0))
        self.assertTrue(hub.get_task(step["review"]["task_id"])["to_agent"].endswith("code-reviewer"))
        step = self.finish_current("APPROVED")
        self.assertEqual((step["status"], step["review"]["index"]), ("reviewing", 1))
        step = self.finish_current("APPROVED: no secrets in code")
        self.assertEqual(step["status"], "done")
        self.assertEqual([r["verdict"] for r in step["reviews"]], ["approved", "approved"])

    def test_changes_send_it_back_then_fail_after_max_rounds(self):
        swarm.run_step(self.pid, "code")
        self.finish_current("Criteria:\n1. met: ok")
        step = self.finish_current("CHANGES\n1. handle empty input")
        self.assertEqual(step["status"], "running")  # re-run automatically
        self.assertEqual(step["attempts"], 2)
        body = hub.get_task(step["task_id"])["body"]
        self.assertIn("handle empty input", body)
        self.finish_current("Criteria:\n1. met: ok")
        step = self.finish_current("CHANGES\nstill broken")
        self.assertEqual(step["status"], "failed")
        self.assertIn("after 2 rounds", step["result"])

    def test_finish_refuses_while_reviewing_and_cancel_stops_review(self):
        swarm.run_step(self.pid, "code")
        self.finish_current("Criteria:\n1. met: ok")
        with self.assertRaises(team.TeamError):
            swarm.finish(self.pid, "done", "summary")
        review_task = swarm._get(self.pid)["steps"][0]["review"]["task_id"]
        swarm.cancel(self.pid)
        self.assertEqual(hub.get_task(review_task)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
