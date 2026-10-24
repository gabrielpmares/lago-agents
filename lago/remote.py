#!/usr/bin/env python3
"""Lago Agents on your phone: a private HTTPS server the Android app talks to.

The phone runs no agents. It shows the same pages as the desktop app, served from here,
and every button calls the same Api methods the desktop window calls, over one
authenticated endpoint. Terminals are mirrored: the phone reads the screen of an agent's
tmux session and can send it text and a few keys, so a question or a permission prompt
can be answered from anywhere.

Reaching it from anywhere is the VPN's job (Tailscale): by default the server listens
only on this machine's Tailscale address and answers only Tailscale peers, so it is
never on your LAN or the internet. On top of the VPN's own encryption:

- TLS with a self-signed certificate made on first start. The phone pins its SHA-256
  fingerprint at pairing, so nobody can stand in for this machine later.
- Pairing a phone needs a one-time code shown in the desktop app (valid ten minutes) and
  the phone password. The code also proves, both ways, that the phone talks to this
  machine before the password leaves it (see `pair_start`).
- After pairing the phone holds a device key, and signs in with it plus the password.
  Sessions lapse after SESSION_IDLE_DAYS unused or SESSION_MAX_DAYS in all; then the
  password is asked again. Devices can be removed from the desktop app at any time.
- Wrong passwords or codes lock that address out for a while, doubling each time.
- Secrets are stored hashed (scrypt for the password, SHA-256 for random tokens), in
  files only you can read. Every sign-in, refusal and device change goes to access.log.

    remote.py serve             what the lago-agents-remote user service runs
    remote.py status            settings, address and paired devices
    remote.py set-password      set the phone password (asked twice, not echoed)
    remote.py pair              open pairing for ten minutes and print the code
    remote.py revoke <device>   remove a paired device
    remote.py publish-app <apk> <version code> <version name> [notes]
                                make an APK the phones' next update (build-apk.sh does this)
"""

import base64
import getpass
import hashlib
import hmac
import ipaddress
import json
import logging
import logging.handlers
import mimetypes
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingUnixStreamServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_store import STATE_DIR  # noqa: E402

FOLDER = STATE_DIR / "remote"
SETTINGS_PATH = FOLDER / "settings.json"
DEVICES_PATH = FOLDER / "devices.json"
PAIRING_PATH = FOLDER / "pairing.json"
CERT_PATH = FOLDER / "cert.pem"
KEY_PATH = FOLDER / "key.pem"
LOG_PATH = FOLDER / "access.log"
APP_DIR = FOLDER / "app"          # the published Android app: LagoAgents.apk and app.json
UI_DIR = Path(__file__).resolve().parent / "ui"
SERVICE = "lago-agents-remote.service"
UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / SERVICE

DEFAULT_PORT = 7443
DEFAULT_WEB_PORT = 7480   # browsers on the tailnet (company.py): plain HTTP inside WireGuard
# Tailscale's addresses (CGNAT range and its IPv6 prefix), and this machine itself.
DEFAULT_ALLOW = ["100.64.0.0/10", "fd7a:115c:a1e0::/48", "127.0.0.0/8", "::1/128"]
PAIRING_SECONDS = 600
PAIRING_ATTEMPTS = 5
PAIR_TICKET_SECONDS = 120
SESSION_IDLE_DAYS = 30
SESSION_MAX_DAYS = 90
TOUCH_EVERY_SECONDS = 60
LOCKOUT_AFTER = 5
LOCKOUT_FIRST_SECONDS = 60
LOCKOUT_MAX_SECONDS = 3600
MAX_BODY = 64 * 1024 * 1024       # a message to Lago can carry ten pictures as data URLs
MIN_PASSWORD = 10
FIT_STALE_SECONDS = 45
EVENTS_HOLD_SECONDS = 50  # a held /v1/events request answers at least this often (heartbeat)
WATCH_SECONDS = 0.25      # how often the alert feed looks for changes to hub.db
REBUILD_SECONDS = 2       # and rebuilds anyway, for what changes with time alone
FEED_IDLE_SECONDS = 120   # the feed sleeps once no phone has listened for this long
OPEN_SECONDS = 15         # a phone whose app is open makes a request at least this often
CONNECTED_SECONDS = 70    # and its live connection at least this often (50 s holds)
ROUTE_REFRESH_SECONDS = 5
SPEECH_KEEP_SECONDS = 120 # speech the phone hasn't fetched by then is dropped
SPEECH_NAME = re.compile(r"^\d{10,25}-\d{1,10}\.(m4a|wav|mp3)$")  # a narrowed terminal goes back to the desktop's width after this unwatched
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
PAIR_CLIENT = b"lago-pair-client-v1|"
PAIR_SERVER = b"lago-pair-server-v1|"
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

# Methods the phone may not call: the desktop's own windows and clipboard, its microphone
# (the phone listens itself), and managing phone access (only from the desktop).
BLOCKED = {"clipboard_files", "open_external", "mic_start", "mic_stop", "mic_cancel"}
# Keys the phone can press in a mirrored terminal.
TERMINAL_KEYS = {"Enter", "Escape", "Up", "Down", "Left", "Right", "Tab", "BTab", "Space",
                 "BSpace", "PageUp", "PageDown", "C-c", "C-d", "y", "n",
                 "1", "2", "3", "4", "5", "6", "7", "8", "9"}

log = logging.getLogger("lago.remote")
_lock = threading.RLock()


class RemoteError(Exception):
    """A problem worth showing as-is."""


# ---------------------------------------------------------------- files

def _ensure_folder():
    FOLDER.mkdir(parents=True, exist_ok=True)
    os.chmod(FOLDER, 0o700)


def _read(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write(path, data):
    """Atomically, readable only by you."""
    _ensure_folder()
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def settings():
    data = _read(SETTINGS_PATH, {})
    return {"listen": data.get("listen", "tailscale"), "port": int(data.get("port", DEFAULT_PORT)),
            "allow": data.get("allow") or DEFAULT_ALLOW, "password": data.get("password"),
            "web": bool(data.get("web")), "web_port": int(data.get("web_port", DEFAULT_WEB_PORT)),
            # "company": behind the company's own tailnet (company_tailnet.py); "tailnet": this one
            "web_via": data.get("web_via", "tailnet")}


def _save_settings(**changes):
    with _lock:
        data = _read(SETTINGS_PATH, {})
        data.update(changes)
        _write(SETTINGS_PATH, data)


def _devices():
    return _read(DEVICES_PATH, {"devices": {}, "sessions": {}})


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _setup_log():
    if log.handlers:
        return
    _ensure_folder()
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    os.chmod(LOG_PATH, 0o600)


# ---------------------------------------------------------------- password

def set_password(password):
    if len(password or "") < MIN_PASSWORD:
        raise RemoteError(f"Use at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    _save_settings(password={"scrypt": dict(SCRYPT),
                             "salt": base64.b64encode(salt).decode(),
                             "hash": base64.b64encode(digest).decode(), "set_at": time.time()})
    _setup_log()
    log.info("phone password changed")


