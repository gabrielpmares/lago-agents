"""A company on one Lago Agents (#A, #B): people, roles per project, the policy, and the
browser server that checks every call against them."""

import json
import threading
import unittest
import urllib.request
from unittest import mock

import support

import company
import config_store
import hub


def setup_company():
    config = config_store.load()
    config["projects"]["shop"] = {"name": "Shop", "cwd": str(support.TMP)}
    config_store.save(config)
    company.setup("Acme", "owner@acme.com")
    ws_demo = hub.create_workspace("demo", None, "Demo task")
    ws_shop = hub.create_workspace("shop", None, "Shop task")
    hub.add_swarm_agent(f"w{ws_demo}-qa", ws_demo, "demo", "qa", "QA")
    hub.add_swarm_agent(f"w{ws_shop}-qa", ws_shop, "shop", "qa", "QA")
    return ws_demo, ws_shop


class PolicyTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.demo, self.shop = setup_company()
        company.request_access({"login": "ann@acme.com", "name": "Ann"})
        company.request_access({"login": "bob@acme.com", "name": "Bob"})

    def test_pending_people_can_do_nothing(self):
        self.assertIsNone(company.role_for("ann@acme.com", "demo"))
        with self.assertRaises(company.AccessError):
            company.authorize("ann@acme.com", "workspace", {"workspace_id": self.demo})

    def test_roles_per_project(self):
        company.admit("ann@acme.com", "watch")
        company.set_project_role("ann@acme.com", "shop", "operate")
        company.set_project_role("ann@acme.com", "demo", "none")
        self.assertEqual(company.projects_for("ann@acme.com"), {"shop": "operate", "": "watch"})
        company.authorize("ann@acme.com", "workspace", {"workspace_id": self.shop})
        company.authorize("ann@acme.com", "send_message", {"agent_id": f"w{self.shop}-qa", "text": "hi"})
        with self.assertRaisesRegex(company.AccessError, "access to demo"):
            company.authorize("ann@acme.com", "workspace", {"workspace_id": self.demo})
        with self.assertRaises(company.AccessError):
            company.authorize("ann@acme.com", "decide_pipeline", {"pipeline_id": 1})  # no such plan
        with self.assertRaisesRegex(company.AccessError, "company admins"):
            company.authorize("ann@acme.com", "save_config", {"config": {}})
        with self.assertRaisesRegex(company.AccessError, "admin access"):
            company.authorize("ann@acme.com", "press", {"agent_id": f"w{self.shop}-qa", "key": "approve"})
        company.authorize("ann@acme.com", "press", {"agent_id": f"w{self.shop}-qa", "key": "interrupt"})

    def test_lago_and_fixed_teams_are_company_level(self):
        company.admit("bob@acme.com", "operate")
        with self.assertRaises(company.AccessError):
            company.authorize("bob@acme.com", "send_message", {"agent_id": "dev-qa", "text": "x"})
        company.admit("bob@acme.com", "watch", admin=True)
        company.authorize("bob@acme.com", "send_message", {"agent_id": "dev-qa", "text": "x"})
        company.authorize("owner@acme.com", "anything_at_all", {})

    def test_filter_result(self):
        company.admit("ann@acme.com", "watch")
        company.set_project_role("ann@acme.com", "demo", "none")
        tasks = [{"href": f"#work/{self.demo}"}, {"href": f"#work/{self.shop}"}]
        self.assertEqual(company.filter_result("ann@acme.com", "my_tasks", tasks), [{"href": f"#work/{self.shop}"}])
        overview = {"agents": {f"w{self.demo}-qa": {}, f"w{self.shop}-qa": {}, "dev-qa": {}},
                    "swarm_agents": {f"w{self.shop}-qa": {"swarm": True, "project": "shop"}},
                    "notifications": [{"href": f"#work/{self.demo}"}, {"href": "#accounts", "kind": "account"}],
                    "open_sessions": [], "account_alert": {"x": 1}, "pipelines_waiting": 3}
        out = company.filter_result("ann@acme.com", "overview", overview)
        self.assertEqual(list(out["agents"]), [f"w{self.shop}-qa"])
        self.assertEqual((out["notifications"], out["account_alert"], out["pipelines_waiting"]), ([], None, 0))
        self.assertIs(company.filter_result("owner@acme.com", "my_tasks", tasks), tasks)


