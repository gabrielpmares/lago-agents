"""Reminders and recurring jobs: "remind me at 5", "check the deploy every morning".

Lago sets them up with his schedule tool. While the app is open, a loop checks every
CHECK_SECONDS and types each due one into his session as a "[Lago] Reminder #N ..."
message (waking him if he's asleep); he then tells you, or does the job. One that came
due while the app was closed fires once when it opens, saying so; a repeating one then
moves on to its next time in the future. Each fire, change and failure goes to
schedules.log in the state folder.
"""

import logging
import logging.handlers
import threading
import time
from datetime import datetime, timedelta

import hub
from config_store import STATE_DIR

REPEATS = ("once", "hourly", "daily", "weekdays", "weekly", "minutes")
MIN_EVERY_MINUTES = 5
CHECK_SECONDS = 20
RETRY_SECONDS = 300
LATE_SECONDS = 120  # due longer ago than this: it was missed while the app was closed
LOG_PATH = STATE_DIR / "schedules.log"

log = logging.getLogger("lago.schedules")


class ScheduleError(Exception):
    pass


def _setup_log():
    if log.handlers:
        return
    handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512 * 1024, backupCount=1,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


_setup_log()


# ---------------------------------------------------------------- times

def parse_when(when=None, in_minutes=None, now=None):
    """The first time, as epoch seconds: `in_minutes` from now, or `when` as a local
    date-time ("2026-10-12 09:00", ISO) or a time of day ("17:30": today, or tomorrow
    if that's passed)."""
    now = now or time.time()
    if in_minutes is not None:
        try:
            minutes = float(in_minutes)
        except (TypeError, ValueError) as error:
            raise ScheduleError("in_minutes must be a number") from error
        if minutes <= 0:
            raise ScheduleError("in_minutes must be more than 0")
        return now + minutes * 60
    text = str(when or "").strip()
    if not text:
        raise ScheduleError("Say when: `when` (a date-time or a time of day) or `in_minutes`")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            clock = datetime.strptime(text, "%H:%M").time()
        except ValueError as error:
            raise ScheduleError(f"Couldn't read the time {text!r}: use 2026-10-12 09:00 or 17:30") from error
        moment = datetime.combine(datetime.fromtimestamp(now).date(), clock)
        if moment.timestamp() <= now:
            moment += timedelta(days=1)
    if moment.tzinfo:
        moment = moment.astimezone().replace(tzinfo=None)
    if moment.timestamp() <= now - 60:
        raise ScheduleError(f"{moment:%Y-%m-%d %H:%M} has already passed")
    return moment.timestamp()


def _step(at, repeat, every_minutes):
    """The occurrence after `at` (local wall-clock time is kept across DST changes)."""
    moment = datetime.fromtimestamp(at)
    if repeat == "hourly":
        moment += timedelta(hours=1)
    elif repeat == "minutes":
        moment += timedelta(minutes=every_minutes)
    elif repeat == "weekly":
        moment += timedelta(days=7)
    else:
        moment += timedelta(days=1)
        while repeat == "weekdays" and moment.weekday() >= 5:
            moment += timedelta(days=1)
    return moment.timestamp()


def next_after(at, repeat, every_minutes, now):
    """The first occurrence later than `now`."""
    while at <= now:
        at = _step(at, repeat, every_minutes)
    return at


def describe(row):
    """How it repeats, in words: "once", "every weekday at 09:00"..."""
    clock = datetime.fromtimestamp(row["next_at"]).strftime("%H:%M")
    return {"once": "once",
            "hourly": f"every hour at :{clock[3:]}",
            "daily": f"every day at {clock}",
            "weekdays": f"every weekday at {clock}",
            "weekly": f"every {datetime.fromtimestamp(row['next_at']):%A} at {clock}",
            "minutes": f"every {row['every_minutes']} minutes"}[row["repeat"]]


# ---------------------------------------------------------------- the table

def add(text, when=None, in_minutes=None, repeat="once", every_minutes=None):
    text = " ".join(str(text or "").split())
    if not text:
        raise ScheduleError("Say what to do or remind them of")
    repeat = repeat or "once"
    if repeat not in REPEATS:
        raise ScheduleError(f"repeat must be one of: {', '.join(REPEATS)}")
    if repeat == "minutes":
        try:
            every_minutes = int(every_minutes)
        except (TypeError, ValueError) as error:
            raise ScheduleError("repeat 'minutes' needs every_minutes") from error
        if every_minutes < MIN_EVERY_MINUTES:
            raise ScheduleError(f"every_minutes must be at least {MIN_EVERY_MINUTES}")
    else:
        every_minutes = None
    now = time.time()
    if repeat == "minutes" and when is None and in_minutes is None:
        in_minutes = every_minutes
    first = parse_when(when, in_minutes, now)
    if repeat == "weekdays":
        while datetime.fromtimestamp(first).weekday() >= 5:
            first = _step(first, "daily", None)
    with hub.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO schedules (text, next_at, repeat, every_minutes, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (text[:1000], first, repeat, every_minutes, now))
        row = get(cursor.lastrowid, conn)
    log.info("added #%d (%s, first %s): %s", row["id"], describe(row), clock(first), text)
    return row


