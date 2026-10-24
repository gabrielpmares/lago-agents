"""Watchdogs: notice agents that went wrong without saying so, and tell whoever can act.

The background loop runs check() every CHECK_SECONDS. For each agent with open work it looks
for three things:

- **stalled**: marked working, but nothing has reached its transcript for `stall_minutes`
  (a turn hung on a tool, a network call, an editor waiting for input);
- **looping**: its last LOOP_REPEATS tool calls are the same call;
- **crashed**: Claude Code exited with an error while it still had an open task. The first
  crash on a task is recovered: the agent restarts, resuming its conversation with a note to
  carry on (`recover`). A second crash on the same task, or a session that was killed on
  purpose, is told instead.

Each finding is raised once per episode (alerts table), logged in the activity history and
told to the right place: the work session's Conductor (and so Lago, if he opened it), Lago
for a run he started, or the team lead for a team member. Lago and Conductors are left
alone (they wait on purpose), and so are sessions on hold.

Settings, `watchdog` in config.json: {"enabled": true, "stall_minutes": 20, "recover": true}.
"""

import time

import activity
import budgets
import config_store
import diagnostics
import holds
import hub
import runner
import transcripts

CHECK_SECONDS = 60
LOOP_REPEATS = 6
DEFAULTS = {"enabled": True, "stall_minutes": 20, "recover": True}
RECOVER_TEXT = ("[Lago] Your session crashed and was restarted, with your conversation so far. "
                "Carry on with task #{task} where you stopped; check the state of anything you were "
                "in the middle of before redoing it.")
KILLED = "The session was killed"
UNSTICK_SECONDS = 120  # marked working, but its turn ended this long ago
CRASH_GRACE_SECONDS = 90  # time for you or a restart to pick a crash up first
ORPHAN_GRACE_SECONDS = 600  # a stopped run this long, nothing queued for it: its task is over


def settings(config):
    return {**DEFAULTS, **(config.get("watchdog") or {})}


def _raised(agent_id, kind, fingerprint):
    with hub.connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO watchdog_alerts VALUES (?, ?, ?, ?)",
                           (agent_id, kind, fingerprint, time.time()))
        return cur.rowcount == 0  # already raised for this episode


def _watched(config, agent_id):
    agent = config["agents"].get(agent_id) or {}
    return agent and not agent.get("conductor") and not agent.get("orchestrator") \
        and agent_id != config_store.ORCHESTRATOR_ID


def _transcript(state):
    return transcripts.find(state["session_id"]) if state.get("session_id") else None


def stalled(state, path, stall_seconds, now):
    """Working, with nothing written to its transcript for `stall_seconds`."""
    if state.get("status") != "working" or not path:
        return False
    try:
        quiet = now - path.stat().st_mtime
    except OSError:
        return False
    return quiet >= stall_seconds and now - (state.get("updated_at") or 0) >= stall_seconds


def looping(events, repeats=LOOP_REPEATS):
    """The repeated call's summary when the last `repeats` tool calls are all the same."""
    calls = [e["text"] for e in events if e["kind"] == "tool"][-repeats:]
    if len(calls) == repeats and len(set(calls)) == 1:
        return calls[0]
    return None


def _tell(config, agent_id, kind, text):
    """Hand a finding to whoever can act on it. Returns where it went."""
    import notify
    import team
    agent = config["agents"].get(agent_id) or {}
    name = agent.get("name", agent_id)
    body = f"[Lago watchdog] {name} ({agent_id}) {text}"
    try:
        if agent.get("workspace") and hub.get_workspace(agent["workspace"]):
            # The Conductor reads it when it's next idle; you (and Lago) hear it now.
            hub.queue_text(config_store.conductor_id(agent["workspace"]), body)
            team.pump(config_store.conductor_id(agent["workspace"]), auto_start=False)
            notify.report(agent["workspace"], "blocked" if kind != "stalled" else "update", body)
            return "session"
        if config_store.RUN_ID.match(agent_id):
            notify.tell_orchestrator(body, start=False)
            return "lago"
        if agent.get("reports_to"):
            team.message(agent["reports_to"], body)
            return "lead"
    except team.TeamError:
        pass
    hub.add_event(None, None, "news", body)  # nobody else: the bell, under Lago
    return "bell"


def _recover(agent_id, task):
    """Restart a crashed agent on its conversation, told to carry on. True when it started."""
    import team
    try:
        team.start(agent_id, keep_session=True, initial_text=RECOVER_TEXT.format(task=task["id"]))
    except team.TeamError as error:
        activity.log("system", "watchdog.recover_failed", f"{agent_id}: {error}", target=agent_id)
        return False
    activity.log("system", "watchdog.recovered", f"Restarted {agent_id} after a crash on task #{task['id']}",
                 target=agent_id, workspace=budgets.workspace_of(agent_id))
    return True


