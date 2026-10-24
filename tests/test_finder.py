import unittest
from unittest import mock

import support

import config_store
import finder
import hub
import swarm


def did(agent_id, catalog_id, title, body="", status="done", project="", rating=None,
        rater="human"):
    """A finished task by a swarm agent, rated `rating` (1 or -1) if given."""
    hub.add_swarm_agent(agent_id, None, project, catalog_id, catalog_id)
    task_id = hub.create_task(agent_id, title, body, from_agent=config_store.ORCHESTRATOR_ID)
    hub.update_task(task_id, status=status)
    if rating is not None:
        hub.rate(catalog_id, task_id, rater, rating, "", "v1")
    return task_id


class HistoryTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_groups_similar_tasks_by_agent_with_their_record(self):
        did("r1-code-reviewer", "code-reviewer", "Security review of the payments API", rating=1)
        did("r2-code-reviewer", "code-reviewer", "Review API security for login", rating=1)
        did("r3-web-researcher", "web-researcher", "Research API pricing of competitors")
        found = finder.history("security review of the orders API")
        self.assertEqual([f["catalog_id"] for f in found], ["code-reviewer"])
        self.assertEqual((found[0]["similar"], found[0]["ups"]), (2, 2))

    def test_one_shared_word_is_not_a_match(self):
        did("r1-contract-reviewer", "contract-reviewer", "Review the supplier contract")
        self.assertEqual(finder.history("review pull request 42"), [])

    def test_your_rating_outweighs_the_conductors(self):
        task = did("r1-code-reviewer", "code-reviewer", "Security review of payments API",
                   rating=1, rater="conductor")
        hub.rate("code-reviewer", task, "human", -1, "missed the injection", "v1")
        found = finder.history("security review api")[0]
        self.assertEqual((found["ups"], found["downs"]), (0, 1))

    def test_better_record_ranks_first_and_failures_count(self):
        did("r1-a", "agent-a", "Write unit tests for parser", status="failed")
        did("r2-b", "agent-b", "Write unit tests for lexer", rating=1)
        found = finder.history("write unit tests for the tokenizer")
        self.assertEqual([f["catalog_id"] for f in found], ["agent-b", "agent-a"])
        self.assertEqual(found[1]["failed"], 1)
        self.assertIn("(failed)", finder.history_text(found))

    def test_test_runs_and_unfinished_tasks_are_not_history(self):
        exam = did("r1-code-reviewer", "code-reviewer", "Security review of payments API")
        run = hub.add_test_run("code-reviewer", "t1", "v1", str(support.TMP))
        hub.update_test_run(run, task_id=exam)
        did("r2-code-reviewer", "code-reviewer", "Security review of login API", status="working")
        self.assertEqual(finder.history("security review api"), [])

    def test_same_project_is_counted(self):
        did("r1-code-reviewer", "code-reviewer", "Security review of payments API",
            project="demo")
        self.assertEqual(finder.history("security review api", "demo")[0]["same_project"], 1)


class FindAgentsTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        import lago_mcp
        self.mcp = lago_mcp
        entry = {"id": "code-reviewer", "name": "Code Reviewer", "summary": "Reviews code",
                 "domain": "quality", "use_when": "", "tags": [], "inputs": [], "outputs": []}
        for p in [mock.patch.object(swarm, "enabled_agents", return_value={"code-reviewer": entry}),
                  mock.patch.object(swarm, "disabled", return_value=set()),
                  mock.patch.object(self.mcp.evals, "ranking", return_value=[]),
                  mock.patch.object(self.mcp, "AGENT_ID", config_store.ORCHESTRATOR_ID)]:
            p.start()
            self.addCleanup(p.stop)

    def test_agents_used_before_come_first(self):
        did("r1-code-reviewer", "code-reviewer", "Security review of payments API", rating=1)
        did("r2-gone", "retired-agent", "Security review of login API", rating=1)
        text = self.mcp.tool_find_agents("security review of the orders api")
        self.assertTrue(text.startswith("Used before for similar tasks"))
        self.assertIn("`code-reviewer`: 1 similar task, 1 finished, 1 up, 0 down", text)
        self.assertNotIn("retired-agent", text)

    def test_no_history_leaves_the_search_as_it_was(self):
        self.assertFalse(self.mcp.tool_find_agents("review").startswith("Used before"))


if __name__ == "__main__":
    unittest.main()