class FakeApi:
    def init(self):
        return {"config": config_store.load(), "catalog": {}}

    def send_message(self, agent_id, text):
        import activity
        activity.log("you", "message.sent", text, target=agent_id)
        return {"ok": True}

    def overview(self):
        return {"agents": {}, "swarm_agents": {}, "notifications": [], "open_sessions": [], "account_alert": None,
                "pipelines_waiting": 0}


class WebServerTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.demo, self.shop = setup_company()
        import remote
        self.remote = remote
        self.server = remote.WebServer(("127.0.0.1", 0), ["127.0.0.0/8"], FakeApi(), RuntimeError,
                                       {"init", "send_message", "overview"}, {"127.0.0.1", "localhost"})
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.identity = {"login": "ann@acme.com", "name": "Ann", "picture": "", "device": "ann-laptop"}
        p = mock.patch.object(remote.WebHandler, "who", lambda handler: self.identity)
        p.start()
        self.addCleanup(p.stop)

    def get(self, path, host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"Host": host} if host else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def call(self, method, **params):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/call", method="POST",
                                     data=json.dumps({"id": 1, "method": method, "params": params}).encode(),
                                     headers={"Content-Type": "application/json", "X-Lago-Web": "1"})
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read())}

    def test_strangers_get_the_join_page_and_can_ask(self):
        status, page = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Ask to Join", page)
        self.assertEqual(self.call("overview")["status"], 403)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/request", method="POST", data=b"")
        urllib.request.urlopen(req).read()
        self.assertEqual(company.member("ann@acme.com")["status"], "pending")
        self.assertIn("asked to join", self.get("/")[1])

    def test_the_join_page_lets_its_form_send_its_origin(self):
        # Under no-referrer, Chromium posts the form with "Origin: null", and over plain http on
        # the tailnet there's no Sec-Fetch-Site to tell it apart from another site's form
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            self.assertEqual(r.headers.get_all("Referrer-Policy"), ["same-origin"])
        def ask(origin):
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/request", method="POST", data=b"",
                                         headers={"Origin": origin})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(ask("null"), 403)
        self.assertEqual(ask("http://evil.example"), 403)
        self.assertIsNone(company.member("ann@acme.com"))
        self.assertEqual(ask(f"http://127.0.0.1:{self.port}"), 200)
        self.assertEqual(company.member("ann@acme.com")["status"], "pending")

    def test_people_kept_out_see_nothing_even_if_once_let_in(self):
        company.request_access(self.identity)
        company.admit("ann@acme.com", "operate")
        company.block("ANN@acme.com")
        self.assertEqual(company.member("ann@acme.com")["status"], "removed")
        status, page = self.get("/")
        self.assertEqual(status, 403)
        self.assertNotIn("Acme", page)
        self.assertEqual(self.call("overview")["status"], 403)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/request", method="POST", data=b"")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(req)
        with self.assertRaises(company.AccessError):
            company.request_access(self.identity)
        with self.assertRaises(company.AccessError):
            company.block("owner@acme.com")
        company.unblock("ann@acme.com")
        self.assertEqual(self.get("/")[0], 200)

    def test_members_get_the_app_and_calls_follow_their_role(self):
        company.request_access(self.identity)
        company.admit("ann@acme.com", "watch")
        status, page = self.get("/")
        self.assertIn("web-bridge.js", page)
        me = self.call("me")["data"]
        self.assertEqual((me["login"], me["projects"]["demo"]), ("ann@acme.com", "watch"))
        refused = self.call("send_message", agent_id=f"w{self.shop}-qa", text="hi")
        self.assertFalse(refused["ok"])
        self.assertIn("operate access", refused["error"])
        company.set_project_role("ann@acme.com", "shop", "operate")
        self.assertTrue(self.call("send_message", agent_id=f"w{self.shop}-qa", text="hi")["ok"])
        import activity
        self.assertEqual(activity.timeline(kind="message")[0]["actor"], "ann@acme.com")  # attributed to her
        init = self.call("init")["data"]
        self.assertTrue(init["web"])
        self.assertNotIn("accounts", init["config"])

    def test_the_page_may_call_its_own_server(self):
        company.request_access(self.identity)
        company.admit("ann@acme.com", "watch")
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            policy = r.headers["Content-Security-Policy"]
        self.assertIn("connect-src 'self'", policy)

    def test_other_host_names_are_refused(self):
        self.assertEqual(self.get("/", host="evil.example.com")[0], 421)


