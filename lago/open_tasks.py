"""Where each open run stands, in plain words, and what you (or Lago, on your word) can do
about it: carry on or abandon.

A run is an agent Lago or New Task started directly (`r<N>-<agent>`, no work session) with
one task. Its task can sit open after the agent stopped: a usage limit it's waiting out,
the app closed under it, a stop from the app. standing() says which, so nothing looks
"Working" when nobody is; continue_run() and abandon() are the two ways out.
"""

import time

import accounts
import activity
import config_store
import hub
import runner
import team
import transcripts

QUIET_SECONDS = 2 * 3600  # running, but its log hasn't moved this long
CONTINUE_TEXT = ("[Lago] You stopped before finishing your task. Carry on where you left off, "
                 "and finish it with complete_task.")
ABANDONED = "Abandoned"


def _clock(at):
    return time.strftime("%H:%M" if at - time.time() < 86400 else "%a %H:%M", time.localtime(at))


def _last_words(state, limit=240):
    path = transcripts.find(state["session_id"]) if state.get("session_id") else None
    text = (transcripts.last_assistant_text(path) if path else "") or ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _log_age(state, now):
    path = transcripts.find(state["session_id"]) if state.get("session_id") else None
    return now - path.stat().st_mtime if path and path.exists() else None


def standing(task, state=None, running=None, now=None, room_at=None):
    """{"state", "says", "last", "can_continue"} for an open task's run. `state` is one of
    working, quiet, waiting_usage, ready (usage has room again), needs_you, stopped, queued,
    blocked. `room_at`: when an
    account has room again by the last usage readings (accounts.room_again_at)."""
    now = now or time.time()
    agent_id = task["to_agent"]
    state = state if state is not None else hub.get_state(agent_id)
    is_running = agent_id in running if running is not None else runner.is_running(agent_id)
    status = state.get("status")
    out = {"last": _last_words(state), "can_continue": False}
    if task["status"] == "blocked":
        return {**out, "state": "blocked", "says": "Waiting for other tasks to finish first."}
    if task["status"] in ("queued", "sent") and not is_running:
        return {**out, "state": "queued", "says": "Not started yet.", "can_continue": True}
    if state.get("usage_wait"):
        if room_at is not None and room_at <= max(now, time.time()):  # "now" is read after `now`
            return {**out, "state": "ready", "can_continue": True,
                    "says": "It stopped when usage ran out. There's room again now, so it can carry on."}
        return {**out, "state": "waiting_usage", "can_continue": True,
                "says": f"Out of usage: it carries on by itself at {_clock(state['usage_wait'])}."}
    if not is_running:
        since = state.get("updated_at")
        when = f" {_ago(now - since)} ago" if since else ""
        return {**out, "state": "stopped", "can_continue": True,
                "says": f"Stopped{when} before finishing. Nobody is working on it."}
    if status in ("needs_approval", "needs_input", "needs_you"):
        return {**out, "state": "needs_you", "says": state.get("detail") or "It's waiting for an answer."}
    age = _log_age(state, now)
    if age is not None and age > QUIET_SECONDS:
        return {**out, "state": "quiet", "can_continue": True,
                "says": f"Running, but it hasn't done anything for {_ago(age)}."}
    return {**out, "state": "working", "says": state.get("detail") or "Working on it."}


def _ago(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"


def runs(now=None):
    """Every open task of a direct run, oldest first, with its standing."""
    now = now or time.time()
    running, states = set(runner.running_agents()), hub.all_states()
    room_at = accounts.room_again_at(config_store.load())
    out = []
    for row in hub.swarm_agents():
        agent_id = row["agent_id"]
        if row["workspace"] or not config_store.RUN_ID.match(agent_id):
            continue
        for task in hub.open_tasks_for(agent_id):
            out.append({"task_id": task["id"], "title": task["title"], "agent_id": agent_id,
                        "name": row["name"].split(" · ")[0], "started_at": task["created_at"],
                        "from_agent": task["from_agent"],
                        **standing(task, states.get(agent_id) or {}, running, now, room_at)})
    return sorted(out, key=lambda r: r["started_at"])


def _open_task(task_id):
    task = hub.get_task(int(task_id))
    if task is None:
        raise team.TeamError(f"There's no task #{task_id}")
    if task["status"] not in hub.OPEN_TASK:
        raise team.TeamError(f"Task #{task_id} is already {task['status']}")
    return task


def continue_run(task_id, actor=None):
    """Get a stopped (or stuck) run going again on its own conversation. Returns a sentence."""
    task = _open_task(task_id)
    agent_id = task["to_agent"]
    if task["status"] == "blocked":
        raise team.TeamError(f"Task #{task_id} waits for other tasks; it starts when they finish")
    config = config_store.load()
    if accounts.with_room_now(config) is None:
        at = accounts.room_again_at(config)
        raise team.TeamError("Every account is out of usage" + (f" until {_clock(at)}" if at else "")
                             + ": it can't carry on before then")
    hub.set_state(agent_id, usage_wait=None)
    if runner.is_running(agent_id):
        hub.queue_text(agent_id, CONTINUE_TEXT)
        team.pump(agent_id, auto_start=False)
    else:
        team.start(agent_id, initial_text=CONTINUE_TEXT, keep_session=True)
    activity.log(actor or activity.actor(), "task.continued", f"#{task_id} {task['title']}", target=agent_id)
    return f"Task #{task_id} carries on ({team.label(config, agent_id)})."


def abandon(task_id, reason="", actor=None):
    """Stop the run and close its task as cancelled, with why. Returns a sentence."""
    task = _open_task(task_id)
    agent_id = task["to_agent"]
    if config_store.RUN_ID.match(agent_id) and runner.is_running(agent_id):
        team.stop(agent_id)
    hub.set_state(agent_id, usage_wait=None)
    who = "Lago, on your word" if (actor or activity.actor()) == config_store.ORCHESTRATOR_ID else "you"
    team.finish(task["id"], f"{ABANDONED} by {who}" + (f": {reason.strip()}" if reason.strip() else "."),
                status="cancelled")
    return f"Task #{task_id} abandoned."


def brief(now=None):
    """Lago's lines about open runs, for his wake-up brief and his open_tasks tool."""
    rows = runs(now)
    if not rows:
        return "No runs are open."
    lines = []
    for r in rows:
        line = f"- Task #{r['task_id']} \"{r['title']}\" ({r['name']}, `{r['agent_id']}`): {r['says']}"
        if r["last"]:
            line += f" Its last words: \"{r['last']}\""
        lines.append(line)
    return "\n".join(lines)
