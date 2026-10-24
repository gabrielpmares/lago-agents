"""The shared record of what every agent is doing: live status and tasks.

It's a SQLite file in the state folder. The app, each agent's MCP server and the
Claude Code hooks all open it, so every write is a short transaction.
"""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from config_store import STATE_DIR

DB_PATH = STATE_DIR / "hub.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_state (
    agent_id     TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'stopped',
    detail       TEXT NOT NULL DEFAULT '',
    session_id   TEXT,
    current_task INTEGER,
    started_at   REAL,
    updated_at   REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team        TEXT,
    from_agent  TEXT,            -- NULL means you, the human
    to_agent    TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL,   -- queued, sent, working, done, failed, cancelled
    result      TEXT NOT NULL DEFAULT '',
    result_seen INTEGER NOT NULL DEFAULT 0,  -- the delegating lead already has the result
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
-- Text waiting to be typed into an agent's session (tasks, results, messages).
-- It's delivered when the agent is idle, so it never lands in a permission prompt.
CREATE TABLE IF NOT EXISTS inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    to_agent    TEXT NOT NULL,
    text        TEXT NOT NULL,
    task_id     INTEGER,          -- set for task results, so ones already seen can be dropped
    created_at  REAL NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 0
);
-- Swarm agents that have been woken for a project at least once. They live here, not
-- in config.json, because the Conductor creates them while you may be editing config.
-- A work session: one conversation with a Conductor about one task in one project.
CREATE TABLE IF NOT EXISTS workspaces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    area        TEXT,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open, closed
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS swarm_agents (
    agent_id    TEXT PRIMARY KEY,
    workspace   INTEGER,
    project     TEXT NOT NULL,
    catalog_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
-- Plans the Conductor proposes. `steps` is JSON: the plan plus each step's progress.
CREATE TABLE IF NOT EXISTS pipelines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace   INTEGER,
    project     TEXT NOT NULL,
    area        TEXT,
    title       TEXT NOT NULL,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL,   -- draft, changes_requested, approved, running, done, failed, rejected, cancelled
    steps       TEXT NOT NULL,
    feedback    TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
-- Things a work session has to tell you: done, a question, a plan to approve. Each one
-- may be spoken aloud (voice.py) and, for sessions the Orchestrator opened, relayed to it.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace   INTEGER,
    project     TEXT,
    kind        TEXT NOT NULL,   -- done, failed, question, gate, approval, blocked, permission, waiting, update, decided
    text        TEXT NOT NULL,
    spoken      TEXT NOT NULL DEFAULT '',   -- what was said aloud, if anything
    seen        INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
-- Reminders and recurring jobs Lago set up: each is typed into his session when due.
CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    text          TEXT NOT NULL,              -- what Lago does or tells you then
    next_at       REAL NOT NULL,
    repeat        TEXT NOT NULL DEFAULT 'once',  -- once, hourly, daily, weekdays, weekly, minutes
    every_minutes INTEGER,
    status        TEXT NOT NULL DEFAULT 'active',  -- active, done, cancelled
    last_at       REAL,
    created_at    REAL NOT NULL
);
-- Notifications you dismissed (notifications.py keys), so they stay out of the list.
CREATE TABLE IF NOT EXISTS dismissed (
    key         TEXT PRIMARY KEY,
    at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ws ON events (workspace, id);
CREATE INDEX IF NOT EXISTS inbox_to ON inbox (to_agent, delivered);
-- Which Claude account each session ran on, from each time it was started or resumed.
CREATE TABLE IF NOT EXISTS account_sessions (
    session_id TEXT NOT NULL,
    account    TEXT NOT NULL,
    agent_id   TEXT,
    started_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS account_sessions_started ON account_sessions (started_at);
CREATE INDEX IF NOT EXISTS tasks_to ON tasks (to_agent, status);
-- Thumbs up or down on one task a swarm agent did. `version` is the hash of the agent's
-- prompt when it ran (evals.py), so a rewritten prompt starts its own record.
CREATE TABLE IF NOT EXISTS agent_ratings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id  TEXT NOT NULL,
    task_id     INTEGER,
    rater       TEXT NOT NULL,   -- conductor, lago, human
    verdict     INTEGER NOT NULL,  -- 1 up, -1 down
    reason      TEXT NOT NULL DEFAULT '',
    version     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE (task_id, rater)
);
CREATE INDEX IF NOT EXISTS agent_ratings_agent ON agent_ratings (catalog_id, version);
-- One of your test cases run against one version of an agent, then graded.
CREATE TABLE IF NOT EXISTS test_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id  TEXT NOT NULL,
    test_id     TEXT NOT NULL,
    version     TEXT NOT NULL,
    proposal    INTEGER,          -- set when it tests a proposed prompt
    task_id     INTEGER,
    folder      TEXT NOT NULL,
    status      TEXT NOT NULL,    -- queued, running, grading, graded, error
    score       REAL,             -- 0..1 from the grader
    notes       TEXT NOT NULL DEFAULT '',
    gate        TEXT NOT NULL DEFAULT '',   -- JSON: evals.gate() on its last test batch
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS test_runs_agent ON test_runs (catalog_id, version);
-- A rewritten prompt the agent-designer proposed from an agent's thumbs-down, waiting
-- for the tests and then for your approval.
CREATE TABLE IF NOT EXISTS agent_proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_id  TEXT NOT NULL,
    status      TEXT NOT NULL,    -- preparing, drafting, testing, ready, approved, rejected, failed, reverted
    reason      TEXT NOT NULL DEFAULT '',
    old_version TEXT NOT NULL,
    old_prompt  TEXT NOT NULL,
    new_prompt  TEXT NOT NULL DEFAULT '',
    new_version TEXT NOT NULL DEFAULT '',
    task_id     INTEGER,
    notes       TEXT NOT NULL DEFAULT '',
    gate        TEXT NOT NULL DEFAULT '',   -- JSON: evals.gate() on its last test batch
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_from ON tasks (from_agent, status);
-- What each agent conversation has cost so far at API prices (budgets.py).
CREATE TABLE IF NOT EXISTS spend (
    session_id  TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    workspace   INTEGER,          -- the work session it belongs to, if any
    cost        REAL NOT NULL DEFAULT 0,
    checked_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_ws ON spend (workspace);
-- A budget you extended, was warned about or that paused its agents. `scope` is
-- "agent:<agent id>:<session id>" (one conversation) or "session:<work session id>".
CREATE TABLE IF NOT EXISTS budgets (
    scope       TEXT PRIMARY KEY,
    extra       REAL NOT NULL DEFAULT 0,   -- dollars added with Extend
    warned      INTEGER NOT NULL DEFAULT 0,  -- told you it passed WARN_AT of its limit
    paused_at   REAL
);
-- Every saved change to config.json: what it was before and after, so it can be undone.
CREATE TABLE IF NOT EXISTS config_revisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL DEFAULT '',   -- what saved it: app, lago, restore
    changed      TEXT NOT NULL,              -- JSON list of changed paths, e.g. "agents.am-qa"
    before       TEXT NOT NULL,              -- JSON config before
    after        TEXT NOT NULL,              -- JSON config after
    restored_from INTEGER,                   -- set when this save undid another revision
    created_at   REAL NOT NULL
);
-- What happened, in one place (activity.py): tasks handed out and finished, plans decided,
-- budgets, Lago's decisions, config changes, routines and watchdogs.
CREATE TABLE IF NOT EXISTS activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,      -- you, lago, an agent id, or system
    kind        TEXT NOT NULL,      -- task.assigned, pipeline.approved, budget.extended, ...
    text        TEXT NOT NULL,
    target      TEXT,               -- what it's about: an agent, task #, pipeline #
    workspace   INTEGER,
    project     TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_at ON activity (created_at);
CREATE INDEX IF NOT EXISTS activity_ws ON activity (workspace, id);
-- What the watchdogs (watchdog.py) already raised, so each episode is told once.
CREATE TABLE IF NOT EXISTS watchdog_alerts (
    agent_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- stalled, looping, crashed
    fingerprint TEXT NOT NULL,      -- the task and moment it's about
    at          REAL NOT NULL,
    PRIMARY KEY (agent_id, kind, fingerprint)
);
-- What projects are working toward (goals.py); work sessions link to one.
CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    parent_id   INTEGER,
    target      TEXT NOT NULL DEFAULT '',   -- a date or quarter, as you said it
    status      TEXT NOT NULL DEFAULT 'active',   -- active, achieved, dropped
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
-- Standing status notes (statuscards.py), rewritten only when what they watch changes.
CREATE TABLE IF NOT EXISTS status_cards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    project      TEXT NOT NULL DEFAULT '',
    query        TEXT NOT NULL DEFAULT '',
    summary      INTEGER NOT NULL DEFAULT 0,   -- the project's own summary card
    text         TEXT NOT NULL DEFAULT '',
    fingerprint  TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    archived     INTEGER NOT NULL DEFAULT 0,
    generated_at REAL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
-- A work session's documents and their revisions and comments (documents.py).
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace   INTEGER NOT NULL,
    path        TEXT NOT NULL,
    title       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE (workspace, path)
);
CREATE TABLE IF NOT EXISTS document_revisions (
    document    INTEGER NOT NULL,
    number      INTEGER NOT NULL,
    digest      TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (document, number)
);
CREATE TABLE IF NOT EXISTS document_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document    INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    quote       TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    by          TEXT NOT NULL,
    created_at  REAL NOT NULL
);
-- People in the company (company.py): Tailscale identities, their company-wide role and status.
CREATE TABLE IF NOT EXISTS members (
    login        TEXT PRIMARY KEY,          -- their Tailscale login, lowercased
    name         TEXT NOT NULL,
    picture      TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL DEFAULT 'watch',   -- watch, operate, admin: on projects without their own
    admin        INTEGER NOT NULL DEFAULT 0,      -- a company admin: manages people and settings
    status       TEXT NOT NULL DEFAULT 'pending', -- pending, active, removed
    account      TEXT,                            -- their Claude account on this computer, if connected
    requested_at REAL,
    joined_at    REAL,
    last_seen    REAL
);
CREATE TABLE IF NOT EXISTS member_projects (
    login        TEXT NOT NULL,
    project      TEXT NOT NULL,             -- '' for work outside projects
    role         TEXT NOT NULL,             -- watch, operate, admin, or none
    PRIMARY KEY (login, project)
);
-- Reviewers' verdicts on pull requests (pull_requests.py), per head commit.
CREATE TABLE IF NOT EXISTS pr_reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    number      INTEGER NOT NULL,
    head        TEXT NOT NULL,
    reviewer    TEXT NOT NULL,
    verdict     TEXT NOT NULL,      -- approve, changes, comment
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
-- Secrets' names and variables (vault.py); their values live only in your desktop keyring.
CREATE TABLE IF NOT EXISTS secrets (
    name        TEXT NOT NULL,
    project     TEXT NOT NULL DEFAULT '',   -- '' for every agent
    env         TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (name, project)
);
CREATE TABLE IF NOT EXISTS secret_access (
    name        TEXT NOT NULL,
    project     TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    at          REAL NOT NULL
);
-- Comments on tasks (tickets.py): each one is passed on to the other side of the task.
CREATE TABLE IF NOT EXISTS task_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    author      TEXT NOT NULL,      -- you, or an agent id
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS task_comments_task ON task_comments (task_id, id);
-- Your verdicts on Lago's "decided" reports (decisions.py), read back into his prompt.
CREATE TABLE IF NOT EXISTS decision_verdicts (
    event_id    INTEGER PRIMARY KEY,
    verdict     INTEGER NOT NULL,   -- 1 good call, -1 not what you'd have done
    note        TEXT NOT NULL DEFAULT '',
    at          REAL NOT NULL
);
-- Routines (routines.py): recurring jobs owned by one swarm agent, their revisions and runs.
CREATE TABLE IF NOT EXISTS routines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    agent           TEXT NOT NULL,           -- catalog agent id
    instructions    TEXT NOT NULL,
    project         TEXT NOT NULL DEFAULT '',
    folder          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active',
    repeat          TEXT NOT NULL DEFAULT 'daily',   -- schedules.REPEATS, or 'never' (webhook/manual only)
    every_minutes   INTEGER,
    next_at         REAL,
    concurrency     TEXT NOT NULL DEFAULT 'skip',
    catch_up        TEXT NOT NULL DEFAULT 'once',
    only_if_changed TEXT NOT NULL DEFAULT '',
    report          TEXT NOT NULL DEFAULT 'lago',
    hook_hash       TEXT,                    -- sha256 of the webhook secret
    last_fingerprint TEXT,
    revision        INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS routine_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id  INTEGER NOT NULL,
    revision    INTEGER NOT NULL,
    snapshot    TEXT NOT NULL,              -- JSON of FIELDS
    actor       TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS routine_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id  INTEGER NOT NULL,
    trigger     TEXT NOT NULL,              -- schedule, webhook, manual
    status      TEXT NOT NULL,              -- running, done, failed, skipped, coalesced
    reason      TEXT NOT NULL DEFAULT '',
    task_id     INTEGER,
    agent_id    TEXT,
    payload     TEXT NOT NULL DEFAULT '',
    coalesced   INTEGER NOT NULL DEFAULT 0, -- firings skipped while this run was going
    created_at  REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS routine_runs_routine ON routine_runs (routine_id, id);
