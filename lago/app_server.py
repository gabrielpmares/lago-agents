"""Lago Agents without GTK (macOS, or `lago-agents --browser`): the same app, served to a
window on this computer.

A small HTTP server on 127.0.0.1 serves ui/ and answers the page's calls (POST /v1/call), the
way the GTK window's script handler does. Agents' terminals are real terminals in the page:
each one is `tmux attach` in a pseudo-terminal here, streamed to the page's xterm.js
(GET /v1/term) with its keystrokes and size sent back (POST /v1/term/input, /resize).

Only you get in: the server listens on loopback, answers only its own Host names, and wants
a secret that the window is opened with (?t=…, then an HttpOnly, SameSite=Strict cookie).
Every call also carries a header a web page elsewhere can't send across origins.

    app_server.py [view]       serve, and open the window (a native one with pywebview,
                               else an app window in Chrome, else the default browser)
"""

import fcntl
import json
import os
import pty
import secrets
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_store  # noqa: E402
import desktop  # noqa: E402
import runner  # noqa: E402

UI_DIR = Path(__file__).resolve().parent / "ui"
STATE_FILE = config_store.STATE_DIR / "app-server.json"
COOKIE = "lago_desk"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".png": "image/png",
         ".json": "application/json", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "media-src 'self' blob: data:; connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")


class PageWindow:
    """Stands in for the GTK window: the page shows terminals itself."""

    def show_terminal(self, _agent_id):
        pass

    def hide_terminal(self):
        pass


def _page_api():
    import api

    class PageApi(api.Api):
        def __init__(self):
            super().__init__(PageWindow())

        def init(self):
            return {**super().init(), "page_terminal": True}

        def show_terminal(self, agent_id):
            return {"shown": runner.is_running(agent_id), "page": True}

        def hide_terminal(self):
            return {"ok": True}

        def clipboard_files(self):
            return {"files": []}  # a browser hands the page what's pasted itself

    return PageApi(), api.ApiError, api.UI_THREAD


