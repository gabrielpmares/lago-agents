"""A persistent cache for everything Lago fetches from elsewhere (GitHub, servers over SSH,
service health, git): stale-while-revalidate, kept in SQLite in the state folder.

    cache.get(key, fetch, fresh=60, keep=86400)

returns {"value", "fetched_at", "stale", "refreshing", "error"}:
- younger than `fresh` seconds: the cached value, no fetch
- older, but younger than `keep`: the cached value at once, and a background refresh
- missing, older than `keep`, or refresh=True: fetched now
- if a fetch fails and there's a cached value, that value is returned with the error

The file survives restarts and is shared by every process (the app and each agent's MCP
server), so a page opens with what was last seen instead of waiting on the network.
"""

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager

from config_store import STATE_DIR

DB_PATH = STATE_DIR / "cache.db"
log = logging.getLogger("lago.cache")

_refreshing = set()
_lock = threading.Lock()


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                     "fetched_at REAL NOT NULL)")
        yield conn
        conn.commit()
    finally:
        conn.close()


def read(key):
    """(value, fetched_at) or (None, None)."""
    with _connect() as conn:
        row = conn.execute("SELECT value, fetched_at FROM cache WHERE key = ?", (key,)).fetchone()
    return (json.loads(row[0]), row[1]) if row else (None, None)


def write(key, value):
    now = time.time()
    with _connect() as conn:
        conn.execute("INSERT INTO cache (key, value, fetched_at) VALUES (?, ?, ?) ON CONFLICT (key) "
                     "DO UPDATE SET value = excluded.value, fetched_at = excluded.fetched_at",
                     (key, json.dumps(value), now))
    return now


def forget(prefix):
    with _connect() as conn:
        conn.execute("DELETE FROM cache WHERE key LIKE ?", (prefix.replace("%", r"\%") + "%",))


def _background(key, fetch):
    with _lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run():
        try:
            write(key, fetch())
        except Exception as error:  # the cached value stays; the next call tries again
            log.warning("cache.refresh_failed key=%s error=%r", key, error)
        finally:
            with _lock:
                _refreshing.discard(key)
    threading.Thread(target=run, daemon=True).start()


def get(key, fetch, fresh=60, keep=86400, refresh=False):
    value, fetched_at = read(key)
    age = time.time() - fetched_at if fetched_at else None
    if value is not None and not refresh and age < fresh:
        return {"value": value, "fetched_at": fetched_at, "stale": False, "refreshing": False, "error": None}
    if value is not None and not refresh and age < keep:
        _background(key, fetch)
        return {"value": value, "fetched_at": fetched_at, "stale": True, "refreshing": True, "error": None}
    try:
        value = fetch()
    except Exception as error:
        if value is None:
            raise
        return {"value": value, "fetched_at": fetched_at, "stale": True, "refreshing": False,
                "error": str(error)}
    return {"value": value, "fetched_at": write(key, value), "stale": False, "refreshing": False,
            "error": None}


def value(key, fetch, **options):
    """Just the value, for callers that don't show freshness (agents' tools)."""
    return get(key, fetch, **options)["value"]


def meta(result):
    """What the UI shows about freshness."""
    return {"fetched_at": result["fetched_at"], "stale": result["stale"],
            "refreshing": result["refreshing"], "error": result["error"]}
