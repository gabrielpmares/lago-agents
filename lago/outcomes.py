"""Points for whole jobs: your verdict on a work session's result, and test drives.

When you accept a session's result, every agent that did one of its steps earns a share of
OUTCOME_WEIGHT, split by how its own step went: a step the Conductor rated up earns a full
share, an unrated one half, and one rated down (or that failed) earns nothing and loses points
instead. When you reject the result, the steps that failed or were rated down lose the points;
if none were, every step does. You decide what success is, not the agents. Judging again
replaces the earlier verdict.

A test drive gives the same task to a few agents at once, each in its own scratch folder. The
one you pick earns a small rating (evals.WEIGHT["pick"]); the others lose very little, since
losing a comparison isn't failing and the pick is often taste. Each counts once per drive.

Both are ratings in agent_ratings (raters "outcome" and "pick"), so they flow into
evals.score() with the rest.
"""

import activity
import config_store
import evals
import hub
import swarm
from team import TeamError

OUTCOME_WEIGHT = evals.WEIGHT["outcome"]
SHARE = {1: 1.0, 0: 0.5}          # a step's share by the Conductor's (or your) verdict on it
PICK_WEIGHT = evals.WEIGHT["pick"]
LOSER_WEIGHT = 0.1
DRIVE_MIN, DRIVE_MAX = 2, 3
DRIVES_DIR = config_store.SESSIONS_DIR / "drives"


# ---------------------------------------------------------------- a session's whole result

def _step_verdict(task_id):
    """1, -1 or 0: your rating of the step if you gave one, else the Conductor's."""
    rows = {r["rater"]: r["verdict"] for r in hub.ratings("task_id = ?", (task_id,))}
    return rows.get("human", rows.get("conductor", 0))


def _steps(workspace_id):
    """[(task id, status, verdict)] for every step of the session's finished plans that ran."""
    found = []
    for pipeline in hub.pipelines("workspace = ? AND status IN ('done', 'failed')",
                                  (workspace_id,), limit=50):
        for step in pipeline["steps"]:
            if step.get("task_id") and step["status"] in ("done", "failed"):
                found.append((step["task_id"], step["status"], _step_verdict(step["task_id"])))
    return found


def shares(steps, accepted):
    """{task id: (verdict, weight)}: how the whole job's points are split between its steps."""
    bad = {t for t, status, verdict in steps if status == "failed" or verdict < 0}
    if not accepted:
        blamed = bad or {t for t, _, _ in steps}
        return {t: (-1, OUTCOME_WEIGHT / len(blamed)) for t in blamed}
    good = {t: SHARE[verdict] for t, _, verdict in steps if t not in bad}
    total = sum(good.values())
    split = {t: (1, OUTCOME_WEIGHT * share / total) for t, share in good.items()}
    split.update({t: (-1, OUTCOME_WEIGHT / len(steps)) for t in bad})
    return split


def judge_session(workspace_id, accepted, reason=""):
    """Your verdict on a session's whole result. Returns a line saying who got what."""
    workspace = hub.get_workspace(int(workspace_id))
    if workspace is None:
        raise TeamError(f"There's no work session #{workspace_id}.")
    steps = _steps(workspace["id"])
    if not steps:
        raise TeamError(f"Session #{workspace['id']} has no finished steps to judge yet.")
    reason = " ".join((reason or "").split())[:600]
    if not accepted and not reason:
        raise TeamError("Say what's wrong with the result: the agents learn from the reason.")
    for task_id, _, _ in steps:  # judging again replaces the earlier split
        hub.unrate(task_id, "outcome")
    note = ("You accepted the whole result" if accepted else "You rejected the whole result") \
        + (f": {reason}" if reason else ".")
    earned, lost = [], []
    for task_id, (verdict, weight) in shares(steps, accepted).items():
        catalog_id, version = evals._task_agent(task_id)
        hub.rate(catalog_id, task_id, "outcome", verdict, note, version, round(weight, 3))
        (earned if verdict > 0 else lost).append(catalog_id)
    hub.update_workspace(workspace["id"], outcome="accepted" if accepted else "rejected")
    activity.log(activity.actor(), "session.accepted" if accepted else "session.rejected",
                 f"#{workspace['id']} {workspace['title']}" + (f": {reason}" if reason else ""),
                 target=f"session #{workspace['id']}", workspace=workspace["id"],
                 project=workspace["project"] or None)
    line = f"Session #{workspace['id']} {'accepted' if accepted else 'rejected'}."
    if earned:
        line += f" Points to {', '.join(sorted(set(earned)))}."
    if lost:
        line += f" Points off {', '.join(sorted(set(lost)))}."
    for catalog_id in set(lost):
        evals.maybe_improve(catalog_id)
    return line


