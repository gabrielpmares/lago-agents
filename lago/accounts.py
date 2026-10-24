"""The Claude subscriptions agents can run on, and how much of each one's limits is used.

Claude Code keeps its login in its config folder (~/.claude by default, or
$CLAUDE_CONFIG_DIR). The main account is ~/.claude. Every other account gets its own
folder under the state folder, holding only its login (.credentials.json, .claude.json);
everything else (agent definitions, skills, plugins, settings, session transcripts and
memory) is a symlink into ~/.claude, so agents behave the same on either account and can
resume each other's sessions.

config.json:
    "accounts": {"<id>": {"label": "Work", "email": "…"}}   the main account is always there
    "active_account": "<id>"          new sessions start on this one
    "accounts_auto_switch": false     turn off moving to another account when one runs out

When an account runs out, agents move to another one: new sessions once the active account
passes SWITCH_AT percent of a limit (checked while the app is open), and an agent that hits
the limit mid-task is resumed on the other account (lago_hook.py, on StopFailure). When no
other account can take over, an alert asks you to log in to one, and agents that stopped
carry on by themselves once a limit resets (team.resume_after_reset).

Usage comes from Claude Code itself: `claude -p /usage` (no model call, no cost) for the
plan's session and weekly limits, `claude auth status` for who is logged in. Lago also
adds up the tokens of its own sessions per account, for today and the last 7 days.
"""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import cache
import config_store
import hub
import token_usage
from config_store import STATE_DIR

MAIN = "main"
MAIN_DIR = Path.home() / ".claude"
ACCOUNTS_DIR = STATE_DIR / "accounts"
# What a second account shares with ~/.claude. Everything else in its folder is its own.
SHARED = ("agents", "skills", "plugins", "projects", "commands", "hooks", "output-styles",
          "agent-memory", "settings.json", "CLAUDE.md", "keybindings.json")
ACCOUNT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
USAGE_FRESH = 180      # seconds before /usage is asked again
STATUS_FRESH = 600
SWITCH_AT = 95         # % of a limit at which new sessions move to another account
EXHAUSTED = 99         # % at which an account counts as out
ALERT_KEY = "account-alert"
ALERT_REPEAT = 1800    # the same alert isn't raised again within this many seconds
# "Oct 19, 8:50pm (Europe/Berlin)", "8:50pm (Europe/Berlin)", "Oct 22, 5am (Europe/Berlin)"
RESETS = re.compile(r"^(?:([A-Za-z]{3})[a-z]* (\d{1,2}),? (?:at )?)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)"
                    r"(?: \(([^)]+)\))?$", re.IGNORECASE)
LIMIT_LINE = re.compile(r"^Current (session|week)(?: \((.+?)\))?: (\d+)% used(?: · resets (.+))?$")
ACTIVITY_LINE = re.compile(r"^Last (24h|7d) · ([\d,]+) requests? · ([\d,]+) sessions?$")


class AccountError(Exception):
    pass


# ---------------------------------------------------------------------- which accounts

def all_accounts(config):
    """{id: {"label"}} with the main account first."""
    saved = config.get("accounts") or {}
    accounts = {MAIN: {"label": "Main", **saved.get(MAIN, {})}}
    accounts.update({k: v for k, v in saved.items() if k != MAIN})
    return accounts


def active(config):
    chosen = config.get("active_account") or MAIN
    return chosen if chosen in all_accounts(config) else MAIN


def config_dir(account_id):
    return MAIN_DIR if account_id == MAIN else ACCOUNTS_DIR / account_id


def state_file(account_id):
    """Claude Code's own state for an account: onboarding, trusted folders, its login."""
    return Path.home() / ".claude.json" if account_id == MAIN else config_dir(account_id) / ".claude.json"


