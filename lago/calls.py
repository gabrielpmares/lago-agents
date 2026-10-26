"""Phone calls with Lago, in the phone app: you call him, or he rings you when something
needs you and nobody has dealt with it.

A call is a conversation by voice. The phone turns what you say into text (Android's speech
recognition) and sends it with say(); Lago answers as usual, and the 🔊 line of every reply
is spoken in his own voice (Kokoro, made on this computer as at the desk) and played on the
phone. While a call is on, his replies are always spoken and always go to the phone,
whatever the voice settings and quiet hours say (voice.speak_reply and voice.to_phone ask
active()). "Let me talk to Chip" (or Atlas, or Lago) hands the call to that persona.

He rings you (consider(), run by remote.py's alert feed while a phone listens) for an item
on the bell's list that is still there RING_AFTER minutes after it appeared (`wait_minutes`),
of a kind you said may ring, outside the quiet hours, once per item and at most
MAX_PER_HOUR times an hour. Unanswered, the phone stops ringing after RING_SECONDS.

When he hasn't said anything STALL_SECONDS after you spoke, stalled() looks at his terminal:
if Claude is failing (overloaded, a usage limit, an API error) he tells you so aloud, in his
voice, once per thing you said; and the phone shows why he's quiet.

The call lives in CALL_PATH, so the hook process that speaks Lago's replies sees it too
(and touches SPOKE_PATH whenever it speaks on a call).
Settings, `calls` in config.json: {"ring": true, "wait_minutes": 10,
"kinds": ["stuck", "decisions", "crashes"]}.
"""

import json
import os
import re
import secrets
import threading
import time

import config_store
import features
import notify
import personas
from config_store import STATE_DIR

CALL_PATH = STATE_DIR / "remote" / "call.json"
LOG_PATH = STATE_DIR / "remote" / "calls.json"   # what rang and when, for the limits
SPOKE_PATH = STATE_DIR / "remote" / "call-spoke"  # touched whenever he speaks on a call
STALL_SECONDS = 20
SLOW_SECONDS = 45
RING_SECONDS = 45
STALE_SECONDS = 90       # a call the phone stopped checking on has ended
MAX_PER_HOUR = 3
AFTER_CALL_SECONDS = 120  # no ringing again straight after a call
KEEP_SECONDS = 7 * 86400

# What may ring you, by the bell's item kinds (notifications.py).
KIND_GROUPS = {
    "stuck": ("blocked", "waiting", "failed", "input", "reply", "question"),
    "decisions": ("permission", "plan", "gate", "budget"),
    "crashes": ("crashed",),
    "finished": ("done", "solved"),
}
DEFAULTS = {"ring": True, "wait_minutes": 10, "kinds": ["stuck", "decisions", "crashes"]}

# "Let me talk to Chip", "put Atlas on", "switch to Lago", "Hey Chip, ...".
SWITCH = (r"\b(?:talk|speak|chat)\s+(?:to|with)\s+({names})\b",
          r"\b(?:put|get|bring)\s+({names})\s+on\b",
          r"\bswitch\s+(?:me\s+)?(?:over\s+)?to\s+({names})\b",
          r"\bis\s+({names})\s+(?:there|around)\b",
          r"^\W*(?:hey|hi|hello|ok|okay)\W+({names})\b")

# What Claude Code shows when it can't answer, and what he says about it.
TROUBLE = ((re.compile(r"overloaded|\b529\b", re.I), "Claude's servers are overloaded, and I'm retrying"),
           (re.compile(r"usage limit|rate limit|\b429\b", re.I), "I've hit a usage limit"),
           (re.compile(r"API Error|connection error|network error|timed out", re.I), "I can't reach Claude"))

_lock = threading.Lock()


def settings(config):
    out = {**DEFAULTS, **(config.get("calls") or {})}
    out["kinds"] = [k for k in out["kinds"] if k in KIND_GROUPS]
    return out


