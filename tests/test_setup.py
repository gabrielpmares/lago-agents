import unittest
from unittest import mock

import support

import agentdefs
import config_store
import lago_mcp
import team

SKILL = {"name": "gws-calendar", "description": "Google Calendar: manage events.", "source": "user",
         "path": str(support.TMP / "skills" / "gws-calendar" / "SKILL.md")}


class SetupTests(unittest.TestCase):
    """Lago offers to set up a way in instead of saying it can't."""

    def setUp(self):
        support.reset()
        skill_file = support.TMP / "skills" / "gws-calendar" / "SKILL.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text("---\nname: gws-calendar\nmetadata:\n  requires:\n    bins:\n"
                              "      - surely-not-installed-cli\n---\nUse it.\n")
        for p in [mock.patch.object(agentdefs, "skills", return_value={"gws-calendar": SKILL}),
                  mock.patch.object(agentdefs, "plugins", return_value=[]),
                  mock.patch.object(lago_mcp, "AGENT_ID", config_store.ORCHESTRATOR_ID)]:
            p.start()
            self.addCleanup(p.stop)

    def test_lago_sees_skills_and_what_they_still_need(self):
        text = lago_mcp.tool_find_skills("calendar")
        self.assertIn("`gws-calendar`", text)
        self.assertIn("Needs `surely-not-installed-cli`, not installed yet", text)

    def test_setup_terminal_runs_only_setup_programs(self):
        with self.assertRaises(team.TeamError):
            lago_mcp.tool_open_setup_terminal("rm -rf ~", "nope")
        import server
        with mock.patch.object(server, "launch") as launch, \
                mock.patch.object(lago_mcp.desktop, "terminal_argv", return_value=["xterm", "-e", "x"]), \
                mock.patch.object(lago_mcp.subprocess, "Popen") as window:
            text = lago_mcp.tool_open_setup_terminal("surely-not-installed-cli auth login", "sign in")
        launch.assert_called_once()
        window.assert_called_once()
        self.assertIn("terminal window opened", text)


if __name__ == "__main__":
    unittest.main()


class SkillVisibilityTests(unittest.TestCase):
    """A hundred installed skills mustn't fill every session's context."""

    def setUp(self):
        support.reset()
        skills = {n: {"name": n, "description": "", "source": "user", "path": ""}
                  for n in ("gws-shared", "gws-calendar", "clean-names")}
        skills["ponytail:ponytail"] = {"name": "ponytail:ponytail", "description": "",
                                       "source": "plugin ponytail", "path": ""}
        patch = mock.patch.object(agentdefs, "skills", return_value=skills)
        patch.start()
        self.addCleanup(patch.stop)

    def test_lago_sees_only_his_few(self):
        import team as team_module
        overrides = team_module.skill_overrides(config_store.load(), {"orchestrator": True}, None, None)
        self.assertEqual(overrides, {"gws-calendar": "off", "clean-names": "off"})

    def test_lagos_list_is_a_setting(self):
        import team as team_module
        config = config_store.load()
        config["orchestrator"] = {"skills": ["gws-calendar"]}
        overrides = team_module.skill_overrides(config, {"orchestrator": True}, None, None)
        self.assertNotIn("gws-calendar", overrides)
        self.assertEqual(overrides["gws-shared"], "off")

    def test_an_agent_sees_its_own_in_full_and_the_rest_by_name(self):
        import team as team_module
        overrides = team_module.skill_overrides(config_store.load(), {}, {"skills": ["gws-calendar"]}, None)
        self.assertEqual(overrides, {"gws-shared": "name-only", "clean-names": "name-only"})