def update(schedule_id, text=None, when=None, repeat=None, every_minutes=None):
    """Change an active reminder. A new `when` or repeat moves its next time; otherwise it
    keeps the one it has."""
    row = get(schedule_id)
    if row is None or row["status"] != "active":
        raise ScheduleError(f"There's no active reminder #{schedule_id}")
    changes = {}
    if text is not None:
        changes["text"] = " ".join(str(text).split())[:1000]
        if not changes["text"]:
            raise ScheduleError("Say what to do or remind them of")
    repeat = repeat or row["repeat"]
    if repeat not in REPEATS:
        raise ScheduleError(f"repeat must be one of: {', '.join(REPEATS)}")
    if repeat == "minutes":
        try:
            every_minutes = int(every_minutes if every_minutes is not None else row["every_minutes"])
        except (TypeError, ValueError) as error:
            raise ScheduleError("repeat 'minutes' needs every_minutes") from error
        if every_minutes < MIN_EVERY_MINUTES:
            raise ScheduleError(f"every_minutes must be at least {MIN_EVERY_MINUTES}")
    else:
        every_minutes = None
    changes["repeat"], changes["every_minutes"] = repeat, every_minutes
    now = time.time()
    if when:
        first = parse_when(when, None, now)
    elif repeat != row["repeat"] or every_minutes != row["every_minutes"]:
        first = parse_when(None, every_minutes, now) if repeat == "minutes" else row["next_at"]
    else:
        first = row["next_at"]
    if repeat == "weekdays":
        while datetime.fromtimestamp(first).weekday() >= 5:
            first = _step(first, "daily", None)
    changes["next_at"] = first
    with hub.connect() as conn:
        conn.execute(f"UPDATE schedules SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
                     (*changes.values(), row["id"]))
        row = get(row["id"], conn)
    log.info("changed #%d (%s, next %s): %s", row["id"], describe(row), clock(row["next_at"]), row["text"])
    return row


def run_now(schedule_id, deliver):
    """Hand an active reminder to Lago now, as if it came due; its next time stays."""
    row = get(schedule_id)
    if row is None or row["status"] != "active":
        raise ScheduleError(f"There's no active reminder #{schedule_id}")
    deliver(f"[Lago] Reminder #{row['id']} ({describe(row)}), run now by the human:\n{row['text']}")
    log.info("ran #%d now: %s", row["id"], row["text"])


def get(schedule_id, conn=None):
    if conn is None:
        with hub.connect() as conn:
            return get(schedule_id, conn)
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (int(schedule_id),)).fetchone()
    return dict(row) if row else None


def active():
    with hub.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM schedules WHERE status = 'active' ORDER BY next_at")]


def cancel(schedule_id):
    try:
        schedule_id = int(schedule_id)
    except (TypeError, ValueError) as error:
        raise ScheduleError("No such reminder") from error
    with hub.connect() as conn:
        done = conn.execute("UPDATE schedules SET status = 'cancelled' WHERE id = ? "
                            "AND status = 'active'", (schedule_id,)).rowcount
    if not done:
        raise ScheduleError(f"There's no active reminder #{schedule_id}")
    log.info("cancelled #%d", schedule_id)


def clock(at):
    return datetime.fromtimestamp(at).strftime("%a %Y-%m-%d %H:%M")


# ---------------------------------------------------------------- firing

def fire_due(deliver, now=None):
    """Hand each due schedule to `deliver(text)`, then move it on or mark it done. Each is
    claimed with a conditional update first, so it fires once even if two loops run."""
    now = now or time.time()
    with hub.connect() as conn:
        due = [dict(r) for r in conn.execute(
            "SELECT * FROM schedules WHERE status = 'active' AND next_at <= ? ORDER BY next_at", (now,))]
    for row in due:
        later = None if row["repeat"] == "once" else \
            next_after(row["next_at"], row["repeat"], row["every_minutes"], now)
        with hub.connect() as conn:
            claimed = conn.execute(
                "UPDATE schedules SET next_at = ?, status = ?, last_at = ? "
                "WHERE id = ? AND status = 'active' AND next_at = ?",
                (later or row["next_at"], "active" if later else "done", now,
                 row["id"], row["next_at"])).rowcount
        if not claimed:
            continue
        missed = now - row["next_at"] > LATE_SECONDS
        try:
            deliver(_message(row, missed, later))
        except Exception as error:  # noqa: BLE001 - logged, and the loop goes on
            log.error("#%d was due but couldn't reach Lago: %r", row["id"], error)
            if not later:  # a one-off isn't lost: it tries again in RETRY_SECONDS
                with hub.connect() as conn:
                    conn.execute("UPDATE schedules SET status = 'active', next_at = ? WHERE id = ?",
                                 (now + RETRY_SECONDS, row["id"]))
            continue
        log.info("fired #%d%s: %s", row["id"],
                 f" (missed at {clock(row['next_at'])})" if missed else "", row["text"])


def _message(row, missed, later):
    due = datetime.fromtimestamp(row["next_at"]).strftime("%H:%M")
    head = f"[Lago] Reminder #{row['id']} ({describe(row) if later else 'once'}, due {due})"
    if missed:
        head += f". It came due on {clock(row['next_at'])} while Lago Agents was closed"
    tail = f"\nNext time: {clock(later)}." if later else ""
    return f"{head}:\n{row['text']}{tail}"


def start_firing(deliver):
    """Check for due schedules now and every CHECK_SECONDS, in the background."""
    def loop():
        while True:
            try:
                fire_due(deliver)
            except Exception:  # a failed check must not end the loop
                log.exception("checking schedules failed")
            time.sleep(CHECK_SECONDS)
    threading.Thread(target=loop, name="schedules", daemon=True).start()
