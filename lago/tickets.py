"""Tasks as tickets: comment threads, "blocked by" links and labels.

- **Comments**: anyone on a task (you, the lead who handed it out, the agent doing it) can
  comment. A comment reaches the other side as a message: the agent doing an open task, or
  the lead (or you) when the agent comments. Your comment on a finished task can reopen it.
- **Blocked by**: a task handed out with `blocked_by` (task ids) waits as `blocked`, without
  reaching its agent, until every task it waits on is done; then it goes out by itself. If one
  it waits on fails or is cancelled, whoever handed it out is told.
- **Labels**: short tags on a task, for finding and filtering.
"""

import json
import time

import activity
import config_store
import hub

MAX_LABELS = 8


class TicketError(Exception):
    pass


def _task(task_id):
    task = hub.get_task(int(task_id))
    if task is None:
        raise TicketError(f"There's no task #{task_id}")
    return task


def blocked_by(task):
    return json.loads(task.get("blocked_by") or "[]")


def labels(task):
    return json.loads(task.get("labels") or "[]")


def clean_labels(values):
    out = []
    for value in values or []:
        label = "-".join(str(value).lower().split())[:24]
        if label and label not in out:
            out.append(label)
    if len(out) > MAX_LABELS:
        raise TicketError(f"At most {MAX_LABELS} labels")
    return out


def check_blockers(ids, task_id=None):
    """The open tasks among `ids` (raises on unknown ids or a task blocking itself)."""
    waiting = []
    for dep in ids or []:
        dep = int(dep)
        if dep == task_id:
            raise TicketError("A task can't wait on itself")
        task = _task(dep)
        if task["status"] in hub.OPEN_TASK:
            waiting.append(dep)
    return waiting


def release(finished_id):
    """A task finished: send out the tasks that were waiting only on finished work, and tell
    whoever handed out a task that waits on one that failed. Returns the ids sent out."""
    import team
    finished = _task(finished_id)
    sent = []
    for task in hub.tasks("status = 'blocked' AND blocked_by LIKE ?", (f"%{finished_id}%",), limit=200):
        deps = blocked_by(task)
        if finished["id"] not in deps:
            continue
        if finished["status"] != "done":
            note = (f"[Lago] Task #{task['id']} \"{task['title']}\" waits on task #{finished['id']}, which "
                    f"{finished['status']}. It stays blocked: change what it waits on, or cancel it.")
            if task["from_agent"]:
                hub.queue_text(task["from_agent"], note)
                team.pump(task["from_agent"], auto_start=False)
            activity.log("system", "task.blocker_failed", note[7:300], target=f"task #{task['id']}")
            continue
        if all(_task(d)["status"] == "done" for d in deps):
            team.deliver(task["id"])
            sent.append(task["id"])
    return sent


def comment(task_id, text, author=None, reopen=False):
    """Add a comment and pass it on. Returns who it went to (or None)."""
    import team
    task = _task(task_id)
    text = (text or "").strip()
    if not text:
        raise TicketError("Write the comment")
    author = author or activity.actor()
    with hub.connect() as conn:
        conn.execute("INSERT INTO task_comments (task_id, author, text, created_at) VALUES (?, ?, ?, ?)",
                     (task["id"], author, text[:8000], time.time()))
    config = config_store.load()
    who = team.label(config, author if author != "you" else None)
    to = None
    if author == task["to_agent"]:
        to = task["from_agent"]  # the agent answering: back to whoever handed it out
        if to:
            hub.queue_text(to, f"[Comment on task #{task['id']} \"{task['title']}\" from {who}]\n{text}")
            team.pump(to, auto_start=False)
    elif task["status"] in ("sent", "working", "queued"):
        to = task["to_agent"]
        hub.queue_text(to, f"[Comment on your task #{task['id']} \"{task['title']}\" from {who}]\n{text}\n\n"
                       "Take it into account; reply with the comment_task tool if it needs an answer.")
        team.pump(to, auto_start=False)
    elif reopen and task["status"] in ("done", "failed"):
        hub.update_task(task["id"], status="sent", result_seen=0)
        to = task["to_agent"]
        hub.queue_text(to, f"[Lago task #{task['id']} reopened by {who}]\nTitle: {task['title']}\n\n{text}\n\n"
                       f"Your earlier result:\n{team.clip(task['result'], 1500)}\n\n"
                       + ("When you're done, end your turn with your result as your last message."
                          if (config["agents"].get(to) or {}).get("direct") else
                          f"When you're done, call complete_task with task_id {task['id']} and your result."))
        team.pump(to)
    activity.log(author, "task.commented", f"#{task['id']} {task['title']}: {text[:200]}",
                 target=f"task #{task['id']}")
    return to


def comments(task_id):
    with hub.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM task_comments WHERE task_id = ? ORDER BY id",
                                              (int(task_id),))]


def set_labels(task_id, values):
    task = _task(task_id)
    hub.update_task(task["id"], labels=json.dumps(clean_labels(values)))
    return labels(_task(task_id))


def set_blocked_by(task_id, ids):
    """Change what a blocked (or not yet started) task waits on; it goes out once nothing is open."""
    import team
    task = _task(task_id)
    if task["status"] not in ("blocked", "queued"):
        raise TicketError(f"Task #{task['id']} already went out ({task['status']})")
    ids = [int(i) for i in ids or []]
    waiting = check_blockers(ids, task["id"])
    hub.update_task(task["id"], blocked_by=json.dumps(ids), status="blocked" if waiting else task["status"])
    if not waiting:
        team.deliver(task["id"])
    return waiting
