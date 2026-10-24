"""Routines: recurring jobs owned by one swarm agent, run without going through Lago.

A routine names an agent, what it should do, and where (a project or a folder). It fires on
its schedule, from a webhook (POST /v1/hooks/<secret> on the phone server, reachable over
Tailscale only), or when you or Lago say "run it now". Each firing becomes a run: the agent
wakes as `r<N>-<agent>`, does the job, and goes back to sleep.

Policies, each chosen per routine:

- **concurrency**: `skip` (the default) skips a firing while the last run is still going and
  counts it on that run; `parallel` starts another run anyway.
- **catch_up**: after the app was closed through a due time, `skip` moves on to the next
  time; `once` (the default) runs once for everything missed.
- **only_if_changed**: `git` skips a scheduled firing when the project's repository (HEAD and
  working tree) hasn't changed since the last run; empty always runs.
- **report**: `lago` (the default) sends each result to Lago, who tells you; `failures` keeps
  successes in the run history and tells him only about failed runs.

Every routine change keeps a revision (`routine_revisions`), and every firing a run row
(`routine_runs`), so you can see what ran, what was skipped and why. Their tables are in
hub.py.
"""

import hashlib
import json
import secrets
import subprocess
import time
from pathlib import Path

import activity
import config_store
import features
import hub
import schedules

CONCURRENCY = ("skip", "parallel")
CATCH_UP = ("skip", "once")
CHANGED = ("", "git")
REPORT = ("lago", "failures")
STATUSES = ("active", "paused", "archived")
FIELDS = ("title", "agent", "instructions", "project", "folder", "repeat", "every_minutes",
          "concurrency", "catch_up", "only_if_changed", "report")
CHECK_SECONDS = 30



class RoutineError(Exception):
    pass


def _validate(fields):
    import swarm
    out = {k: v for k, v in fields.items() if k in FIELDS}
    if "title" in out and not str(out["title"]).strip():
        raise RoutineError("Give the routine a title")
    if "instructions" in out and not str(out["instructions"]).strip():
        raise RoutineError("Say what the agent should do each time")
    if "agent" in out:
        out["agent"] = str(out["agent"]).strip().strip("`")
        if out["agent"] not in swarm.enabled_agents(config_store.load()):
            raise RoutineError(f"There's no switched-on swarm agent {out['agent']!r}")
    if out.get("project"):
        if out["project"] not in config_store.load()["projects"]:
            raise RoutineError(f"There's no project {out['project']!r}")
    if out.get("folder") and not str(out["folder"]).startswith(("/", "~/")):
        raise RoutineError("Give the folder as an absolute path or one starting with ~/")
    for key, allowed in (("concurrency", CONCURRENCY), ("catch_up", CATCH_UP),
                         ("only_if_changed", CHANGED), ("report", REPORT)):
        if key in out and out[key] not in allowed:
            raise RoutineError(f"{key} must be one of: {', '.join(a or '(empty)' for a in allowed)}")
    if "repeat" in out and out["repeat"] not in (*schedules.REPEATS, "never"):
        raise RoutineError(f"repeat must be one of: {', '.join(schedules.REPEATS)}, never")
    if out.get("repeat") == "once":
        raise RoutineError("A routine repeats: use a reminder for a one-off job")
    return out


def _snapshot(row, actor):
    with hub.connect() as conn:
        conn.execute("INSERT INTO routine_revisions (routine_id, revision, snapshot, actor, created_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (row["id"], row["revision"], json.dumps({k: row[k] for k in FIELDS}), actor,
                      time.time()))


def get(routine_id):
    with hub.connect() as conn:
        row = conn.execute("SELECT * FROM routines WHERE id = ?", (int(routine_id),)).fetchone()
    if row is None:
        raise RoutineError(f"There's no routine #{routine_id}")
    return dict(row)


def all_routines(include_archived=False):
    with hub.connect() as conn:
        rows = conn.execute("SELECT * FROM routines" + ("" if include_archived else
                            " WHERE status != 'archived'") + " ORDER BY id")
        return [dict(r) for r in rows]


def _first_time(repeat, every_minutes, when=None, now=None):
    """The first firing: at `when` if given, else one interval from now."""
    if repeat == "never":
        return None
    now = now or time.time()
    if when:
        first = schedules.parse_when(when, None, now)
    elif repeat == "minutes":
        first = now + every_minutes * 60
    else:
        first = schedules.next_after(now, repeat, every_minutes, now)
    if repeat == "weekdays":
        from datetime import datetime
        while datetime.fromtimestamp(first).weekday() >= 5:
            first = schedules._step(first, "daily", None)
    return first


