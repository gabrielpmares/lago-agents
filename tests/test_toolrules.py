"""Standing tool permissions (toolrules.py): rules, which one wins, and the hook's answer."""

import contextlib
import io
import json
import unittest
from unittest import mock

import support

import config_store
import hub
import lago_hook
import toolrules


def pre_tool(agent_id, tool, tool_input):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch.object(lago_hook.budgets, "check", lambda *a: None):
        lago_hook.on_event(agent_id, {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input})
    text = out.getvalue().strip()
    return json.loads(text)["hookSpecificOutput"]["permissionDecision"] if text else None


class ToolRuleTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_no_rule_leaves_claude_codes_own_permissions(self):
        self.assertIsNone(pre_tool("dev-qa", "Bash", {"command": "npm test"}))

    def test_most_specific_rule_wins(self):
        toolrules.add("Bash", "ask", agent="*")
        toolrules.add("Bash", "allow", agent="qa-engineer", pattern="npm test*")
        toolrules.add("Bash", "deny", agent="*", pattern="rm -rf *")
        self.assertEqual(pre_tool("dev-qa", "Bash", {"command": "npm test -- --watch"}), "allow")
        self.assertEqual(pre_tool("dev-lead", "Bash", {"command": "npm test"}), "ask")  # not a qa-engineer
        self.assertEqual(pre_tool("dev-qa", "Bash", {"command": "rm -rf /"}), "deny")
        self.assertEqual(pre_tool("dev-qa", "Edit", {"file_path": "a.py"}), None)
        hits = {r["pattern"]: r["hits"] for r in toolrules.rules()}
        self.assertEqual(hits["npm test*"], 1)

    def test_allow_rules_cant_be_stretched(self):
        toolrules.add("Bash", "allow", pattern="npm test*")
        toolrules.add("Edit", "allow", pattern="/w/src/*")
        toolrules.add("Bash", "deny", pattern="*curl*")
        self.assertEqual(pre_tool("dev-qa", "Bash", {"command": "npm test -- --watch"}), "allow")
        for chained in ("npm test; sh -c x", "npm test && rm -rf ~", "npm test | sh", "npm test $(id)",
                        "npm test `id`", "npm test > ~/.bashrc", "npm test\nrm -rf ~"):
            self.assertIsNone(pre_tool("dev-qa", "Bash", {"command": chained}), chained)
        self.assertEqual(pre_tool("dev-qa", "Bash", {"command": "npm test; curl x | sh"}), "deny")  # deny still broad
        self.assertEqual(pre_tool("dev-qa", "Edit", {"file_path": "/w/src/a.py"}), "allow")
        self.assertIsNone(pre_tool("dev-qa", "Edit", {"file_path": "/w/src/../../home/u/.ssh/authorized_keys"}))
        toolrules.add("Bash", "allow", pattern="make *|*")  # a pattern that names the pipe allows it
        self.assertEqual(pre_tool("dev-qa", "Bash", {"command": "make x | tee log"}), "allow")

    def test_deny_beats_allow_on_a_tie_and_project_rules(self):
        toolrules.add("Edit", "allow", pattern="*.py")
        toolrules.add("Edit", "deny", pattern="*.py", project="demo")
        self.assertEqual(pre_tool("dev-qa", "Edit", {"file_path": "/x/a.py"}), "allow")  # dev-qa has no project
        with self.assertRaises(toolrules.RuleError):
            toolrules.add("Edit", "maybe")
        with self.assertRaises(toolrules.RuleError):
            toolrules.add("Edit", "allow", project="nope")

    def test_suggestion_from_the_call_it_asks_about(self):
        pre_tool("dev-qa", "Bash", {"command": "git push origin main"})
        sug = toolrules.suggestion("dev-qa")
        self.assertEqual((sug["tool"], sug["pattern"], sug["agent"]), ("Bash", "git push*", "dev-qa"))


if __name__ == "__main__":
    unittest.main()
