"""Lago as an MCP server for other Claude sessions (#24): `lago_mcp.py --public`."""

import json
import os
import subprocess
import sys
import unittest

import support

import hub


class PublicMcpTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        hub.add_event(hub.create_workspace("demo", None, "Checkout fix"), "demo", "done",
                      "Checkout bug fixed and deployed")

    def talk(self, *messages, args=("--public",), env_extra=None):
        env = {**os.environ, "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"], **(env_extra or {})}
        env.pop("LAGO_AGENT_ID", None)
        env.update(env_extra or {})
        lines = "".join(json.dumps(m) + "\n" for m in messages)
        proc = subprocess.run([sys.executable, str(support.APP / "lago_mcp.py"), *args], input=lines,
                              capture_output=True, text=True, env=env, timeout=30)
        return [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]

    def test_lists_only_public_tools_and_answers(self):
        replies = self.talk(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "reports", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search", "arguments": {"query": "checkout"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "decide", "arguments": {}}})
        by_id = {r["id"]: r for r in replies}
        names = {t["name"] for t in by_id[2]["result"]["tools"]}
        self.assertEqual(names, {"list_projects", "work_sessions", "session_detail", "open_work_session",
                                 "message_session", "tell_lago", "reports", "search"})
        self.assertIn("Checkout bug fixed", by_id[3]["result"]["content"][0]["text"])
        self.assertIn("Checkout", by_id[4]["result"]["content"][0]["text"])
        self.assertTrue(by_id[5]["result"]["isError"])  # Lago's own tools aren't public

    def test_public_flag_ignored_for_agents(self):
        replies = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                            env_extra={"LAGO_AGENT_ID": "dev-qa"})
        names = {t["name"] for t in replies[0]["result"]["tools"]}
        self.assertNotIn("tell_lago", names)


if __name__ == "__main__":
    unittest.main()