def check_password(password):
    stored = settings()["password"]
    if not stored or not password:
        return False
    params = {k: stored["scrypt"][k] for k in ("n", "r", "p", "maxmem", "dklen")}
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(stored["salt"]), **params)
    return hmac.compare_digest(digest, base64.b64decode(stored["hash"]))


# ---------------------------------------------------------------- certificate

def ensure_certificate(addresses=()):
    """The server's certificate and key, made once (EC P-256, ten years)."""
    if CERT_PATH.exists() and KEY_PATH.exists():
        return
    _ensure_folder()
    if not shutil.which("openssl"):
        raise RemoteError("openssl is needed to make the server's certificate")
    names = ["DNS:" + socket.gethostname(), "DNS:localhost", "IP:127.0.0.1"]
    names += [f"IP:{a}" for a in addresses if a]
    proc = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-days", "3650", "-subj", "/CN=Lago Agents",
         "-addext", "subjectAltName=" + ",".join(names),
         "-keyout", str(KEY_PATH), "-out", str(CERT_PATH)],
        capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RemoteError(f"openssl failed: {proc.stderr.strip()}")
    os.chmod(KEY_PATH, 0o600)
    os.chmod(CERT_PATH, 0o600)


def fingerprint():
    """SHA-256 of the certificate (DER), lowercase hex: what the phone pins."""
    try:
        der = ssl.PEM_cert_to_DER_cert(CERT_PATH.read_text())
    except OSError:
        return None
    return hashlib.sha256(der).hexdigest()


def pretty_fingerprint(fp):
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper() if fp else ""


# ---------------------------------------------------------------- address

def tailscale_ip():
    try:
        proc = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = proc.stdout.split()
    return lines[0] if proc.returncode == 0 and lines else None


def tailscale_name():
    """This machine's MagicDNS name, if Tailscale has one."""
    try:
        proc = subprocess.run(["tailscale", "status", "--json", "--peers=false"],
                              capture_output=True, text=True, timeout=5)
        name = json.loads(proc.stdout)["Self"]["DNSName"].rstrip(".")
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None
    return name or None


def listen_address(conf=None):
    conf = conf or settings()
    if conf["listen"] == "tailscale":
        return tailscale_ip()
    return conf["listen"]


def server_url(conf=None):
    conf = conf or settings()
    host = listen_address(conf)
    if not host:
        return None
    return f"https://{host}:{conf['port']}"


# ---------------------------------------------------------------- pairing

def start_pairing():
    """Open pairing for PAIRING_SECONDS: a fresh one-time code (shown on the desktop)."""
    if not settings()["password"]:
        raise RemoteError("Set the phone password first")
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
    with _lock:
        _write(PAIRING_PATH, {"code": raw, "expires": time.time() + PAIRING_SECONDS,
                              "attempts": 0, "tickets": {}})
    _setup_log()
    log.info("pairing opened")
    return {"code": f"{raw[:5]}-{raw[5:]}", "expires": time.time() + PAIRING_SECONDS}


def cancel_pairing():
    PAIRING_PATH.unlink(missing_ok=True)


def pairing_open():
    data = _read(PAIRING_PATH, None)
    return bool(data and data.get("expires", 0) > time.time())


def _normal_code(code):
    return re.sub(r"[^A-Z0-9]", "", str(code).upper())


def pair_start(nonce, proof):
    """Step one. The phone sends a random nonce and
    HMAC(code, PAIR_CLIENT + fingerprint it saw + "|" + nonce). A phone that reached
    someone else saw another fingerprint, so its proof fails here; and only this machine
    can answer with HMAC(code, PAIR_SERVER + same), which the phone checks before it
    sends the password. Returns that proof and a ticket for step two."""
    fp = fingerprint()
    if not (isinstance(nonce, str) and 16 <= len(nonce) <= 128 and isinstance(proof, str)):
        raise RemoteError("Bad request")
    with _lock:
        data = _read(PAIRING_PATH, None)
        if not data or data.get("expires", 0) < time.time():
            raise RemoteError("Pairing isn't open. Open it on your computer: Lago Agents, Phone, Pair a Phone.")
        message = f"{fp}|{nonce}".encode()
        expected = hmac.new(data["code"].encode(), PAIR_CLIENT + message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, proof.lower()):
            data["attempts"] += 1
            if data["attempts"] >= PAIRING_ATTEMPTS:
                PAIRING_PATH.unlink(missing_ok=True)
                raise RemoteError("Wrong code too many times. Open pairing again on your computer.")
            _write(PAIRING_PATH, data)
            raise RemoteError("Wrong pairing code")
        ticket = secrets.token_urlsafe(32)
        data["tickets"][_sha(ticket)] = time.time() + PAIR_TICKET_SECONDS
        _write(PAIRING_PATH, data)
    server_proof = hmac.new(data["code"].encode(), PAIR_SERVER + message, hashlib.sha256).hexdigest()
    return {"server_proof": server_proof, "ticket": ticket}


def pair_finish(ticket, password, device_name, ip):
    """Step two: the ticket from step one and the phone password. Closes pairing and
    returns the new device's key and a session."""
    with _lock:
        data = _read(PAIRING_PATH, None)
        expires = (data or {}).get("tickets", {}).get(_sha(str(ticket)))
        if not expires or expires < time.time():
            raise RemoteError("Pairing expired. Start again.")
        if not check_password(password):
            raise PermissionError("Wrong password")
        PAIRING_PATH.unlink(missing_ok=True)
        device_id = secrets.token_hex(8)
        device_key = secrets.token_urlsafe(32)
        name = re.sub(r"[\x00-\x1f]", "", str(device_name or "Phone"))[:60] or "Phone"
        store = _devices()
        store["devices"][device_id] = {"name": name, "key": _sha(device_key), "paired_at": time.time(),
                                       "last_seen": time.time(), "last_ip": ip}
        _write(DEVICES_PATH, store)
    log.info("paired device %s (%s) from %s", device_id, name, ip)
    token = _new_session(device_id, ip)
    return {"device_id": device_id, "device_key": device_key, "token": token, "name": name}


# ---------------------------------------------------------------- devices & sessions

def _new_session(device_id, ip):
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        store = _devices()
        _prune(store, now)
        store["sessions"][_sha(token)] = {"device": device_id, "created": now, "used": now, "ip": ip}
        _write(DEVICES_PATH, store)
    return token


def _prune(store, now):
    for key, s in list(store["sessions"].items()):
        if (s["device"] not in store["devices"] or now - s["used"] > SESSION_IDLE_DAYS * 86400
                or now - s["created"] > SESSION_MAX_DAYS * 86400):
            del store["sessions"][key]


