"""Smaller company features: read marks per person, attention routing, secret requests,
and the live browser view."""

import json
import unittest
from unittest import mock

import support

import activity
import company
import config_store
import hub
import notifications
import readstate
import secret_requests
import team
import vault


class ReadStateTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_each_person_has_their_own_marks(self):
        first = readstate.annotate([{"item": "session:1", "at": 1.0}])  # before they were first seen
        self.assertFalse(first[0]["unread"])
        import time
        later = time.time() + 10
        self.assertTrue(readstate.annotate([{"item": "session:1", "at": later}])[0]["unread"])
        with mock.patch("time.time", lambda: later + 5):
            readstate.mark(["session:1"])
        self.assertFalse(readstate.annotate([{"item": "session:1", "at": later}])[0]["unread"])
        activity.set_person({"login": "ann@acme.com", "name": "Ann"})
        try:
            readstate.annotate([])  # Ann is first seen now
            self.assertTrue(readstate.annotate([{"item": "session:1", "at": later + 100}])[0]["unread"])
        finally:
            activity.set_person(None)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        company.setup("Acme", "owner@acme.com")
        company.request_access({"login": "ann@acme.com", "name": "Ann"})
        company.admit("ann@acme.com", "watch")

    def test_a_handler_needs_operate_access_and_gets_the_urgent_items(self):
        with self.assertRaises(company.AccessError):
            company.set_handler("demo", "ann@acme.com")
        company.set_project_role("ann@acme.com", "demo", "operate")
        company.set_handler("demo", "ann@acme.com")
        items = [{"project": "demo", "urgent": True, "at": 2}, {"project": "", "urgent": True, "at": 1}]
        mine = notifications.route([dict(i) for i in items], "owner", company.handlers())
        self.assertEqual([(i["project"], i["urgent"], i.get("for")) for i in mine],
                         [("", True, None), ("demo", False, "ann@acme.com")])
        hers = notifications.route([dict(i) for i in items], "ann@acme.com", company.handlers())
        self.assertTrue(hers[0]["urgent"] and hers[1]["urgent"])
        company.remove("ann@acme.com")
        self.assertEqual(company.handlers(), {})  # someone removed handles nothing


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def store(self, name, project, value):
        self.values[(name, project)] = value

    def lookup(self, name, project):
        return self.values.get((name, project))

    def clear(self, name, project):
        self.values.pop((name, project), None)


class SecretRequestTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        p = mock.patch.object(vault, "_backend", FakeKeyring())
        p.start()
        self.addCleanup(p.stop)
        self.messages = []
        m = mock.patch.object(team, "message", lambda to, text, from_agent=None: self.messages.append((to, text)))
        m.start()
        self.addCleanup(m.stop)

    def test_ask_give_and_the_value_never_reaches_the_conversation(self):
        told = secret_requests.request("dev-qa", "stripe_key", "to run the payment tests")
        self.assertIn("request #1", told)
        self.assertIn("already asked", secret_requests.request("dev-qa", "STRIPE_KEY", "again"))
        self.assertEqual([n["kind"] for n in notifications.build(config_store.load(), {})], ["secret"])
        secret_requests.give(1, "sk_live_supersecretvalue123")
        to, text = self.messages[-1]
        self.assertEqual(to, "dev-qa")
        self.assertNotIn("supersecretvalue", text)
        path = secret_requests.env_file("dev-qa")
        self.assertIn("export STRIPE_KEY='sk_live_supersecretvalue123'", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(vault.env_for("dev-qa"), {"STRIPE_KEY": "sk_live_supersecretvalue123"})
        with self.assertRaises(secret_requests.SecretRequestError):
            secret_requests.give(1, "again")
        secret_requests.forget_file("dev-qa")
        self.assertFalse(path.exists())

    def test_decline_and_bad_requests(self):
        secret_requests.request("dev-qa", "GH_TOKEN", "to push")
        secret_requests.decline(1, "use the deploy key")
        self.assertIn("use the deploy key", self.messages[-1][1])
        with self.assertRaises(secret_requests.SecretRequestError):
            secret_requests.request("dev-qa", "not a name", "x")
        with self.assertRaises(secret_requests.SecretRequestError):
            secret_requests.request("dev-qa", "TOKEN", "")


class BrowserViewTests(unittest.TestCase):
    def test_no_browser_open(self):
        import browser_live
        self.assertEqual(browser_live.view("dev-qa"), {"live": False})

    def test_agents_browser_tool_runs_through_the_watchable_wrapper(self):
        config = team.MCP_SERVERS["playwright"][0](support.TMP / "run")
        self.assertTrue(config["args"][0].endswith("browser_live.py"))
        self.assertEqual(config["args"][1:], ["mcp", str(support.TMP / "run")])


if __name__ == "__main__":
    unittest.main()