def create(title, agent, instructions, project="", folder="", repeat="daily", every_minutes=None,
           when=None, concurrency="skip", catch_up="once", only_if_changed="", report="lago",
           actor=None):
    """Add a routine. `when` is its first time ("09:00", "tomorrow 08:30"); it then repeats."""
    fields = _validate(dict(title=title, agent=agent, instructions=instructions, project=project or "",
                            folder=folder or "", repeat=repeat, every_minutes=every_minutes,
                            concurrency=concurrency, catch_up=catch_up,
                            only_if_changed=only_if_changed or "", report=report))
    if repeat == "minutes":
        try:
            fields["every_minutes"] = int(every_minutes)
        except (TypeError, ValueError) as error:
            raise RoutineError("repeat 'minutes' needs every_minutes") from error
        if fields["every_minutes"] < schedules.MIN_EVERY_MINUTES:
            raise RoutineError(f"every_minutes must be at least {schedules.MIN_EVERY_MINUTES}")
    else:
        fields["every_minutes"] = None
    if fields["only_if_changed"] == "git" and not fields["project"]:
        raise RoutineError("only_if_changed 'git' needs a project")
    try:
        next_at = _first_time(repeat, fields["every_minutes"], when)
    except schedules.ScheduleError as error:
        raise RoutineError(str(error)) from error
    now = time.time()
    with hub.connect() as conn:
        cur = conn.execute(
            f"INSERT INTO routines ({', '.join(fields)}, next_at, created_at, updated_at) "
            f"VALUES ({', '.join('?' * len(fields))}, ?, ?, ?)", (*fields.values(), next_at, now, now))
        routine_id = cur.lastrowid
    row = get(routine_id)
    _snapshot(row, actor or activity.actor())
    activity.log(actor or activity.actor(), "routine.created", f"#{routine_id} {row['title']} "
                 f"({describe(row)}, {row['agent']})", target=f"routine #{routine_id}",
                 project=row["project"] or None)
    return row


def update(routine_id, actor=None, when=None, **changes):
    row = get(routine_id)
    if row["status"] == "archived":
        raise RoutineError(f"Routine #{routine_id} is archived")
    fields = _validate(changes)
    merged = {**row, **fields}
    if merged["only_if_changed"] == "git" and not merged["project"]:
        raise RoutineError("only_if_changed 'git' needs a project")
    if "repeat" in fields or "every_minutes" in fields or when:
        try:
            fields["next_at"] = _first_time(merged["repeat"], merged["every_minutes"], when)
        except schedules.ScheduleError as error:
            raise RoutineError(str(error)) from error
    if not fields:
        return row
    fields["revision"] = row["revision"] + 1
    fields["updated_at"] = time.time()
    with hub.connect() as conn:
        conn.execute(f"UPDATE routines SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), row["id"]))
    row = get(routine_id)
    _snapshot(row, actor or activity.actor())
    activity.log(actor or activity.actor(), "routine.changed", f"#{row['id']} {row['title']}: "
                 f"{', '.join(k for k in fields if k not in ('revision', 'updated_at', 'next_at'))}",
                 target=f"routine #{row['id']}", project=row["project"] or None)
    return row


def set_status(routine_id, status, actor=None):
    if status not in STATUSES:
        raise RoutineError(f"status must be one of: {', '.join(STATUSES)}")
    row = get(routine_id)
    if row["status"] == "archived":
        raise RoutineError(f"Routine #{routine_id} is archived for good")
    next_at = row["next_at"]
    if status == "active" and row["status"] == "paused" and row["repeat"] != "never":
        next_at = schedules.next_after(row["next_at"] or time.time(), row["repeat"],
                                       row["every_minutes"], time.time())
    with hub.connect() as conn:
        conn.execute("UPDATE routines SET status = ?, next_at = ?, updated_at = ? WHERE id = ?",
                     (status, next_at, time.time(), row["id"]))
    activity.log(actor or activity.actor(), f"routine.{status}", f"#{row['id']} {row['title']}",
                 target=f"routine #{row['id']}", project=row["project"] or None)
    return get(routine_id)


def revisions(routine_id):
    with hub.connect() as conn:
        rows = conn.execute("SELECT * FROM routine_revisions WHERE routine_id = ? ORDER BY id DESC",
                            (int(routine_id),))
        return [{**dict(r), "snapshot": json.loads(r["snapshot"])} for r in rows]


def restore(routine_id, revision, actor=None):
    match = [r for r in revisions(routine_id) if r["revision"] == int(revision)]
    if not match:
        raise RoutineError(f"Routine #{routine_id} has no revision {revision}")
    return update(routine_id, actor=actor, **match[0]["snapshot"])


def describe(row):
    if row["repeat"] == "never":
        return "only when triggered"
    if not row.get("next_at"):
        return row["repeat"]
    return schedules.describe({"next_at": row["next_at"], "repeat": row["repeat"],
                               "every_minutes": row["every_minutes"]})


# ---------------------------------------------------------------- webhooks

def new_hook(routine_id, actor=None):
    """Make (or replace) the routine's webhook secret. Returns the secret: shown once."""
    row = get(routine_id)
    secret = secrets.token_urlsafe(24)
    with hub.connect() as conn:
        conn.execute("UPDATE routines SET hook_hash = ?, updated_at = ? WHERE id = ?",
                     (hashlib.sha256(secret.encode()).hexdigest(), time.time(), row["id"]))
    activity.log(actor or activity.actor(), "routine.hook", f"#{row['id']} {row['title']}: new webhook secret",
                 target=f"routine #{row['id']}")
    return secret


def by_hook(secret):
    digest = hashlib.sha256((secret or "").encode()).hexdigest()
    with hub.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM routines WHERE hook_hash IS NOT NULL "
                                               "AND status = 'active'")]
    return next((r for r in rows if secrets.compare_digest(r["hook_hash"], digest)), None)