def login(device_id, device_key, password, ip):
    store = _devices()
    device = store["devices"].get(str(device_id))
    if not device or not hmac.compare_digest(device["key"], _sha(str(device_key))):
        raise LookupError("This phone isn't paired anymore. Pair it again from your computer.")
    if not check_password(password):
        raise PermissionError("Wrong password")
    log.info("sign-in: device %s (%s) from %s", device_id, device["name"], ip)
    return {"token": _new_session(device_id, ip), "name": device["name"]}


def authenticate(token, ip, app_version=None):
    """The device a session token belongs to, or None. Keeps the session alive and notes
    which version of the app the phone runs."""
    if not token:
        return None
    key = _sha(token)
    now = time.time()
    store = _devices()
    session = store["sessions"].get(key)
    if not session:
        return None
    device = store["devices"].get(session["device"])
    if (not device or now - session["used"] > SESSION_IDLE_DAYS * 86400
            or now - session["created"] > SESSION_MAX_DAYS * 86400):
        return None
    version_changed = app_version is not None and device.get("app_version") != app_version
    if now - session["used"] > TOUCH_EVERY_SECONDS or version_changed:
        with _lock:
            store = _devices()
            if key in store["sessions"]:
                store["sessions"][key].update(used=now, ip=ip)
                store["devices"][session["device"]].update(last_seen=now, last_ip=ip)
                if app_version is not None:
                    store["devices"][session["device"]]["app_version"] = app_version
                _write(DEVICES_PATH, store)
    return {"id": session["device"], "name": device["name"]}


def logout(token):
    with _lock:
        store = _devices()
        store["sessions"].pop(_sha(token or ""), None)
        _write(DEVICES_PATH, store)


def devices():
    store = _devices()
    now = time.time()
    _prune(store, now)
    active = {}
    for s in store["sessions"].values():
        active[s["device"]] = max(active.get(s["device"], 0), s["used"])
    return sorted(({"id": did, "name": d["name"], "paired_at": d["paired_at"],
                    "last_seen": active.get(did) or d.get("last_seen"), "last_ip": d.get("last_ip"),
                    "app_version": d.get("app_version"),
                    "signed_in": did in active} for did, d in store["devices"].items()),
                  key=lambda d: -(d["last_seen"] or 0))


def revoke(device_id):
    with _lock:
        store = _devices()
        if store["devices"].pop(device_id, None) is None:
            raise RemoteError("There's no such device")
        _prune(store, time.time())
        _write(DEVICES_PATH, store)
    _setup_log()
    log.info("removed device %s", device_id)


def recent_log(lines=30):
    try:
        return LOG_PATH.read_text().splitlines()[-lines:][::-1]
    except OSError:
        return []


# ---------------------------------------------------------------- app updates

def app_info():
    """The published Android app's version, or None: {code, name, sha256, size, notes, at}."""
    info = _read(APP_DIR / "app.json", None)
    if not info or not (APP_DIR / "LagoAgents.apk").exists():
        return None
    return info


def publish_app(apk, code, name, notes=""):
    """Make an APK the phones' next update. android/build-apk.sh calls this (via
    `remote.py publish-app`); phones hear of it at once through the live alert feed."""
    apk = Path(apk)
    code = int(code)
    current = app_info()
    if current and code <= current["code"]:
        raise RemoteError(f"Version {code} isn't newer than the published {current['code']}")
    data = apk.read_bytes()
    if data[:2] != b"PK":
        raise RemoteError(f"{apk} isn't an APK")
    _ensure_folder()
    APP_DIR.mkdir(exist_ok=True)
    os.chmod(APP_DIR, 0o700)
    tmp = APP_DIR / "LagoAgents.apk.tmp"
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, APP_DIR / "LagoAgents.apk")
    info = {"code": code, "name": str(name), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "notes": str(notes or "")[:2000], "at": time.time()}
    _write(APP_DIR / "app.json", info)
    _setup_log()
    log.info("published app %s (%s)", name, code)
    return info


# ---------------------------------------------------------------- the service

