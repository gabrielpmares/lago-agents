"""A session's documents with revisions and comments (#25)."""

import time
import unittest
from unittest import mock

import support

import config_store
import documents
import hub
import team


class DocumentTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.folder = support.TMP / f"proj-{self._testMethodName}"
        (self.folder / ".claude/plans").mkdir(parents=True)
        config = config_store.load()
        config["projects"]["demo"]["cwd"] = str(self.folder)
        config_store.save(config)
        self.ws = hub.create_workspace("demo", None, "Checkout")
        self.messages = []
        p = mock.patch.object(team, "message", side_effect=lambda to, text, from_agent=None: self.messages.append((to, text)))
        p.start()
        self.addCleanup(p.stop)

    def test_plan_files_are_found_and_revised(self):
        plan = self.folder / ".claude/plans/checkout.md"
        plan.write_text("# Plan\nstep 1\n")
        docs = documents.sync(self.ws)
        self.assertEqual([(d["title"], d["revisions"]) for d in docs], [("checkout", 1)])
        documents.sync(self.ws)
        self.assertEqual(documents.documents(self.ws)[0]["revisions"], 1)  # unchanged: no new revision
        plan.write_text("# Plan\nstep 1\nstep 2\n")
        doc = documents.sync(self.ws)[0]
        self.assertEqual(doc["revisions"], 2)
        self.assertIn("+step 2", documents.diff(doc["id"], 2))
        self.assertEqual(documents.revision(doc["id"], 1)["text"], "# Plan\nstep 1\n")

    def test_share_and_comment(self):
        (self.folder / "docs").mkdir()
        (self.folder / "docs/adr-1.md").write_text("Use Postgres.")
        doc = documents.share(self.ws, "docs/adr-1.md", "ADR 1")
        documents.comment(doc["id"], "Why not SQLite?", quote="Use Postgres.")
        to, text = self.messages[0]
        self.assertEqual(to, f"w{self.ws}")
        self.assertIn('On "Use Postgres.": Why not SQLite?', text)
        self.assertEqual(documents.comments(doc["id"])[0]["revision"], 1)
        with self.assertRaises(documents.DocumentError):
            documents.comment(doc["id"], "  ")

    def test_refusals(self):
        with self.assertRaises(documents.DocumentError):
            documents.share(self.ws, "missing.md")
        big = self.folder / "big.md"
        big.write_text("x" * (documents.MAX_BYTES + 1))
        with self.assertRaises(documents.DocumentError):
            documents.share(self.ws, str(big))


if __name__ == "__main__":
    unittest.main()