def trust_folder(folder):
    """Mark a folder trusted on every account, so a session there doesn't stop at Claude
    Code's "Do you trust this folder?". The folder itself: a trusted parent doesn't cover a
    folder that is its own git repository."""
    path = Path(os.path.expanduser(str(folder))).resolve()
    for account_id in all_accounts(config_store.load()):
        target = state_file(account_id)
        try:
            data = json.loads(target.read_text())
        except (OSError, ValueError):
            continue
        projects = data.setdefault("projects", {})
        if projects.get(str(path), {}).get("hasTrustDialogAccepted"):
            continue
        projects.setdefault(str(path), {})["hasTrustDialogAccepted"] = True
        tmp = target.with_name(target.name + ".lago-tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.chmod(target.stat().st_mode & 0o777)
        os.replace(tmp, target)


def env(account_id):
    """Environment for a `claude` process on this account ({} for the main one)."""
    return {} if account_id == MAIN else {"CLAUDE_CONFIG_DIR": str(config_dir(account_id))}


def _claude(account_id, *args, timeout=60, cwd=None):
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CONFIG_DIR"))}
    environment.update(env(account_id))
    return subprocess.run(["claude", *args], capture_output=True, text=True, timeout=timeout,
                          env=environment, cwd=cwd or STATE_DIR)


# ------------------------------------------------------------------ adding and removing

def new_id(config, label):
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:24] or "account"
    taken = set(all_accounts(config))
    candidate, n = base, 2
    while candidate in taken or not ACCOUNT_ID.match(candidate):
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def prepare(account_id):
    """Create (or repair) a second account's folder: its own login, everything else shared."""
    if account_id == MAIN:
        return
    folder = config_dir(account_id)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    for name in SHARED:
        source, link = MAIN_DIR / name, folder / name
        if source.exists() and not link.exists() and not link.is_symlink():
            link.symlink_to(source)
    # Start from the main account's settings file (onboarding done, trusted folders) without
    # its login, so the first session doesn't stop at Claude Code's setup questions.
    state_file = folder / ".claude.json"
    main_state = Path.home() / ".claude.json"
    if not state_file.exists() and main_state.exists():
        data = json.loads(main_state.read_text())
        for key in ("oauthAccount", "primaryApiKey", "customApiKeyResponses"):
            data.pop(key, None)
        state_file.write_text(json.dumps(data, indent=2))
        state_file.chmod(0o600)


def remove(account_id):
    """Delete a second account's folder: its login and the links (never what they point to)."""
    if account_id == MAIN:
        raise AccountError("The main account can't be removed.")
    folder = config_dir(account_id)
    if not folder.is_dir() or folder.parent != ACCOUNTS_DIR:
        return
    for entry in folder.iterdir():
        if entry.is_symlink():
            entry.unlink()
    shutil.rmtree(folder)
    cache.forget(f"account:{account_id}:")


def login_command(account_id, email=""):
    """The shell command that signs this account in (run in a terminal you can see)."""
    prefix = f"CLAUDE_CONFIG_DIR={config_dir(account_id)} " if account_id != MAIN else ""
    return f"{prefix}claude auth login" + (f" --email {email}" if re.fullmatch(r"[^\s'\"]+@[^\s'\"]+", email or "") else "")


# ------------------------------------------------------------------- what Claude Code says

def _status(account_id):
    try:
        result = _claude(account_id, "auth", "status", "--json", timeout=30)
    except OSError:  # Claude Code isn't installed (yet): the welcome wizard says how
        return {"logged_in": False, "email": "", "plan": "", "method": ""}
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise AccountError((result.stderr or result.stdout or "claude auth status failed").strip())
    return {"logged_in": bool(data.get("loggedIn")), "email": data.get("email") or "",
            "plan": data.get("subscriptionType") or "", "method": data.get("authMethod") or ""}


def status(account_id, refresh=False):
    if account_id != MAIN and not config_dir(account_id).is_dir():
        return {"logged_in": False, "email": "", "plan": "", "method": ""}
    return cache.value(f"account:{account_id}:status", lambda: _status(account_id),
                       fresh=STATUS_FRESH, refresh=refresh)


