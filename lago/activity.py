"""What happened, in one place, and a search over all of Lago's records.

log() is called where something worth a line happens: a task handed out or finished, a plan
approved, a budget extended, Lago deciding for you, a config change, a routine firing, a
watchdog stepping in. timeline() reads it back; search() looks through the activity, tasks,
reports and plans together.
"""

import threading
import time

import hub

KEEP_DAYS = 180


_local = threading.local()


def set_person(who):
    """Who is acting in this thread: a person in the browser ({"login", "name"}), or None."""
    _local.person = who


def person():
    return getattr(_local, "person", None)


def actor():
    """Who is acting: a person in the browser, the agent whose MCP server or hook runs this,
    else you (at the desktop)."""
    import os
    who = person()
    return who["login"] if who else os.environ.get("LAGO_AGENT_ID") or "you"


def log(actor, kind, text, target=None, workspace=None, project=None, at=None):
    """Add a line (`at`: when it happened, for history brought over from elsewhere). Never
    raises: the activity log must not break what it records."""
    if actor in (None, "you") and person():
        actor = person()["login"]
    try:
        with hub.connect() as conn:
            conn.execute("INSERT INTO activity (actor, kind, text, target, workspace, project, "
                         "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (actor or "system", kind, text[:2000], target, workspace, project,
                          at or time.time()))
    except Exception:
        pass


def timeline(workspace=None, project=None, kind=None, actor=None, since=None, limit=200):
    """Newest first, optionally narrowed. `kind` may be a prefix such as "task"."""
    where, params = ["1"], []
    if workspace is not None:
        where.append("workspace = ?")
        params.append(workspace)
    if project:
        where.append("project = ?")
        params.append(project)
    if kind:
        where.append("(kind = ? OR kind LIKE ?)")
        params += [kind, f"{kind}.%"]
    if actor:
        where.append("actor = ?")
        params.append(actor)
    if since:
        where.append("created_at >= ?")
        params.append(since)
    with hub.connect() as conn:
        rows = conn.execute(f"SELECT * FROM activity WHERE {' AND '.join(where)} "
                            f"ORDER BY id DESC LIMIT ?", (*params, limit))
        return [dict(row) for row in rows]


def prune(now=None):
    cutoff = (now or time.time()) - KEEP_DAYS * 86400
    with hub.connect() as conn:
        return conn.execute("DELETE FROM activity WHERE created_at < ?", (cutoff,)).rowcount


# What search() looks through: (kind, table, columns searched, columns returned as title and
# text, when, workspace column).
_SOURCES = (
    ("task", "tasks", ("title", "body", "result"), "title", "result", "created_at", None),
    ("report", "events", ("text",), "kind", "text", "created_at", "workspace"),
    ("plan", "pipelines", ("title", "goal", "summary", "steps"), "title", "goal", "created_at",
     "workspace"),
    ("activity", "activity", ("text", "target"), "kind", "text", "created_at", "workspace"),
)


def _snippet(text, words, width=160):
    text = " ".join((text or "").split())
    low = text.lower()
    at = min((low.find(w) for w in words if low.find(w) >= 0), default=0)
    start = max(0, at - width // 3)
    return ("…" if start else "") + text[start:start + width] + ("…" if start + width < len(text) else "")


def search(query, kinds=None, workspace=None, limit=40):
    """Records containing every word of the query (case-insensitive), newest first."""
    words = [w.lower() for w in query.split() if w.strip()]
    if not words:
        return []
    found = []
    with hub.connect() as conn:
        for kind, table, columns, title, text, when, ws_column in _SOURCES:
            if kinds and kind not in kinds:
                continue
            haystack = " || ' ' || ".join(f"COALESCE({c}, '')" for c in columns)
            where = [f"LOWER({haystack}) LIKE ?" for _ in words]
            params = [f"%{w}%" for w in words]
            if workspace is not None:
                if not ws_column:
                    continue
                where.append(f"{ws_column} = ?")
                params.append(workspace)
            rows = conn.execute(f"SELECT id, {title} AS title, {text} AS text, {when} AS at"
                                f"{', ' + ws_column + ' AS workspace' if ws_column else ''} "
                                f"FROM {table} WHERE {' AND '.join(where)} "
                                f"ORDER BY id DESC LIMIT ?", (*params, limit))
            for row in rows:
                row = dict(row)
                found.append({"kind": kind, "id": row["id"], "title": row["title"] or "",
                              "snippet": _snippet(row["text"], words), "at": row["at"],
                              "workspace": row.get("workspace")})
    found.sort(key=lambda r: r["at"] or 0, reverse=True)
    return found[:limit]