-- Lago's own list of agents on GitHub (ghcatalog.py): the repos, their stars by day.
CREATE TABLE IF NOT EXISTS gh_repos (
    repo         TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    topic        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    license      TEXT NOT NULL,
    copyleft     INTEGER NOT NULL DEFAULT 0,
    stars        INTEGER NOT NULL,
    branch       TEXT NOT NULL DEFAULT '',
    topics       TEXT NOT NULL DEFAULT '[]',
    created_gh   TEXT NOT NULL,
    pushed_gh    TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',   -- JSON from describe(): what, needs, produces, fits
    sha          TEXT NOT NULL DEFAULT '',   -- the commit the summary was written from
    first_seen   REAL NOT NULL,
    seen_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gh_stars (
    repo   TEXT NOT NULL,
    day    TEXT NOT NULL,
    stars  INTEGER NOT NULL,
    PRIMARY KEY (repo, day)
);
CREATE TABLE IF NOT EXISTS gh_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
-- The same task given to a few agents at once, to pick the best (outcomes.py). `tasks` is
-- JSON {catalog id: task id}.
CREATE TABLE IF NOT EXISTS test_drives (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT NOT NULL,
    project     TEXT NOT NULL DEFAULT '',
    tasks       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',   -- running, picked, cancelled
    picked      TEXT,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""

OPEN_TASK = ("queued", "blocked", "sent", "working")
STATUSES = ("stopped", "starting", "idle", "working", "needs_approval", "waiting_on_team",
            "over_budget", "exited")


# Columns added after a table first shipped: (table, column, definition).
MIGRATIONS = (("swarm_agents", "workspace", "INTEGER"), ("pipelines", "workspace", "INTEGER"),
              ("workspaces", "unrestricted", "INTEGER NOT NULL DEFAULT 0"),
              ("workspaces", "orchestrated", "INTEGER NOT NULL DEFAULT 0"),
              ("workspaces", "folder", "TEXT"),
              ("swarm_agents", "folder", "TEXT"),
              ("swarm_agents", "unrestricted", "INTEGER NOT NULL DEFAULT 0"),
              ("swarm_agents", "model", "TEXT"), ("swarm_agents", "variant", "TEXT"),
              ("agent_state", "prompted_at", "REAL"), ("agent_state", "account", "TEXT"),
              ("agent_state", "lago_review", "REAL"),  # a permission prompt Lago is looking at, since
              ("agent_state", "tools_at", "REAL"),  # when its lago tools last connected
              ("agent_state", "usage_wait", "REAL"),  # out of usage everywhere: carry on at
              ("agent_proposals", "gate", "TEXT NOT NULL DEFAULT ''"),
              ("agent_proposals", "attempts", "INTEGER NOT NULL DEFAULT 0"),
              ("test_runs", "variant", "TEXT"),  # a proposal's prompt file it tests
              ("workspaces", "quiet", "INTEGER NOT NULL DEFAULT 0"),  # speaks and rings only for what needs you
              ("workspaces", "held_at", "REAL"),  # on hold since (holds.py)
              ("events", "form", "TEXT"),  # a question's choices (questions.py), JSON
              ("events", "answer", "TEXT"),  # and how it was answered, JSON
              ("events", "answered_at", "REAL"),
              ("workspaces", "goal_id", "INTEGER"),  # the goal it serves (goals.py)
              ("tasks", "blocked_by", "TEXT"),  # JSON task ids it waits on (tickets.py)
              ("tasks", "labels", "TEXT"),  # JSON list of short tags
              ("members", "codex", "TEXT"),  # their Codex home on this computer (member_accounts.py)
              ("workspaces", "account", "TEXT"), ("workspaces", "codex_home", "TEXT"),  # whose subscription
              ("swarm_agents", "account", "TEXT"), ("swarm_agents", "codex_home", "TEXT"),
              ("workspaces", "base_commit", "TEXT"),
              ("agent_state", "last_tool", "TEXT"),
              ("agent_ratings", "weight", "REAL"),  # NULL: the rater's usual weight (evals.WEIGHT)
              ("workspaces", "outcome", "TEXT"),
              ("gh_repos", "built_from", "TEXT NOT NULL DEFAULT ''"))  # a Lago agent's source repo  # accepted or rejected, your verdict on the whole result  # {"tool", "subject"} of its latest call (toolrules.py)  # the folder's git HEAD when it opened (changes.py)
_migrated = False  # the schema and migrations ran in this process
_migrate_lock = threading.Lock()


def _migrate(conn):
    """Once per process: WAL mode (it stays set in the file), the tables and new columns.
    Running the schema on every connection cost ~0.2 ms × hundreds of queries per poll.
    On a first start the app's threads and the scheduler service all migrate at once: a
    column another one has just added is fine."""
    global _migrated
    with _migrate_lock:
        if _migrated:
            return
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as error:
                    if "duplicate column" not in str(error):
                        raise
        _migrated = True


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if not _migrated:
            _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- agent state

def set_state(agent_id, **fields):
    fields["updated_at"] = time.time()
    columns = ", ".join(fields)
    updates = ", ".join(f"{name} = excluded.{name}" for name in fields)
    with connect() as conn:
        conn.execute(f"INSERT INTO agent_state (agent_id, {columns}) "
                     f"VALUES (?, {', '.join('?' * len(fields))}) "
                     f"ON CONFLICT (agent_id) DO UPDATE SET {updates}",
                     (agent_id, *fields.values()))


def get_state(agent_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM agent_state WHERE agent_id = ?", (agent_id,)).fetchone()
    return dict(row) if row else {"agent_id": agent_id, "status": "stopped", "detail": "",
                                  "session_id": None, "current_task": None,
                                  "started_at": None, "updated_at": None}


def record_account_session(session_id, account, agent_id):
    with connect() as conn:
        conn.execute("INSERT INTO account_sessions (session_id, account, agent_id, started_at) "
                     "VALUES (?, ?, ?, ?)", (session_id, account, agent_id, time.time()))


def account_sessions(since):
    """{session id: [(started_at, account), …] oldest first} for sessions that ran on an
    account since `since` (and any earlier starts of those sessions)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT session_id, account, started_at FROM account_sessions WHERE session_id IN "
            "(SELECT session_id FROM account_sessions WHERE started_at >= ?) ORDER BY started_at",
            (since,)).fetchall()
    sessions = {}
    for row in rows:
        sessions.setdefault(row["session_id"], []).append((row["started_at"], row["account"]))
    return sessions


def all_states():
    with connect() as conn:
        return {row["agent_id"]: dict(row) for row in conn.execute("SELECT * FROM agent_state")}


def wait_until_ready(agent_id, timeout=25):
    """After starting a session, wait for Claude Code to report in before typing into it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_state(agent_id)["status"] not in ("starting", "stopped"):
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------- tasks

def create_task(to_agent, title, body, from_agent=None, team=None):
    now = time.time()
    with connect() as conn:
        cur = conn.execute("INSERT INTO tasks (team, from_agent, to_agent, title, body, status, "
                           "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
                           (team, from_agent, to_agent, title, body, now, now))
        return cur.lastrowid


def update_task(task_id, **fields):
    fields["updated_at"] = time.time()
    with connect() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), task_id))