def _read(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _clear():
    CALL_PATH.unlink(missing_ok=True)


def current(now=None):
    """The call ringing or on now, or None. One that rang out or lost its phone is over."""
    now = now or time.time()
    call = _read(CALL_PATH, None)
    if not isinstance(call, dict):
        return None
    if call.get("state") == "ringing" and now - call.get("at", 0) > RING_SECONDS:
        return None
    if call.get("state") == "active" and now - call.get("seen", 0) > STALE_SECONDS:
        return None
    return call


def active(now=None):
    call = current(now)
    return bool(call and call["state"] == "active")


def _persona(config):
    pid = personas.current(config)
    p = personas.PERSONAS[pid]
    return {"id": pid, "name": p["name"], "figure": p["figure"]}


def public(call):
    """What the phone gets: no more than it shows."""
    if not call:
        return None
    return {k: call.get(k) for k in ("id", "state", "persona", "reason", "at")}


def start(reason=None):
    """You call Lago. Ends any call before it; Lago is woken if asleep."""
    config = config_store.load()
    with _lock:
        now = time.time()
        call = {"id": secrets.token_hex(8), "state": "active", "at": now, "seen": now, "asked": now,
                "persona": _persona(config), "reason": reason}
        _write(CALL_PATH, call)
    notify.tell_orchestrator(_opening(config, reason, outgoing=True))
    return public(call)


def ring(item, now=None):
    """Lago rings you about a bell item."""
    now = now or time.time()
    config = config_store.load()
    reason = {k: item.get(k) for k in ("key", "kind", "title", "context", "text", "href")}
    call = {"id": secrets.token_hex(8), "state": "ringing", "at": now, "persona": _persona(config),
            "reason": reason}
    _write(CALL_PATH, call)
    log = _log(now)
    log["called"][item["key"]] = now
    log["times"].append(now)
    _write(LOG_PATH, log)
    return call


def answer(call_id):
    with _lock:
        call = current()
        if not call or call["id"] != call_id:
            return None
        if call["state"] == "ringing":
            now = time.time()
            call.update(state="active", answered=now, seen=now, asked=now)
            _write(CALL_PATH, call)
            answered = True
        else:
            answered = False
    if answered:
        notify.tell_orchestrator(_opening(config_store.load(), call.get("reason"), outgoing=False))
    return public(call)


def end(call_id, how="hung up", now=None):
    """Hang up, decline or let it ring out. Lago hears that a call he was on has ended."""
    with _lock:
        call = _read(CALL_PATH, None)
        if not isinstance(call, dict) or call.get("id") != call_id:
            return False
        _clear()
        now = now or time.time()
        log = _log(now)
        log["ended"] = now
        _write(LOG_PATH, log)
    if call.get("state") == "active":
        notify.tell_orchestrator("[Lago] Phone call: the call ended. Back to your usual replies.", start=False)
    return True


def seen(call_id):
    """The phone is still on the call (it checks every few seconds)."""
    with _lock:
        call = current()
        if not call or call["id"] != call_id:
            return None
        if call["state"] == "active":
            call["seen"] = time.time()
            _write(CALL_PATH, call)
        return call


def say(call_id, text):
    """What you said on the call, to Lago; asking for another persona hands the call to him."""
    text = " ".join((text or "").split())
    if not text:
        return None
    call = seen(call_id)
    if not call or call["state"] != "active":
        return None
    config = config_store.without_swarm(config_store.load())
    wanted = switch_to(text, config)
    switching = wanted and wanted != personas.current(config)
    if switching:
        config_store.save(personas.use(config, wanted), source="phone call")
    with _lock:
        call = current() or call
        call.update(asked=time.time(), persona=_persona(config_store.load()) if switching else call["persona"])
        _write(CALL_PATH, call)
    if switching:
        notify.tell_orchestrator(f"{personas.called_note(wanted)}\n\n(on the phone) {text}")
    else:
        notify.tell_orchestrator(f"(on the phone) {text}")
    return call


def spoke():
    """He said something on the call (voice.speak_reply)."""
    SPOKE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    SPOKE_PATH.touch()


def _spoke_at():
    try:
        return SPOKE_PATH.stat().st_mtime
    except OSError:
        return 0


def stalled(call, terminal, now=None):
    """Why he hasn't answered what you last said: (note for the screen, line for him to say
    now or None once said), or None while all is well. `terminal()` gives his screen."""
    now = now or time.time()
    asked = call.get("asked") or 0
    if not asked or _spoke_at() >= asked or now - asked < STALL_SECONDS:
        return None
    lines = (terminal() or "").splitlines()
    # Only what came after the last message he got ("❯ ..."): an old error further up is over.
    sent = [i for i, line in enumerate(lines) if line.lstrip().startswith("❯") and line.strip() != "❯"]
    bottom = "\n".join(lines[sent[-1] + 1:] if sent else lines[-12:])
    trouble = next((said for pattern, said in TROUBLE if pattern.search(bottom)), None)
    if trouble:
        note, line = trouble[0].upper() + trouble[1:], f"Sorry, I can't answer right now: {trouble}."
    elif now - asked >= SLOW_SECONDS:
        note, line = "Still working on it", "I'm still working on it."
    else:
        return None
    with _lock:
        latest = current(now)
        if not latest or latest["id"] != call["id"] or latest.get("noticed") == [asked, note]:
            return note, None
        latest["noticed"] = [asked, note]
        _write(CALL_PATH, latest)
    return note, line


def switch_to(text, config):
    """The persona `text` asks for ("let me talk to Chip"), or None."""
    if not features.on(config, "personas"):
        return None
    names = {p["name"].lower(): pid for pid, p in personas.PERSONAS.items()}
    pattern = "|".join(re.escape(n) for n in names)
    lowered = text.lower()
    for rule in SWITCH:
        found = re.search(rule.format(names=pattern), lowered)
        if found:
            return names[found.group(1)]
    return None


def _opening(config, reason, outgoing):
    user = (config.get("user_name") or "").strip() or "The human"
    lines = ["[Lago] Phone call: " + (f"{user} is calling you from their phone." if outgoing else
                                      f"you rang {user}'s phone and they answered.")]
    if reason:
        about = " · ".join(x for x in (reason.get("title"), reason.get("context")) if x)
        lines.append(f"It's about: {about}." + (f" {reason['text']}" if reason.get("text") else ""))
        lines.append("Open with that, in a sentence or two, and say what you need from them.")
    else:
        lines.append("Open with where things stand (your threads.md), in two or three sentences, "
                     "and ask what they want.")
    lines.append("They only hear you: in every reply, put everything that matters in the 🔊 line, one "
                 "to three short spoken sentences, nothing they'd need a screen for. What they say "
                 "reaches you as \"(on the phone) ...\". When they ask for something done, do it as "
                 "you would at the desk and tell them in a line.")
    return "\n".join(lines)


def _log(now=None):
    now = now or time.time()
    log = _read(LOG_PATH, {})
    called = {k: t for k, t in (log.get("called") or {}).items() if now - t < KEEP_SECONDS}
    times = [t for t in (log.get("times") or []) if now - t < 3600]
    return {"called": called, "times": times, "ended": log.get("ended", 0)}


def due(items, config, now=None):
    """The bell item to ring you about now, or None."""
    import voice
    now = now or time.time()
    conf = settings(config)
    if not conf["ring"] or current(now) or voice.quiet_now(voice.settings(config), now):
        return None
    log = _log(now)
    if len(log["times"]) >= MAX_PER_HOUR or now - log["ended"] < AFTER_CALL_SECONDS:
        return None
    kinds = {k for group in conf["kinds"] for k in KIND_GROUPS[group]}
    wait = max(0, float(conf["wait_minutes"])) * 60
    for item in items:
        at = item.get("at")
        if item.get("kind") in kinds and item["key"] not in log["called"] and at is not None and now - at >= wait:
            return item
    return None


def consider(items, now=None):
    """Ring if something is due (the alert feed calls this with the bell's list). Returns the call."""
    with _lock:
        call = current(now)
        if call and call["state"] == "ringing" and (call.get("reason") or {}).get("key") not in {i["key"] for i in items}:
            _clear()  # dealt with while it rang
            return None
        item = due(items, config_store.load(), now)
        return ring(item, now) if item else None