def parse_usage(text):
    """The plan limits and recent activity in `claude -p /usage` output."""
    limits, activity = [], {}
    for line in (l.strip() for l in text.splitlines()):
        match = LIMIT_LINE.match(line)
        if match:
            kind, scope, percent, resets = match.groups()
            limits.append({"window": kind, "scope": scope or "", "percent": int(percent),
                           "resets": (resets or "").strip()})
            continue
        match = ACTIVITY_LINE.match(line)
        if match:
            activity[match.group(1)] = {"requests": int(match.group(2).replace(",", "")),
                                        "sessions": int(match.group(3).replace(",", ""))}
    return {"limits": limits, "activity": activity}


def _usage(account_id):
    result = _claude(account_id, "-p", "/usage", timeout=60)
    text = result.stdout.strip()
    parsed = parse_usage(text)
    if not parsed["limits"]:
        raise AccountError(text.splitlines()[0] if text else (result.stderr.strip() or "No usage report."))
    return parsed


def usage(account_id, refresh=False):
    """{"value": {"limits", "activity"}, "fetched_at", "stale", "error"…} for the UI."""
    result = cache.get(f"account:{account_id}:usage", lambda: _usage(account_id),
                       fresh=USAGE_FRESH, keep=7 * 86400, refresh=refresh)
    return {**(result["value"] or {}), **cache.meta(result)}


# ------------------------------------------------------------ Lago's own sessions per account

def _windows():
    now = time.time()
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return {"today": midnight, "week": now - 7 * 86400}


def _lago_tokens():
    """Tokens of Lago's sessions per account, for today and the last 7 days. A message
    counts for the account its session was running on when the message was sent."""
    windows = _windows()
    since = min(windows.values())
    totals = {}
    for session_id, starts in hub.account_sessions(since - 30 * 86400).items():
        path = token_usage.find_session(session_id)
        if not path or path.stat().st_mtime < since:
            continue
        for at, usage in token_usage.messages(path):
            if at < since:
                continue
            account = next((a for started, a in reversed(starts) if started <= at), starts[0][1])
            for window, start in windows.items():
                if at >= start:
                    bucket = totals.setdefault(account, {}).setdefault(
                        window, {"input": 0, "output": 0, "messages": 0})
                    bucket["input"] += sum(usage.get(k) or 0 for k in token_usage.TOKEN_KINDS
                                           if k != "output_tokens")
                    bucket["output"] += usage.get("output_tokens") or 0
                    bucket["messages"] += 1
    return totals


def lago_tokens():
    return cache.value("lago-tokens-by-account", _lago_tokens, fresh=300, keep=86400)


# ------------------------------------------------------------------- running out

def worst(usage_report):
    """The highest % used across an account's limits (0 when unknown)."""
    return max((limit["percent"] for limit in (usage_report or {}).get("limits") or []), default=0)


def auto_switch(config):
    return config.get("accounts_auto_switch", True)


def has_room(account_id, spent):
    """Logged in, below SWITCH_AT on every limit (asked fresh), and not signed in to the same
    Claude login as the `spent` account (that one shares its limits)."""
    try:
        state, spent_email = status(account_id), status(spent).get("email")
        return account_id != spent and state["logged_in"] \
            and not (spent_email and state["email"] == spent_email) \
            and worst(usage(account_id, refresh=True)) < SWITCH_AT
    except Exception:
        return False


def with_room(config, exclude):
    """Another account with room, or None. Members' own accounts (member_accounts.py) are never
    borrowed: each person's work runs only on their own subscription."""
    return next((a for a, info in all_accounts(config).items()
                 if not info.get("member") and has_room(a, exclude)), None)


def make_active(config, account_id):
    config["active_account"] = account_id
    config_store.save(config)


def raise_alert(kind, text, account_id=None):
    """Something the app shows you wherever you are: kind is "switched", "login" or "out"."""
    previous, at = cache.read(ALERT_KEY)
    if previous and previous.get("text") == text and at and time.time() - at < ALERT_REPEAT:
        return
    cache.write(ALERT_KEY, {"id": f"{time.time():.6f}", "kind": kind, "text": text,
                            "account": account_id})


def alert():
    return cache.read(ALERT_KEY)[0]


