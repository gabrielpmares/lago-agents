#!/usr/bin/env python3
"""Connectors: hosted MCP servers (Linear, Notion, GitHub…) your agents can use, and Lago
Agents as an MCP server other assistants can use.

A connector is an entry in config.json's "connectors": a remote MCP server (Streamable HTTP)
or a local command, which projects get it, and which of its tools are switched off. Signing
in happens here, once: the MCP authorization flow (OAuth with dynamic client registration and
PKCE, through the browser) or a pasted token. Tokens live only in the keyring (vault.py's).

Agents don't see a remote server's address or token. Their MCP config runs
`connectors.py proxy <id>`, a stdio server that forwards each message to the connector,
refreshes the sign-in when it expires, hides switched-off tools and logs every tool call
(History, and the Review tab of the Connectors page).
"""

import base64
import hashlib
import json
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import activity  # noqa: E402
import config_store  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable or "/usr/bin/python3"
CONNECTOR_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
KEYRING_PROJECT = ":connectors"  # project ids can't hold a colon, so no clash with secrets
PROTOCOL_VERSION = "2025-06-18"
SIGN_IN_SECONDS = 300
CALL_SECONDS = 300
# Names an agent's own config already uses for something else.
RESERVED = {"lago", "playwright", "rea"}

# A few common provider-hosted MCP servers, ready to sign in to; any other is added by its
# address (add_connector). auth: "oauth" (sign in through the browser), "token" (paste one;
# `token_help` says where it comes from) or "none".
CATALOG = [
    {"id": "github", "name": "GitHub", "url": "https://api.githubcopilot.com/mcp/", "auth": "token",
     "token_help": "A fine-grained personal access token from github.com/settings/tokens, "
                   "limited to the repositories agents may touch.",
     "description": "Repositories, issues, pull requests and Actions through GitHub's own MCP server."},
    {"id": "linear", "name": "Linear", "url": "https://mcp.linear.app/mcp", "auth": "oauth",
     "description": "Find, create and update issues, projects and comments in Linear."},
    {"id": "notion", "name": "Notion", "url": "https://mcp.notion.com/mcp", "auth": "oauth",
     "description": "Search, read and edit pages and databases in your Notion workspace."},
    # Sign in with an OAuth client you create once in the provider's developer console: these
    # don't let new apps register themselves. Connectors in one `group` share the client.
    *[{"id": f"google-{key}", "name": name, "url": f"https://{host}.googleapis.com/mcp/v1",
       "auth": "oauth", "client": "google", "description": what}
      for key, name, host, what in (
          ("gmail", "Gmail", "gmailmcp", "Search and read mail, write drafts and manage labels."),
          ("calendar", "Google Calendar", "calendarmcp",
           "Calendars and events: find free times, create, move and answer invites."),
          ("drive", "Google Drive", "drivemcp", "Search, read, create and copy files in Drive."))],
    {"id": "slack", "name": "Slack", "url": "https://mcp.slack.com/mcp", "auth": "oauth", "client": "slack",
     "description": "Search channels and messages, read threads and post in Slack."},
]

# Where to create the OAuth client for each `client` group, and what to tell the provider.
OWN_CLIENT_PORT = 33418
OWN_CLIENT_REDIRECT = f"http://127.0.0.1:{OWN_CLIENT_PORT}/callback"
CLIENTS = {
    "google": {"console": "https://console.cloud.google.com/apis/credentials",
               "help": "Google's MCP servers are in Google's Workspace Developer Preview: join it "
                       "first. In a Google Cloud project, enable each product's API and its MCP "
                       "service (e.g. gmailmcp.googleapis.com), set up the OAuth consent screen, "
                       "then create an OAuth client ID of type Web application, ticked as an AI "
                       "agent client, with this redirect URI. One client serves every Google "
                       "connector.",
               "params": {"access_type": "offline", "prompt": "consent"}},
    "slack": {"console": "https://api.slack.com/apps",
              "help": "Create a Slack app for your workspace, add this redirect URL under OAuth & "
                      "Permissions, and copy its Client ID and Client Secret."},
}


ICONS_DIR = APP_DIR / "ui" / "connectors"  # logos by catalog id: Simple Icons (CC0), else the site's icon