def service_state():
    try:
        active = subprocess.run(["systemctl", "--user", "is-active", SERVICE], capture_output=True,
                                text=True, timeout=5).stdout.strip()
        enabled = subprocess.run(["systemctl", "--user", "is-enabled", SERVICE], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return {"active": False, "enabled": False}
    return {"active": active == "active", "enabled": enabled == "enabled"}


def write_unit():
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(f"""[Unit]
Description=Lago Agents: the phone app's private server (Tailscale only)
After=network-online.target tailscaled.service

[Service]
ExecStart=/usr/bin/python3 "{Path(__file__).resolve()}" serve
Environment="PATH={os.environ.get('PATH', '/usr/bin:/bin')}"
Restart=always
RestartSec=10
Nice=5

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=15)


def set_enabled(on):
    if on:
        if not settings()["password"]:
            raise RemoteError("Set the phone password first")
        write_unit()
        cmd = ["systemctl", "--user", "enable", "--now", SERVICE]
    else:
        cmd = ["systemctl", "--user", "disable", "--now", SERVICE]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RemoteError(proc.stderr.strip() or "systemctl failed")


def status():
    conf = settings()
    return {"password_set": bool(conf["password"]), "url": server_url(conf), "port": conf["port"],
            "listen": conf["listen"], "tailscale_ip": tailscale_ip(), "tailscale_name": tailscale_name(),
            "fingerprint": pretty_fingerprint(fingerprint()), "fingerprint_hex": fingerprint(),
            "service": service_state(), "pairing": pairing_open(), "devices": devices(), "app": app_info(),
            "log": recent_log()}


# ---------------------------------------------------------------- lockouts

class Lockouts:
    """Failed sign-ins per address: after LOCKOUT_AFTER in a row, refuse that address for
    a while, doubling up to LOCKOUT_MAX_SECONDS."""

    def __init__(self):
        self.failures = {}
        self.lock = threading.Lock()

    def wait(self, ip):
        with self.lock:
            entry = self.failures.get(ip)
            return max(0, int(entry["until"] - time.time())) if entry else 0

    def failed(self, ip):
        with self.lock:
            entry = self.failures.setdefault(ip, {"count": 0, "until": 0})
            entry["count"] += 1
            if entry["count"] >= LOCKOUT_AFTER:
                seconds = min(LOCKOUT_MAX_SECONDS, LOCKOUT_FIRST_SECONDS * 2 ** (entry["count"] - LOCKOUT_AFTER))
                entry["until"] = time.time() + seconds

    def succeeded(self, ip):
        with self.lock:
            self.failures.pop(ip, None)


# ---------------------------------------------------------------- the phone's Api

def _remote_api_class():
    """The desktop's Api, with what only makes sense on the desktop replaced."""
    import api as desktop
    import config_store
    import runner

    class HeadlessWindow:
        """Stands in for the GTK window: the phone mirrors terminals itself."""

        def show_terminal(self, _agent_id):
            pass

        def hide_terminal(self):
            pass

    class RemoteApi(desktop.Api):
        def __init__(self):
            super().__init__(HeadlessWindow())

        def init(self):
            data = super().init()
            data["theme"] = None  # follow the phone's own light or dark setting
            data["remote"] = True
            return data

        def welcome(self, password="", restricted=False, mode=None):
            """Wake Lago if he's asleep (with your Linux password if he skips
            permissions, and the mode you picked), without greeting aloud at the desk."""
            if mode:
                self.set_lago_mode(mode, tell=False)
            config = config_store.load()
            oid = config_store.ORCHESTRATOR_ID
            if not runner.is_running(oid) and desktop._skip_permissions(config) and not restricted:
                if not password and not config_store.bypass_unlocked():
                    return {"needs_password": True}
                return self.start_orchestrator(password=password, unrestricted=True)
            if not runner.is_running(oid):
                return self.start_orchestrator(unrestricted=False)
            return {"ok": True}

        def show_terminal(self, agent_id):
            return {"shown": runner.is_running(agent_id), "mirrored": True}

        def open_attachment(self, path):
            raise desktop.ApiError("Open it on your phone")

        def open_url(self, url):
            raise desktop.ApiError("Open it on your phone")

        def open_file(self, path, sessions=None):
            raise desktop.ApiError("Open it on your phone")  # the page shows it with read_file

        # --- mirrored terminals

        def terminal_snapshot(self, agent_id, lines=300):
            """The session's screen and recent scrollback, with colours (ANSI)."""
            _session_id(agent_id)
            if not runner.is_running(agent_id):
                return {"running": False, "text": "", "width": 0, "height": 0}
            lines = max(0, min(int(lines), 2000))
            target = f"={runner.session_name(agent_id)}:"
            ok, text = runner.tmux("capture-pane", "-p", "-e", "-t", target, "-S", f"-{lines}")
            import redact
            text = redact.text(text) if ok else text
            ok2, size = runner.tmux("display-message", "-p", "-t", target,
                                    "#{pane_width} #{pane_height} #{cursor_x} #{cursor_y}")
            width, height, cx, cy = (int(x) for x in size.split()) if ok2 else (0, 0, 0, 0)
            with _fit_lock:
                if agent_id in _fitted:
                    _fitted[agent_id] = time.time()
            return {"running": ok, "text": text if ok else "", "width": width, "height": height,
                    "cursor": [cx, cy]}

        def terminal_fit(self, agent_id, cols, rows=50):
            """Narrow the session's window to the phone's width while you look at it, so
            Claude Code redraws for a phone instead of a wide desktop pane. Undone by
            terminal_unfit, or by the watchdog once the phone stops looking."""
            _session_id(agent_id)
            if not runner.is_running(agent_id):
                raise desktop.ApiError("This session isn't running")
            cols, rows = max(30, min(int(cols), 400)), max(10, min(int(rows), 200))
            ok, out = runner.tmux("resize-window", "-t", f"={runner.session_name(agent_id)}:",
                                  "-x", str(cols), "-y", str(rows))
            if not ok:
                raise desktop.ApiError(out or "Couldn't resize the session")
            with _fit_lock:
                _fitted[agent_id] = time.time()
            return {"ok": True}

        def terminal_unfit(self, agent_id):
            _session_id(agent_id)
            _unfit(agent_id)
            return {"ok": True}

        def terminal_send(self, agent_id, text, enter=True):
            """Type text into the session (pasted, so it arrives whole), then Enter."""
            _session_id(agent_id)
            if not runner.is_running(agent_id):
                raise desktop.ApiError("This session isn't running")
            text = str(text)[:20000]
            target = f"={runner.session_name(agent_id)}:"
            buffer = f"lago-phone-{agent_id}"
            if text:
                ok, out = runner.tmux("load-buffer", "-b", buffer, "-", input_text=text)
                if ok:
                    ok, out = runner.tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", target)
                if not ok:
                    raise desktop.ApiError(out or "Couldn't type into the session")
                if enter:
                    time.sleep(0.4)
            if enter:
                runner.tmux("send-keys", "-t", target, "Enter")
            return {"ok": True}

        def terminal_key(self, agent_id, key):
            _session_id(agent_id)
            if key not in TERMINAL_KEYS:
                raise desktop.ApiError(f"Unknown key {key!r}")
            if not runner.is_running(agent_id):
                raise desktop.ApiError("This session isn't running")
            ok, out = runner.keys(agent_id, key)
            if not ok:
                raise desktop.ApiError(out)
            return {"ok": True}

        # --- Lago's voice on the phone

        def speech_done(self, id):
            """The phone played (or skipped) a piece of speech: forget it."""
            import voice
            if isinstance(id, str) and SPEECH_NAME.match(id):
                (voice.PHONE_QUEUE / id).unlink(missing_ok=True)
            return {"ok": True}

        # --- the phone's notifications

        def alerts(self):
            """What needs you (the bell's list) and a few counts, for the phone's
            background check."""
            view = self.overview()
            return {"notifications": view["notifications"], "pipelines_waiting": view["pipelines_waiting"],
                    "reports_unseen": view["reports_unseen"], "at": time.time()}

    def _session_id(agent_id):
        if not isinstance(agent_id, str) or not SESSION_ID.match(agent_id):
            raise desktop.ApiError("Invalid session")

    _fitted = {}  # session id -> when the phone last looked at it
    _fit_lock = threading.Lock()

    def _unfit(agent_id):
        """Give the window back to the desktop: its size follows the latest client again."""
        with _fit_lock:
            _fitted.pop(agent_id, None)
        target = f"={runner.session_name(agent_id)}:"
        runner.tmux("resize-window", "-A", "-t", target)  # (this sets "manual" again, so unset after)
        runner.tmux("set-window-option", "-t", target, "-u", "window-size")

    def _watch_fitted():
        while True:
            time.sleep(15)
            with _fit_lock:
                stale = [a for a, seen in _fitted.items() if time.time() - seen > FIT_STALE_SECONDS]
            for agent_id in stale:
                _unfit(agent_id)

    threading.Thread(target=_watch_fitted, name="terminal-fit", daemon=True).start()

    allowed = {name for name in dir(RemoteApi) if not name.startswith("_")
               and callable(getattr(RemoteApi, name)) and name not in BLOCKED
               and not name.startswith("remote_")}
    return RemoteApi, desktop.ApiError, allowed


def _rewrite_files(value):
    """Turn file:// links to Lago's attachments into URLs the phone can load."""
    import attachments
    prefix = attachments.FOLDER.as_uri() + "/"
    if isinstance(value, str):
        return "/v1/attachments/" + value[len(prefix):] if value.startswith(prefix) else value
    if isinstance(value, list):
        return [_rewrite_files(v) for v in value]
    if isinstance(value, dict):
        return {k: _rewrite_files(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------- Lago's voice on the phone

class VoiceRoute:
    """Where Lago talks: on your phone while you use it, else on this computer.

    Each request from the phone says how it wants Lago's voice (X-Lago-Voice: "open" while
    the app is on screen, "always" even in the background, "off" for the computer) and
    whether the app is on screen (X-Lago-Foreground). From that this keeps
    voice.ROUTE_FILE current, which voice.say() reads in whatever process speaks."""

    def __init__(self):
        self.devices = {}
        self.lock = threading.Lock()
        self.phone = None
        threading.Thread(target=self._loop, name="voice-route", daemon=True).start()

    def note(self, device_id, mode, foreground):
        if mode not in ("open", "always", "off"):
            return
        with self.lock:
            self.devices[device_id] = {"mode": mode, "foreground": foreground, "at": time.time()}
        self._write()

    def _wanted(self):
        now = time.time()
        with self.lock:
            return any((d["mode"] == "always" and now - d["at"] < CONNECTED_SECONDS)
                       or (d["mode"] == "open" and d["foreground"] and now - d["at"] < OPEN_SECONDS)
                       for d in self.devices.values())

    def _write(self, force=False):
        import voice
        phone = self._wanted()
        if phone == self.phone and not (force and phone):
            return
        if phone != self.phone:
            log.info("Lago's voice now on the %s", "phone" if phone else "computer")
        self.phone = phone
        voice.ROUTE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = voice.ROUTE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"phone": phone, "until": time.time() + 3 * ROUTE_REFRESH_SECONDS}))
        os.replace(tmp, voice.ROUTE_FILE)

    def _loop(self):
        while True:
            self._write(force=True)  # keeps "until" ahead while the phone has it
            time.sleep(ROUTE_REFRESH_SECONDS)


def pending_speech():
    """Speech waiting for the phone, oldest first: [{"id"}], dropping what's too old."""
    import voice
    found = []
    if voice.PHONE_QUEUE.is_dir():
        for path in sorted(voice.PHONE_QUEUE.iterdir()):
            if not SPEECH_NAME.match(path.name):
                continue
            if time.time() - path.stat().st_mtime > SPEECH_KEEP_SECONDS:
                path.unlink(missing_ok=True)
                continue
            found.append({"id": path.name})
    return found


def hush_stamp():
    import voice
    try:
        return int(voice.HUSH_STAMP.stat().st_mtime)
    except OSError:
        return 0


# ---------------------------------------------------------------- live alerts

class AlertFeed:
    """What needs you, pushed to phones the moment it changes.

    A phone asks GET /v1/events?since=<version> and the request is held open until the
    list differs from that version (or EVENTS_HOLD_SECONDS pass, as a heartbeat). The list
    is rebuilt when hub.db changes (agents' hooks write their status there; checked every
    WATCH_SECONDS) and at least every REBUILD_SECONDS for what changes with time alone.
    Nothing runs while no phone is listening."""

    def __init__(self, api):
        self.api = api
        self.version = 0
        self.items = []
        self.signature = None
        self.cond = threading.Condition()
        self.listened = 0.0
        self.thread = None
        self.app = app_info()
        self.speech = []
        self.hush = hush_stamp()

    def wait(self, since, hold):
        with self.cond:
            self.listened = time.time()
            if not self.thread:
                self.thread = threading.Thread(target=self._loop, name="alert-feed", daemon=True)
                self.thread.start()
            self.cond.wait_for(lambda: self.version and self.version != since, timeout=hold)
            return {"version": self.version, "notifications": self.items, "app": self.app,
                    "speech": self.speech, "hush": self.hush}

    def _loop(self):
        import hub
        import voice
        files = [hub.DB_PATH, hub.DB_PATH.with_name(hub.DB_PATH.name + "-wal"), APP_DIR / "app.json",
                 voice.PHONE_QUEUE, voice.HUSH_STAMP]
        stamp, built = None, 0.0
        while True:
            if time.time() - self.listened > FEED_IDLE_SECONDS:
                time.sleep(1)
                stamp = None
                continue
            now_stamp = tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in files)
            if now_stamp != stamp or time.time() - built > REBUILD_SECONDS:
                stamp, built = now_stamp, time.time()
                try:
                    items = self.api.alerts()["notifications"]
                except Exception as error:  # keep the feed alive; the phone keeps its last list
                    log.info("alert feed: %r", error)
                    items = None
                if items is not None:
                    app, speech, hush = app_info(), pending_speech(), hush_stamp()
                    signature = json.dumps([[(i["key"], i["title"], i.get("text")) for i in items],
                                            app and app["code"], [x["id"] for x in speech], hush])
                    if signature != self.signature:
                        with self.cond:
                            self.signature, self.items, self.app = signature, items, app
                            self.speech, self.hush = speech, hush
                            self.version += 1
                            self.cond.notify_all()
            time.sleep(WATCH_SECONDS)