def resets_at(text, now=None):
    """When a /usage "resets" text comes round, as epoch seconds, or None if unreadable."""
    match = RESETS.match((text or "").strip())
    if not match:
        return None
    month, day, hour, minute, half, zone = match.groups()
    try:
        tz = ZoneInfo(zone) if zone else None
    except (KeyError, ValueError):
        tz = None
    now = datetime.fromtimestamp(now or time.time(), tz)
    hour = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
    moment = now.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
    if month:
        try:
            moment = moment.replace(month=datetime.strptime(month.title(), "%b").month, day=int(day))
        except ValueError:
            return None
        if moment < now - timedelta(days=180):  # December's reading of a January date
            moment = moment.replace(year=moment.year + 1)
    elif moment < now - timedelta(minutes=5):
        moment += timedelta(days=1)
    return moment.timestamp()


def room_at(usage_report):
    """When an account has room again: the last reset among the limits it's spent (now when
    it has room, None when a reset time can't be read)."""
    spent = [l for l in (usage_report or {}).get("limits") or [] if l["percent"] >= SWITCH_AT]
    times = [resets_at(l["resets"]) for l in spent]
    return None if None in times else max(times, default=time.time())


def room_again_at(config):
    """The first time any logged-in account has room again (from the last /usage readings),
    or None when that can't be told."""
    times = [room_at(usage(a)) for a in all_accounts(config) if status(a)["logged_in"]]
    times = [t for t in times if t]
    return min(times) if times else None


def with_room_now(config):
    """A logged-in account with room (asked fresh), the active one first; None if all are out."""
    current = active(config)
    for account_id in [current] + [a for a in all_accounts(config) if a != current]:
        try:
            if status(account_id)["logged_in"] and \
                    worst(usage(account_id, refresh=True)) < SWITCH_AT:
                return account_id
        except Exception:
            continue
    return None


def resets_text(usage_report):
    spent = [l for l in (usage_report or {}).get("limits") or [] if l["percent"] >= SWITCH_AT]
    return f" ({spent[0]['window']} limit resets {spent[0]['resets']})" if spent and spent[0]["resets"] else ""


def out_of_usage(config, spent_account, usage_report):
    """`spent_account` ran out: make another account active, or ask you to log in to one.
    Returns the account that takes over, or None."""
    accounts = all_accounts(config)
    used = worst(usage_report)
    label = accounts[spent_account]["label"] + (" is out of usage" if used >= EXHAUSTED
                                                 else f" is at {used}% of a limit")
    when = resets_text(usage_report)
    if not auto_switch(config):
        raise_alert("out", f"{label}{when}. Automatic switching is off.", spent_account)
        return None
    current = active(config)
    target = current if has_room(current, spent_account) else with_room(config, spent_account)
    if target:
        if current == spent_account:
            make_active(config, target)
        raise_alert("switched", f"{label}{when}. Agents now run on "
                    f"{accounts[target]['label']}.", target)
        return target
    spent_email = status(spent_account).get("email")
    yours = [a for a in accounts if a != spent_account and not accounts[a].get("member")]
    waiting = [a for a in yours if not status(a, refresh=True)["logged_in"]]
    same = [a for a in yours if spent_email and status(a)["email"] == spent_email]
    if waiting:
        raise_alert("login", f"{label}{when}. Log in to "
                    f"{accounts[waiting[0]]['label']} to keep your agents working.", waiting[0])
    elif same:
        raise_alert("login", f"{label}{when}. {accounts[same[0]]['label']} is signed in to the "
                    f"same Claude login ({spent_email}): log it in to a different one to keep "
                    "your agents working.", same[0])
    else:
        raise_alert("out", f"Every account is out of usage{when}. Agents carry on by themselves "
                    "once a limit resets.", spent_account)
    return None


def check_active(config):
    """While the app is open: move new sessions off the active account once it nears a limit.
    Uses the last /usage reading (refreshed in the background), so it's cheap to call often."""
    account_id = active(config)
    if not status(account_id)["logged_in"]:
        return
    report = usage(account_id)
    if worst(report) >= SWITCH_AT:
        out_of_usage(config, account_id, report)