def icon(catalog_id):
    """The logo's path for the page, or None (it shows the name's first letter)."""
    for suffix in (".svg", ".png"):
        if catalog_id and (ICONS_DIR / f"{catalog_id}{suffix}").exists():
            return f"connectors/{catalog_id}{suffix}"
    return None


class ConnectorError(Exception):
    pass


def catalog_entry(catalog_id):
    return next((c for c in CATALOG if c["id"] == catalog_id), None)


def installed(config=None):
    return (config or config_store.load()).get("connectors") or {}


def connector(connector_id, config=None):
    found = installed(config).get(connector_id)
    if not found:
        raise ConnectorError(f"There's no connector {connector_id!r}")
    return found


# ---------------------------------------------------------------- credentials (keyring)

def _keyring():
    import vault
    return vault.backend()


def credential(connector_id):
    try:
        raw = _keyring().lookup(f"connector-{connector_id}", KEYRING_PROJECT)
    except Exception:
        return None
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _store_credential(connector_id, cred):
    _keyring().store(f"connector-{connector_id}", KEYRING_PROJECT, json.dumps(cred))


def _clear_credential(connector_id):
    try:
        _keyring().clear(f"connector-{connector_id}", KEYRING_PROJECT)
    except Exception:
        pass


def signed_in(connector_id, conf):
    return conf.get("auth", "none") == "none" or conf.get("command") or bool(credential(connector_id))


# ---------------------------------------------------------------- HTTP

def _request(url, data=None, headers=None, method=None, form=False, timeout=20):
    body = None
    headers = dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = json.dumps(data).encode()
            headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", "lago-agents/1")  # Cloudflare turns Python's default away
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    return urllib.request.urlopen(request, timeout=timeout)