# ---------------------------------------------------------------- HTTP

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; font-src 'self'; connect-src 'none'; media-src 'self' blob:; "
       "object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'")


def _phone_index():
    """index.html for the phone: its stylesheet, the native bridge before app.js."""
    html = (UI_DIR / "index.html").read_text()
    html = html.replace('<html lang="en">', '<html lang="en" class="remote">', 1)
    html = html.replace('<meta name="viewport" content="width=device-width, initial-scale=1">',
                        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                        f'\n  <meta http-equiv="Content-Security-Policy" content="{CSP}">', 1)
    html = html.replace('<link rel="stylesheet" href="styles.css">',
                        '<link rel="stylesheet" href="styles.css">\n  <link rel="stylesheet" href="mobile.css">', 1)
    html = html.replace('<script src="app.js"></script>',
                        '<script src="/native/bridge.js"></script>\n  <script src="app.js"></script>', 1)
    # If index.html changed shape, still give the phone its sheet and bridge.
    if "mobile.css" not in html:
        html = html.replace("</head>", '  <link rel="stylesheet" href="mobile.css">\n</head>', 1)
    if "/native/bridge.js" not in html:
        html = html.replace("<script", '<script src="/native/bridge.js"></script>\n  <script', 1)
    return html.encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LagoAgents"
    sys_version = ""
    timeout = 60

    # --- plumbing

    def log_message(self, fmt, *args):  # no per-request noise; access.log has what matters
        pass

    @property
    def ip(self):
        return self.client_address[0]

    def send_bytes(self, status, body, content_type="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, data):
        self.send_bytes(status, json.dumps(data).encode())

    def error(self, status, message, **more):
        self.send_json(status, {"ok": False, "error": message, **more})

    def body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            raise ValueError("Request too large")
        raw = self.rfile.read(length) if length else b""
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    def token(self):
        auth = self.headers.get("Authorization") or ""
        return auth[7:].strip() if auth.startswith("Bearer ") else None

    def device(self):
        version = self.headers.get("X-Lago-App")
        device = authenticate(self.token(), self.ip, int(version) if version and version.isdigit() else None)
        if device and self.headers.get("X-Lago-Voice"):
            self.server.voice.note(device["id"], self.headers["X-Lago-Voice"], self.headers.get("X-Lago-Foreground") == "1")
        return device

    # --- routes

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/v1/hello":
            return self.send_json(200, {"app": "lago-agents", "version": 1,
                                        "host": socket.gethostname(), "pairing": pairing_open()})
        device = self.device()
        if not device:
            return self.error(401, "Sign in again")
        if path.startswith("/v1/ui/"):
            return self.static(path[len("/v1/ui/"):])
        if path.startswith("/v1/attachments/"):
            return self.attachment(path[len("/v1/attachments/"):])
        if path == "/v1/events":
            query = dict(p.split("=", 1) for p in urlsplit(self.path).query.split("&") if "=" in p)
            try:
                since = int(query.get("since", "0"))
            except ValueError:
                since = 0
            return self.send_json(200, {"ok": True, **self.server.feed.wait(since, EVENTS_HOLD_SECONDS)})
        if path == "/v1/app/latest":
            return self.send_json(200, {"ok": True, "app": app_info()})
        if path == "/v1/app/apk":
            apk = APP_DIR / "LagoAgents.apk"
            if not app_info():
                return self.error(404, "No app published yet")
            log.info("app download by device %s from %s", device["id"], self.ip)
            return self.send_bytes(200, apk.read_bytes(), "application/vnd.android.package-archive")
        if path.startswith("/v1/speech/"):
            import voice
            name = path[len("/v1/speech/"):]
            target = voice.PHONE_QUEUE / name
            if not SPEECH_NAME.match(name) or not target.is_file():
                return self.error(404, "Gone")
            kind = {"m4a": "audio/mp4", "wav": "audio/wav", "mp3": "audio/mpeg"}[name.rsplit(".", 1)[1]]
            return self.send_bytes(200, target.read_bytes(), kind)
        if path == "/v1/me":
            return self.send_json(200, {"ok": True, "device": device})
        return self.error(404, "Not found")

    def do_POST(self):
        path = urlsplit(self.path).path
        wait = self.server.lockouts.wait(self.ip)
        try:
            data = self.body()
        except (ValueError, json.JSONDecodeError) as error:
            return self.error(400, str(error))
        if path.startswith("/v1/hooks/"):
            return self.hook(path.rsplit("/", 1)[-1], data, wait)
        if path in ("/v1/pair/start", "/v1/pair/finish", "/v1/login"):
            if wait:
                return self.error(429, f"Too many wrong attempts. Try again in {wait} seconds.", retry_after=wait)
            return self.credentials(path, data)
        device = self.device()
        if not device:
            return self.error(401, "Sign in again")
        if path == "/v1/call":
            return self.call(device, data)
        if path == "/v1/logout":
            logout(self.token())
            log.info("signed out: device %s from %s", device["id"], self.ip)
            return self.send_json(200, {"ok": True})
        return self.error(404, "Not found")

    def hook(self, secret, data, wait):
        """A routine's webhook: its secret in the path, any JSON body as the trigger payload.
        Wrong secrets count toward the same lockout as wrong passwords."""
        import config_store
        import features
        import routines
        if not features.on(config_store.load(), "routines"):
            return self.error(404, "Not found")
        if wait:
            return self.error(429, f"Too many wrong attempts. Try again in {wait} seconds.", retry_after=wait)
        routine = routines.by_hook(secret)
        if routine is None:
            self.server.lockouts.failed(self.ip)
            log.info("refused a webhook from %s: unknown secret", self.ip)
            return self.error(404, "Not found")
        try:
            run = routines.fire(routine["id"], "webhook", json.dumps(data)[:4000] if data else "")
        except routines.RoutineError as error:
            return self.error(409, str(error))
        log.info("webhook fired routine #%d from %s: %s", routine["id"], self.ip, run["status"])
        return self.send_json(202, {"ok": True, "run": run["id"], "status": run["status"]})

    def credentials(self, path, data):
        lockouts = self.server.lockouts
        try:
            if path == "/v1/pair/start":
                result = pair_start(data.get("nonce"), data.get("proof"))
            elif path == "/v1/pair/finish":
                result = pair_finish(data.get("ticket"), data.get("password"), data.get("device_name"), self.ip)
            else:
                result = login(data.get("device_id"), data.get("device_key"), data.get("password"), self.ip)
        except PermissionError as error:
            lockouts.failed(self.ip)
            log.info("refused %s from %s: %s", path, self.ip, error)
            return self.error(403, str(error))
        except LookupError as error:
            lockouts.failed(self.ip)
            log.info("refused %s from %s: unknown device", path, self.ip)
            return self.error(410, str(error))
        except RemoteError as error:
            if "Wrong" in str(error):
                lockouts.failed(self.ip)
            log.info("refused %s from %s: %s", path, self.ip, error)
            return self.error(400, str(error))
        if path != "/v1/pair/start":
            lockouts.succeeded(self.ip)
        return self.send_json(200, {"ok": True, **result})

    def call(self, device, data):
        method, params = data.get("method"), data.get("params") or {}
        if not isinstance(method, str) or method not in self.server.allowed or not isinstance(params, dict):
            return self.send_json(200, {"ok": False, "error": f"Unknown method {method}"})
        handler = getattr(self.server.api, method)
        try:
            result = handler(**params)
        except self.server.api_error as error:
            return self.send_json(200, {"ok": False, "error": str(error)})
        except TypeError as error:
            return self.send_json(200, {"ok": False, "error": f"Bad call: {error}"})
        except Exception as error:  # report backend bugs instead of hanging the phone
            print(f"lago-remote: {method} failed: {error!r}", file=sys.stderr)
            return self.send_json(200, {"ok": False, "error": f"Something went wrong: {error}"})
        if method in ("start_orchestrator", "restart_lago", "welcome", "say_to_orchestrator", "send_message", "press",
                      "terminal_send", "terminal_key", "start_agent", "stop_agent", "server_deploy"):
            log.info("%s from device %s", method, device["id"])
        return self.send_json(200, {"ok": True, "data": _rewrite_files(result)})

    def static(self, relative):
        if relative in ("", "index.html"):
            return self.send_bytes(200, _phone_index(), "text/html; charset=utf-8",
                                   {"Content-Security-Policy": CSP})
        target = (UI_DIR / relative).resolve()
        if UI_DIR not in target.parents or not target.is_file():
            return self.error(404, "Not found")
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/javascript", "image/svg+xml"):
            kind += "; charset=utf-8"
        return self.send_bytes(200, target.read_bytes(), kind)

    def attachment(self, name):
        import attachments
        if "/" in name or not attachments.NAME.match(name):
            return self.error(404, "Not found")
        path = attachments.FOLDER / name
        if not path.is_file():
            return self.error(404, "No longer kept")
        kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return self.send_bytes(200, path.read_bytes(), kind,
                               {"Content-Disposition": f'inline; filename="{attachments.display_name(name)}"'})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, context, allow, api, api_error, allowed):
        family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.address_family = family
        super().__init__(address, Handler)
        self.context = context
        self.allow = [ipaddress.ip_network(n, strict=False) for n in allow]
        self.lockouts = Lockouts()
        self.api, self.api_error, self.allowed = api, api_error, allowed
        self.feed = AlertFeed(api)
        self.voice = VoiceRoute()

    def verify_request(self, request, client_address):
        try:
            ip = ipaddress.ip_address(client_address[0].split("%")[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.allow)

    def get_request(self):
        sock, address = self.socket.accept()
        sock.settimeout(Handler.timeout)
        # The handshake runs in the request's own thread (on first read), so a slow or
        # silent client can't stall the others.
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (ssl.SSLError, ConnectionError, TimeoutError, socket.timeout)):
            return  # a dropped phone connection or a probe that didn't speak TLS
        super().handle_error(request, client_address)