# ---------------------------------------------------------------- test drives

def start_drive(agents, task, project="", from_agent=config_store.ORCHESTRATOR_ID):
    """Run `task` with each of `agents` (2 or 3), each in its own scratch folder. Returns the
    drive id and {catalog id: task id}."""
    agents = list(dict.fromkeys(a.strip().strip("`") for a in agents or [] if a.strip()))
    if not DRIVE_MIN <= len(agents) <= DRIVE_MAX:
        raise TeamError(f"A test drive compares {DRIVE_MIN} or {DRIVE_MAX} different agents.")
    if not (task or "").strip():
        raise TeamError("Say what the agents should do.")
    enabled = swarm.enabled_agents(config_store.load())
    missing = [a for a in agents if a not in enabled]
    if missing:
        raise TeamError(f"Not switched-on swarm agents: {', '.join(missing)}. find_agents lists them.")
    drive_id = hub.create_drive(task.strip(), (project or "").strip(), {})
    intro = (f"Test drive #{drive_id}: {len(agents)} agents get this same task and the human "
             "picks the best result. Work only in your current folder, and finish with your "
             "full result.\n\n")
    started = {}
    for catalog_id in agents:
        folder = DRIVES_DIR / str(drive_id) / catalog_id
        folder.mkdir(parents=True, exist_ok=True)
        task_id, _, _ = swarm.run_agent(catalog_id, intro + task.strip(), folder=str(folder),
                                        from_agent=from_agent)
        started[catalog_id] = task_id
    hub.update_drive(drive_id, tasks=started)
    return drive_id, started


def _drive(drive_id):
    drive = hub.get_drive(int(drive_id))
    if drive is None:
        raise TeamError(f"There's no test drive #{drive_id}.")
    return drive


def drive_text(drive_id):
    """Where a test drive stands: each agent's task and whether it finished."""
    drive = _drive(drive_id)
    lines = [f"Test drive #{drive['id']} ({drive['status']}"
             + (f", picked `{drive['picked']}`" if drive["picked"] else "") + f"): {drive['task'][:200]}"]
    for catalog_id, task_id in drive["tasks"].items():
        task = hub.get_task(task_id) or {}
        lines.append(f"- `{catalog_id}`: task #{task_id}, {task.get('status', 'unknown')}")
    return "\n".join(lines)


def pick(drive_id, winner, reason=""):
    """Record which agent's result you liked best: a small up for it, a tiny down for the
    others. Picking again replaces the earlier pick."""
    drive = _drive(drive_id)
    winner = (winner or "").strip().strip("`")
    if drive["status"] == "cancelled":
        raise TeamError(f"Test drive #{drive['id']} was cancelled.")
    if winner not in drive["tasks"]:
        raise TeamError(f"`{winner}` wasn't in test drive #{drive['id']}: "
                        f"{', '.join(drive['tasks'])}.")
    unfinished = [c for c, t in drive["tasks"].items()
                  if (hub.get_task(t) or {}).get("status") not in ("done", "failed")]
    if unfinished:
        raise TeamError(f"Still running: {', '.join(unfinished)}. Pick once every result is in.")
    reason = " ".join((reason or "").split())[:600]
    for catalog_id, task_id in drive["tasks"].items():
        won = catalog_id == winner
        _, version = evals._task_agent(task_id)
        hub.rate(catalog_id, task_id, "pick", 1 if won else -1,
                 f"Test drive #{drive['id']}: " + ("picked" if won else f"`{winner}` was picked")
                 + (f" ({reason})" if reason else ""), version,
                 PICK_WEIGHT if won else LOSER_WEIGHT)
    hub.update_drive(drive["id"], status="picked", picked=winner, reason=reason)
    activity.log(activity.actor(), "drive.picked", f"#{drive['id']} {winner}",
                 target=f"test drive #{drive['id']}", project=drive["project"] or None)
    return f"Picked `{winner}` in test drive #{drive['id']}: a small rating up for it."