def _json(url, **kwargs):
    try:
        with _request(url, **kwargs) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        raise ConnectorError(f"{urllib.parse.urlparse(url).netloc} answered {error.code}: {detail}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ConnectorError(f"Couldn't reach {urllib.parse.urlparse(url).netloc}: {error}") from error


def _try_json(url):
    try:
        return _json(url)
    except ConnectorError:
        return None


# ---------------------------------------------------------------- OAuth (MCP authorization)

def _origin(url):
    parts = urllib.parse.urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def _well_known(base, name):
    """RFC 8615 places: with the path inserted after the well-known name, then at the root."""
    parts = urllib.parse.urlparse(base)
    path = parts.path.rstrip("/")
    places = [f"{parts.scheme}://{parts.netloc}/.well-known/{name}{path}"] if path else []
    return places + [f"{parts.scheme}://{parts.netloc}/.well-known/{name}"]


def _resource_metadata_url(url):
    """The protected-resource metadata URL a server names in its 401, if it does."""
    try:
        with _request(url, data={"jsonrpc": "2.0", "id": 0, "method": "initialize",
                                 "params": _initialize_params()},
                      headers={"Accept": "application/json, text/event-stream"}):
            return None
    except urllib.error.HTTPError as error:
        found = re.search(r'resource_metadata="([^"]+)"', error.headers.get("WWW-Authenticate", ""))
        return found.group(1) if found else None
    except (urllib.error.URLError, OSError):
        return None


def discover(url):
    """Where to sign in for the MCP server at `url`: {authorization_endpoint, token_endpoint,
    registration_endpoint, scopes, resource}."""
    resource_meta = None
    named = _resource_metadata_url(url)
    for place in ([named] if named else []) + _well_known(url, "oauth-protected-resource"):
        resource_meta = _try_json(place)
        if resource_meta:
            break
    issuer = ((resource_meta or {}).get("authorization_servers") or [_origin(url)])[0]
    meta = None
    for place in _well_known(issuer, "oauth-authorization-server") + _well_known(issuer, "openid-configuration"):
        meta = _try_json(place)
        if meta and meta.get("authorization_endpoint"):
            break
    if not meta or not meta.get("authorization_endpoint"):
        # Servers from before the 2025-06 spec: the endpoints at their default places.
        base = _origin(issuer)
        meta = {"authorization_endpoint": f"{base}/authorize", "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register"}
    scopes = (resource_meta or {}).get("scopes_supported") or []
    return {"authorization_endpoint": meta["authorization_endpoint"],
            "token_endpoint": meta["token_endpoint"],
            "registration_endpoint": meta.get("registration_endpoint"),
            "scopes": scopes, "resource": (resource_meta or {}).get("resource") or url}


def _loopback(port=0):
    """A one-shot server on 127.0.0.1 for the browser's redirect: (server, redirect uri, result).
    `port` 0 picks a free one; an OAuth client you registered yourself needs the fixed one."""
    got = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in query and "error" not in query:
                self.send_response(404)
                self.end_headers()
                return
            got.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in query
            self.wfile.write((f"<!doctype html><meta charset=utf-8><title>Lago Agents</title>"
                              f"<body style='font:16px system-ui;padding:40px'>"
                              f"<h1>{'Connected' if ok else 'Not connected'}</h1>"
                              f"<p>{'You can close this tab and go back to Lago Agents.' if ok else 'Sign-in was cancelled or refused.'}"
                              f"</p></body>").encode())

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as error:
        raise ConnectorError(f"Port {port} is busy, so the sign-in can't come back: {error}") from error
    server.timeout = 1
    return server, f"http://127.0.0.1:{server.server_port}/callback", got


def _sign_in_oauth(conf, own_client=None, params=None):
    """The browser sign-in. `own_client` ({client_id, client_secret}) is an OAuth client you
    created in the provider's console, for servers that don't let apps register themselves."""
    found = discover(conf["url"])
    server, redirect, got = _loopback(OWN_CLIENT_PORT if own_client else 0)
    try:
        if own_client:
            client = own_client
        elif not found["registration_endpoint"]:
            raise ConnectorError(f"{conf['name']} doesn't let new apps register themselves, so Lago "
                                 "can't sign in to it. Use a token if it offers one (Custom connector).")
        else:
            client = _json(found["registration_endpoint"], data={
                "client_name": "Lago Agents", "redirect_uris": [redirect],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none"})
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        extra = params or {}
        params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
                  "resource": found["resource"]}
        if found["scopes"]:
            params["scope"] = " ".join(found["scopes"])
        params.update(extra)
        joiner = "&" if "?" in found["authorization_endpoint"] else "?"
        webbrowser.open(found["authorization_endpoint"] + joiner + urllib.parse.urlencode(params))
        deadline = time.time() + SIGN_IN_SECONDS
        while "code" not in got and "error" not in got and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "code" not in got:
        raise ConnectorError("Sign-in " + (f"was refused: {got['error'][0]}" if "error" in got
                                           else "timed out. Try again when you're ready."))
    if got.get("state", [""])[0] != state:
        raise ConnectorError("Sign-in answered for another request. Try again.")
    fields = {"grant_type": "authorization_code", "code": got["code"][0], "redirect_uri": redirect,
              "client_id": client["client_id"], "code_verifier": verifier, "resource": found["resource"]}
    if client.get("client_secret"):
        fields["client_secret"] = client["client_secret"]
    token = _json(found["token_endpoint"], data=fields, form=True)
    return {"kind": "oauth", "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
            "token_endpoint": found["token_endpoint"], "resource": found["resource"],
            "access_token": token["access_token"], "refresh_token": token.get("refresh_token", ""),
            "expires_at": time.time() + float(token.get("expires_in") or 3600)}


def _refresh(connector_id, cred):
    if not cred.get("refresh_token"):
        raise ConnectorError("The sign-in expired. Sign in again on the Connectors page.")
    fields = {"grant_type": "refresh_token", "refresh_token": cred["refresh_token"],
              "client_id": cred["client_id"], "resource": cred["resource"]}
    if cred.get("client_secret"):
        fields["client_secret"] = cred["client_secret"]
    try:
        token = _json(cred["token_endpoint"], data=fields, form=True)
    except ConnectorError as error:
        raise ConnectorError(f"The sign-in expired and couldn't be renewed ({error}). "
                             "Sign in again on the Connectors page.") from error
    cred = {**cred, "access_token": token["access_token"],
            "refresh_token": token.get("refresh_token") or cred["refresh_token"],
            "expires_at": time.time() + float(token.get("expires_in") or 3600)}
    _store_credential(connector_id, cred)
    return cred


def bearer(connector_id, force_refresh=False):
    """The Authorization header value for a connector, refreshed when it's about to expire."""
    cred = credential(connector_id)
    if not cred:
        return None
    if cred.get("kind") == "oauth" and (force_refresh or cred.get("expires_at", 0) - 60 < time.time()):
        cred = _refresh(connector_id, cred)
    return f"Bearer {cred.get('access_token') or cred.get('token')}"


# ---------------------------------------------------------------- talking to a remote server

def _initialize_params():
    return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "lago-agents", "version": "1"}}


