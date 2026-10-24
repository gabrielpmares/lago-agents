"""Connectors (connectors.py): hosted MCP servers for agents, signed in once, reached through
Lago's proxy; and Lago Agents as an MCP server for other assistants."""

import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import support

import activity
import config_store
import connectors
import team
from test_vault import FakeKeyring

TOOLS = [{"name": "list_issues", "description": "List issues", "inputSchema": {"type": "object"},
          "annotations": {"readOnlyHint": True}},
         {"name": "delete_issue", "description": "Delete an issue", "inputSchema": {"type": "object"}}]


class FakeServer:
    """A Streamable HTTP MCP server that wants `Bearer good` (or `Bearer fresh`), answering
    tools/list as SSE, plus an OAuth provider with dynamic registration."""

    def __init__(self):
        seen = self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, body=b"", kind="application/json", headers=()):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                base = f"http://127.0.0.1:{outer.port}"
                if self.path == "/.well-known/oauth-protected-resource/mcp":
                    return self._send(200, json.dumps({"resource": f"{base}/mcp", "authorization_servers": [base]}).encode())
                if self.path == "/.well-known/oauth-authorization-server":
                    return self._send(200, json.dumps({"authorization_endpoint": f"{base}/authorize", "token_endpoint": f"{base}/token",
                                                       "registration_endpoint": f"{base}/register"}).encode())
                self._send(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/register":
                    return self._send(201, json.dumps({"client_id": "client-1"}).encode())
                if self.path == "/token":
                    form = urllib.parse.parse_qs(body.decode())
                    seen.append(("token", form["grant_type"][0]))
                    token = "good" if form["grant_type"][0] == "authorization_code" else "fresh"
                    return self._send(200, json.dumps({"access_token": token, "refresh_token": "r1", "expires_in": 3600}).encode())
                if self.headers.get("Authorization") not in ("Bearer good", "Bearer fresh"):
                    return self._send(401, b"{}", headers=[("WWW-Authenticate", f'Bearer resource_metadata="http://127.0.0.1:{outer.port}/.well-known/oauth-protected-resource/mcp"')])
                message = json.loads(body)
                seen.append((message.get("method"), self.headers.get("Mcp-Session-Id")))
                if "id" not in message:
                    return self._send(202)
                if message["method"] == "initialize":
                    return self._send(200, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}}).encode(),
                        headers=[("Mcp-Session-Id", "s-1")])
                if message["method"] == "tools/list":
                    event = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": TOOLS}})
                    return self._send(200, f"event: message\ndata: {event}\n\n".encode(), kind="text/event-stream")
                if message["method"] == "tools/call":
                    return self._send(200, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "content": [{"type": "text", "text": "3 issues"}]}}).encode())
                self._send(200, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}).encode())

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_port
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def browser_that_approves(url):
    """Stands in for you signing in: follows the authorization URL straight to the redirect."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    back = f"{query['redirect_uri'][0]}?code=abc&state={query['state'][0]}"
    threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=10).read(), daemon=True).start()
    return True


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        self.keyring = FakeKeyring()
        patch = mock.patch("vault._backend", self.keyring)
        patch.start()
        self.addCleanup(patch.stop)
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        # Catalog connectors with no sign-in, whatever the shipped catalog holds.
        docs = {"id": "context7", "name": "Context7", "url": "https://mcp.context7.com/mcp", "auth": "none",
                "description": "Library docs."}
        wiki = {"id": "deepwiki", "name": "DeepWiki", "url": "https://mcp.deepwiki.com/mcp", "auth": "none",
                "description": "Repo docs."}
        catalog = mock.patch.object(connectors, "CATALOG", [*connectors.CATALOG, docs, wiki])
        catalog.start()
        self.addCleanup(catalog.stop)

    def add_custom(self, auth="token"):
        return connectors.add(name="Tracker", url=self.server.url, auth=auth)["id"]

    def test_catalog_add_and_who_gets_it(self):
        connectors.add("context7")
        config = config_store.load()
        qa, lago = config["agents"]["dev-qa"], {"orchestrator": True}
        self.assertIn("context7", connectors.servers(config, qa))
        self.assertIn("context7", connectors.servers(config, lago))  # Lago too, by default
        connectors.update("context7", lago=False)
        self.assertNotIn("context7", connectors.servers(config_store.load(), lago))
        connectors.update("context7", projects=["demo"], lago=True)
        config = config_store.load()
        self.assertNotIn("context7", connectors.servers(config, qa))  # dev-qa has no project
        self.assertIn("context7", connectors.servers(config, {"orchestrator": True, "project": "demo"}))
        with self.assertRaises(connectors.ConnectorError):
            connectors.add("context7")
        with self.assertRaises(connectors.ConnectorError):
            connectors.update("context7", projects=["nope"])

    def test_lago_is_told_which_connectors_exist(self):
        connectors.add("context7")
        connectors.add("deepwiki")
        connectors.update("deepwiki", lago=False, projects=["demo"])
        note = connectors.prompt_note(config_store.load(), {"orchestrator": True})
        self.assertIn("Context7: yours too, as the `context7` MCP tools", note)
        self.assertIn("DeepWiki: not yours; agents get it for Demo", note)
        self.assertEqual(connectors.prompt_note(config_store.load() | {"connectors": {}}, {"orchestrator": True}), "")

    def test_lago_restarts_when_idle_to_get_new_connectors(self):
        oid = config_store.ORCHESTRATOR_ID
        run = config_store.STATE_DIR / "runs" / oid
        run.mkdir(parents=True, exist_ok=True)
        (run / "mcp.json").write_text(json.dumps({"mcpServers": {"lago": {}}}))
        connectors.add("context7")
        connectors._lago_restarted.clear()
        restarts = []
        status = {"status": "working", "session_id": None}
        with mock.patch("runner.is_running", lambda a: True), \
                mock.patch("hub.get_state", lambda a: status), \
                mock.patch("team.restart_orchestrator", lambda: restarts.append(1)):
            self.assertFalse(connectors.refresh_lago())  # busy: wait
            status["status"] = "idle"
            self.assertTrue(connectors.refresh_lago())
            self.assertFalse(connectors.refresh_lago())  # once per change, not a loop
            (run / "mcp.json").write_text(json.dumps({"mcpServers": {"lago": {}, "context7": {}}}))
            self.assertFalse(connectors.refresh_lago())  # already has it
        self.assertEqual(restarts, [1])

    def test_not_signed_in_means_agents_dont_get_it(self):
        cid = self.add_custom()
        config = config_store.load()
        self.assertEqual(connectors.servers(config, config["agents"]["dev-qa"]), {})
        with self.assertRaises(connectors.ConnectorError):
            connectors.sign_in(cid, "wrong")  # the server refuses it, so it isn't kept
        self.assertIsNone(connectors.credential(cid))
        self.assertIn("2 tools", connectors.sign_in(cid, "good")["message"])
        config = config_store.load()
        spec = connectors.servers(config, config["agents"]["dev-qa"])[cid]
        self.assertEqual(spec["args"][1:], ["proxy", cid])
        self.assertNotIn("good", json.dumps(spec))  # the token never reaches the agent's config
        self.assertEqual(connectors.allowed(config, config["agents"]["dev-qa"]), [f"mcp__{cid}"])
        connectors.update(cid, ask=True)
        self.assertEqual(connectors.allowed(config_store.load(), config["agents"]["dev-qa"]), [])

    def test_agent_mcp_config_includes_connectors_but_project_servers_win(self):
        cid = self.add_custom()
        connectors.sign_in(cid, "good")
        config = config_store.load()
        servers = team._mcp_config(config, "dev-qa", None, support.TMP)["mcpServers"]
        self.assertIn(cid, servers)
        self.assertIn("lago", servers)

    def test_oauth_sign_in_and_refresh(self):
        cid = self.add_custom(auth="oauth")
        with mock.patch("webbrowser.open", browser_that_approves):
            connectors.sign_in(cid)
        cred = connectors.credential(cid)
        self.assertEqual((cred["client_id"], cred["access_token"]), ("client-1", "good"))
        self.keyring.store(f"connector-{cid}", connectors.KEYRING_PROJECT, json.dumps({**cred, "expires_at": 0}))
        self.assertEqual(connectors.bearer(cid), "Bearer fresh")
        self.assertIn(("token", "refresh_token"), self.server.seen)

    def test_tools_and_switching_them_off(self):
        cid = self.add_custom()
        connectors.sign_in(cid, "good")
        connectors.update(cid, blocked_tools=["delete_issue"])
        tools = {t["name"]: t for t in connectors.check(cid)["tools"]}
        self.assertFalse(tools["delete_issue"]["on"])
        self.assertTrue(tools["list_issues"]["read_only"])

    def test_proxy_forwards_hides_blocked_tools_and_logs_calls(self):
        cid = self.add_custom()
        connectors.sign_in(cid, "good")
        connectors.update(cid, blocked_tools=["delete_issue"])
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": connectors._initialize_params()},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_issues", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "delete_issue", "arguments": {}}},
        ]
        out = []
        read, write = os.pipe()
        with os.fdopen(write, "w") as stdin_writer:
            stdin_writer.write("".join(json.dumps(m) + "\n" for m in messages))
        import io
        lines = io.StringIO()
        with mock.patch.object(sys, "stdin", os.fdopen(read)), mock.patch.object(sys, "stdout", lines), \
                mock.patch.dict(os.environ, {"LAGO_AGENT_ID": "dev-qa"}):
            connectors.proxy(cid)
            for _ in range(100):  # requests run on threads: wait for the last answers
                out = [json.loads(l) for l in lines.getvalue().splitlines()]
                if len(out) >= 4:
                    break
                threading.Event().wait(0.05)
        by_id = {m["id"]: m for m in out}
        self.assertEqual([t["name"] for t in by_id[2]["result"]["tools"]], ["list_issues"])
        self.assertEqual(by_id[3]["result"]["content"][0]["text"], "3 issues")
        self.assertIn("switched off", by_id[4]["error"]["message"])
        self.assertIn(("tools/call", "s-1"), self.server.seen)  # the session id is carried on
        calls = connectors.calls()
        self.assertEqual([(c["actor"], c["text"]) for c in calls], [("dev-qa", "Tracker: list_issues")])

    def test_remove_deletes_the_sign_in(self):
        cid = self.add_custom()
        connectors.sign_in(cid, "good")
        connectors.remove(cid)
        self.assertIsNone(connectors.credential(cid))
        self.assertNotIn(cid, connectors.installed())
        self.assertTrue(activity.timeline(kind="connector.removed"))

    def test_custom_connector_checks(self):
        for bad in ({"name": "", "url": self.server.url}, {"name": "X", "url": "http://example.com"},
                    {"name": "X", "url": "https://a", "command": "b"}, {"name": "lago", "url": "https://a"}):
            with self.assertRaises(connectors.ConnectorError):
                connectors.add(**bad)

    def test_lago_as_an_mcp_server_setup(self):
        found = connectors.assistants()
        self.assertIn("lago_mcp.py --public", found["snippets"]["Claude Code"])
        self.assertEqual(json.loads(found["snippets"]["OpenCode (opencode.json)"])["mcp"]["lago-agents"]["type"], "local")

    def test_public_server_inside_an_agent_session_offers_nothing(self):
        env = {**os.environ, "LAGO_AGENT_ID": "dev-qa"}
        proc = subprocess.run([sys.executable, str(support.APP / "lago_mcp.py"), "--public"],
                              input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}) + "\n",
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(json.loads(proc.stdout.splitlines()[0])["result"]["tools"], [])


if __name__ == "__main__":
    unittest.main()


class OwnClientTests(unittest.TestCase):
    """Google, Slack and the like: you create the OAuth client once, per provider."""

    def setUp(self):
        support.reset()
        self.keyring = FakeKeyring()
        patch = mock.patch("vault._backend", self.keyring)
        patch.start()
        self.addCleanup(patch.stop)
        for p in [mock.patch.object(connectors, "check", return_value={"tools": ["a"]}),
                  mock.patch.object(connectors, "refresh_lago_soon", create=True)]:
            p.start()
            self.addCleanup(p.stop)
        connectors.add("google-gmail")
        connectors.add("google-calendar")

    def fake_oauth(self, conf, own_client=None, params=None):
        self.used = (own_client, params)
        return {"kind": "oauth", **own_client, "token_endpoint": "t", "resource": conf["url"],
                "access_token": "a", "refresh_token": "r", "expires_at": 9e9}

    def test_asks_for_a_client_first(self):
        with self.assertRaises(connectors.ConnectorError) as raised:
            connectors.sign_in("google-gmail")
        self.assertIn("own OAuth client", str(raised.exception))

    def test_one_google_client_serves_every_google_connector(self):
        with mock.patch.object(connectors, "_sign_in_oauth", side_effect=self.fake_oauth):
            connectors.sign_in("google-gmail", client_id="id-1", client_secret="s-1")
            self.assertEqual(self.used[1], {"access_type": "offline", "prompt": "consent"})
            connectors.sign_in("google-calendar")  # nothing typed again
        self.assertEqual(self.used[0], {"client_id": "id-1", "client_secret": "s-1"})
        listed = {c["id"]: c for c in connectors.listing()["installed"]}
        self.assertTrue(listed["google-calendar"]["client"]["known"])
        self.assertEqual(listed["google-calendar"]["client"]["redirect"], connectors.OWN_CLIENT_REDIRECT)

    def test_catalog_ids_are_unique_and_groups_are_known(self):
        ids = [c["id"] for c in connectors.CATALOG]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(c["client"] in connectors.CLIENTS for c in connectors.CATALOG if c.get("client")))
