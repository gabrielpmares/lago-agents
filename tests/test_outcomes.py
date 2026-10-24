import unittest
from unittest import mock

import support

import config_store
import evals
import hub
import outcomes
import swarm
from team import TeamError

ENTRY = {"id": "x", "name": "X", "summary": "", "domain": "dev", "use_when": "", "tags": [],
         "inputs": [], "outputs": [], "body": "prompt", "source": ""}


def entry(catalog_id):
    return {**ENTRY, "id": catalog_id}


CATALOG = {c: entry(c) for c in ("architect", "implementer", "reviewer", "researcher")}


class Base(unittest.TestCase):
    def setUp(self):
        support.reset()
        for p in [mock.patch.object(evals, "_entry", side_effect=lambda c: CATALOG[c]),
                  mock.patch.object(evals, "maybe_improve", return_value=None),
                  mock.patch.object(evals, "stars", return_value=0),
                  mock.patch.object(evals, "tests", return_value=[])]:
            p.start()
            self.addCleanup(p.stop)

    def task(self, agent_id, catalog_id, status="done", workspace=None):
        hub.add_swarm_agent(agent_id, workspace, "demo", catalog_id, catalog_id)
        task_id = hub.create_task(agent_id, f"{catalog_id} step", "body")
        hub.update_task(task_id, status=status)
        return task_id

    def outcome(self, task_id):
        rows = hub.ratings("task_id = ? AND rater = 'outcome'", (task_id,))
        return (rows[0]["verdict"], rows[0]["weight"]) if rows else None


class SharesTests(unittest.TestCase):
    def test_accepted_split_by_how_each_step_went(self):
        split = outcomes.shares([(1, "done", 1), (2, "done", 0), (3, "done", -1)], True)
        self.assertEqual(split[1][0], 1)
        self.assertAlmostEqual(split[1][1], 2 * split[2][1])  # rated up earns twice unrated
        self.assertAlmostEqual(split[1][1] + split[2][1], outcomes.OUTCOME_WEIGHT)
        self.assertEqual(split[3][0], -1)  # rated down loses points even in a good job

    def test_one_agent_gets_the_whole_share(self):
        self.assertEqual(outcomes.shares([(1, "done", 0)], True), {1: (1, outcomes.OUTCOME_WEIGHT)})

    def test_rejected_blames_failed_steps_only(self):
        split = outcomes.shares([(1, "done", 1), (2, "failed", 0)], False)
        self.assertEqual(split, {2: (-1, outcomes.OUTCOME_WEIGHT)})

    def test_rejected_with_no_culprit_blames_every_step(self):
        split = outcomes.shares([(1, "done", 0), (2, "done", 1)], False)
        self.assertEqual({t: v for t, (v, _) in split.items()}, {1: -1, 2: -1})


class JudgeSessionTests(Base):
    def setUp(self):
        super().setUp()
        self.ws = hub.create_workspace("demo", None, "Build the export")
        self.design = self.task("w1-architect", "architect", workspace=self.ws)
        self.code = self.task("w1-implementer", "implementer", workspace=self.ws)
        hub.rate("architect", self.design, "conductor", 1, "", "v")
        steps = [{"id": "design", "agent": "architect", "status": "done", "task_id": self.design},
                 {"id": "code", "agent": "implementer", "status": "done", "task_id": self.code},
                 {"id": "later", "agent": "reviewer", "status": "skipped", "task_id": None}]
        pid = hub.create_pipeline(self.ws, "demo", None, "Plan", "goal", steps)
        hub.update_pipeline(pid, status="done")

    def test_accepting_shares_the_points(self):
        line = outcomes.judge_session(self.ws, True)
        self.assertIn("Points to architect, implementer", line)
        self.assertEqual(self.outcome(self.design)[0], 1)
        self.assertGreater(self.outcome(self.design)[1], self.outcome(self.code)[1])
        self.assertEqual(hub.get_workspace(self.ws)["outcome"], "accepted")

    def test_judging_again_replaces_the_verdict(self):
        outcomes.judge_session(self.ws, True)
        outcomes.judge_session(self.ws, False, "the export drops rows")
        self.assertEqual(self.outcome(self.design)[0], -1)
        self.assertEqual(self.outcome(self.code)[0], -1)
        self.assertEqual(hub.get_workspace(self.ws)["outcome"], "rejected")

    def test_rejecting_needs_a_reason(self):
        with self.assertRaises(TeamError):
            outcomes.judge_session(self.ws, False)

    def test_outcome_moves_the_score(self):
        before = evals.score(CATALOG["implementer"])["score"]
        outcomes.judge_session(self.ws, True)
        self.assertGreater(evals.score(CATALOG["implementer"])["score"], before)


class TestDriveTests(Base):
    def setUp(self):
        super().setUp()
        self.n = 0

        def fake_run(catalog_id, task, folder="", from_agent=None, **_):
            self.n += 1
            return self.task(f"r{self.n}-{catalog_id}", catalog_id, status="working"), "", ""
        for p in [mock.patch.object(swarm, "enabled_agents", return_value=CATALOG),
                  mock.patch.object(swarm, "run_agent", side_effect=fake_run),
                  mock.patch.object(outcomes, "DRIVES_DIR", support.TMP / "drives")]:
            p.start()
            self.addCleanup(p.stop)

    def finish_all(self, drive_id):
        for task_id in hub.get_drive(drive_id)["tasks"].values():
            hub.update_task(task_id, status="done")

    def test_needs_two_or_three_agents(self):
        with self.assertRaises(TeamError):
            outcomes.start_drive(["architect"], "do it")
        with self.assertRaises(TeamError):
            outcomes.start_drive(list(CATALOG), "do it")

    def test_pick_waits_for_every_result(self):
        drive_id, _ = outcomes.start_drive(["architect", "researcher"], "compare options")
        with self.assertRaises(TeamError):
            outcomes.pick(drive_id, "architect")

    def test_pick_rates_winner_a_little_and_losers_less(self):
        drive_id, started = outcomes.start_drive(["architect", "researcher"], "compare options")
        self.finish_all(drive_id)
        outcomes.pick(drive_id, "researcher", "clearer")
        won = hub.ratings("task_id = ?", (started["researcher"],))[0]
        lost = hub.ratings("task_id = ?", (started["architect"],))[0]
        self.assertEqual((won["verdict"], won["weight"]), (1, outcomes.PICK_WEIGHT))
        self.assertEqual((lost["verdict"], lost["weight"]), (-1, outcomes.LOSER_WEIGHT))
        self.assertEqual(hub.get_drive(drive_id)["picked"], "researcher")
        card = evals.score(CATALOG["architect"])
        self.assertEqual(card["downs"], 0)  # losing a drive isn't a thumbs down

    def test_picking_again_replaces_the_pick(self):
        drive_id, started = outcomes.start_drive(["architect", "researcher"], "compare options")
        self.finish_all(drive_id)
        outcomes.pick(drive_id, "researcher")
        outcomes.pick(drive_id, "architect")
        self.assertEqual(hub.ratings("task_id = ?", (started["architect"],))[0]["verdict"], 1)
        self.assertEqual(len(hub.ratings("rater = 'pick'")), 2)


if __name__ == "__main__":
    unittest.main()