def get_task(task_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def tasks(where="1", params=(), limit=300):
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM tasks WHERE {where} ORDER BY id DESC LIMIT ?",
                            (*params, limit))
        return [dict(row) for row in rows]


def latest_tasks(agent_ids):
    """{agent id: its newest task} for these agents, in one query."""
    agent_ids = list(agent_ids)
    if not agent_ids:
        return {}
    marks = ", ".join("?" * len(agent_ids))
    return {task["to_agent"]: task for task in tasks(
        f"id IN (SELECT MAX(id) FROM tasks WHERE to_agent IN ({marks}) GROUP BY to_agent)",
        agent_ids, limit=len(agent_ids))}


def open_tasks_for(agent_id):
    return tasks(f"to_agent = ? AND status IN ({', '.join('?' * len(OPEN_TASK))})", (agent_id, *OPEN_TASK))


def tasks_involving(agent_id, limit=100):
    return tasks("to_agent = ? OR from_agent = ?", (agent_id, agent_id), limit)


# ---------------------------------------------------------------- inbox

def queue_text(to_agent, text, task_id=None):
    with connect() as conn:
        conn.execute("INSERT INTO inbox (to_agent, text, task_id, created_at) VALUES (?, ?, ?, ?)",
                     (to_agent, text, task_id, time.time()))