# ---------------------------------------------------------------- firing

def runs(routine_id=None, limit=30):
    sync_runs()
    with hub.connect() as conn:
        rows = conn.execute("SELECT * FROM routine_runs" + (" WHERE routine_id = ?" if routine_id else "")
                            + " ORDER BY id DESC LIMIT ?",
                            ((int(routine_id), limit) if routine_id else (limit,)))
        return [dict(r) for r in rows]


def _add_run(routine, trigger, status, reason="", payload="", task_id=None, agent_id=None):
    with hub.connect() as conn:
        cur = conn.execute("INSERT INTO routine_runs (routine_id, trigger, status, reason, task_id, "
                           "agent_id, payload, created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (routine["id"], trigger, status, reason, task_id, agent_id, payload[:4000],
                            time.time(), None if status == "running" else time.time()))
        return cur.lastrowid


def _running(routine_id):
    with hub.connect() as conn:
        row = conn.execute("SELECT * FROM routine_runs WHERE routine_id = ? AND status = 'running' "
                           "ORDER BY id DESC LIMIT 1", (routine_id,)).fetchone()
    return dict(row) if row else None


def git_fingerprint(folder):
    """HEAD plus the working tree's changes, hashed; None when it isn't a git repository."""
    def git(*args):
        return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True,
                              timeout=20)
    head = git("rev-parse", "HEAD")
    if head.returncode:
        return None
    status = git("status", "--porcelain")
    return hashlib.sha256((head.stdout + status.stdout).encode()).hexdigest()[:16]


def _folder(routine):
    import swarm
    if routine["project"]:
        return swarm.project_cwd(config_store.load()["projects"][routine["project"]])
    return Path(routine["folder"]).expanduser() if routine["folder"] else None


