"""The backlog: tasks waiting to be started, and finished ones kept as their history.

Items come from another tool (paperclip_import.py brings a Paperclip company's whole task
tree). Each keeps what it had there: its parent and sub-tasks, what blocks it, labels,
priority, the agent and project it belonged to, its comments and documents (with their
revisions), and its status. An open item is waiting: Start turns it into a Lago task (New Task,
filled in) and marks it started; Dismiss puts it away. Finished and cancelled ones are history:
readable and searchable, never started again.
"""

import json
import time

import activity
import hub

OPEN = ("backlog", "todo", "in_progress", "in_review", "blocked")
STATES = OPEN + ("done", "cancelled", "started", "dismissed")
SCHEMA = """
CREATE TABLE IF NOT EXISTS backlog_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,              -- "paperclip"
    ref          TEXT NOT NULL,              -- its id there, so importing twice doesn't duplicate it
    identifier   TEXT NOT NULL DEFAULT '',   -- what people called it there, e.g. ACME-12
    company      TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,              -- OPEN, done, cancelled, started, dismissed
    priority     TEXT NOT NULL DEFAULT '',
    project      TEXT NOT NULL DEFAULT '',
    agent        TEXT NOT NULL DEFAULT '',   -- the catalog agent it was assigned to
    goal_id      INTEGER,
    parent_id    INTEGER,
    blocked_by   TEXT NOT NULL DEFAULT '[]', -- backlog ids
    labels       TEXT NOT NULL DEFAULT '[]',
    comments     TEXT NOT NULL DEFAULT '[]', -- [{"by", "text", "at"}]
    documents    TEXT NOT NULL DEFAULT '[]', -- [{"key", "title", "body", "revisions": [{"number", "body", "at", "by"}]}]
    workspace    INTEGER,                    -- the Lago task it became
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    finished_at  REAL,
    UNIQUE (source, ref)
);
"""
JSON_FIELDS = ("blocked_by", "labels", "comments", "documents")
_ready = False


class BacklogError(Exception):
    pass


def _ensure():
    global _ready
    if _ready:
        return
    with hub.connect() as conn:
        conn.executescript(SCHEMA)
        # The first Paperclip import (2026-10-24) kept open tasks in imported_tasks.
        if conn.execute("SELECT name FROM sqlite_master WHERE name = 'imported_tasks'").fetchone():
            for row in conn.execute("SELECT * FROM imported_tasks").fetchall():
                conn.execute("INSERT OR IGNORE INTO backlog_items (source, ref, identifier, title, body, status, priority, "
                             "project, agent, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (row["source"], f"imported-{row['id']}", row["ref"], row["title"], row["body"],
                              {"waiting": "todo"}.get(row["status"], row["status"]), row["priority"], row["project"],
                              row["agent"], row["created_at"], row["updated_at"]))
            conn.execute("DROP TABLE imported_tasks")
    _ready = True


def _row(row):
    item = dict(row)
    for key in JSON_FIELDS:
        item[key] = json.loads(item[key] or "[]")
    item["open"] = item["status"] in OPEN
    return item


