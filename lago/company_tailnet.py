"""The company's own tailnet, separate from your personal one.

This computer stays on your personal tailnet (the phone app, your friends). For the company, a
second Tailscale connection runs as your user (userspace networking, its own state and socket in
the state folder: the system's Tailscale isn't touched), logged in to the company's tailnet
under a different account. `tailscale serve` on that connection publishes browser access
(remote.py's web listener, on a socket in the state folder that only your user can open, so
other people's programs on a shared computer can't reach it or pose as a visitor) at
http://<name>.<company tailnet>, and tells Lago Agents who each visitor is through Tailscale's
identity headers.

    company_tailnet.py status
    company_tailnet.py start        # install and start the user service
    company_tailnet.py login        # print the link that logs it in to the company's tailnet
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_store import STATE_DIR  # noqa: E402

FOLDER = STATE_DIR / "company-tailnet"
SOCKET = FOLDER / "tailscaled.sock"
LOGIN_LOG = FOLDER / "login.log"
BIN = Path("/snap/tailscale/current/bin")  # Tailscale's snap ships both programs; else PATH, sbin
SERVICE = "lago-agents-company-tailnet.service"
UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / SERVICE
WEB_SOCKET = FOLDER / "web.sock"  # remote.py listens here, behind `tailscale serve`
HOSTNAME = f"lago-{socket.gethostname()}"[:40]


class TailnetError(Exception):
    pass


def _program(name):
    path = BIN / name
    if path.exists():
        return str(path)
    import shutil
    found = shutil.which(name) or next((f"{d}/{name}" for d in ("/usr/sbin", "/usr/local/sbin", "/sbin")
                                         if os.access(f"{d}/{name}", os.X_OK)), None)
    if not found:
        raise TailnetError(f"Tailscale's {name} isn't installed")
    return found


def cli(*args, timeout=20):
    """Run `tailscale` against the company connection."""
    proc = subprocess.run([_program("tailscale"), f"--socket={SOCKET}", *args], capture_output=True,
                          text=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def write_unit():
    FOLDER.mkdir(parents=True, exist_ok=True)
    FOLDER.chmod(0o700)
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(f"""[Unit]
Description=Lago Agents: the company's own Tailscale connection (separate from your personal tailnet)
After=network-online.target

[Service]
ExecStart={_program('tailscaled')} --tun=userspace-networking --statedir={FOLDER} --socket={SOCKET} --port=0
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""")


def start():
    write_unit()
    for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", SERVICE]):
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise TailnetError(proc.stderr.strip() or "systemctl failed")
    for _ in range(30):  # wait for its socket
        if SOCKET.exists():
            return
        time.sleep(0.5)
    raise TailnetError("The company connection didn't start (journalctl --user -u lago-agents-company-tailnet)")


def stop():
    subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE], capture_output=True, timeout=30)


def status():
    """{"running", "state", "login", "name", "tailnet", "url", "auth_url"}."""
    out = {"running": SOCKET.exists(), "state": "Stopped", "login": "", "name": "", "tailnet": "",
           "url": "", "auth_url": _pending_auth_url()}
    if not out["running"]:
        return out
    try:
        code, text = cli("status", "--json", "--peers=false")
        data = json.loads(text) if code in (0, 1) and text.startswith("{") else {}
    except (subprocess.TimeoutExpired, ValueError, TailnetError):
        data = {}
    out["state"] = data.get("BackendState") or "Unknown"
    me = data.get("Self") or {}
    users = data.get("User") or {}
    user = users.get(str(me.get("UserID"))) or {}
    out["login"] = (user.get("LoginName") or "").lower()
    out["name"] = (me.get("DNSName") or "").rstrip(".")
    out["tailnet"] = (data.get("CurrentTailnet") or {}).get("Name", "")
    out["auth_url"] = data.get("AuthURL") or out["auth_url"]
    if out["state"] == "Running":
        out["auth_url"] = ""
        if out["name"]:
            out["url"] = f"http://{out['name']}"
    return out


def _pending_auth_url():
    try:
        text = LOGIN_LOG.read_text()
    except OSError:
        return ""
    for word in text.split():
        if word.startswith("https://login.tailscale.com/"):
            return word
    return ""


def login():
    """Start logging the company connection in; returns the link to open (in a browser where you
    sign in with the company's account, not your personal one)."""
    if not SOCKET.exists():
        start()
    LOGIN_LOG.unlink(missing_ok=True)
    with open(LOGIN_LOG, "w") as log:
        subprocess.Popen([_program("tailscale"), f"--socket={SOCKET}", "up", f"--hostname={HOSTNAME}",
                          "--accept-dns=false", "--reset"], stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
    for _ in range(40):
        url = _pending_auth_url() or status()["auth_url"]
        if url:
            return url
        if status()["state"] == "Running":
            return ""
        time.sleep(0.5)
    raise TailnetError("Tailscale didn't give a login link; try again")


def serve_web():
    """Publish browser access on the company tailnet (port 80), proxying to remote.py."""
    code, text = cli("serve", "--bg", "--http=80", f"unix:{WEB_SOCKET}", timeout=30)
    if code != 0:
        raise TailnetError(text or "tailscale serve failed")


def unserve_web():
    cli("serve", "--http=80", "off", timeout=30)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "start":
        start()
    elif command == "login":
        print(login() or "Already logged in.")
    elif command == "serve":
        serve_web()
    print(json.dumps(status(), indent=2))
