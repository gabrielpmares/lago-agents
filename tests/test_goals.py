"""Goals (#12): per-project goals, sub-goals, linked sessions and progress."""

import unittest

import support

import goals
import hub


class GoalTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_tree_link_and_progress(self):
        top = goals.add("Launch Webstore", "demo", target="Q1")
        sub = goals.add("Billing works", "demo", parent=top["id"])
        ws1 = hub.create_workspace("demo", None, "Stripe")
        ws2 = hub.create_workspace("demo", None, "Invoices")
        goals.link(ws1, sub["id"])
        goals.link(ws2, sub["id"])
        hub.add_event(ws1, "demo", "solved", "done")
        hub.update_workspace(ws2, status="closed")
        p = goals.progress(sub["id"])
        self.assertEqual(p["counts"], {"open": 1, "solved": 1, "closed": 1})
        self.assertEqual(p["parents"], ["Launch Webstore"])
        self.assertEqual([c["id"] for c in goals.progress(top["id"])["children"]], [sub["id"]])
        lines = goals.session_lines(hub.get_workspace(ws1))
        self.assertIn("Billing works", "\n".join(lines))
        self.assertIn("Which serves: Launch Webstore", "\n".join(lines))

    def test_rules(self):
        top = goals.add("A", "demo")
        with self.assertRaises(goals.GoalError):
            goals.add("", "demo")
        with self.assertRaises(goals.GoalError):
            goals.add("B", "other", parent=top["id"])
        sub = goals.add("B", "demo", parent=top["id"])
        with self.assertRaises(goals.GoalError):
            goals.update(top["id"], parent_id=sub["id"])  # no cycles
        ws = hub.create_workspace("elsewhere", None, "x")
        with self.assertRaises(goals.GoalError):
            goals.link(ws, top["id"])
        goals.update(top["id"], status="achieved")
        self.assertEqual([g["id"] for g in goals.all_goals("demo")], [sub["id"]])


if __name__ == "__main__":
    unittest.main()