def add(source, ref, title, **fields):
    """Add an item, or return the one already brought over with this ref. Returns its id."""
    _ensure()
    now = time.time()
    values = {"identifier": "", "company": "", "body": "", "status": "todo", "priority": "", "project": "",
              "agent": "", "goal_id": None, "parent_id": None, "created_at": now, "finished_at": None, **fields}
    if values["status"] not in STATES:
        raise BacklogError(f"Unknown status {values['status']!r}")
    for key in JSON_FIELDS:
        values[key] = json.dumps(values.get(key) or [])
    columns = ["source", "ref", "title", *values.keys(), "updated_at"]
    with hub.connect() as conn:
        existing = conn.execute("SELECT id FROM backlog_items WHERE source = ? AND ref = ?", (source, ref)).fetchone()
        if existing:
            return existing["id"]
        cursor = conn.execute(f"INSERT INTO backlog_items ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                              (source, ref, str(title)[:300], *values.values(), now))
        return cursor.lastrowid


def link(item_id, parent_id=None, blocked_by=None):
    """Set an item's parent and blockers once every item of an import exists."""
    _ensure()
    with hub.connect() as conn:
        conn.execute("UPDATE backlog_items SET parent_id = ?, blocked_by = ? WHERE id = ?",
                     (parent_id, json.dumps(blocked_by or []), int(item_id)))


def get(item_id):
    _ensure()
    with hub.connect() as conn:
        row = conn.execute("SELECT * FROM backlog_items WHERE id = ?", (int(item_id),)).fetchone()
    if not row:
        raise BacklogError(f"There's no backlog item #{item_id}")
    return _row(row)


def items(which="open", project=None):
    """Items, as a flat list in tree order (each after its parent): `which` is "open", "done"
    (finished, cancelled, started or dismissed) or "all"."""
    _ensure()
    with hub.connect() as conn:
        rows = [_row(r) for r in conn.execute("SELECT * FROM backlog_items ORDER BY id")]
    if project is not None:
        rows = [r for r in rows if r["project"] == project]
    by_id = {r["id"]: r for r in rows}
    keep = {r["id"] for r in rows if which == "all" or (which == "open") == r["open"]}
    for item_id in list(keep):  # a shown item's parents show too, so the tree reads right
        parent = by_id[item_id]["parent_id"]
        while parent in by_id and parent not in keep:
            keep.add(parent)
            parent = by_id[parent]["parent_id"]
    children = {}
    for r in rows:
        if r["id"] in keep:
            children.setdefault(r["parent_id"] if r["parent_id"] in keep else None, []).append(r)
    rank = {"critical": 0, "urgent": 0, "high": 1, "medium": 2, "low": 3}
    out = []

    def walk(parent, depth):
        for r in sorted(children.get(parent, []), key=lambda r: (not r["open"], rank.get(r["priority"], 2), r["id"])):
            r["depth"] = depth
            r["children"] = len(children.get(r["id"], []))
            out.append(r)
            walk(r["id"], depth + 1)
    walk(None, 0)
    return out


def counts():
    _ensure()
    with hub.connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) n FROM backlog_items GROUP BY status").fetchall()
    open_n = sum(r["n"] for r in rows if r["status"] in OPEN)
    return {"open": open_n, "done": sum(r["n"] for r in rows) - open_n}


def set_status(item_id, status, workspace=None):
    """Mark an item started (it became Lago task `workspace`), dismissed, or open again."""
    item = get(item_id)
    if status not in ("started", "dismissed", "todo"):
        raise BacklogError(f"An item can be started, dismissed or reopened, not set to {status!r}")
    if status == "started" and not item["open"]:
        raise BacklogError("Only an open item can be started")
    with hub.connect() as conn:
        conn.execute("UPDATE backlog_items SET status = ?, workspace = COALESCE(?, workspace), updated_at = ? WHERE id = ?",
                     (status, workspace, time.time(), item["id"]))
    activity.log(activity.actor(), f"backlog.{status}", f"#{item['id']} {item['title']}", project=item["project"] or None)


def start_text(item):
    """What a new Lago task is asked when an item starts: its description, then what the
    tool it came from knew (identifier, labels, latest comments, documents by name)."""
    parts = [item["title"], "", item["body"].strip()]
    facts = [f for f in (item["identifier"] and f"Was {item['identifier']}", item["labels"] and "Labels: " + ", ".join(item["labels"])) if f]
    if facts:
        parts += ["", " · ".join(facts)]
    if item["comments"]:
        parts += ["", "Comments so far:"] + [f"- {c.get('by') or 'someone'}: {' '.join(str(c.get('text', '')).split())[:400]}"
                                            for c in item["comments"][-8:]]
    if item["documents"]:
        parts += ["", "Documents: " + ", ".join(d.get("title") or d.get("key") for d in item["documents"])]
    return "\n".join(parts).strip()
