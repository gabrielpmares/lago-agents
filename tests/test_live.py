"""The Live Map's data (live.py): what agents are doing in plain words, and each plan over time."""

import json
import shutil
import subprocess
import time
import unittest
from unittest import mock

import support

import api
import config_store
import hub
import lago_hook
import live


class LiveTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        with mock.patch("background.start_loops"):
            self.api = api.Api(None)

    def test_tool_calls_read_as_sentences(self):
        cases = {("Edit", "file_path", "/w/src/checkout.ts"): "is editing checkout.ts",
                 ("Bash", "command", "cd app && npm test -- --watch"): "is running the tests",
                 ("Bash", "command", "pip install requests"): "is installing packages",
                 ("Bash", "command", "git commit -m x"): "is committing its changes",
                 ("WebFetch", "url", "https://docs.python.org/3/x"): "is reading docs.python.org",
                 ("Grep", "pattern", "def main"): "is searching the code for def main"}
        for (tool, key, value), expected in cases.items():
            self.assertEqual(live.sentence(tool, {key: value}), expected)
        self.assertEqual(live.sentence("mcp__lago__delegate", {"agent": "qa", "title": "Test it"}), "is handing “Test it” to qa")
        self.assertEqual(live.sentence("mcp__notion__search", {}), "is using notion (search)")

    def test_the_hook_records_each_call_and_the_feed_shows_it(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": "/w/a.py"}}
        with mock.patch.object(lago_hook.budgets, "check", lambda *a: None):
            lago_hook.on_event("dev-qa", event)
        rows = self.api.live_activity()
        self.assertEqual((rows[0]["agent"], rows[0]["sentence"]), ("QA", "is editing a.py"))
        self.assertEqual(self.api.live_activity(after=rows[0]["id"]), [])  # only what's new
        self.assertEqual(self.api.live_activity(project="demo"), [])  # dev-qa works outside projects

    def test_a_broken_feed_never_blocks_the_permission_check(self):
        import toolrules
        toolrules.add("Bash", "deny", pattern="rm *")
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "rm x"}}
        out = []
        with mock.patch.object(live, "record", side_effect=RuntimeError("disk full")), \
                mock.patch.object(lago_hook.budgets, "check", lambda *a: None), \
                mock.patch("builtins.print", lambda text, **k: out.append(text)):
            lago_hook.on_event("dev-qa", event)
        self.assertEqual(json.loads(out[-1])["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_a_plan_over_time(self):
        now = time.time()
        with hub.connect() as conn:
            done = conn.execute("INSERT INTO tasks (to_agent, title, body, status, created_at, updated_at) VALUES ('a', 't', '', 'done', ?, ?)",
                                (now - 600, now - 300)).lastrowid
            running = conn.execute("INSERT INTO tasks (to_agent, title, body, status, created_at, updated_at) VALUES ('b', 't', '', 'working', ?, ?)",
                                   (now - 200, now)).lastrowid
            steps = [{"id": "s1", "agent": "spec-writer", "title": "Spec", "status": "done", "task_id": done, "gate": True},
                     {"id": "s2", "agent": "implementer", "title": "Code", "status": "running", "task_id": running},
                     {"id": "s3", "agent": "qa-engineer", "title": "Test", "status": "pending", "task_id": None}]
            conn.execute("INSERT INTO pipelines (project, title, goal, status, steps, created_at, updated_at) "
                         "VALUES ('demo', 'Fix it', 'g', 'running', ?, ?, ?)", (json.dumps(steps), now - 700, now))
            conn.execute("INSERT INTO pipelines (project, title, goal, status, steps, created_at, updated_at) "
                         "VALUES ('demo', 'Old', 'g', 'done', '[]', ?, ?)", (now - 9 * 86400, now - 9 * 86400))
        plans = self.api.timeline(project="demo")
        self.assertEqual([p["title"] for p in plans], ["Fix it"])  # the old one is past the window
        spec, code, test = plans[0]["steps"]
        self.assertEqual((spec["start"], spec["end"], spec["gate"]), (now - 600, now - 300, True))
        self.assertEqual((code["start"], code["end"]), (now - 200, None))  # still running
        self.assertEqual((test["start"], test["end"]), (None, None))
        self.assertEqual(self.api.timeline(project="elsewhere"), [])

    def test_handoffs_go_from_the_lead_to_whoever_got_the_task_and_reports_go_back_up(self):
        now = time.time()
        nodes = [{"id": "task-7", "kind": "task"}, {"id": "w7", "kind": "lead", "parent": "task-7"},
                 {"id": "w7-architect", "kind": "agent", "parent": "w7"}, {"id": "w7-qa-engineer", "kind": "agent", "parent": "w7"}]
        with hub.connect() as conn:
            live._ensure()
            for agent, tool, at in [("w7", "mcp__lago__run_step", now - 20), ("w7-architect", "mcp__lago__complete_task", now - 5),
                                    ("w7", "mcp__lago__delegate", now - 3),  # its task isn't there yet
                                    ("w7", "mcp__lago__run_step", now - 120),  # too long ago
                                    ("w9", "mcp__lago__complete_task", now - 1)]:  # not on this map
                conn.execute("INSERT INTO tool_calls (agent_id, tool, subject, sentence, at) VALUES (?, ?, '', '', ?)", (agent, tool, at))
            conn.execute("INSERT INTO tasks (to_agent, from_agent, title, body, status, created_at, updated_at) "
                         "VALUES ('w7-architect', 'w7', 't', '', 'done', ?, ?)", (now - 19, now - 5))
        self.assertEqual([(x["from"], x["to"], x["kind"]) for x in live.handoffs(nodes, now=now)],
                         [("w7", "w7-architect", "hand_out"), ("w7-architect", "w7", "report")])
        with hub.connect() as conn:
            conn.execute("INSERT INTO tasks (to_agent, from_agent, title, body, status, created_at, updated_at) "
                         "VALUES ('w7-qa-engineer', 'w7', 't', '', 'sent', ?, ?)", (now - 2, now - 2))
        self.assertEqual(live.handoffs(nodes, now=now)[-1]["to"], "w7-qa-engineer")

    def test_a_tasks_cost_shows_on_its_timeline_row_while_budgets_are_on(self):
        now = time.time()
        workspace = hub.create_workspace("demo", "", "Fix it")
        hub.record_spend("s1", f"w{workspace}", workspace, 3.5)
        hub.record_spend("s2", f"w{workspace}-qa-engineer", workspace, 1.25)
        with hub.connect() as conn:
            conn.execute("INSERT INTO pipelines (project, workspace, title, goal, status, steps, created_at, updated_at) "
                         "VALUES ('demo', ?, 'Fix it', 'g', 'running', '[]', ?, ?)", (workspace, now, now))
        config = config_store.load()
        config["budgets"] = {"session": 20}
        config_store.save(config)
        self.assertEqual(self.api.timeline(project="demo")[0]["cost"], {"spent": 4.75, "limit": 20.0, "paused": False})
        config["features"] = {"budgets": False}
        config_store.save(config)
        self.assertIsNone(self.api.timeline(project="demo")[0]["cost"])

    def test_a_finished_plan_plays_back_from_what_its_agents_did(self):
        now = time.time()
        workspace = hub.create_workspace("demo", "", "Fix it")
        lead, coder = f"w{workspace}", f"w{workspace}-implementer"
        live._ensure()
        with hub.connect() as conn:
            task = conn.execute("INSERT INTO tasks (to_agent, from_agent, title, body, status, created_at, updated_at) "
                                "VALUES (?, ?, 't', '', 'done', ?, ?)", (coder, lead, now - 500, now - 100)).lastrowid
            steps = [{"id": "code", "agent": "implementer", "title": "Code", "status": "done", "task_id": task}]
            plan = conn.execute("INSERT INTO pipelines (project, workspace, title, goal, status, steps, created_at, updated_at) "
                                "VALUES ('demo', ?, 'Fix it', 'g', 'done', ?, ?, ?)", (workspace, json.dumps(steps), now - 600, now - 90)).lastrowid
            for agent, text, at in [(lead, "is drafting a plan", now - 550), (coder, "is running “echo sk-ant-" + "a" * 30 + "”", now - 300),
                                    ("dev-qa", "is reading a.py", now - 300),  # someone else's work
                                    (coder, "is editing b.py", now - 10)]:  # after the plan finished
                conn.execute("INSERT INTO tool_calls (agent_id, tool, subject, sentence, at) VALUES (?, 'Bash', '', ?, ?)", (agent, text, at))
        got = self.api.replay(plan)
        self.assertEqual((got["start"], got["end"], got["source"]), (now - 600, now - 90, "recorded"))
        self.assertEqual([(e["agent"], e["at"]) for e in got["events"]], [("Lead", now - 550), ("Implementer", now - 300)])
        self.assertNotIn("sk-ant-aaaa", got["events"][1]["sentence"])
        self.assertEqual(got["plan"]["steps"][0]["start"], now - 500)
        with self.assertRaises(api.ApiError):
            self.api.replay(9999)

        # Once the recorded calls have expired, the agents' transcripts tell the story.
        with hub.connect() as conn:
            conn.execute("DELETE FROM tool_calls")
        hub.record_spend("sess-1", coder, workspace, 1.0)
        stamp = lambda at: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(at))
        transcript = support.TMP / "sess-1.jsonl"
        transcript.write_text("\n".join(json.dumps(e) for e in [
            {"type": "assistant", "timestamp": stamp(now - 400), "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/c.py"}}]}},
            {"type": "assistant", "timestamp": stamp(now - 5), "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/w/d.py"}}]}},
        ]) + "\n")
        with mock.patch("transcripts.find", lambda session_id: transcript if session_id == "sess-1" else None):
            got = self.api.replay(plan)
        self.assertEqual((got["source"], [e["sentence"] for e in got["events"]]), ("transcripts", ["is editing c.py"]))

    def test_files_a_shell_command_reads_and_changes_count_as_touches_but_not_as_activity(self):
        folder = support.TMP / "shell"
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "src").mkdir(parents=True)
        for name in ("calc.py", "src/b.py", "README.md"):
            (folder / name).write_text("x\n")
        command = ("cat calc.py missing.py && cd src && sed -n '1,9p' b.py | grep -n x\n"
                   "cat >> ../calc.py <<'EOF'\nprint(1 > 2)\nEOF\n"
                   "sed -i 's/a/b/' ../README.md; python3 -m unittest 2>&1 > /dev/null")
        found = live.shell_files(command, str(folder))
        real = folder.resolve()  # macOS's /var is a link to /private/var
        self.assertEqual(found["read"], [str(real / "calc.py"), str(real / "src/b.py")])  # missing.py isn't there
        self.assertEqual(found["edit"], [str(real / "calc.py"), str(real / "README.md")])  # not "2)" from the heredoc
        self.assertEqual(live.shell_files(command, None), {"read": [], "edit": []})

        config = config_store.load()
        config["projects"]["shell"] = {"name": "Shell", "cwd": str(folder)}
        config_store.save(config)
        live.record("dev-qa", "Bash", {"command": command}, command, cwd=str(folder))
        touched = self.api.file_activity("shell", tree=False)["touched"]
        self.assertEqual(sorted(touched), ["README.md", "calc.py", "src/b.py"])
        self.assertEqual(touched["calc.py"]["touches"][0]["kind"], "edit")
        self.assertEqual([r["tool"] for r in self.api.live_activity()], ["Bash"])  # one line in the feed, not five

        # An agent whose session has closed is gone from the config: named by its role.
        live.record("w9-qa-engineer", "Read", {"file_path": str(folder / "README.md")}, str(folder / "README.md"))
        self.assertEqual(self.api.file_activity("shell", tree=False)["touched"]["README.md"]["touches"][0]["agent"], "Qa engineer")
        self.assertEqual(self.api.live_activity()[0]["agent"], "Qa engineer")

    def test_files_light_up_with_who_touched_them(self):
        folder = support.TMP / "lit"
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "src").mkdir(parents=True)
        config = config_store.load()
        config["projects"]["lit"] = {"name": "Lit", "cwd": str(folder)}
        config_store.save(config)
        for name in ("src/a.py", "src/b.py", "README.md"):
            (folder / name).write_text("x\n")
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
        now = time.time()
        calls = [("dev-qa", "Read", folder / "src/a.py", now - 900), ("dev-qa", "Edit", folder / "src/b.py", now - 600),
                 ("dev-lead", "Write", folder / "src/b.py", now - 60),  # a second agent within the window: conflict
                 ("dev-lead", "Edit", folder / ".worktrees/p4-code/src/a.py", now - 30),  # a step's worktree
                 ("dev-qa", "Read", support.TMP / "elsewhere/x.py", now - 10),  # not this project's
                 ("dev-qa", "Read", folder / "README.md", now - 2 * 86400)]  # past the window
        with hub.connect() as conn:
            live._ensure()
            for agent, tool, path, at in calls:
                conn.execute("INSERT INTO tool_calls (agent_id, tool, subject, sentence, at) VALUES (?, ?, ?, '', ?)",
                             (agent, tool, str(path), at))
        out = self.api.file_activity("lit")
        self.assertEqual(out["files"], ["README.md", "src/a.py", "src/b.py"])
        self.assertEqual(set(out["touched"]), {"src/a.py", "src/b.py"})
        a, b = out["touched"]["src/a.py"], out["touched"]["src/b.py"]
        self.assertEqual((a["read_at"], a["edit_at"], a["conflict"]), (now - 900, now - 30, False))
        self.assertEqual(a["touches"][0]["branch"], "lago/p4-code")
        self.assertTrue(b["conflict"])
        self.assertEqual([(t["agent"], t["kind"]) for t in b["touches"]], [("Lead", "edit"), ("QA", "edit")])
        with self.assertRaises(api.ApiError):
            self.api.file_activity("nowhere")


if __name__ == "__main__":
    unittest.main()
