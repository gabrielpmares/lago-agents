"""Goals: what a project is working toward, with the work sessions that serve each one.

A goal belongs to a project (or none), may sit under a parent goal, and is active, achieved
or dropped. Work sessions link to one goal; a goal's progress is its sessions (open, solved,
closed) and its sub-goals. Conductors see their session's goal and its parents in their
opening message, so they know why the work matters.
"""

import time

import activity
import hub

STATUSES = ("active", "achieved", "dropped")


class GoalError(Exception):
    pass


def get(goal_id):
    with hub.connect() as conn:
        row = conn.execute("SELECT * FROM goals WHERE id = ?", (int(goal_id),)).fetchone()
    if row is None:
        raise GoalError(f"There's no goal #{goal_id}")
    return dict(row)


def all_goals(project=None, include_closed=False):
    where, params = ["1"], []
    if project is not None:
        where.append("project = ?")
        params.append(project)
    if not include_closed:
        where.append("status = 'active'")
    with hub.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM goals WHERE {' AND '.join(where)} ORDER BY id", params)]


def add(title, project="", description="", parent=None, target=""):
    title = " ".join(str(title or "").split())
    if not title:
        raise GoalError("Give the goal a title")
    if parent is not None:
        parent_row = get(parent)
        if (parent_row["project"] or "") != (project or ""):
            raise GoalError("A sub-goal belongs to the same project as its parent")
    now = time.time()
    with hub.connect() as conn:
        cur = conn.execute("INSERT INTO goals (project, title, description, parent_id, target, "
                           "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                           (project or "", title[:200], (description or "").strip()[:2000], parent,
                            (target or "").strip()[:40], now, now))
        goal_id = cur.lastrowid
    activity.log(activity.actor(), "goal.added", f"#{goal_id} {title}", target=f"goal #{goal_id}",
                 project=project or None)
    return get(goal_id)


def update(goal_id, **changes):
    goal = get(goal_id)
    fields = {k: v for k, v in changes.items() if k in ("title", "description", "target", "status", "parent_id")}
    if "status" in fields and fields["status"] not in STATUSES:
        raise GoalError(f"status must be one of: {', '.join(STATUSES)}")
    if fields.get("parent_id") is not None:
        if int(fields["parent_id"]) == goal["id"] or goal["id"] in _ancestors(int(fields["parent_id"])):
            raise GoalError("A goal can't sit under itself")
    if not fields:
        return goal
    fields["updated_at"] = time.time()
    with hub.connect() as conn:
        conn.execute(f"UPDATE goals SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), goal["id"]))
    if "status" in fields and fields["status"] != goal["status"]:
        activity.log(activity.actor(), f"goal.{fields['status']}", f"#{goal['id']} {goal['title']}",
                     target=f"goal #{goal['id']}", project=goal["project"] or None)
    return get(goal_id)


def _ancestors(goal_id):
    seen = []
    while goal_id is not None and goal_id not in seen:
        seen.append(goal_id)
        goal_id = get(goal_id)["parent_id"]
    return seen


def link(workspace_id, goal_id):
    """Link a work session to a goal (None unlinks)."""
    ws = hub.get_workspace(int(workspace_id))
    if ws is None:
        raise GoalError(f"There's no work session #{workspace_id}")
    if goal_id is not None:
        goal = get(goal_id)
        if goal["project"] and goal["project"] != ws["project"]:
            raise GoalError(f"Goal #{goal['id']} belongs to another project")
    hub.update_workspace(ws["id"], goal_id=goal_id)
    return ws["id"]


def progress(goal_id):
    """{"goal", "sessions": {open, solved, closed}, "children": [...], "parents": [titles]}."""
    goal = get(goal_id)
    with hub.connect() as conn:
        sessions = [dict(r) for r in conn.execute(
            "SELECT id, title, status FROM workspaces WHERE goal_id = ? ORDER BY id", (goal["id"],))]
        solved = {r["workspace"] for r in conn.execute(
            "SELECT DISTINCT workspace FROM events WHERE kind = 'solved' AND workspace IN "
            f"({','.join('?' * len(sessions)) or 'NULL'})", [s["id"] for s in sessions])}
        children = [dict(r) for r in conn.execute("SELECT * FROM goals WHERE parent_id = ? ORDER BY id",
                                                  (goal["id"],))]
    for s in sessions:
        s["solved"] = s["id"] in solved
    counts = {"open": sum(s["status"] == "open" for s in sessions),
              "solved": sum(s["solved"] for s in sessions),
              "closed": sum(s["status"] == "closed" for s in sessions)}
    parents = [get(g)["title"] for g in _ancestors(goal["parent_id"])] if goal["parent_id"] else []
    return {"goal": goal, "sessions": sessions, "counts": counts, "children": children, "parents": parents}


def session_lines(workspace):
    """The opening lines a Conductor gets about its session's goal, or []."""
    if not workspace.get("goal_id"):
        return []
    try:
        goal = get(workspace["goal_id"])
    except GoalError:
        return []
    chain = [get(g) for g in _ancestors(goal["parent_id"])] if goal["parent_id"] else []
    lines = ["", "## The goal this serves", f"{goal['title']}" + (f": {goal['description']}" if goal["description"] else "")]
    if chain:
        lines.append("Which serves: " + " → ".join(g["title"] for g in chain))
    return lines