class Remote:
    """A Streamable HTTP MCP session with one connector. `post` returns the JSON-RPC messages
    the server sent back for one message (none for a notification)."""

    def __init__(self, connector_id, conf):
        self.id, self.conf = connector_id, conf
        self.session = None
        self.protocol = None

    def _headers(self, force_refresh=False):
        headers = {"Accept": "application/json, text/event-stream"}
        auth = bearer(self.id, force_refresh) if self.conf.get("auth", "none") != "none" else None
        if auth:
            headers["Authorization"] = auth
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        return headers

    def post(self, message):
        for attempt in range(2):
            try:
                response = _request(self.conf["url"], data=message, headers=self._headers(force_refresh=attempt > 0),
                                    timeout=CALL_SECONDS)
                break
            except urllib.error.HTTPError as error:
                if error.code == 401 and attempt == 0 and (credential(self.id) or {}).get("kind") == "oauth":
                    continue
                if error.code == 401:
                    raise ConnectorError(f"{self.conf['name']} refused the sign-in. Sign in again on the "
                                         "Connectors page.") from error
                raw = error.read()
                try:  # Google's MCP servers answer with an HTTP error status around a normal reply
                    found = json.loads(raw)
                except ValueError:
                    found = None
                if isinstance(found, dict) and found.get("jsonrpc"):
                    return [found]
                raise ConnectorError(f"{self.conf['name']} answered {error.code}: "
                                     f"{raw.decode(errors='replace')[:300]}") from error
            except (urllib.error.URLError, OSError) as error:
                raise ConnectorError(f"Couldn't reach {self.conf['name']}: {error}") from error
        with response:
            if response.headers.get("Mcp-Session-Id"):
                self.session = response.headers["Mcp-Session-Id"]
            if response.status == 202:
                return []
            kind = response.headers.get("Content-Type", "")
            if "text/event-stream" in kind:
                return list(self._events(response, message.get("id")))
            raw = response.read()
            if not raw.strip():
                return []
            found = json.loads(raw)
            return found if isinstance(found, list) else [found]

    @staticmethod
    def _events(response, wanted_id):
        """The messages on an SSE stream, until the answer to `wanted_id`."""
        data = []
        for line in response:
            line = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                try:
                    message = json.loads("\n".join(data))
                except ValueError:
                    message = None
                data = []
                if message is not None:
                    yield message
                    if isinstance(message, dict) and message.get("id") == wanted_id and "method" not in message:
                        return

    def initialize(self):
        replies = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _initialize_params()})
        result = next((r.get("result") for r in replies if r.get("id") == 1), None)
        if result is None:
            error = next((r.get("error") for r in replies if r.get("error")), {})
            raise ConnectorError(f"{self.conf['name']} didn't start a session: {error.get('message', 'no answer')}")
        self.protocol = result.get("protocolVersion") or PROTOCOL_VERSION
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def tools(self):
        found, cursor = [], None
        for page in range(20):
            params = {"cursor": cursor} if cursor else {}
            replies = self.post({"jsonrpc": "2.0", "id": 100 + page, "method": "tools/list", "params": params})
            result = next((r.get("result") for r in replies if r.get("id") == 100 + page), None) or {}
            found += result.get("tools") or []
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return found


# ---------------------------------------------------------------- managing connectors

def _check_id(connector_id):
    if not CONNECTOR_ID.match(connector_id or "") or connector_id in RESERVED:
        raise ConnectorError("A connector's id uses lowercase letters, digits and dashes "
                             f"(and isn't {', '.join(sorted(RESERVED))})")


def _save(connector_id, conf):
    config = config_store.without_swarm(config_store.load())
    connectors = config.setdefault("connectors", {})
    if conf is None:
        connectors.pop(connector_id, None)
    else:
        connectors[connector_id] = conf
    config_store.save(config)


