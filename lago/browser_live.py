"""Watch an agent use its browser, live.

An agent whose manifest asks for `mcp: playwright` gets a browser through Playwright's MCP
server. Instead of letting that server start a browser nobody can see, `mcp` here starts a
headless Chromium itself, with a debugging port on this computer's loopback only and a fresh
profile in the run folder (no saved logins, deleted afterwards), then runs the MCP server
connected to it. While it runs, `live.json` in the run's browser folder says where it is, and
the app shows what the browser shows: view() takes a screenshot of the page through Chrome's
DevTools protocol, about once a second while you watch.

    browser_live.py mcp <run_dir>      # what the agent's MCP config runs
"""

import base64
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_store import STATE_DIR  # noqa: E402

PLAYWRIGHT_MCP = "@playwright/mcp@0.0.83"
BROWSERS = Path.home() / ".cache" / "ms-playwright"
VIEWPORT = "1280,800"


class BrowserError(Exception):
    pass


def chromium():
    """Playwright's newest Chromium, or None."""
    found = sorted(BROWSERS.glob("chromium-*/chrome-linux*/chrome"),
                   key=lambda p: int(p.parts[-3].split("-")[1]) if p.parts[-3].split("-")[1].isdigit() else 0)
    return str(found[-1]) if found else None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port, path, timeout=2):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def mcp(run_dir):
    """Start Chromium, run Playwright's MCP server on it (talking over our stdin and stdout),
    and clean up when it ends."""
    folder = Path(run_dir) / "browser"
    folder.mkdir(parents=True, exist_ok=True)
    plain = ["npx", "-y", PLAYWRIGHT_MCP, "--image-responses", "omit", "--file-paths", "absolute",
             "--output-dir", str(folder)]
    binary = chromium()
    if not binary:  # no Chromium of our own: the MCP server's own headless browser, unwatchable
        os.execvp("npx", plain + ["--headless", "--isolated", "--browser", "chromium"])
    port = _free_port()
    profile = folder / "profile"
    shutil.rmtree(profile, ignore_errors=True)
    # --no-sandbox as Playwright itself launches it (Ubuntu blocks the sandbox it needs).
    browser = subprocess.Popen([binary, "--headless=new", "--no-sandbox", f"--remote-debugging-port={port}",
                                "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}",
                                f"--window-size={VIEWPORT}", "--no-first-run", "--no-default-browser-check",
                                "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    live = folder / "live.json"
    server = None
    # Claude Code ends its MCP servers with a signal: clean up then too, not only on a normal end.
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))
    try:
        for _ in range(100):
            try:
                _get(port, "/json/version", timeout=0.5)
                break
            except OSError:
                time.sleep(0.1)
        live.write_text(json.dumps({"port": port, "pid": browser.pid, "started_at": time.time()}))
        server = subprocess.Popen(plain + ["--cdp-endpoint", f"http://127.0.0.1:{port}"])
        return server.wait()
    finally:
        if server and server.poll() is None:
            server.terminate()
        live.unlink(missing_ok=True)
        browser.terminate()
        try:
            browser.wait(5)
        except subprocess.TimeoutExpired:
            browser.kill()
        shutil.rmtree(profile, ignore_errors=True)


# ---------------------------------------------------------------- watching it

def _live(agent_id):
    path = STATE_DIR / "runs" / agent_id / "browser" / "live.json"
    try:
        info = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        os.kill(info["pid"], 0)
    except (OSError, KeyError):
        return None
    return info


def _ws_call(url, method, params, timeout=5):
    """One DevTools protocol call over a WebSocket (just enough of the protocol for this)."""
    rest = url.split("://", 1)[1]
    host_port, path = rest.split("/", 1)
    host, port = host_port.rsplit(":", 1)
    if host not in ("127.0.0.1", "localhost"):
        raise BrowserError("Not a local browser")
    with socket.create_connection((host, int(port)), timeout=timeout) as sock:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        reader = sock.makefile("rb")
        if b" 101 " not in reader.readline():
            raise BrowserError("The browser refused the connection")
        while reader.readline() not in (b"\r\n", b""):
            pass
        payload = json.dumps({"id": 1, "method": method, "params": params}).encode()
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
                                  if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        message = b""
        while True:
            first, second = reader.read(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", reader.read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", reader.read(8))[0]
            data = reader.read(length)
            if first & 0x0F in (0x1, 0x0):
                message += data
                if first & 0x80:
                    reply = json.loads(message)
                    if reply.get("id") == 1:
                        if "error" in reply:
                            raise BrowserError(reply["error"].get("message", "DevTools error"))
                        return reply.get("result") or {}
                    message = b""  # an event, not our answer
            elif first & 0x0F == 0x8:
                raise BrowserError("The browser closed the connection")


def view(agent_id):
    """{"live": bool, "url", "title", "pages", "image": a data: URL of what it shows}."""
    info = _live(agent_id)
    if not info:
        return {"live": False}
    try:
        pages = [t for t in _get(info["port"], "/json/list") if t.get("type") == "page"]
    except OSError:
        return {"live": False}
    if not pages:
        return {"live": True, "url": "", "title": "", "pages": 0, "image": ""}
    page = pages[0]  # the one used most recently comes first
    try:
        shot = _ws_call(page["webSocketDebuggerUrl"], "Page.captureScreenshot", {"format": "jpeg", "quality": 60})
        image = "data:image/jpeg;base64," + shot.get("data", "")
    except (OSError, BrowserError, ValueError):
        image = ""
    return {"live": True, "url": page.get("url", ""), "title": page.get("title", ""), "pages": len(pages), "image": image}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "mcp":
        sys.exit(mcp(sys.argv[2]))
    print(__doc__)
