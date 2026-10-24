import json
import unittest
from pathlib import Path
from unittest import mock

import support

import agentdefs
import config_store
import evals
import publish
from team import TeamError

PROMPT = """---
name: Report Writer
domain: docs
summary: Writes the weekly report.
---
Ask Sam, then read /home/someone/work/notes.md and the webstore board.
Mail ana@example.com. The server is 10.0.0.12 or box.tail1234.ts.net.
Use the key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789.
Install pkg@1.2.3 with npx.
"""


class ScrubTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        config = json.loads(config_store.CONFIG_PATH.read_text())
        config.update({"user_name": "Sam", "projects": {"webstore": {"name": "Webstore"}}})
        config_store.CONFIG_PATH.write_text(json.dumps(config))
        patch = mock.patch.object(publish, "_names", return_value=["Sam"])
        patch.start()
        self.addCleanup(patch.stop)

    def test_removes_what_identifies_you_and_flags_what_to_check(self):
        text, findings = publish.scrub("agent.md", PROMPT)
        for gone in ("Sam", "ana@example.com", "10.0.0.12", "sk-ant-api03", "/home/someone"):
            self.assertNotIn(gone, text)
        self.assertIn("Ask the user", text)
        self.assertIn("pkg@1.2.3", text)  # a pinned package isn't an email
        to_check = {f["text"] for f in findings if f["action"] == "check"}
        self.assertEqual(to_check, {"webstore", "box.tail1234.ts.net", "~/work/notes.md"})
        self.assertIn("secret", {f["kind"] for f in findings})


class PublishTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.root = support.TMP / "pub"
        agents = self.root / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "report-writer.md").write_text(PROMPT)
        entry = {"id": "report-writer", "name": "Report Writer", "summary": "Writes the weekly "
                 "report.", "domain": "docs", "use_when": "", "avoid_when": "", "inputs": [],
                 "outputs": [], "body": "v1", "extends": ""}
        for p in [mock.patch.object(agentdefs, "CATALOG_AGENTS", agents),
                  mock.patch.object(agentdefs, "catalog", return_value={"report-writer": entry}),
                  mock.patch.object(evals, "tests", return_value=[{"id": "t1", "task": "Write it",
                                                                   "criteria": ["short"]}]),
                  mock.patch.object(publish, "PUBLISH_DIR", self.root / "publish"),
                  mock.patch.object(publish, "owner", return_value="someone"),
                  mock.patch.object(publish, "_names", return_value=["Sam"])]:
            p.start()
            self.addCleanup(p.stop)
        self.entry = entry

    def test_prepare_writes_the_repository_files(self):
        prepared = publish.prepare("report-writer")
        self.assertEqual(prepared["repo"], "someone/report-writer")
        self.assertEqual(set(prepared["files"]), {"agent.md", "tests.json", "README.md",
                                                  "lago.json", "LICENSE"})
        folder = self.root / "publish" / "report-writer"
        self.assertNotIn("Sam", (folder / "agent.md").read_text())
        self.assertIn("MIT License", (folder / "LICENSE").read_text())

    def test_publish_needs_your_check_and_the_same_version(self):
        with self.assertRaises(TeamError):
            publish.publish("report-writer")  # nothing prepared
        publish.prepare("report-writer")
        with self.assertRaises(TeamError):
            publish.publish("report-writer")  # the ts.net address waits for your check
        self.entry["body"] = "v2"
        with self.assertRaises(TeamError):
            publish.publish("report-writer", checked=True)  # changed since you read it

    def test_publish_creates_a_public_repo_once_then_pushes(self):
        publish.prepare("report-writer")
        with mock.patch.object(publish, "_gh") as gh, mock.patch.object(publish, "_git", return_value=""):
            publish.publish("report-writer", checked=True)
            self.assertEqual(gh.call_args_list[0][0][:4], ("repo", "create", "someone/report-writer", "--public"))
            self.assertIn("lago-agent", gh.call_args_list[1][0])
            gh.reset_mock()
            publish.prepare("report-writer")
            publish.publish("report-writer", checked=True)
            gh.assert_not_called()  # an update is a push, not a new repository
        self.assertTrue(publish.status("report-writer")["published_at"])


if __name__ == "__main__":
    unittest.main()