def add(catalog_id=None, name="", url="", command="", auth="none", connector_id=""):
    """Add a connector from the catalog, or a custom one (a URL, or a command to run)."""
    entry = catalog_entry(catalog_id) if catalog_id else None
    if catalog_id and not entry:
        raise ConnectorError(f"There's no connector {catalog_id!r} in the catalog")
    if entry:
        conf = {"catalog": entry["id"], "name": entry["name"], "url": entry["url"], "auth": entry["auth"]}
        connector_id = connector_id or entry["id"]
    else:
        name, url, command = (name or "").strip(), (url or "").strip(), (command or "").strip()
        if not name:
            raise ConnectorError("Give the connector a name")
        if bool(url) == bool(command):
            raise ConnectorError("Give either the server's URL or the command that starts it")
        if url and not re.match(r"^https://|^http://(127\.0\.0\.1|localhost)[:/]", url):
            raise ConnectorError("The URL must start with https:// (or http://localhost)")
        if command:
            try:
                shlex.split(command)
            except ValueError as error:
                raise ConnectorError(f"The command doesn't parse: {error}") from error
        if auth not in ("none", "token", "oauth") or (command and auth != "none"):
            raise ConnectorError("Sign-in is none, token or oauth (and none for a command)")
        conf = {"name": name, "auth": auth, **({"url": url} if url else {"command": command})}
        connector_id = connector_id or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32]
    _check_id(connector_id)
    if connector_id in installed():
        raise ConnectorError(f"There's already a connector {connector_id!r}")
    conf.update(projects=[], lago=True, ask=False, blocked_tools=[], added_at=time.time())
    _save(connector_id, conf)
    activity.log(activity.actor(), "connector.added", conf["name"], target=f"connector {connector_id}")
    return {"ok": True, "id": connector_id, "message": f"Added {conf['name']}."}


def own_client(connector_id, conf=None):
    """The OAuth client a connector of a `client` group signs in with: the one it used before,
    else one another connector of the group uses. None when you haven't given one yet."""
    conf = conf or connector(connector_id)
    group = (catalog_entry(conf.get("catalog")) or {}).get("client")
    if not group:
        return None
    for other_id, other in [(connector_id, conf)] + list(installed().items()):
        if (catalog_entry(other.get("catalog")) or {}).get("client") == group:
            cred = credential(other_id) or {}
            if cred.get("client_id") and cred.get("client_secret"):
                return {"client_id": cred["client_id"], "client_secret": cred["client_secret"]}
    return None


def sign_in(connector_id, token="", client_id="", client_secret=""):
    """Connect: through the browser (oauth), or with a pasted token. Then check it answers.
    A connector of a `client` group signs in with your own OAuth client (`client_id`,
    `client_secret`, given once per group)."""
    conf = connector(connector_id)
    group = (catalog_entry(conf.get("catalog")) or {}).get("client")
    if conf.get("auth") == "token":
        token = (token or "").strip()
        if not token:
            raise ConnectorError("Paste the token")
        _store_credential(connector_id, {"kind": "token", "token": token})
    elif conf.get("auth") == "oauth" and group:
        client = {"client_id": client_id.strip(), "client_secret": client_secret.strip()} \
            if (client_id or "").strip() else own_client(connector_id, conf)
        if not client or not client["client_secret"]:
            raise ConnectorError(f"{conf['name']} needs your own OAuth client: its Client ID and "
                                 "Client Secret.")
        _store_credential(connector_id, _sign_in_oauth(conf, client, CLIENTS[group].get("params")))
    elif conf.get("auth") == "oauth":
        _store_credential(connector_id, _sign_in_oauth(conf))
    try:
        tools = check(connector_id)["tools"]
    except ConnectorError as error:
        if conf.get("auth") == "token":
            _clear_credential(connector_id)
        raise ConnectorError(f"Signed in, but {error}") from error
    activity.log(activity.actor(), "connector.signed_in", conf["name"], target=f"connector {connector_id}")
    import redact
    redact.forget()
    return {"ok": True, "message": f"{conf['name']} is connected: {len(tools)} tools."}


def sign_out(connector_id):
    conf = connector(connector_id)
    _clear_credential(connector_id)
    activity.log(activity.actor(), "connector.signed_out", conf["name"], target=f"connector {connector_id}")
    return {"ok": True, "message": f"Signed out of {conf['name']}."}


