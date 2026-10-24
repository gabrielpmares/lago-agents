import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import support

import config_store
import ghcatalog
import github
import hub
import swarm


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def item(name, stars=100, spdx="MIT", age=200, description="", topics=(), **extra):
    return {"full_name": name, "stargazers_count": stars, "license": {"spdx_id": spdx},
            "description": description, "topics": list(topics), "created_at": iso(age),
            "pushed_at": iso(1), "default_branch": "main", "fork": False, "archived": False,
            **extra}


def fake_gh(results):
    """A stand-in for `gh`: search results by the query's first word, plus commits/READMEs."""
    def run(*args, **_):
        if args[:4] == ("api", "-X", "GET", "search/repositories"):
            q = next(a[2:] for a in args if a.startswith("q="))
            return {"items": results.get(q.split()[0], [])}
        if "/commits/" in args[1]:
            return {"sha": "abc1234def"}
        if "/readme" in args[1]:
            return {"content": "IyBSZWFkbWU="}  # "# Readme"
        raise AssertionError(args)
    return run


class ListingRulesTests(unittest.TestCase):
    def test_only_open_source_licenses(self):
        self.assertEqual(ghcatalog.listable(item("a/b")), "")
        self.assertEqual(ghcatalog.license_of(item("a/b", spdx="AGPL-3.0")), ("AGPL-3.0", True))
        self.assertEqual(ghcatalog.listable(item("a/b", spdx="NOASSERTION")), "no open-source license")
        self.assertEqual(ghcatalog.listable({**item("a/b"), "license": None}), "no open-source license")

    def test_no_forks_archives_or_lists(self):
        self.assertTrue(ghcatalog.listable(item("a/b", fork=True)))
        self.assertTrue(ghcatalog.listable(item("a/b", archived=True)))
        self.assertTrue(ghcatalog.listable(item("x/awesome-mcp-servers")))

    def test_topic_ignores_the_words_every_repo_shares(self):
        self.assertEqual(ghcatalog.topic_of(item("a/pg-mcp", description="MCP server for Postgres "
                                                 "databases in Claude Code")), "data")
        self.assertEqual(ghcatalog.topic_of(item("a/thing", description="A Claude Code skill")),
                         "other")


class RefreshTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.results = {
            "topic:mcp-server": [item("a/pg-mcp", 500, description="Postgres database MCP server"),
                                 item("b/closed", 900, spdx="NOASSERTION"),
                                 item("c/brand-new", 3000, age=2, description="browser automation")],
            "topic:claude-skills": [item("d/seo-skill", 80, description="SEO content skill")]}
        for p in [mock.patch.object(github, "_gh", side_effect=fake_gh(self.results)),
                  mock.patch.object(ghcatalog, "SEARCH_PAUSE", 0),
                  mock.patch.object(ghcatalog, "describe")]:
            p.start()
            self.addCleanup(p.stop)

    def test_lists_licensed_repos_with_kind_and_topic(self):
        self.assertEqual(ghcatalog.refresh(), 3)
        self.assertIsNone(ghcatalog.get("b/closed"))
        pg = ghcatalog.get("a/pg-mcp")
        self.assertEqual((pg["kind"], pg["topic"], pg["license"]), ("mcp", "data", "MIT"))
        self.assertEqual(ghcatalog.get("d/seo-skill")["kind"], "skill")

    def test_trending_ranks_by_stars_gained_and_holds_new_repos(self):
        ghcatalog.refresh()
        week_ago = (date.today() - timedelta(days=8)).isoformat()
        hub_rows = [("a/pg-mcp", week_ago, 490), ("d/seo-skill", week_ago, 20)]
        with hub.connect() as conn:
            conn.executemany("INSERT INTO gh_stars (repo, day, stars) VALUES (?, ?, ?)", hub_rows)
        ranked = ghcatalog.trending()
        self.assertEqual([c["repo"] for c in ranked], ["d/seo-skill", "a/pg-mcp"])  # +60 beats +10
        self.assertEqual(ranked[0]["week"], 60)
        self.assertTrue(ghcatalog.get("c/brand-new")["held"])

    def test_describes_the_best_new_ones_once(self):
        ghcatalog.refresh()
        self.assertEqual(ghcatalog.describe.call_count, 2)  # the too-new one isn't ranked yet

    def test_search_matches_the_task(self):
        ghcatalog.refresh()
        self.assertEqual([c["repo"] for c in ghcatalog.search("query my postgres database")],
                         ["a/pg-mcp"])
        self.assertEqual(ghcatalog.search("x"), [])

    def test_refresh_if_due_waits_a_day(self):
        ghcatalog.refresh()
        with mock.patch.object(ghcatalog, "refresh") as again:
            ghcatalog.refresh_if_due()
        again.assert_not_called()


class DescribeTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        with mock.patch.object(github, "_gh", side_effect=fake_gh(
                {"topic:mcp-server": [item("a/pg-mcp", description="Postgres MCP server")]})), \
                mock.patch.object(ghcatalog, "SEARCH_PAUSE", 0), \
                mock.patch.object(ghcatalog, "describe"):
            ghcatalog.refresh()

    def test_summary_pinned_to_a_commit_and_unfit_repos_hidden(self):
        answer = {"what": "Lets an agent query Postgres.", "needs": "A database URL",
                  "produces": "Query results", "risk": "medium", "fits": False, "topic": "data"}
        with mock.patch.object(github, "_gh", side_effect=fake_gh({})), \
                mock.patch.object(ghcatalog.memory, "_ask_model", return_value=answer) as ask:
            ghcatalog.describe("a/pg-mcp")
        self.assertIn("untrusted data", ask.call_args[0][0])
        card = ghcatalog.get("a/pg-mcp")
        self.assertEqual((card["sha"], card["summary"]["risk"]), ("abc1234def", "medium"))
        self.assertEqual(ghcatalog.cards(), [])  # a framework or app isn't offered as an agent


class ImportTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        with mock.patch.object(github, "_gh", side_effect=fake_gh(
                {"topic:mcp-server": [item("a/pg-mcp", description="Postgres database MCP server")]})), \
                mock.patch.object(ghcatalog, "SEARCH_PAUSE", 0), \
                mock.patch.object(ghcatalog, "describe"):
            ghcatalog.refresh()

    def test_import_is_one_step_by_default(self):
        import ghimport
        with mock.patch.object(ghimport, "import_repo", return_value="Imported.") as one_step:
            self.assertEqual(ghcatalog.import_repo("a/pg-mcp"), "Imported.")
        one_step.assert_called_once_with("a/pg-mcp", "mcp")

    def test_careful_import_goes_through_the_builder(self):
        config = config_store.load()
        config["swarm"]["careful_import"] = True
        config_store.save(config)
        with mock.patch.object(swarm, "run_agent", return_value=(9, "r1-b", "m")) as run:
            self.assertEqual(ghcatalog.import_repo("a/pg-mcp"), 9)
        args, kwargs = run.call_args
        self.assertEqual(args[0], "github-agent-builder")
        self.assertEqual(kwargs["inputs"]["mode"], "build")

    def test_only_listed_repos(self):
        with self.assertRaises(Exception):
            ghcatalog.import_repo("someone/unlicensed")

    def test_lago_sees_github_matches_last(self):
        import lago_mcp
        with mock.patch.object(swarm, "enabled_agents", return_value={}), \
                mock.patch.object(swarm, "disabled", return_value=set()), \
                mock.patch.object(lago_mcp.evals, "ranking", return_value=[]), \
                mock.patch.object(lago_mcp, "AGENT_ID", config_store.ORCHESTRATOR_ID):
            text = lago_mcp.tool_find_agents("query postgres database")
        self.assertIn("On GitHub, not installed", text)
        self.assertIn("a/pg-mcp", text)


if __name__ == "__main__":
    unittest.main()


class SharedBuildTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        results = {"topic:lago-agent": [item("ann/pg-agent", 40, description="Postgres agent")],
                   "topic:mcp-server": [item("a/pg-mcp", 500, description="Postgres database MCP server")]}
        gh = fake_gh(results)

        def run(*args, **kw):
            if args[1] == "repos/ann/pg-agent/contents/lago.json":
                return {"content": "eyJzb3VyY2UiOiAiYS9wZy1tY3BAYWJjIn0="}  # {"source": "a/pg-mcp@abc"}
            return gh(*args, **kw)
        with mock.patch.object(github, "_gh", side_effect=run), \
                mock.patch.object(ghcatalog, "SEARCH_PAUSE", 0), \
                mock.patch.object(ghcatalog, "describe"):
            ghcatalog.refresh()

    def test_published_builds_are_found_by_their_source(self):
        self.assertEqual([c["repo"] for c in ghcatalog.builds_of("a/pg-mcp")], ["ann/pg-agent"])

    def test_import_takes_the_shared_build(self):
        import ghimport
        with mock.patch.object(ghimport, "import_repo", return_value="Imported.") as one_step:
            line = ghcatalog.import_repo("a/pg-mcp")
        one_step.assert_called_once_with("ann/pg-agent", "lago")
        self.assertIn("same agent other people use", line)

    def test_careful_builds_use_the_stronger_model(self):
        config = config_store.load()
        config["swarm"]["careful_import"] = True
        config_store.save(config)
        with hub.connect() as conn:
            conn.execute("UPDATE gh_repos SET built_from = ''")
        with mock.patch.object(swarm, "run_agent", return_value=(9, "r1-b", "m")) as run:
            ghcatalog.import_repo("a/pg-mcp")
        self.assertEqual(run.call_args.kwargs["mode"], "certain")


class InstalledTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        with mock.patch.object(github, "_gh", side_effect=fake_gh(
                {"topic:mcp-server": [item("a/pg-mcp", description="Postgres database MCP server")]})), \
                mock.patch.object(ghcatalog, "SEARCH_PAUSE", 0), \
                mock.patch.object(ghcatalog, "describe"):
            ghcatalog.refresh()
        entry = {"id": "pg-agent", "source": "A/pg-mcp@abc123"}
        patch = mock.patch.object(ghcatalog.agentdefs, "catalog", return_value={"pg-agent": entry})
        patch.start()
        self.addCleanup(patch.stop)

    def test_recognizes_an_agent_built_from_the_repo(self):
        have = ghcatalog.installed()["a/pg-mcp"]
        self.assertEqual(have["agents"], ["pg-agent"])
        self.assertEqual(ghcatalog.have_text(have), "agent pg-agent")

    def test_import_refuses_what_you_already_have(self):
        with self.assertRaises(Exception) as raised:
            ghcatalog.import_repo("a/pg-mcp")
        self.assertIn("already have", str(raised.exception))

    def test_a_build_under_way_counts(self):
        hub.add_swarm_agent("r1-github-agent-builder", None, "", ghcatalog.BUILDER, "b")
        hub.create_task("r1-github-agent-builder", "Turn b/other into a swarm agent", "x")
        with mock.patch.object(ghcatalog, "get", return_value={"repo": "b/other"}):
            self.assertTrue(ghcatalog.installed()["b/other"]["importing"])


class ExplainTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        with mock.patch.object(github, "_gh", side_effect=fake_gh(
                {"topic:mcp-server": [item("a/pg-mcp", description="Postgres database MCP server")]})), \
                mock.patch.object(ghcatalog, "SEARCH_PAUSE", 0), \
                mock.patch.object(ghcatalog, "describe"):
            ghcatalog.refresh()
        answer = {"what": "Queries Postgres.", "reward": "Faster data checks.", "risks": ["Writes data"],
                  "risk": "medium", "verdict": "try", "why": "Your demo project uses Postgres.",
                  "fits": [{"project": "demo", "how": "Check its tables."},
                           {"project": "made-up", "how": "x"}],
                  "similar": ["not-an-agent"]}
        gh = fake_gh({})

        def run(*args, **kw):
            if "/git/trees/" in args[1]:
                return {"tree": [{"path": "README.md", "type": "blob"}]}
            return gh(*args, **kw)
        for p in [mock.patch.object(github, "_gh", side_effect=run),
                  mock.patch.object(ghcatalog.memory, "_ask_model", return_value=answer)]:
            p.start()
            self.addCleanup(p.stop)

    def test_explains_with_your_projects_and_keeps_only_real_ones(self):
        d = ghcatalog.explain("a/pg-mcp")
        self.assertEqual((d["verdict"], d["risk"]), ("try", "medium"))
        self.assertEqual([f["project"] for f in d["fits"]], ["demo"])  # no invented projects
        self.assertEqual(d["similar"], [])
        prompt = ghcatalog.memory._ask_model.call_args[0][0]
        self.assertIn("untrusted data", prompt)
        self.assertIn("(id `demo`)", prompt)

    def test_answer_is_kept_per_commit(self):
        ghcatalog.explain("a/pg-mcp")
        ghcatalog.explain("a/pg-mcp")
        self.assertEqual(ghcatalog.memory._ask_model.call_count, 1)
        ghcatalog.explain("a/pg-mcp", refresh=True)
        self.assertEqual(ghcatalog.memory._ask_model.call_count, 2)
