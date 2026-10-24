"""Secrets hidden on screen: what agents print passes through here before anyone sees it.

Agents' terminals, transcripts and output reach the app, the phone and browsers of people in
the company. A key that an agent echoes (`cat .env`, a curl with a token, a stack trace with a
connection string) would show to everyone watching. Before any of it is shown:

- **known values** are replaced: the vault's secrets (vault.py) and the values in each
  project's `.env` files;
- **shapes of secrets** are replaced: API keys of the common providers (Anthropic, OpenAI,
  GitHub, AWS, Slack, Google, Stripe), JWTs, bearer tokens, private key blocks, passwords in
  URLs, and the value of any `SOMETHING_TOKEN=…` / `password: …` assignment.

What's shown instead is HIDDEN. The agent itself still has the real value. Off with
`{"redact": false}` in config.json.
"""

import re
import threading
import time
from pathlib import Path

import config_store

HIDDEN = "[hidden]"
MIN_KNOWN = 8          # shorter known values would hide ordinary words
KNOWN_FRESH = 60       # seconds between re-reading the vault and the .env files

SHAPES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]
# The value after a name that says it's secret; the name stays so the output still reads.
ASSIGNED = re.compile(
    r"(?i)(\b[A-Z0-9_.-]*(?:secret|token|passw(?:or)?d|passwd|api[_-]?key|apikey|private[_-]?key|access[_-]?key|credential)"
    r"[A-Z0-9_.-]*[\"']?\s*[=:]\s*[\"']?)((?![\d.]+\b)(?![A-Za-z_.]+\b)(?!\[hidden\])[^\s\"',;}{()\[\]<>]{6,})")
# (a value that's only digits, or only a code name like `request.headers`, isn't a secret)
BEARER = re.compile(r"(?i)(\b(?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]{16,})")
URL_PASSWORD = re.compile(r"(://[^/\s:@]+:)([^@\s/]{3,})(@)")

SECRET_NAME = re.compile(r"(?i)secret|token|pass|api_?key|private|access_?key|credential|dsn")
_lock = threading.Lock()
_known = {"at": 0.0, "pattern": None}


def enabled():
    return config_store.load().get("redact", True) is not False


def _env_values(path):
    out = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.replace("export ", "").strip(), value.strip().strip("\"'")
        # A secret by its name, or a long random-looking value; never NODE_ENV=production.
        if (SECRET_NAME.search(name) and len(value) >= MIN_KNOWN) or _random_looking(value):
            out.append(value)
    return out


def _random_looking(value):
    return len(value) >= 20 and " " not in value and "/" not in value \
        and re.search(r"\d", value) and re.search(r"[A-Za-z]", value)


def known_values():
    """The secrets this computer knows: the vault's, and every project's .env values."""
    values = set()
    try:
        import vault
        for row in vault.secrets():
            value = vault.backend().lookup(row["name"], row["project"])
            if value and len(value) >= MIN_KNOWN:
                values.add(value)
    except Exception:  # a locked keyring mustn't stop anything from showing
        pass
    for project in (config_store.load().get("projects") or {}).values():
        folder = Path(project.get("cwd") or "").expanduser()
        if project.get("cwd") and folder.is_dir():
            for env in folder.glob(".env*"):
                if env.is_file() and not env.name.endswith((".example", ".sample", ".template")):
                    values.update(_env_values(env))
    return values


def _known_pattern():
    with _lock:
        if time.time() - _known["at"] > KNOWN_FRESH:
            values = sorted(known_values(), key=len, reverse=True)
            _known["pattern"] = re.compile("|".join(map(re.escape, values))) if values else None
            _known["at"] = time.time()
        return _known["pattern"]


def forget():
    """Re-read the known values on the next call (a secret was just saved)."""
    with _lock:
        _known["at"] = 0.0


def text(value, known=None, on=None):
    """`value` with every secret in it hidden."""
    if not value or not isinstance(value, str) or not (enabled() if on is None else on):
        return value
    known = _known_pattern() if known is None else known
    if known:  # False: there are none
        value = known.sub(HIDDEN, value)
    for shape in SHAPES:
        value = shape.sub(HIDDEN, value)
    value = BEARER.sub(lambda m: m.group(1) + HIDDEN, value)
    value = URL_PASSWORD.sub(lambda m: m.group(1) + HIDDEN + m.group(3), value)
    return ASSIGNED.sub(lambda m: m.group(1) + HIDDEN, value)


def deep(value):
    """Every string in a structure of dicts and lists, hidden (a copy)."""
    if not enabled():
        return value
    known = _known_pattern() or False

    def walk(item):
        if isinstance(item, str):
            return text(item, known, True)
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v) for v in item]
        return item
    return walk(value)