def remove(connector_id):
    conf = connector(connector_id)
    _clear_credential(connector_id)
    _save(connector_id, None)
    activity.log(activity.actor(), "connector.removed", conf["name"], target=f"connector {connector_id}")
    return {"ok": True, "message": f"Removed {conf['name']}."}


def update(connector_id, projects=None, lago=None, ask=None, blocked_tools=None):
    conf = dict(connector(connector_id))
    known = config_store.load()["projects"]
    if projects is not None:
        unknown = [p for p in projects if p not in known]
        if unknown:
            raise ConnectorError(f"There's no project {unknown[0]!r}")
        conf["projects"] = list(projects)
    if lago is not None:
        conf["lago"] = bool(lago)
    if ask is not None:
        conf["ask"] = bool(ask)
    if blocked_tools is not None:
        conf["blocked_tools"] = sorted({str(t) for t in blocked_tools})
    _save(connector_id, conf)
    activity.log(activity.actor(), "connector.changed", conf["name"], target=f"connector {connector_id}")
    return {"ok": True}


def check(connector_id):
    """Start a session with the connector and list its tools: {tools: [{name, description}]}."""
    conf = connector(connector_id)
    if conf.get("command"):
        tools = _stdio_tools(conf["command"])
    else:
        if not signed_in(connector_id, conf):
            raise ConnectorError(f"Sign in to {conf['name']} first")
        remote = Remote(connector_id, conf)
        remote.initialize()
        tools = remote.tools()
    blocked = set(conf.get("blocked_tools") or ())
    return {"tools": [{"name": t.get("name", ""), "description": (t.get("description") or "").strip()[:300],
                       "on": t.get("name") not in blocked,
                       "read_only": bool((t.get("annotations") or {}).get("readOnlyHint"))} for t in tools]}


