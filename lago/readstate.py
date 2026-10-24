"""What's new for each person: their own "read" marks on work sessions and agents' runs.

Everyone who watches (you at the desktop, members in their browsers) has their own marks.
Opening a work session or an agent's page marks it read for that person; it's new again once
something happens in it after that. The first time someone is seen, everything up to then
counts as read, so nobody starts with a wall of "new".
"""

import time

import activity
import hub

SCHEMA = """
CREATE TABLE IF NOT EXISTS read_states (
    login   TEXT NOT NULL,
    item    TEXT NOT NULL,   -- session:<id>, agent:<id>, or * for when they were first seen
    seen_at REAL NOT NULL,
    PRIMARY KEY (login, item)
);
"""


def viewer():
    """Who is looking: the person in the browser, else the owner at the desktop."""
    who = activity.person()
    return (who or {}).get("login") or "owner"


def _marks(login):
    with hub.connect() as conn:
        conn.executescript(SCHEMA)
        rows = dict(conn.execute("SELECT item, seen_at FROM read_states WHERE login = ?", (login,)).fetchall())
        if "*" not in rows:
            rows["*"] = time.time()
            conn.execute("INSERT OR IGNORE INTO read_states (login, item, seen_at) VALUES (?, '*', ?)", (login, rows["*"]))
    return rows


def annotate(items):
    """Set "unread" on each {"item", "at"}: something happened since this viewer last looked."""
    marks = _marks(viewer())
    for entry in items:
        seen = marks.get(entry.get("item"), marks["*"])
        entry["unread"] = bool(entry.get("at")) and entry["at"] > seen + 1
    return items


def mark(items, login=None):
    login = login or viewer()
    _marks(login)
    now = time.time()
    with hub.connect() as conn:
        conn.executemany("INSERT INTO read_states (login, item, seen_at) VALUES (?, ?, ?) "
                         "ON CONFLICT (login, item) DO UPDATE SET seen_at = excluded.seen_at",
                         [(login, item, now) for item in items if item and item != "*"])