def take_inbox(agent_id):
    """Pending texts for an agent, marked delivered in the same transaction."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT inbox.id, inbox.text FROM inbox LEFT JOIN tasks ON tasks.id = inbox.task_id "
            "WHERE inbox.to_agent = ? AND inbox.delivered = 0 "
            "AND (inbox.task_id IS NULL OR tasks.result_seen = 0) ORDER BY inbox.id",
            (agent_id,)).fetchall()
        conn.execute("UPDATE inbox SET delivered = 1 WHERE to_agent = ? AND delivered = 0",
                     (agent_id,))
    return [row["text"] for row in rows]


def has_inbox(agent_id):
    with connect() as conn:
        return conn.execute("SELECT 1 FROM inbox WHERE to_agent = ? AND delivered = 0 LIMIT 1",
                            (agent_id,)).fetchone() is not None


# ---------------------------------------------------------------- swarm agents

def add_swarm_agent(agent_id, workspace, project, catalog_id, name, folder=None,
                    unrestricted=False, model=None, variant=None):
    """A swarm agent's session: a step's agent in a work session, or (no workspace) one Lago
    runs directly in `folder`. `model` overrides its manifest's; `variant` is the path of a
    proposed manifest it runs with instead (evals.py)."""
    with connect() as conn:
        conn.execute("INSERT INTO swarm_agents (agent_id, workspace, project, catalog_id, name, "
                     "folder, unrestricted, model, variant, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT (agent_id) DO UPDATE SET name = excluded.name",
                     (agent_id, workspace, project, catalog_id, name, folder,
                      int(bool(unrestricted)), model, variant, time.time()))


def swarm_agents():
    with connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM swarm_agents ORDER BY agent_id")]


# ---------------------------------------------------------------- pipelines

OPEN_PIPELINE = ("draft", "changes_requested", "approved", "running")


def _pipeline(row):
    if row is None:
        return None
    pipeline = dict(row)
    pipeline["steps"] = json.loads(pipeline["steps"])
    return pipeline


def create_pipeline(workspace, project, area, title, goal, steps):
    now = time.time()
    with connect() as conn:
        cur = conn.execute("INSERT INTO pipelines (workspace, project, area, title, goal, status, "
                           "steps, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
                           (workspace, project, area, title, goal, json.dumps(steps), now, now))
        return cur.lastrowid


def update_pipeline(pipeline_id, **fields):
    if "steps" in fields:
        fields["steps"] = json.dumps(fields["steps"])
    fields["updated_at"] = time.time()
    with connect() as conn:
        conn.execute(f"UPDATE pipelines SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), pipeline_id))


def get_pipeline(pipeline_id):
    with connect() as conn:
        return _pipeline(conn.execute("SELECT * FROM pipelines WHERE id = ?",
                                      (pipeline_id,)).fetchone())


def pipelines(where="1", params=(), limit=200):
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM pipelines WHERE {where} ORDER BY id DESC LIMIT ?",
                            (*params, limit))
        return [_pipeline(row) for row in rows]


# ---------------------------------------------------------------- workspaces

def create_workspace(project, area, title, unrestricted=False, orchestrated=False, folder=None):
    """A work session. `project` is "" for one that belongs to no project; it then works in
    `folder`."""
    now = time.time()
    with connect() as conn:
        cur = conn.execute("INSERT INTO workspaces (project, area, title, unrestricted, orchestrated, "
                           "folder, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (project, area, title, int(bool(unrestricted)), int(bool(orchestrated)),
                            folder, now, now))
        return cur.lastrowid


def update_workspace(workspace_id, **fields):
    fields["updated_at"] = time.time()
    with connect() as conn:
        conn.execute(f"UPDATE workspaces SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), workspace_id))


def get_workspace(workspace_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return dict(row) if row else None


def workspaces(where="1", params=(), limit=500):
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM workspaces WHERE {where} ORDER BY id DESC LIMIT ?",
                            (*params, limit))
        return [dict(row) for row in rows]


# ---------------------------------------------------------------- events

def add_event(workspace, project, kind, text, spoken="", form=None):
    with connect() as conn:
        cur = conn.execute("INSERT INTO events (workspace, project, kind, text, spoken, form, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (workspace, project, kind, text, spoken, form, time.time()))
        return cur.lastrowid


def events(where="1", params=(), limit=100):
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM events WHERE {where} ORDER BY id DESC LIMIT ?",
                            (*params, limit))
        return [dict(row) for row in rows]


def mark_events_seen(workspace=None):
    with connect() as conn:
        if workspace is None:
            conn.execute("UPDATE events SET seen = 1 WHERE seen = 0")
        else:
            conn.execute("UPDATE events SET seen = 1 WHERE seen = 0 AND workspace = ?", (workspace,))


# ---------------------------------------------------------------- evaluations (evals.py)

def _rows(sql, params=()):
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def rate(catalog_id, task_id, rater, verdict, reason, version, weight=None):
    """One rater's thumbs on one task; rating it again replaces the old one. `weight`
    overrides the rater's usual weight (a share of a whole job's points)."""
    with connect() as conn:
        conn.execute("INSERT INTO agent_ratings (catalog_id, task_id, rater, verdict, reason, "
                     "version, weight, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT (task_id, rater) DO UPDATE SET verdict = excluded.verdict, "
                     "reason = excluded.reason, weight = excluded.weight, "
                     "created_at = excluded.created_at",
                     (catalog_id, task_id, rater, verdict, reason, version, weight, time.time()))


def unrate(task_id, rater):
    with connect() as conn:
        conn.execute("DELETE FROM agent_ratings WHERE task_id = ? AND rater = ?", (task_id, rater))


def ratings(where="1", params=(), limit=500):
    return _rows(f"SELECT * FROM agent_ratings WHERE {where} ORDER BY id DESC LIMIT ?",
                 (*params, limit))


def add_test_run(catalog_id, test_id, version, folder, proposal=None, variant=None):
    now = time.time()
    with connect() as conn:
        return conn.execute("INSERT INTO test_runs (catalog_id, test_id, version, proposal, "
                            "variant, folder, status, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                            (catalog_id, test_id, version, proposal, variant, folder, now,
                             now)).lastrowid


def _update(table, row_id, fields):
    fields["updated_at"] = time.time()
    with connect() as conn:
        conn.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), row_id))


def create_drive(task, project, tasks):
    now = time.time()
    with connect() as conn:
        return conn.execute("INSERT INTO test_drives (task, project, tasks, created_at, "
                            "updated_at) VALUES (?, ?, ?, ?, ?)",
                            (task, project, json.dumps(tasks), now, now)).lastrowid


def get_drive(drive_id):
    rows = _rows("SELECT * FROM test_drives WHERE id = ?", (drive_id,))
    if not rows:
        return None
    rows[0]["tasks"] = json.loads(rows[0]["tasks"])
    return rows[0]


def update_drive(drive_id, **fields):
    if "tasks" in fields:
        fields["tasks"] = json.dumps(fields["tasks"])
    _update("test_drives", drive_id, fields)


def update_test_run(run_id, **fields):
    _update("test_runs", run_id, fields)


def test_runs(where="1", params=(), limit=500):
    return _rows(f"SELECT * FROM test_runs WHERE {where} ORDER BY id DESC LIMIT ?",
                 (*params, limit))


def add_proposal(catalog_id, reason, old_version, old_prompt):
    now = time.time()
    with connect() as conn:
        return conn.execute("INSERT INTO agent_proposals (catalog_id, status, reason, old_version, "
                            "old_prompt, created_at, updated_at) "
                            "VALUES (?, 'preparing', ?, ?, ?, ?, ?)",
                            (catalog_id, reason, old_version, old_prompt, now, now)).lastrowid


def update_proposal(proposal_id, **fields):
    _update("agent_proposals", proposal_id, fields)


def proposals(where="1", params=(), limit=200):
    return _rows(f"SELECT * FROM agent_proposals WHERE {where} ORDER BY id DESC LIMIT ?",
                 (*params, limit))


# ---------------------------------------------------------------- budgets (budgets.py)

def record_spend(session_id, agent_id, workspace, cost):
    with connect() as conn:
        conn.execute("INSERT INTO spend (session_id, agent_id, workspace, cost, checked_at) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT (session_id) DO UPDATE SET "
                     "cost = excluded.cost, checked_at = excluded.checked_at",
                     (session_id, agent_id, workspace, cost, time.time()))


def get_spend(session_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM spend WHERE session_id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def workspace_spend(workspace):
    """Dollars every conversation in a work session has cost."""
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM spend WHERE workspace = ?",
                           (workspace,)).fetchone()
    return row[0]


def get_budget(scope):
    with connect() as conn:
        row = conn.execute("SELECT * FROM budgets WHERE scope = ?", (scope,)).fetchone()
    return dict(row) if row else {"scope": scope, "extra": 0.0, "warned": 0, "paused_at": None}


def set_budget(scope, **fields):
    columns = ", ".join(fields)
    updates = ", ".join(f"{name} = excluded.{name}" for name in fields)
    with connect() as conn:
        conn.execute(f"INSERT INTO budgets (scope, {columns}) VALUES (?, {', '.join('?' * len(fields))}) "
                     f"ON CONFLICT (scope) DO UPDATE SET {updates}", (scope, *fields.values()))