class Terminal:
    """`tmux attach` to one agent's session in a pseudo-terminal, for one page."""

    def __init__(self, agent_id, cols, rows):
        self.agent_id = agent_id
        self.id = secrets.token_hex(8)
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        env["TERM"] = "xterm-256color"
        # subprocess, not pty.fork(): forking a process with threads (and, on macOS, the
        # window's Objective-C runtime) is unsafe; subprocess execs at once.
        self.fd, child = pty.openpty()
        self.proc = subprocess.Popen(runner.attach_argv(agent_id), stdin=child, stdout=child, stderr=child,
                                     cwd=str(Path.home()), env=env, start_new_session=True, close_fds=True)
        os.close(child)
        self.pid = self.proc.pid
        self.resize(cols, rows)
        self.last_seen = time.time()

    def resize(self, cols, rows):
        cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 300))
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.kill(self.pid, signal.SIGWINCH)  # not its controlling terminal: tell it the size changed
        except OSError:
            pass

    def write(self, data):
        os.write(self.fd, data)

    def close(self):
        """Detach: the agent keeps running in tmux."""
        try:
            os.kill(self.pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        threading.Thread(target=self.proc.wait, daemon=True).start()  # collect it when it exits


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LagoAgents"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    # --- who may come in

    def host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        return host in self.server.hosts

    def authed(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        given = cookie[COOKIE].value if COOKIE in cookie else ""
        return secrets.compare_digest(given, self.server.token)

    def send(self, status, body=b"", kind="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def deny(self, status=403, text="Not allowed"):
        self.send(status, json.dumps({"ok": False, "error": text}).encode())

    # --- pages

    def do_GET(self):
        if not self.host_ok():
            return self.deny(421, "Wrong host")
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == "/" and query.get("t") and secrets.compare_digest(query["t"][0], self.server.token):
            # The window opens with the secret once; from then on it's a cookie no other site sees.
            view = query.get("view", [""])[0]
            return self.send(303, extra={"Location": "/" + (f"#{view}" if view else ""), "Set-Cookie":
                                         f"{COOKIE}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"})
        if not self.authed():
            return self.send(403, b"<p>Open Lago Agents from its app icon.</p>", "text/html; charset=utf-8")
        if url.path == "/v1/term":
            return self.terminal_stream(query)
        path = "index.html" if url.path in ("/", "/index.html") else url.path.lstrip("/")
        target = (UI_DIR / path).resolve()
        if UI_DIR.resolve() not in target.parents or not target.is_file():
            return self.deny(404, "Not found")
        body = target.read_bytes()
        if path == "index.html":
            body = body.replace(b'<script src="app.js"></script>',
                                b'<script src="web-bridge.js"></script>\n<script src="app.js"></script>')
            body = body.replace(b'<html lang="en">', b'<html lang="en" class="page-desk">', 1)
        self.send(200, body, TYPES.get(target.suffix, "application/octet-stream"), {"Content-Security-Policy": CSP})

    # --- calls

    def do_POST(self):
        if not self.host_ok():
            return self.deny(421, "Wrong host")
        if not self.authed() or self.headers.get("X-Lago-Web") != "1":
            return self.deny()
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024 * 1024:
            return self.deny(413, "Too big")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.deny(400, "Bad request")
        path = urlsplit(self.path).path
        if path == "/v1/call":
            return self.call(data)
        if path == "/v1/term/input":
            term = self.server.terminals.get(str(data.get("id")))
            if not term:
                return self.deny(410, "That terminal closed")
            try:
                term.write(str(data.get("data", "")).encode("utf-8", "surrogatepass")[:65536])
            except OSError:
                return self.deny(410, "That terminal closed")
            return self.send(200, b'{"ok":true}')
        if path == "/v1/term/resize":
            term = self.server.terminals.get(str(data.get("id")))
            if term:
                term.resize(data.get("cols", 80), data.get("rows", 24))
            return self.send(200, b'{"ok":true}')
        return self.deny(404, "Not found")

    def call(self, data):
        method, params = data.get("method", ""), data.get("params") or {}
        handler = getattr(self.server.api, method, None)
        if not isinstance(method, str) or method.startswith("_") or not callable(handler) or not isinstance(params, dict):
            return self.send(200, json.dumps({"ok": False, "error": f"Unknown method {method}"}).encode())
        try:
            result = {"ok": True, "data": handler(**params)}
        except self.server.api_error as error:
            result = {"ok": False, "error": str(error)}
        except Exception as error:  # report backend bugs instead of hanging the promise
            print(f"lago-agents: {method} failed: {error!r}", file=sys.stderr)
            result = {"ok": False, "error": f"Something went wrong: {error}"}
        self.send(200, json.dumps(result, default=str).encode())

    # --- terminals

    def terminal_stream(self, query):
        agent_id = query.get("agent", [""])[0]
        if agent_id not in config_store.load()["agents"] or not runner.is_running(agent_id):
            return self.deny(404, "That session isn't running")
        try:
            term = Terminal(agent_id, query.get("cols", ["100"])[0], query.get("rows", ["30"])[0])
        except (OSError, ValueError) as error:
            return self.deny(500, f"Couldn't open the terminal: {error}")
        self.server.terminals[term.id] = term
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Term-Id", term.id)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                ready, _, _ = select.select([term.fd, self.connection], [], [], 30)
                if self.connection in ready:
                    import socket
                    if not self.connection.recv(1, socket.MSG_PEEK):  # the page went away
                        break
                if term.fd not in ready:
                    continue
                try:
                    data = os.read(term.fd, 65536)
                except OSError:  # tmux detached or the session ended
                    break
                if not data:
                    break
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.terminals.pop(term.id, None)
            term.close()
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0):
        super().__init__(("127.0.0.1", port), Handler)
        self.token = secrets.token_urlsafe(32)
        self.api, self.api_error, _ = _page_api()
        self.terminals = {}
        port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/?t={self.token}"


def _running():
    """The server another copy of the app already runs: (url) or None."""
    try:
        info = json.loads(STATE_FILE.read_text())
        os.kill(int(info["pid"]), 0)
    except (OSError, ValueError, KeyError):
        return None
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/v1/ping", timeout=1):
            pass
    except urllib.error.HTTPError:
        return info["url"]  # it answered (403/404): it's ours
    except Exception:
        return None
    return info["url"]


def serve(start_view="", open_window=True):
    already = _running()
    if already:
        desktop.open_app_window(already + (f"&view={start_view}" if start_view else ""))
        return None
    server = Server()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.touch(mode=0o600, exist_ok=True)
    STATE_FILE.chmod(0o600)
    STATE_FILE.write_text(json.dumps({"pid": os.getpid(), "port": server.server_address[1], "url": server.url}))
    import background
    background.start_loops()
    url = server.url + (f"&view={start_view}" if start_view else "")
    if not open_window:
        print(url, flush=True)
        try:
            server.serve_forever()
        finally:
            _stop(server)
        return server
    threading.Thread(target=server.serve_forever, name="app-server", daemon=True).start()
    try:
        desktop.app_window(url)  # returns when the window closes (or at once, for a browser tab)
    finally:
        if desktop.WINDOW_BLOCKS:
            _stop(server)
    if not desktop.WINDOW_BLOCKS:  # a browser tab: keep serving until you quit (Ctrl+C)
        print(f"Lago Agents is open in your browser. Keep this running; Ctrl+C quits.\n{url}", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            _stop(server)
    return server


def _stop(server):
    config_store.lock_bypass()  # your password is asked again next time the app opens
    for term in list(server.terminals.values()):
        term.close()
    STATE_FILE.unlink(missing_ok=True)
    server.shutdown()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    serve(args[0].lstrip("#") if args else "", open_window="--no-window" not in sys.argv)