def _stdio_tools(command):
    lines = "".join(json.dumps(m) + "\n" for m in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _initialize_params()},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
    try:
        proc = subprocess.run(shlex.split(command), input=lines, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConnectorError(f"The command didn't start: {error}") from error
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") == 2:
            return (message.get("result") or {}).get("tools") or []
    raise ConnectorError("The command didn't list its tools" + (f": {proc.stderr.strip()[-300:]}" if proc.stderr.strip() else ""))


# The ones most people look for first, in this order; the rest follow as listed.
FEATURED = ["google-gmail", "google-calendar", "google-drive", "google-docs", "google-sheets",
            "slack", "notion", "github", "linear", "atlassian", "hubspot", "zoom", "dropbox",
            "airtable", "asana", "stripe", "figma"]


def _shown_order(entry):
    return FEATURED.index(entry["id"]) if entry["id"] in FEATURED else len(FEATURED)


def listing():
    """What the Connectors page shows: the catalog and what's installed, with sign-in state."""
    config = config_store.load()
    mine = installed(config)
    rows = []
    for connector_id, conf in sorted(mine.items(), key=lambda kv: kv[1].get("name", kv[0]).lower()):
        rows.append({"id": connector_id, **conf, "signed_in": bool(signed_in(connector_id, conf)),
                     "custom": not conf.get("catalog"), "icon": icon(conf.get("catalog")),
                     "description": (catalog_entry(conf.get("catalog")) or {}).get("description")
                     or conf.get("url") or conf.get("command", "")})
    for row in rows:  # what the sign-in asks for when it needs your own OAuth client
        group = (catalog_entry(row.get("catalog")) or {}).get("client")
        if group:
            row["client"] = {**CLIENTS[group], "group": group, "redirect": OWN_CLIENT_REDIRECT,
                             "known": bool(own_client(row["id"], row))}
    taken = {c.get("catalog") for c in mine.values()}
    return {"installed": rows,
            "catalog": [{**c, "installed": c["id"] in taken, "icon": icon(c["id"])}
                        for c in sorted(CATALOG, key=_shown_order)],
            "projects": {pid: p.get("name", pid) for pid, p in config["projects"].items()},
            "assistants": assistants()}


def calls(limit=200):
    """Connector tool calls, newest first (the Review tab)."""
    return activity.timeline(kind="connector.call", limit=limit)


# ---------------------------------------------------------------- what agents get

def servers(config, agent):
    """{name: Claude Code MCP server} of the connectors this agent gets: signed in, for its
    project (or every project), and for Lago only when he's allowed them."""
    found = {}
    for connector_id, conf in installed(config).items():
        if agent.get("orchestrator") and not conf.get("lago"):
            continue
        if conf.get("projects") and agent.get("project") not in conf["projects"]:
            continue
        if not signed_in(connector_id, conf):
            continue
        found[connector_id] = {"type": "stdio", "command": PYTHON,
                               "args": [str(APP_DIR / "connectors.py"), "proxy", connector_id]}
    return found


def prompt_note(config, agent):
    """For Lago and the leads: which connectors they have and which the agents they start get,
    so they don't tell you an app isn't connected when it is."""
    mine = servers(config, agent)
    lines = []
    for connector_id, conf in sorted(installed(config).items()):
        if not signed_in(connector_id, conf):
            continue
        where = ", ".join(config["projects"].get(p, {}).get("name", p) for p in conf.get("projects") or []) \
            or "every project"
        if connector_id in mine:
            lines.append(f"- {conf['name']}: yours too, as the `{connector_id}` MCP tools; agents get it for {where}.")
        else:
            lines.append(f"- {conf['name']}: not yours; agents get it for {where}, so give work that needs it to one.")
    if not lines:
        return ""
    return ("## Connectors\nApps the human connected through MCP (the Connectors page). Use them "
            "before saying you can't reach something there.\n" + "\n".join(lines))


LAGO_RESTART_COOLDOWN = 600
_lago_restarted = {}


def _lago_running_servers():
    path = config_store.STATE_DIR / "runs" / config_store.ORCHESTRATOR_ID / "mcp.json"
    try:
        return set(json.loads(path.read_text())["mcpServers"]) - {"lago"}
    except (OSError, ValueError, KeyError):
        return None


def refresh_lago():
    """Lago only gets connectors when his session starts, and it outlives the app. When he's
    idle with other connectors than the ones he should have, restart him on the same
    conversation. At most once per change in ten minutes, so a keyring that can't be read
    never sets off a loop."""
    import hub
    import memory
    import runner
    import team
    oid = config_store.ORCHESTRATOR_ID
    running = _lago_running_servers()
    if running is None or not runner.is_running(oid) or config_store.rotating():
        return False
    config = config_store.load()
    wanted = set(servers(config, config["agents"][oid]))
    if wanted == running:
        return False
    key = (frozenset(running), frozenset(wanted))
    if time.time() - _lago_restarted.get(key, 0) < LAGO_RESTART_COOLDOWN:
        return False
    state = hub.get_state(oid)
    if state["status"] not in ("idle", "stopped") or not memory.between_turns(state.get("session_id")):
        return False
    _lago_restarted[key] = time.time()
    team.restart_orchestrator()
    activity.log("system", "connector.lago_restarted",
                 "Restarted Lago so he has " + (", ".join(sorted(wanted)) or "no connectors"))
    return True


def refresh_lago_soon():
    """After a change on the Connectors page: try now, without holding up the page."""
    def work():
        try:
            refresh_lago()
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()


def allowed(config, agent):
    """Permission rules for the connectors that don't ask before each use."""
    return [f"mcp__{name}" for name in servers(config, agent)
            if not installed(config)[name].get("ask")]


# ---------------------------------------------------------------- the proxy agents run

def _write(message, lock):
    with lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def _error(message_id, text):
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": -32000, "message": text}}


def proxy(connector_id):
    """A stdio MCP server that forwards to the connector (see the module's docstring)."""
    import os
    conf = connector(connector_id)
    agent_id = os.environ.get("LAGO_AGENT_ID") or "an agent"
    blocked = set(conf.get("blocked_tools") or ())
    lock = threading.Lock()
    ready = threading.Event()
    stdio = None
    if conf.get("command"):
        words = shlex.split(conf["command"])
        stdio = subprocess.Popen(words, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

        def relay():
            for line in stdio.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                _write(_filtered(message, blocked), lock)
        threading.Thread(target=relay, daemon=True).start()
    remote = None if stdio else Remote(connector_id, conf)

    def handle(message):
        if not stdio and message.get("method") != "initialize":
            ready.wait(60)
        try:
            for reply in remote.post(message):
                _write(_filtered(reply, blocked), lock)
        except ConnectorError as error:
            if "id" in message and "method" in message:
                _write(_error(message["id"], str(error)), lock)
        finally:
            if message.get("method") == "initialize":
                ready.set()

    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("method") == "tools/call":
            tool = (message.get("params") or {}).get("name", "")
            if tool in blocked:
                _write(_error(message.get("id"), f"{tool} is switched off for {conf['name']}."), lock)
                continue
            activity.log(agent_id, "connector.call", f"{conf['name']}: {tool}", target=f"connector {connector_id}")
        if stdio:
            stdio.stdin.write(json.dumps(message) + "\n")
            stdio.stdin.flush()
            continue
        if message.get("method") == "initialize":
            remote.protocol = None
        threading.Thread(target=handle, args=(message,), daemon=True).start()
    if stdio:
        stdio.terminate()


def _filtered(message, blocked):
    """Hide switched-off tools from a tools/list answer."""
    tools = (message.get("result") or {}).get("tools") if isinstance(message, dict) else None
    if blocked and isinstance(tools, list):
        message = {**message, "result": {**message["result"],
                                         "tools": [t for t in tools if t.get("name") not in blocked]}}
    return message


# ---------------------------------------------------------------- Lago as an MCP server

ASSISTANT_NAME = "lago-agents"


def public_command():
    return [PYTHON, str(APP_DIR / "lago_mcp.py"), "--public"]


def _claude_has():
    try:
        data = json.loads((Path.home() / ".claude.json").read_text())
    except (OSError, ValueError):
        return False
    return ASSISTANT_NAME in (data.get("mcpServers") or {})


def _codex_has():
    import tomllib
    try:
        data = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text())
    except (OSError, ValueError):
        return False
    return ASSISTANT_NAME in (data.get("mcp_servers") or {})


def _installed_cli(name):
    import shutil
    return shutil.which(name) is not None


def assistants():
    """Whether Claude Code and Codex can use Lago Agents, and setup for anything else."""
    command = public_command()
    return {
        "name": ASSISTANT_NAME,
        "claude": {"available": _installed_cli("claude"), "connected": _claude_has()},
        "codex": {"available": _installed_cli("codex"), "connected": _codex_has()},
        "snippets": {
            "Claude Code": shlex.join(["claude", "mcp", "add", "--scope", "user", ASSISTANT_NAME, "--", *command]),
            "Codex": shlex.join(["codex", "mcp", "add", ASSISTANT_NAME, "--", *command]),
            "Claude Desktop, Cursor and others (JSON)": json.dumps(
                {"mcpServers": {ASSISTANT_NAME: {"command": command[0], "args": command[1:]}}}, indent=2),
            "OpenCode (opencode.json)": json.dumps(
                {"mcp": {ASSISTANT_NAME: {"type": "local", "command": command, "enabled": True}}}, indent=2),
        },
    }


def connect_assistant(which, on=True):
    """Add Lago Agents to (or remove it from) Claude Code's or Codex's user-level MCP servers."""
    if which not in ("claude", "codex"):
        raise ConnectorError(f"Lago can set up Claude Code or Codex, not {which!r}")
    if not _installed_cli(which):
        raise ConnectorError(f"{'Claude Code' if which == 'claude' else 'Codex'} isn't installed")
    if on:
        args = ([which, "mcp", "add", "--scope", "user"] if which == "claude" else [which, "mcp", "add"]) \
            + [ASSISTANT_NAME, "--", *public_command()]
    else:
        args = [which, "mcp", "remove"] + (["--scope", "user"] if which == "claude" else []) + [ASSISTANT_NAME]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    connected = _claude_has() if which == "claude" else _codex_has()
    if connected != on:
        raise ConnectorError((proc.stderr or proc.stdout or "It didn't work").strip()[-400:])
    label = "Claude Code" if which == "claude" else "Codex"
    activity.log(activity.actor(), "connector.assistant", f"{'Added Lago Agents to' if on else 'Removed Lago Agents from'} {label}")
    return {"ok": True, "message": f"{label} can use Lago Agents now." if on else f"Removed from {label}."}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "proxy":
        proxy(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == "check":
        print(json.dumps(check(sys.argv[2]), indent=2))
    else:
        sys.exit("usage: connectors.py proxy <id> | check <id>")
