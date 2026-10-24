"""Lago Agents without GTK (app_server.py, used on macOS): the page's calls, who gets in, and
agents' terminals streamed to the page from a real tmux session (on a tmux server of the
test's own)."""

import http.client
import json
import shutil
import socket
import threading
import time
import unittest
from unittest import mock

import support

import app_server
import config_store
import runner


class AppServerTests(unittest.TestCase):
    def setUp(self):
        support.reset()
        with mock.patch("background.start_loops"):
            self.server = app_server.Server()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.cookie = f"{app_server.COOKIE}={self.server.token}"

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Host": host or f"127.0.0.1:{self.port}", **(headers or {})})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def call(self, method, **params):
        response, data = self.request("POST", "/v1/call", {"method": method, "params": params},
                                      {"Cookie": self.cookie, "X-Lago-Web": "1", "Content-Type": "application/json"})
        return response.status, json.loads(data)

    def test_only_the_window_opened_with_the_secret_gets_in(self):
        response, _ = self.request("GET", f"/?t={self.server.token}&view=tasks")
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader("Location"), "/#tasks")
        self.assertIn("HttpOnly; SameSite=Strict", response.getheader("Set-Cookie"))
        response, page = self.request("GET", "/", headers={"Cookie": self.cookie})
        self.assertEqual(response.status, 200)
        self.assertIn(b'<script src="web-bridge.js"></script>', page)  # the page talks over HTTP
        self.assertIn(b'class="page-desk"', page)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertEqual(self.request("GET", "/")[0].status, 403)  # no cookie
        self.assertEqual(self.request("GET", "/?t=guess")[0].status, 403)
        self.assertEqual(self.request("GET", "/", headers={"Cookie": f"{app_server.COOKIE}=guess"})[0].status, 403)
        self.assertEqual(self.request("GET", "/", headers={"Cookie": self.cookie}, host="evil.example")[0].status, 421)
        self.assertEqual(self.request("GET", "/../config.json", headers={"Cookie": self.cookie})[0].status, 404)
        # A page elsewhere can't call: no cookie across sites, and no custom header without CORS.
        response, _ = self.request("POST", "/v1/call", {"method": "init"}, {"Cookie": self.cookie})
        self.assertEqual(response.status, 403)

    def test_the_page_calls_the_app_like_the_window_does(self):
        status, reply = self.call("init")
        self.assertEqual((status, reply["ok"], reply["data"]["page_terminal"]), (200, True, True))
        self.assertFalse(self.call("no_such_method")[1]["ok"])
        self.assertFalse(self.call("_private")[1]["ok"])
        self.assertEqual(self.call("show_terminal", agent_id="dev-qa")[1]["data"], {"shown": False, "page": True})

    @unittest.skipUnless(shutil.which("tmux"), "tmux isn't installed")
    def test_an_agents_terminal_streams_to_the_page_and_takes_its_keys(self):
        socket_name = f"lago-test-{int(time.time() * 1000) % 10**8}"
        patch = mock.patch.object(runner, "SOCKET", socket_name)
        patch.start()
        self.addCleanup(patch.stop)
        real_tmux = support.REAL_TMUX
        with mock.patch.object(runner, "tmux", real_tmux), mock.patch.object(runner, "is_running", lambda a: real_tmux("has-session", "-t", f"={runner.session_name(a)}")[0]):
            self.addCleanup(support.end_tmux)
            ok, out = real_tmux("new-session", "-d", "-s", runner.session_name("dev-qa"), "-x", "100", "-y", "30",
                                "sh", "-c", "PS1='$ ' exec sh -i")
            self.assertTrue(ok, out)
            support.note_tmux()
            self.assertTrue(self.call("show_terminal", agent_id="dev-qa")[1]["data"]["shown"])

            conn = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            conn.sendall((f"GET /v1/term?agent=dev-qa&cols=90&rows=24 HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                          f"Cookie: {self.cookie}\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(1)
            self.assertIn(b"200", head.split(b"\r\n")[0])
            term_id = next(line.split(b": ")[1].decode() for line in head.split(b"\r\n") if line.lower().startswith(b"x-term-id"))

            def seen(text, seconds=10):
                deadline, data = time.time() + seconds, b""
                conn.settimeout(0.5)
                while time.time() < deadline:
                    try:
                        data += conn.recv(65536)
                    except socket.timeout:
                        pass
                    if text in data:
                        return True
                return False

            response, _ = self.request("POST", "/v1/term/input", {"id": term_id, "data": "echo page-$((40+2))\r"},
                                       {"Cookie": self.cookie, "X-Lago-Web": "1"})
            self.assertEqual(response.status, 200)
            self.assertTrue(seen(b"page-42"))  # typed in the page, run in the session, back on the page
            self.assertIn("page-42", real_tmux("capture-pane", "-p", "-t", f"={runner.session_name('dev-qa')}:")[1])

            self.request("POST", "/v1/term/resize", {"id": term_id, "cols": 70, "rows": 20}, {"Cookie": self.cookie, "X-Lago-Web": "1"})
            deadline = time.time() + 5
            while time.time() < deadline:
                width = real_tmux("display-message", "-p", "-t", f"={runner.session_name('dev-qa')}:", "#{window_width}")[1]
                if width == "70":
                    break
                time.sleep(0.2)
            self.assertEqual(width, "70")

            conn.close()  # the page goes away: its attach ends, the session keeps running
            deadline = time.time() + 5
            while self.server.terminals and time.time() < deadline:
                time.sleep(0.1)
            self.assertEqual(self.server.terminals, {})
            self.assertTrue(real_tmux("has-session", "-t", f"={runner.session_name('dev-qa')}")[0])
            response, _ = self.request("POST", "/v1/term/input", {"id": term_id, "data": "x"}, {"Cookie": self.cookie, "X-Lago-Web": "1"})
            self.assertEqual(response.status, 410)


if __name__ == "__main__":
    unittest.main()
