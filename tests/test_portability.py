"""Team export and import (#21)."""

import io
import json
import shutil
import tarfile
import unittest
from unittest import mock

import support

import agentdefs
import config_store
import portability


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.app = support.TMP / f"app-{self._testMethodName}"
        (self.app / "catalog/agents").mkdir(parents=True)
        (self.app / "catalog/tests").mkdir(parents=True)
        (self.app / "claude-agents").mkdir(parents=True)
        (self.app / "claude-skills/sql-style").mkdir(parents=True)
        (self.app / "catalog/agents/sql-migrator.md").write_text("---\nname: SQL Migrator\nextends: implementer\n---\nGo.\n")
        (self.app / "catalog/tests/sql-migrator.json").write_text('{"tests": []}')
        (self.app / "claude-agents/implementer.md").write_text("impl")
        (self.app / "claude-agents/qa-engineer.md").write_text("qa")
        (self.app / "claude-skills/sql-style/SKILL.md").write_text("skill")
        links = support.TMP / f"links-{self._testMethodName}"
        patches = [mock.patch.object(portability, "APP_DIR", self.app),
                   mock.patch.object(portability, "SKILLS_LINK_DIR", links / "skills"),
                   mock.patch.object(portability, "AGENTS_LINK_DIR", links / "agents"),
                   mock.patch.object(agentdefs, "catalog", return_value={"sql-migrator": {"extends": "implementer"}})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.links = links

    def test_export_contents(self):
        out = support.TMP / "team.tar.gz"
        manifest = portability.export(out, teams=["dev"], catalog=["sql-migrator"], skills=["sql-style"])
        self.assertEqual(manifest["files"], ["catalog/agents/sql-migrator.md", "catalog/tests/sql-migrator.json",
                                             "claude-agents/implementer.md", "claude-agents/qa-engineer.md",
                                             "claude-skills/sql-style/SKILL.md"])
        self.assertEqual(set(manifest["agents"]), {"dev-lead", "dev-qa"})
        self.assertNotIn("cwd", manifest["teams"]["dev"])  # no local paths

    def test_import_adds_only_what_is_new(self):
        out = support.TMP / "team2.tar.gz"
        portability.export(out, teams=["dev"], catalog=["sql-migrator"], skills=["sql-style"])
        # Pretend we're on another machine: remove our copies, change one, rename the team.
        (self.app / "catalog/agents/sql-migrator.md").unlink()
        (self.app / "claude-agents/qa-engineer.md").write_text("my own qa")
        shutil.rmtree(self.app / "claude-skills/sql-style")
        config = config_store.load()
        config["teams"] = {}
        config["agents"] = {}
        config_store.save(config)
        plan = portability.preview(out)
        self.assertIn("catalog/agents/sql-migrator.md", plan["new"])
        self.assertEqual(plan["different"], ["claude-agents/qa-engineer.md"])
        portability.apply(out)
        self.assertEqual((self.app / "claude-agents/qa-engineer.md").read_text(), "my own qa")  # kept
        self.assertTrue((self.app / "catalog/agents/sql-migrator.md").exists())
        self.assertTrue((self.links / "skills/sql-style").is_symlink())
        config = config_store.load()
        self.assertEqual(config["teams"]["dev"]["cwd"], "~")
        self.assertEqual(config["agents"]["dev-qa"]["reports_to"], "dev-lead")

    def _evil(self, name, data=b"x", kind=tarfile.REGTYPE):
        path = support.TMP / f"evil-{abs(hash(name))}.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            m = json.dumps({"format": portability.FORMAT}).encode()
            info = tarfile.TarInfo("manifest.json"); info.size = len(m); tar.addfile(info, io.BytesIO(m))
            info = tarfile.TarInfo(name); info.type = kind; info.size = len(data) if kind == tarfile.REGTYPE else 0
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/passwd"
            tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        return path

    def test_unsafe_archives_are_refused(self):
        for name, kind in (("../../.bashrc", tarfile.REGTYPE), ("catalog/agents/../../x.md", tarfile.REGTYPE),
                           ("lago_hook.py", tarfile.REGTYPE), ("catalog/agents/a.md", tarfile.SYMTYPE)):
            with self.assertRaises(portability.PortabilityError, msg=name):
                portability.preview(self._evil(name, kind=kind))


if __name__ == "__main__":
    unittest.main()