def fire(routine_id, trigger="manual", payload="", force=False):
    """Run a routine now, subject to its policies (`force` ignores only_if_changed).
    Returns the run row."""
    import swarm
    import team
    routine = get(routine_id)
    if routine["status"] != "active" and trigger != "manual":
        raise RoutineError(f"Routine #{routine_id} is {routine['status']}")
    sync_runs()
    running = _running(routine["id"])
    if running and routine["concurrency"] == "skip":
        with hub.connect() as conn:
            conn.execute("UPDATE routine_runs SET coalesced = coalesced + 1 WHERE id = ?", (running["id"],))
        run_id = _add_run(routine, trigger, "coalesced", f"run #{running['id']} was still going", payload)
        return get_run(run_id)
    fingerprint = None
    if routine["only_if_changed"] == "git":
        folder = _folder(routine)
        fingerprint = git_fingerprint(folder) if folder else None
        if fingerprint and fingerprint == routine["last_fingerprint"] and trigger == "schedule" and not force:
            run_id = _add_run(routine, trigger, "skipped", "nothing changed in the repository", payload)
            return get_run(run_id)
    task = routine["instructions"] + (f"\n\n## Trigger payload\n{payload[:3000]}" if payload else "")
    intro = (f"Routine #{routine['id']} \"{routine['title']}\" ({describe(routine)}) runs you: "
             "one job, no pipeline.")
    try:
        task_id, agent_id, _ = swarm.run_agent(
            routine["agent"], task, project_id=routine["project"], folder=routine["folder"],
            from_agent=config_store.ORCHESTRATOR_ID if routine["report"] == "lago" else None,
            intro=intro)
    except team.TeamError as error:
        run_id = _add_run(routine, trigger, "failed", str(error), payload)
        _tell_failure(routine, str(error))
        return get_run(run_id)
    with hub.connect() as conn:
        conn.execute("UPDATE routines SET last_fingerprint = COALESCE(?, last_fingerprint) WHERE id = ?",
                     (fingerprint, routine["id"]))
    run_id = _add_run(routine, trigger, "running", "", payload, task_id, agent_id)
    activity.log("system", "routine.fired", f"#{routine['id']} {routine['title']} ({trigger}) as {agent_id}",
                 target=f"routine #{routine['id']}", project=routine["project"] or None)
    return get_run(run_id)


def get_run(run_id):
    with hub.connect() as conn:
        return dict(conn.execute("SELECT * FROM routine_runs WHERE id = ?", (run_id,)).fetchone())


def _tell_failure(routine, why):
    import notify
    try:
        notify.tell_orchestrator(f"[Lago] Routine #{routine['id']} \"{routine['title']}\" failed: {why}",
                                 start=False)
    except Exception:  # telling is a courtesy; the run row records it
        pass


def sync_runs():
    """Close runs whose task finished; tell Lago about failures of 'failures'-report routines."""
    with hub.connect() as conn:
        open_runs = [dict(r) for r in conn.execute(
            "SELECT r.*, t.status AS task_status, t.result AS task_result, o.report AS report, "
            "o.title AS title FROM routine_runs r JOIN tasks t ON t.id = r.task_id "
            "JOIN routines o ON o.id = r.routine_id WHERE r.status = 'running'")]
    for run in open_runs:
        if run["task_status"] in hub.OPEN_TASK:
            continue
        status = "done" if run["task_status"] == "done" else "failed"
        with hub.connect() as conn:
            conn.execute("UPDATE routine_runs SET status = ?, finished_at = ? WHERE id = ? AND status = 'running'",
                         (status, time.time(), run["id"]))
        if status == "failed" and run["report"] == "failures":
            _tell_failure({"id": run["routine_id"], "title": run["title"]}, run["task_result"][:500])


def fire_due(now=None):
    """Fire each active routine whose time came. Returns the runs started or skipped."""
    if not features.on(config_store.load(), "routines"):
        return []
    now = now or time.time()
    with hub.connect() as conn:
        due = [dict(r) for r in conn.execute(
            "SELECT * FROM routines WHERE status = 'active' AND next_at IS NOT NULL AND next_at <= ?",
            (now,))]
    fired = []
    for routine in due:
        later = schedules.next_after(routine["next_at"], routine["repeat"], routine["every_minutes"], now)
        with hub.connect() as conn:
            claimed = conn.execute("UPDATE routines SET next_at = ? WHERE id = ? AND next_at = ?",
                                   (later, routine["id"], routine["next_at"])).rowcount
        if not claimed:
            continue
        missed = now - routine["next_at"] > schedules.LATE_SECONDS
        if missed and routine["catch_up"] == "skip":
            fired.append(get_run(_add_run(routine, "schedule", "skipped",
                                          "missed while Lago Agents was closed")))
            continue
        try:
            fired.append(fire(routine["id"], "schedule"))
        except RoutineError as error:
            fired.append(get_run(_add_run(routine, "schedule", "failed", str(error))))
    return fired


def start_firing():
    import logging
    import threading
    log = logging.getLogger("lago.routines")

    def loop():
        while True:
            try:
                fire_due()
                sync_runs()
            except Exception:  # a failed check must not end the loop
                log.exception("checking routines failed")
            time.sleep(CHECK_SECONDS)
    threading.Thread(target=loop, name="routines", daemon=True).start()