def serve():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    _setup_log()
    conf = settings()
    if not conf["password"] and not conf["web"]:
        raise SystemExit("lago-remote: set the phone password first (remote.py set-password)")
    if conf["web"] and conf["web_via"] == "company" and not conf["password"]:
        start_web(None, conf).serve_forever()  # only browsers, only behind the company tailnet
        return
    host = listen_address(conf)
    if not host:
        raise SystemExit("lago-remote: Tailscale has no address yet (is it up?). Retrying soon.")
    if conf["web"]:
        web = start_web(host, conf)
        if not conf["password"]:
            web.serve_forever()  # browsers only: no phone password set
            return
    ensure_certificate([host, tailscale_ip()])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(CERT_PATH, KEY_PATH)
    remote_api, api_error, allowed = _remote_api_class()
    server = Server((host, conf["port"]), context, conf["allow"], remote_api(), api_error, allowed)
    log.info("listening on https://%s:%s", host, conf["port"])
    print(f"lago-remote: listening on https://{host}:{conf['port']} "
          f"(certificate {pretty_fingerprint(fingerprint())})", flush=True)
    server.serve_forever()


# ---------------------------------------------------------------- browsers on the tailnet

# The company's people reach Lago Agents from a browser on the tailnet. There are no passwords:
# `company.whois` asks Tailscale whose device a connection comes from, and `company.authorize`
# checks every call against that person's role on the project it touches.

