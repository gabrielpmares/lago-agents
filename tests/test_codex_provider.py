"""A ChatGPT subscription through Codex: provider choice, Codex transcripts, and the
conversation loop (codex_session.py) driven by a fake `codex` that answers like the real one."""

import io
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

import support

import codex_runtime
import codex_session
import config_store
import hub
import lago_hook
import team
import transcripts

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time, uuid
args = sys.argv[1:]
log = os.environ["FAKE_CODEX_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\n")
prompt = sys.stdin.read()
thread = args[args.index("resume") + 1] if "resume" in args else "019a0000-1111-2222-3333-" + uuid.uuid4().hex[:12]
day = os.path.join(os.environ["FAKE_CODEX_SESSIONS"], "2026", "10", "22")
os.makedirs(day, exist_ok=True)
with open(os.path.join(day, f"rollout-2026-10-22T10-00-00-{thread}.jsonl"), "a") as f:
    f.write(json.dumps({"timestamp": "2026-10-22T10:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": prompt}}) + "\n")
    f.write(json.dumps({"timestamp": "2026-10-22T10:00:01Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "Done: " + prompt.splitlines()[0]}}) + "\n")
print(json.dumps({"type": "thread.started", "thread_id": thread}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Done: " + prompt.splitlines()[0]}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
'''


class CodexProviderTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.bin = support.TMP / f"bin-{self._testMethodName}"
        self.bin.mkdir()
        fake = self.bin / "codex"
        fake.write_text(FAKE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.log = support.TMP / f"codex-{self._testMethodName}.log"
        self.sessions = support.TMP / f"codex-sessions-{self._testMethodName}"
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_CODEX_LOG": str(self.log),
               "FAKE_CODEX_SESSIONS": str(self.sessions)}
        patches = [mock.patch.dict(os.environ, env), mock.patch.object(transcripts, "CODEX_SESSIONS", self.sessions),
                   mock.patch.object(lago_hook, "_detached"), mock.patch.object(lago_hook, "pump_later"),
                   mock.patch.object(team, "pump")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def test_provider_and_runtime_choice(self):
        config = config_store.load()
        self.assertEqual(codex_runtime.provider(config), "claude")
        config["runtime"] = {"default": "codex"}
        self.assertEqual(codex_runtime.runtime({"team": "dev"}, None, config), "codex")
        self.assertEqual(codex_runtime.runtime({"swarm": True}, {"runtime": "claude"}, config), "claude")
        self.assertTrue(codex_runtime.interactive({"orchestrator": True, "swarm": True}))
        self.assertFalse(codex_runtime.interactive({"swarm": True}))

    def test_turn_args(self):
        args = codex_runtime.turn_args("w1", {"swarm": True}, None, "You are a lead.", "py", "/app", thread="t-1")
        self.assertEqual(args[:4], ["codex", "exec", "resume", "t-1"])
        self.assertIn('developer_instructions="You are a lead."', args)
        self.assertIn('sandbox_mode="workspace-write"', args)
        self.assertEqual(args[-1], "-")
        lago = codex_runtime.turn_args("orchestrator", {"orchestrator": True, "unrestricted": True}, None, "x", "py", "/app")
        self.assertIn('sandbox_mode="read-only"', lago)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", lago)
        free = codex_runtime.turn_args("w1", {"unrestricted": True}, None, "x", "py", "/app")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", free)
        self.assertNotIn("resume", free)

    def test_project_mcp_servers_reach_codex(self):
        flags = codex_runtime.server_flags({"postgres": {"command": "npx", "args": ["-y", "pg@1.0"], "env": {"URL": "x"}},
                                            "docs site": {"url": "https://example.com/mcp"}, "lago": {"command": "py"}})
        self.assertIn('mcp_servers.postgres.args=["-y", "pg@1.0"]', flags)
        self.assertIn('mcp_servers.postgres.env={URL = "x"}', flags)
        self.assertIn('mcp_servers.docs_site.url="https://example.com/mcp"', flags)
        self.assertFalse(any("mcp_servers.lago." in f for f in flags))

    def test_codex_transcripts(self):
        day = self.sessions / "2026/10/22"
        day.mkdir(parents=True)
        (day / "rollout-2026-10-22T10-00-00-019a0000-aaaa-bbbb-cccc-000000000001.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"timestamp": "t", "type": "event_msg", "payload": {"type": "user_message", "message": "Fix it"}},
            {"timestamp": "t", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "npm test"}), "call_id": "c1"}},
            {"timestamp": "t", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "Process exited with code 1"}},
            {"timestamp": "t", "type": "event_msg", "payload": {"type": "agent_message", "message": "Tests fail."}},
            {"timestamp": "t", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 900, "output_tokens": 100}},
                                                               "rate_limits": {"primary": {"used_percent": 42.0, "window_minutes": 300}}}}]) + "\n")
        path = transcripts.find("codex:019a0000-aaaa-bbbb-cccc-000000000001")
        events = transcripts.read(path)["events"]
        self.assertEqual([e["kind"] for e in events], ["prompt", "tool", "result", "say"])
        self.assertIn("npm test", events[1]["text"])
        self.assertFalse(events[2]["ok"])
        numbers = transcripts.codex_numbers(path)
        self.assertEqual((numbers["context_tokens"], numbers["rate_limits"]["primary"]["used_percent"]), (1000, 42.0))
        self.assertIsNone(transcripts.find("codex:../../etc"))

    def test_conversation_loop_runs_turns_and_finishes_tasks(self):
        task = team.assign("dev-qa", "Write tests", "Cover the cart", from_agent="dev-lead")
        prompts = [f"[Lago task #{task} from Lead]\nTitle: Write tests\n\nCover the cart", "Also the checkout"]
        run = config_store.STATE_DIR / "runs" / "dev-qa"
        run.mkdir(parents=True, exist_ok=True)
        (run / "system-prompt.md").write_text("You are QA.")

        def next_prompt(agent_id):
            if not prompts:
                raise SystemExit
            return prompts.pop(0)
        with mock.patch.object(codex_session, "next_prompt", side_effect=next_prompt), \
                mock.patch("sys.stdout", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                codex_session.main("dev-qa")
        first, second = self.calls()
        self.assertNotIn("resume", first)
        self.assertIn("resume", second)
        thread = (run / "codex-thread").read_text()
        self.assertEqual(second[second.index("resume") + 1], thread)
        state = hub.get_state("dev-qa")
        self.assertEqual((state["status"], state["session_id"]), ("idle", f"codex:{thread}"))
        done = hub.get_task(task)
        self.assertEqual(done["status"], "done")
        self.assertIn("Done: [Lago task", done["result"])

    def test_failed_turn_goes_idle_with_the_error(self):
        (self.bin / "codex").write_text("#!/bin/sh\necho '{\"type\":\"turn.failed\",\"error\":{\"message\":\"token expired\"}}'\nexit 1\n")
        run = config_store.STATE_DIR / "runs" / "dev-lead"
        run.mkdir(parents=True, exist_ok=True)
        (run / "system-prompt.md").write_text("x")
        prompts = ["hello"]
        with mock.patch.object(codex_session, "next_prompt", side_effect=lambda a: prompts.pop(0) if prompts else (_ for _ in ()).throw(SystemExit)), \
                mock.patch("sys.stdout", new=io.StringIO()):
            with self.assertRaises(SystemExit):
                codex_session.main("dev-lead")
        self.assertIn("token expired", hub.get_state("dev-lead")["detail"])

    def test_test_reports_login_problems(self):
        (self.bin / "codex").write_text("#!/bin/sh\necho '{\"type\":\"turn.failed\",\"error\":{\"message\":\"Your access token could not be refreshed.\"}}'\n")
        self.assertEqual(codex_runtime.test(), {"ok": False, "message": "Your access token could not be refreshed."})


if __name__ == "__main__":
    unittest.main()
