"""Swarm agents on the Codex CLI (#28)."""

import shlex
import unittest
from unittest import mock

import support

import codex_runtime
import config_store
import hub
import team


class CodexRuntimeTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_runtime_follows_the_agent_then_the_provider(self):
        claude, codex = {"runtime": {"default": "claude"}}, {"runtime": {"default": "codex"}}
        self.assertEqual(codex_runtime.runtime({"swarm": True}, {"runtime": "codex"}, claude), "codex")
        self.assertEqual(codex_runtime.runtime({"swarm": True}, {}, claude), "claude")
        self.assertEqual(codex_runtime.runtime({"swarm": True, "conductor": True}, {}, codex), "codex")
        self.assertEqual(codex_runtime.runtime({"orchestrator": True, "swarm": True}, {}, codex), "codex")
        self.assertEqual(codex_runtime.runtime({"team": "dev", "runtime": "claude"}, None, codex), "claude")
        self.assertEqual(codex_runtime.runtime({"swarm": True}, {"runtime": "gemini"}, claude), "claude")

    def test_command(self):
        cmd = codex_runtime.command("r3-web", {"swarm": True, "model": "gpt-5-codex"}, {}, "/w", "/runs/r3",
                                    "/usr/bin/python3", "/app", state_home="/tmp/s")
        parts = shlex.split(cmd.split(" - < ")[0])
        self.assertEqual(parts[:2], ["codex", "exec"])
        self.assertIn('mcp_servers.lago.env={LAGO_AGENT_ID = "r3-web", XDG_STATE_HOME = "/tmp/s"}', parts)
        self.assertIn('mcp_servers.lago.args=["/app/lago_mcp.py"]', parts)
        self.assertEqual(parts[parts.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(parts[parts.index("--model") + 1], "gpt-5-codex")
        free = codex_runtime.command("r3-web", {"swarm": True, "unrestricted": True, "model": "opus"}, {}, "/w",
                                     "/runs/r3", "python3", "/app")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", free)
        self.assertNotIn("--model", free)  # a Claude model name isn't passed to Codex

    def test_exit_closes_open_tasks_with_last_message(self):
        hub.add_swarm_agent("r1-web", None, "", "web", "Web")
        task = hub.create_task("r1-web", "Find prices", "body", "orchestrator")
        hub.update_task(task, status="working")
        run_dir = config_store.STATE_DIR / "runs" / "r1-web"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "last-message.txt").write_text("Bitcoin is 100k.")
        with mock.patch.object(team, "pump"):
            team.codex_exited("r1-web", "0")
        self.assertEqual((hub.get_task(task)["status"], hub.get_task(task)["result"].endswith("Bitcoin is 100k.")), ("done", True))
        self.assertEqual(hub.get_state("r1-web")["status"], "stopped")

    def test_exit_without_message_fails(self):
        hub.add_swarm_agent("r2-web", None, "", "web", "Web")
        task = hub.create_task("r2-web", "x", "body", "orchestrator")
        hub.update_task(task, status="working")
        (config_store.STATE_DIR / "runs" / "r2-web").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(team, "pump"):
            team.codex_exited("r2-web", "1")
        self.assertEqual(hub.get_task(task)["status"], "failed")
        self.assertIn("exited with code 1", hub.get_task(task)["result"])


if __name__ == "__main__":
    unittest.main()