def unstick(running, quiet_seconds=UNSTICK_SECONDS):
    """Agents marked working whose log says the turn ended long ago (a message lost to a prompt
    on their screen, a `!` command typed in): mark them idle and hand them what's waiting, so
    the app doesn't show them thinking forever. Lago included."""
    import team
    for agent_id in running:
        if team._stuck_working(agent_id, quiet_seconds=quiet_seconds):
            hub.set_state(agent_id, status="idle", detail="")
            team.pump(agent_id, auto_start=False)


def close_orphan_runs(running, now=None):
    """Runs Lago or the evaluations started directly whose session stopped without finishing
    their task (stopped from the app, the process ended, a budget stop never resumed). Nothing
    else closes those tasks, so the run would show as working forever. Close each as failed,
    with the run's last message as its result. Returns the task ids closed."""
    import team
    now = now or time.time()
    closed = []
    for row in hub.swarm_agents():
        agent_id = row["agent_id"]
        if row["workspace"] or not config_store.RUN_ID.match(agent_id) or agent_id in running:
            continue
        state = hub.get_state(agent_id)
        if state.get("status") not in ("stopped", "idle") or state.get("usage_wait") \
                or now - (state.get("updated_at") or now) < ORPHAN_GRACE_SECONDS \
                or hub.has_inbox(agent_id) or holds.held(agent_id):
            continue
        for task in hub.open_tasks_for(agent_id):
            if task["status"] != "working":  # queued or blocked: it hasn't started yet
                continue
            path = transcripts.find(state["session_id"]) if state.get("session_id") else None
            last = (transcripts.last_assistant_text(path) if path else "") or ""
            team.finish(task["id"], "The run stopped before it finished, so this task was closed. "
                        + (f"Its last message:\n\n{last}" if last else "It left no message."),
                        status="failed")
            activity.log("system", "watchdog.run_closed",
                         f"Closed task #{task['id']}: {agent_id} had stopped without finishing it",
                         target=agent_id)
            closed.append(task["id"])
    return closed


def check(now=None, config=None):
    """Look at every agent once. Returns the findings raised this time."""
    now = now or time.time()
    config = config or config_store.load()
    options = settings(config)
    if not options["enabled"]:
        return []
    running = set(runner.running_agents())
    unstick(running)
    close_orphan_runs(running, now)
    findings = []
    for agent_id, state in hub.all_states().items():
        if not _watched(config, agent_id) or holds.held(agent_id):
            continue
        open_tasks = hub.open_tasks_for(agent_id)
        task = open_tasks[0] if open_tasks else None
        label = f"task #{task['id']} \"{task['title']}\"" if task else "its work"
        path = _transcript(state)
        if state.get("status") == "exited" and task:
            if now - (state.get("updated_at") or now) < CRASH_GRACE_SECONDS:
                continue
            if options["recover"] and not (state.get("detail") or "").startswith(KILLED) \
                    and not _raised(agent_id, "recovered", str(task["id"])):
                if _recover(agent_id, task):
                    continue
            if not _raised(agent_id, "crashed", f"{task['id']}:{int(state.get('updated_at') or 0)}"):
                why = diagnostics.text(diagnostics.explain(path=path)) if path else ""
                findings.append((agent_id, "crashed",
                                 f"crashed while working on {label}. {state.get('detail') or ''}\n{why}".strip()))
            continue
        if agent_id not in running or not task:
            continue
        if stalled(state, path, options["stall_minutes"] * 60, now):
            fingerprint = f"{task['id']}:{int(path.stat().st_mtime)}"
            if not _raised(agent_id, "stalled", fingerprint):
                minutes = int((now - path.stat().st_mtime) // 60)
                screen = runner.capture(agent_id, lines=12).strip()
                findings.append((agent_id, "stalled",
                                 f"has shown no progress on {label} for {minutes} minutes. "
                                 f"Its screen:\n{screen[-800:]}"))
            continue
        if path and state.get("status") == "working":
            repeated = looping(transcripts.read(path)["events"])
            if repeated and not _raised(agent_id, "looping", f"{task['id']}:{repeated}"):
                findings.append((agent_id, "looping",
                                 f"seems stuck in a loop on {label}: it ran `{repeated}` "
                                 f"{LOOP_REPEATS} times in a row. Consider interrupting it "
                                 "and giving it a different approach."))
    for agent_id, kind, text in findings:
        where = _tell(config, agent_id, kind, text)
        activity.log("system", f"watchdog.{kind}",
                     f"{agent_id} {text.splitlines()[0][:300]} (told the {where})",
                     target=agent_id, workspace=(config["agents"].get(agent_id) or {}).get("workspace"))
    return findings