if __name__ == "__main__":
    unittest.main()


class MapTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_project_map_shape_and_no_project_scope(self):
        import attachments, background
        background.start_loops = lambda: None
        attachments.start_sweeping = lambda: None
        import api
        demo, shop = setup_company()
        m = api.Api(None).project_map("demo")
        kinds = [n["kind"] for n in m["nodes"]]
        self.assertEqual(kinds, ["project", "task", "lead", "agent"])
        self.assertIn({"from": "project", "to": f"task-{demo}"}, m["edges"])
        self.assertEqual(company.scope("project_map", {"project_id": ""}), "")
        company.request_access({"login": "cy@acme.com", "name": "Cy"})
        company.admit("cy@acme.com", "watch")
        company.authorize("cy@acme.com", "project_map", {"project_id": ""})

    def test_the_terminal_wall_shows_each_running_agents_screen_with_secrets_hidden(self):
        import attachments, background, runner
        background.start_loops = lambda: None
        attachments.start_sweeping = lambda: None
        import api
        setup_company()
        a = api.Api(None)
        agents = [n["id"] for n in a.project_map("demo")["nodes"] if n["kind"] in ("lead", "agent")]
        screen = "\x1b[32m✓ tests pass\x1b[0m\ntoken sk-ant-" + "a" * 30 + "\n\n\n"
        with mock.patch.object(runner, "running_agents", lambda: {agents[1]}), \
                mock.patch.object(runner, "tmux", lambda *args: (True, screen)):
            wall = a.terminal_wall("demo")
        self.assertEqual([w["id"] for w in wall], [agents[1]])  # only what's running
        self.assertTrue(wall[0]["task"])
        self.assertTrue(wall[0]["text"].startswith("\x1b[32m✓ tests pass"))
        self.assertNotIn("sk-ant-aaaa", wall[0]["text"])
        self.assertFalse(wall[0]["text"].endswith("\n"))
        self.assertEqual(company.scope("terminal_wall", {"project": ""}), "")
        company.request_access({"login": "cy@acme.com", "name": "Cy"})
        company.admit("cy@acme.com", "watch")
        company.set_project_role("cy@acme.com", "shop", "none")
        company.authorize("cy@acme.com", "terminal_wall", {"project": "demo"})
        with self.assertRaisesRegex(company.AccessError, "access to shop"):
            company.authorize("cy@acme.com", "terminal_wall", {"project": "shop"})


class MemberSubscriptionTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        import attachments, background
        background.start_loops = lambda: None
        attachments.start_sweeping = lambda: None
        self.demo, self.shop = setup_company()
        company.request_access({"login": "ann@acme.com", "name": "Ann"})
        company.admit("ann@acme.com", "operate")

    def test_connect_claude_makes_her_own_account(self):
        import member_accounts, server, accounts
        with mock.patch.object(server, "launch", return_value="login-x") as launch, \
                mock.patch.object(accounts, "prepare"):
            member_accounts.connect("ann@acme.com", "Ann", "claude")
        aid = member_accounts.account_id("ann@acme.com")
        self.assertEqual(company.member("ann@acme.com")["account"], aid)
        self.assertEqual(config_store.load()["accounts"][aid]["member"], "ann@acme.com")
        self.assertIn("claude auth login", launch.call_args.args[2])
        # Her account is never borrowed when yours runs out.
        with mock.patch.object(accounts, "has_room", return_value=True):
            self.assertNotEqual(accounts.with_room(config_store.load(), "main"), aid)

    def test_connect_codex_uses_device_login(self):
        import member_accounts, server
        with mock.patch.object(server, "launch", return_value="login-x") as launch:
            member_accounts.connect("ann@acme.com", "Ann", "codex")
        self.assertIn("codex login --device-auth", launch.call_args.args[2])
        self.assertIn("CODEX_HOME=", launch.call_args.args[2])

    def test_members_need_their_own_subscription_and_a_project(self):
        import activity, api, swarm, member_accounts
        a = api.Api(None)
        activity.set_person({"login": "ann@acme.com", "name": "Ann"})
        try:
            with self.assertRaisesRegex(api.ApiError, "Pick a project"):
                a.new_task("Do it")
            with self.assertRaisesRegex(api.ApiError, "Connect your Claude subscription"):
                a.new_task("Do it", project="demo")
            with hub.connect() as conn:
                conn.execute("UPDATE members SET account = 'm-ann' WHERE login = 'ann@acme.com'")
            import team
            (support.TMP / "demo").mkdir(exist_ok=True)
            with mock.patch.object(team, "start"):
                res = a.new_task("Do it", project="demo")
            ws = hub.get_workspace(int(res["href"].split("/")[1]))
            self.assertEqual(ws["account"], "m-ann")
            self.assertEqual(config_store.load()["agents"][f"w{ws['id']}"]["account"], "m-ann")
            self.assertFalse(ws["unrestricted"])
        finally:
            activity.set_person(None)


class JoinTests(unittest.TestCase):
    def setUp(self):
        support.reset()

    def test_check_hub_and_join(self):
        import http.server
        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<title>Lago Agents</title>"
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        self.assertTrue(company.check_hub(url)["ok"])
        self.assertFalse(company.check_hub("not a url")["ok"])
        self.assertFalse(company.check_hub("http://127.0.0.1:1")["ok"])
        company.join(url)
        config = config_store.load()
        self.assertEqual((config["company"]["joined"], config["onboarded"]), (url, True))


class CompanyTailnetTests(unittest.TestCase):
    """Browser access behind the company's own tailnet: identity from Tailscale's serve headers."""

    def setUp(self):
        support.reset()
        setup_company()
        import remote
        self.socket = support.TMP / "company-tailnet" / "web.sock"
        self.server = remote.CompanyWebServer(self.socket, FakeApi(), RuntimeError, {"init", "overview"},
                                              {"127.0.0.1", "lago-box.acme.ts.net"})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def page(self, headers):
        import http.client
        import socket as sockets
        conn = http.client.HTTPConnection("localhost")
        conn.sock = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
        conn.sock.connect(str(self.socket))
        conn.request("GET", "/", headers=headers)
        try:
            return conn.getresponse().read().decode()
        finally:
            conn.close()

    def test_only_your_user_can_open_the_socket(self):
        import stat
        self.assertEqual(stat.S_IMODE(self.socket.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.socket.parent.stat().st_mode), 0o700)
        self.assertIn("web-bridge.js", self.page({"Host": "lago-box.acme.ts.net"}))  # your own programs: you

    def test_visitors_are_who_tailscale_says(self):
        page = self.page({"Host": "lago-box.acme.ts.net", "Tailscale-User-Login": "Dee@Acme.com", "Tailscale-User-Name": "Dee"})
        self.assertIn("Ask to Join", page)
        self.assertIn("dee@acme.com", page)

    def test_the_owners_company_login_is_the_owner(self):
        company.add_owner_login("sam@acme-company.com")
        self.assertTrue(company.is_owner("sam@acme-company.com"))
        self.assertTrue(company.is_owner("owner@acme.com"))
        page = self.page({"Host": "lago-box.acme.ts.net", "Tailscale-User-Login": "sam@acme-company.com"})
        self.assertIn("web-bridge.js", page)