# Like the phone's, except that the page itself calls this server (the phone goes through its app).
WEB_CSP = CSP.replace("connect-src 'none'", "connect-src 'self'")


def _web_index():
    html = (UI_DIR / "index.html").read_text()
    html = html.replace('<html lang="en">', '<html lang="en" class="web">', 1)
    html = html.replace('<script src="app.js"></script>', '<script src="web-bridge.js"></script>\n  <script src="app.js"></script>', 1)
    if "web-bridge.js" not in html:
        html = html.replace("<script", '<script src="web-bridge.js"></script>\n  <script', 1)
    return html.encode()


def _join_page(who, member):
    """The page someone not yet in the company sees."""
    import html as html_lib
    name = html_lib.escape(who["name"]) if who else "there"
    if not who:
        body = ("<h1>Lago Agents</h1><p>This computer couldn't tell who you are. Connect through the "
                "company's Tailscale network and reload.</p>")
    elif member and member["status"] == "pending":
        body = (f"<h1>Hi, {name}</h1><p>You asked to join. An admin of the company will let you in; "
                "reload this page once they have.</p>")
    elif member and member["status"] == "removed":
        body = f"<h1>Hi, {name}</h1><p>You no longer have access. Ask an admin if that's a mistake.</p>"
    else:
        body = (f"<h1>Hi, {name}</h1><p>You're signed in to the network as {html_lib.escape(who['login'])}, but "
                "you aren't in this company yet.</p><form method='post' action='/v1/request'>"
                "<button type='submit'>Ask to Join</button></form>")
    page = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width, initial-scale=1'><title>Lago Agents</title><style>"
            "body{font-family:system-ui,sans-serif;background:#0f1012;color:#e8e9eb;display:grid;place-items:center;"
            "min-height:100vh;margin:0;padding:16px}main{max-width:440px}p{color:#a6aab1;line-height:1.5}"
            "button{background:#4fb89e;color:#0a1d18;border:0;border-radius:8px;padding:10px 18px;font:inherit;"
            "font-weight:600;cursor:pointer}</style></head><body><main>" + body + "</main></body></html>")
    return page.encode()


