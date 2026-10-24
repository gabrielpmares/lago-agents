"""Hold a whole work session: stop its Conductor and every agent it woke at once, and carry on
later.

hold() interrupts whatever each of them is doing (Esc, as you would in its terminal), and
keeps new messages and pipeline steps from reaching them: team.pump() and swarm.run_step()
check held(). Their conversations stay. release() lets the messages through again and tells
each interrupted agent to carry on where it stopped.
"""

import time

import activity
import config_store
import hub
import runner

RESUME_TEXT = "[Lago] The human paused this work session and has now resumed it. Carry on where you stopped."


class HoldError(Exception):
    pass


def _workspace_of(agent_id):
    import budgets
    return budgets.workspace_of(agent_id)


def held(agent_id):
    """True when the agent belongs to a work session that's on hold."""
    workspace = _workspace_of(agent_id)
    if workspace is None:
        return False
    ws = hub.get_workspace(workspace)
    return bool(ws and ws.get("held_at"))


def members(workspace_id):
    """The Conductor and every agent the session woke."""
    conductor = config_store.conductor_id(workspace_id)
    return [conductor] + [r["agent_id"] for r in hub.swarm_agents() if r["workspace"] == workspace_id]


def hold(workspace_id, reason=""):
    """Put the session on hold. Returns the agents that were interrupted."""
    ws = hub.get_workspace(int(workspace_id))
    if ws is None:
        raise HoldError(f"There's no work session #{workspace_id}")
    if ws["status"] == "closed":
        raise HoldError(f"Work session #{ws['id']} is closed")
    if ws.get("held_at"):
        raise HoldError(f"Work session #{ws['id']} is already on hold")
    hub.update_workspace(ws["id"], held_at=time.time())
    interrupted = []
    for agent_id in members(ws["id"]):
        if not runner.is_running(agent_id):
            continue
        if hub.get_state(agent_id)["status"] in ("working", "waiting_on_team"):
            runner.keys(agent_id, "C-c" if hub.get_state(agent_id).get("account") == "codex" else "Escape")
            hub.set_state(agent_id, status="idle", detail="On hold")
            interrupted.append(agent_id)
    activity.log(activity.actor(), "session.held", f"#{ws['id']} {ws['title']}" + (f": {reason}" if reason else ""),
                 target=f"session #{ws['id']}", workspace=ws["id"], project=ws["project"] or None)
    return interrupted


def release(workspace_id):
    """Take the session off hold and let its agents carry on. Returns the agents told."""
    import team
    ws = hub.get_workspace(int(workspace_id))
    if ws is None:
        raise HoldError(f"There's no work session #{workspace_id}")
    if not ws.get("held_at"):
        raise HoldError(f"Work session #{ws['id']} isn't on hold")
    hub.update_workspace(ws["id"], held_at=None)
    told = []
    for agent_id in members(ws["id"]):
        if not runner.is_running(agent_id):
            continue
        if hub.get_state(agent_id).get("detail") == "On hold" or hub.has_inbox(agent_id):
            if hub.get_state(agent_id).get("detail") == "On hold":
                hub.queue_text(agent_id, RESUME_TEXT)
                hub.set_state(agent_id, detail="")
            team.pump(agent_id, auto_start=False)
            told.append(agent_id)
    activity.log(activity.actor(), "session.released", f"#{ws['id']} {ws['title']}",
                 target=f"session #{ws['id']}", workspace=ws["id"], project=ws["project"] or None)
    return told