class WebHandler(Handler):
    timeout = 60

    @property
    def ip(self):
        return "company tailnet" if self.server.via == "company" else self.client_address[0]

    def who(self):
        """The person on the other end: Tailscale's answer, or the owner on this computer."""
        import company
        import config_store
        if self.server.via == "company":
            # Only `tailscale serve` (and your own programs) can open the socket; it says who's visiting.
            login = (self.headers.get("Tailscale-User-Login") or "").strip().lower()
            if login:
                return {"login": login, "name": self.headers.get("Tailscale-User-Name") or login,
                        "picture": self.headers.get("Tailscale-User-Profile-Pic") or "", "device": "company tailnet"}
        elif self.ip.split("%")[0] not in ("127.0.0.1", "::1"):
            return company.whois(self.ip)
        login = company.owner() or "owner"
        return {"login": login, "name": config_store.load().get("user_name") or login, "picture": "", "device": "this computer"}

    def host_ok(self):
        """Refuse other Host names, so a web page elsewhere can't reach this server by DNS tricks."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in self.server.names

    def allowed_person(self, who):
        import company
        if not who or company.is_blocked(who["login"]):
            return None
        if company.is_owner(who["login"]):
            return {"login": who["login"], "status": "active"}
        member = company.member(who["login"])
        return member if member and member["status"] == "active" else None

    def do_GET(self):
        if not self.host_ok():
            return self.error(421, "Wrong host")
        import company
        path = unquote(urlsplit(self.path).path)
        who = self.who()
        if who and company.is_blocked(who["login"]):
            return self.error(403, "Not available")
        if path in ("/", "/index.html"):
            if not self.allowed_person(who):
                member = company.member(who["login"]) if who else None
                return self.send_bytes(200, _join_page(who, member), "text/html; charset=utf-8")
            return self.send_bytes(200, _web_index(), "text/html; charset=utf-8", {"Content-Security-Policy": WEB_CSP})
        if path.startswith("/v1/attachments/"):
            if not self.allowed_person(who):
                return self.error(403, "Not in this company")
            return self.attachment(path[len("/v1/attachments/"):])
        if path.startswith("/v1/"):
            return self.error(404, "Not found")
        return self.static(path.lstrip("/"))

    def do_POST(self):
        import activity
        import company
        if not self.host_ok():
            return self.error(421, "Wrong host")
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).hostname not in self.server.names:
            return self.error(403, "Wrong origin")
        path = urlsplit(self.path).path
        who = self.who()
        if who and company.is_blocked(who["login"]):
            return self.error(403, "Not available")
        if path == "/v1/request":
            if who:
                company.request_access(who)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path != "/v1/call" or self.headers.get("X-Lago-Web") != "1":
            return self.error(404, "Not found")
        if not self.allowed_person(who):
            return self.error(403, "Not in this company")
        try:
            data = self.body()
        except (ValueError, json.JSONDecodeError) as error:
            return self.error(400, str(error))
        method, params = data.get("method"), data.get("params") or {}
        if method == "me":
            return self.send_json(200, {"ok": True, "data": {**who, "owner": company.is_owner(who["login"]),
                                                             "admin": company.company_admin(who["login"]),
                                                             "projects": company.projects_for(who["login"])}})
        if not isinstance(method, str) or method not in self.server.allowed or not isinstance(params, dict):
            return self.send_json(200, {"ok": False, "error": f"Unknown method {method}"})
        try:
            company.authorize(who["login"], method, params)
        except company.AccessError as error:
            return self.send_json(200, {"ok": False, "error": str(error)})
        activity.set_person({"login": who["login"], "name": who["name"]})
        try:
            result = getattr(self.server.api, method)(**params)
            if method == "init":
                result["web"] = True
                result["me"] = {**who, "owner": company.is_owner(who["login"]),
                                "admin": company.company_admin(who["login"]), "projects": company.projects_for(who["login"])}
            result = company.filter_result(who["login"], method, result)
        except self.server.api_error as error:
            return self.send_json(200, {"ok": False, "error": str(error)})
        except company.AccessError as error:
            return self.send_json(200, {"ok": False, "error": str(error)})
        except TypeError as error:
            return self.send_json(200, {"ok": False, "error": f"Bad call: {error}"})
        except Exception as error:  # report backend bugs instead of hanging the page
            print(f"lago-web: {method} failed: {error!r}", file=sys.stderr)
            return self.send_json(200, {"ok": False, "error": f"Something went wrong: {error}"})
        finally:
            activity.set_person(None)
        if company.needed(method) != "watch":
            log.info("web %s by %s", method, who["login"])
        return self.send_json(200, {"ok": True, "data": _rewrite_files(result)})


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, allow, api, api_error, allowed, names, via="tailnet"):
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, WebHandler)
        self.allow = [ipaddress.ip_network(n, strict=False) for n in allow]
        self.api, self.api_error, self.allowed = api, api_error, allowed
        self.names = names
        self.via = via

    verify_request = Server.verify_request


class CompanyWebServer(ThreadingUnixStreamServer):
    """The web listener behind the company tailnet: a socket only your user can open."""
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, path, api, api_error, allowed, names):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.unlink(missing_ok=True)
        old = os.umask(0o177)
        try:
            super().__init__(str(path), WebHandler)
        finally:
            os.umask(old)
        self.api, self.api_error, self.allowed = api, api_error, allowed
        self.names = names
        self.via = "company"


def web_names(host):
    names = {host.lower(), "localhost", "127.0.0.1", "::1"}
    ts_ip, ts_name = tailscale_ip(), tailscale_name()
    if ts_ip:
        names.add(ts_ip)
    if ts_name:
        names.add(ts_name.rstrip(".").lower())
        names.add(ts_name.split(".")[0].lower())
    return names


def start_web(host, conf):
    """Serve browsers, in a thread: on this tailnet's address, or (web_via "company") on a private
    socket behind the company tailnet's `tailscale serve`. Returns the server."""
    import company
    remote_api, api_error, allowed = _remote_api_class()
    if conf["web_via"] == "company":
        import company_tailnet
        info = company_tailnet.status()
        names = {"127.0.0.1", "localhost", "::1"}
        if info["name"]:
            names |= {info["name"].lower(), info["name"].split(".")[0].lower()}
        server = CompanyWebServer(company_tailnet.WEB_SOCKET, remote_api(), api_error, allowed, names)
        threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
        if info["state"] == "Running":
            try:  # older versions published a loopback port; point Tailscale at the socket
                company_tailnet.serve_web()
            except company_tailnet.TailnetError as error:
                log.warning("couldn't point the company tailnet at the socket: %s", error)
        log.info("browsers: behind the company tailnet at %s", info["url"] or "(not logged in yet)")
        print(f"lago-remote: browsers on {info['url'] or 'the company tailnet (log it in first)'}", flush=True)
        return server
    if not company.owner():
        me = company.whois(tailscale_ip() or host)
        if me:
            company.setup(company.company()["name"], me["login"])
    server = WebServer((host, conf["web_port"]), conf["allow"], remote_api(), api_error, allowed, web_names(host))
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("browsers: listening on http://%s:%s", host, conf["web_port"])
    print(f"lago-remote: browsers on http://{tailscale_name() or host}:{conf['web_port']}", flush=True)
    return server


def set_web_via(via):
    if via not in ("company", "tailnet"):
        raise RemoteError("Serve browsers on the company tailnet or this one")
    _save_settings(web_via=via)


def set_web(on):
    """Switch browser access on or off, restarting the service that serves it (it also serves
    the phone, which keeps working if its password is set)."""
    _save_settings(web=bool(on))
    if on or settings()["password"]:
        write_unit()
        cmds = [["systemctl", "--user", "enable", SERVICE], ["systemctl", "--user", "restart", SERVICE]]
    else:
        cmds = [["systemctl", "--user", "disable", "--now", SERVICE]]
    for cmd in cmds:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RemoteError(proc.stderr.strip() or "systemctl failed")


def web_url(conf=None):
    conf = conf or settings()
    if conf["web_via"] == "company":
        import company_tailnet
        return company_tailnet.status()["url"] or None
    name = tailscale_name() or tailscale_ip()
    return f"http://{name}:{conf['web_port']}" if name else None

# ---------------------------------------------------------------- command line

def main(argv):
    command = argv[1] if len(argv) > 1 else "status"
    try:
        if command == "serve":
            serve()
        elif command == "set-password":
            first = getpass.getpass("New phone password: ")
            if getpass.getpass("Again: ") != first:
                raise RemoteError("The two don't match")
            set_password(first)
            print("Phone password set.")
        elif command == "pair":
            ensure_certificate([listen_address(), tailscale_ip()])
            info = start_pairing()
            print(f"Pairing code: {info['code']} (valid {PAIRING_SECONDS // 60} minutes)")
            print(f"Address:      {server_url() or '(Tailscale is not up)'}")
            print(f"Certificate:  {pretty_fingerprint(fingerprint())}")
        elif command == "publish-app" and len(argv) > 4:
            info = publish_app(argv[2], argv[3], argv[4], argv[5] if len(argv) > 5 else "")
            print(f"Published {info['name']} (version {info['code']}, {info['size'] // 1024} KB). "
                  "Paired phones are told now and update themselves.")
        elif command == "revoke" and len(argv) > 2:
            revoke(argv[2])
            print("Removed.")
        elif command == "status":
            print(json.dumps({k: v for k, v in status().items() if k != "log"}, indent=2, default=str))
        else:
            print(__doc__)
            return 2
    except RemoteError as error:
        print(f"lago-remote: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
