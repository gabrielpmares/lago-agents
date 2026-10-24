"""Budgets: an agent that spends too much stops until you say it may carry on.

Spend is what an agent's conversations would cost at API prices (token_usage.cost). Plans
don't bill in dollars, but this weighs models and cache use roughly the way their limits do.
Two limits, set on the Accounts page (`budgets` in config.json, in dollars, 0 for none):
- agent: one conversation of one agent (a pipeline step, a run Lago started, a team member);
- session: everything in one work session, its Conductor and every agent it woke.
Lago himself has no budget: he's the one you talk to.

On top of those, budget windows (`budget_windows` in config.json) cap spend over a week or a
month: for everything, one project, or one agent (a catalog agent, all its copies):
    [{"scope": "all" | "project:<id>" | "agent:<id>", "window": "week" | "month", "limit": 50}]
A window that runs out stops the agents it covers the same way, until it's extended or the
next week or month starts.

The hooks measure a conversation's spend before its tool calls (at most every CHECK_SECONDS)
and when its turn ends. Past WARN_AT of a limit you're told once. At the limit the agent's
turn ends there and then, it shows as "Out of budget" (app, bell and phone), and new prompts
wait in its inbox. Extend gives the budget another default amount and lets the agents it
stopped carry on; Stop ends those agents and fails their open tasks, so whoever handed the
work out hears about it.
"""

import re
import time
from datetime import datetime, timedelta

import config_store
import activity
import features
import hub
import token_usage

DEFAULTS = {"agent": 10.0, "session": 40.0}
WARN_AT = 0.8
CHECK_SECONDS = 15
WORKSPACE_ID = re.compile(r"^w(\d+)(?:-|$)")
LABELS = {"agent": "conversation", "session": "work session", "window": "window"}
WINDOWS = ("week", "month")
WINDOW_EXTEND = 0.25  # Extend on a window adds this share of its limit
RESUME_TEXT = "[Lago] Your budget was extended. Carry on where you stopped."
STOPPED_RESULT = "Stopped: it used its budget before finishing."


def limits(config):
    """{"agent": dollars, "session": dollars}; 0 means no limit."""
    raw = config.get("budgets") or {}
    out = {}
    for kind, default in DEFAULTS.items():
        try:
            out[kind] = max(0.0, float(raw.get(kind, default)))
        except (TypeError, ValueError):
            out[kind] = default
    return out


def workspace_of(agent_id):
    """The work session a swarm agent (`w<N>`, `w<N>-<agent>`) belongs to, or None."""
    match = WORKSPACE_ID.match(agent_id or "")
    return int(match.group(1)) if match else None


def exempt(agent_id):
    return agent_id == config_store.ORCHESTRATOR_ID


def windows(config):
    """The valid budget windows: [{"scope", "window", "limit"}]."""
    out = []
    for raw in config.get("budget_windows") or []:
        try:
            limit = float(raw.get("limit") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        scope = str(raw.get("scope") or "all")
        if raw.get("window") in WINDOWS and limit > 0 and \
                (scope == "all" or scope.split(":", 1)[0] in ("project", "agent")):
            out.append({"scope": scope, "window": raw["window"], "limit": limit})
    return out


def window_start(window, now=None):
    """Monday 00:00 of this week, or the 1st of this month, local time."""
    day = datetime.fromtimestamp(now or time.time()).replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "week":
        day -= timedelta(days=day.weekday())
    else:
        day = day.replace(day=1)
    return day.timestamp()


def _identity(agent_id):
    """(catalog agent, project) of an agent, as far as the hub knows."""
    row = next((r for r in hub.swarm_agents() if r["agent_id"] == agent_id), None)
    if row:
        return row["catalog_id"], row["project"] or None
    workspace = workspace_of(agent_id)
    ws = hub.get_workspace(workspace) if workspace is not None else None
    return agent_id, (ws["project"] or None) if ws else None


def _applies(window, agent_id):
    if window["scope"] == "all":
        return True
    kind, value = window["scope"].split(":", 1)
    catalog, project = _identity(agent_id)
    return value == (project if kind == "project" else catalog) or (kind == "agent" and value == agent_id)


def _window_key(window, now=None):
    start = datetime.fromtimestamp(window_start(window["window"], now)).strftime("%Y-%m-%d")
    return f"window:{window['scope']}:{window['window']}:{start}"


def _scopes(agent_id, session_id, config=None):
    scopes = [(f"agent:{agent_id}:{session_id}", "agent")] if session_id else []
    workspace = workspace_of(agent_id)
    if workspace is not None:
        scopes.append((f"session:{workspace}", "session"))
    for window in windows(config or config_store.load()):
        if _applies(window, agent_id):
            scopes.append((_window_key(window), "window"))
    return scopes


def _window_of(scope, config):
    return next((w for w in windows(config) if _window_key(w) == scope), None)


def _spent(kind, agent_id, session_id, scope=None, config=None):
    if kind == "session":
        return hub.workspace_spend(workspace_of(agent_id))
    if kind == "window":
        import spending
        window = _window_of(scope, config or config_store.load())
        kind_, _, value = window["scope"].partition(":")
        return spending.window_spent(window_start(window["window"]),
                                     agent=value if kind_ == "agent" else None,
                                     project=value if kind_ == "project" else None)
    row = hub.get_spend(session_id)
    return row["cost"] if row else 0.0


def state(agent_id, session_id=None, config=None):
    """The budgets on an agent's current conversation:
    [{"scope", "kind", "spent", "limit" (0: none), "paused", "warned"}]."""
    if exempt(agent_id):
        return []
    config = config or config_store.load()
    session_id = session_id or hub.get_state(agent_id).get("session_id")
    base = limits(config)
    out = []
    for scope, kind in _scopes(agent_id, session_id, config):
        budget = hub.get_budget(scope)
        own = _window_of(scope, config)["limit"] if kind == "window" else base[kind]
        out.append({"scope": scope, "kind": kind,
                    "spent": _spent(kind, agent_id, session_id, scope, config),
                    "limit": own + budget["extra"] if own else 0.0,
                    "paused": bool(budget["paused_at"]), "warned": bool(budget["warned"])})
    return out


def session_state(workspace_id, config=None):
    """A work session's own budget: {"spent", "limit", "paused"}."""
    config = config or config_store.load()
    base = limits(config)["session"]
    budget = hub.get_budget(f"session:{workspace_id}")
    return {"spent": hub.workspace_spend(workspace_id),
            "limit": base + budget["extra"] if base else 0.0, "paused": bool(budget["paused_at"])}


def _over(budget):
    return bool(budget["limit"]) and (budget["paused"] or budget["spent"] >= budget["limit"])


def _window_name(scope):
    """"window:project:webstore:month:2026-10-01" -> "the monthly budget for project webstore"."""
    _, *middle, window, _start = scope.split(":")
    target = ":".join(middle)
    who = "everything" if target == "all" else target.replace(":", " ", 1)
    return f"the {'weekly' if window == 'week' else 'monthly'} budget for {who}"


def _reason(budget):
    if budget["kind"] == "window":
        name = _window_name(budget["scope"])
        return (f"Spending has reached ${budget['spent']:.2f} at API prices, past {name} "
                f"(${budget['limit']:.2f}). Extend the budget or stop it in Lago Agents.")
    what = "This conversation" if budget["kind"] == "agent" else \
        f"Work session #{budget['scope'].split(':')[1]}"
    return (f"{what} has cost ${budget['spent']:.2f} at API prices, past its "
            f"${budget['limit']:.2f} budget. Extend the budget or stop it in Lago Agents.")


def blocked(agent_id, session_id=None):
    """Why a budget keeps the agent from carrying on, or None. A paused budget whose limit
    you switched off lets go."""
    for budget in state(agent_id, session_id):
        if budget["paused"] and not budget["limit"]:
            hub.set_budget(budget["scope"], paused_at=None)
        elif budget["paused"]:
            return _reason(budget)
    return None


def check(agent_id, session_id, transcript_path, force=False):
    """Measure the conversation's spend (at most every CHECK_SECONDS unless `force`), warn
    or pause on its budgets, and return why the agent must stop now, or None."""
    if exempt(agent_id) or not session_id or not features.on(config_store.load(), "budgets"):
        return None
    row = hub.get_spend(session_id)
    if transcript_path and (force or not row or time.time() - row["checked_at"] >= CHECK_SECONDS):
        hub.record_spend(session_id, agent_id, workspace_of(agent_id),
                         token_usage.session_cost(transcript_path))
    config = config_store.load()
    why = None
    for budget in state(agent_id, session_id, config):
        if not budget["limit"]:
            if budget["paused"]:
                hub.set_budget(budget["scope"], paused_at=None)
        elif _over(budget):
            if not budget["paused"]:
                hub.set_budget(budget["scope"], paused_at=time.time())
                _tell(config, agent_id, budget, warning=False)
            why = why or _reason(budget)
        elif budget["spent"] >= WARN_AT * budget["limit"] and not budget["warned"]:
            hub.set_budget(budget["scope"], warned=1)
            _tell(config, agent_id, budget, warning=True)
    if why:
        hub.set_state(agent_id, status="over_budget", detail=why)
    return why


def _tell(config, agent_id, budget, warning):
    """Report it for the agent's work session (spoken, and relayed to Lago if he opened it);
    a run Lago started tells him directly. Team agents show in the bell only."""
    import notify
    import team
    name = (config["agents"].get(agent_id) or {}).get("name", agent_id)
    label = LABELS[budget["kind"]]
    if budget["kind"] == "window":
        window = _window_name(budget["scope"])
        text = (f"Spending has reached ${budget['spent']:.2f} of {window} (${budget['limit']:.2f})."
                if warning else
                f"{name} ({agent_id}) stopped: {window} (${budget['limit']:.2f}) is used up "
                f"(${budget['spent']:.2f}). Extend the budget or stop it (app action `budget`), "
                "or ask the human.")
    elif warning:
        text = (f"{name} ({agent_id}) has used ${budget['spent']:.2f} of its "
                f"${budget['limit']:.2f} {label} budget.")
    else:
        text = (f"{name} ({agent_id}) used its ${budget['limit']:.2f} {label} budget "
                f"(${budget['spent']:.2f} so far) and stopped. Extend the budget or stop it "
                "(app action `budget`), or ask the human.")
    workspace = workspace_of(agent_id)
    activity.log(agent_id, "budget.warning" if warning else "budget.hit", text, target=agent_id,
                 workspace=workspace)
    try:
        if workspace is not None and hub.get_workspace(workspace):
            notify.report(workspace, "update" if warning else "blocked", text)
        elif config_store.RUN_ID.match(agent_id) and not warning:
            notify.tell_orchestrator(f"[Lago] {text}", start=False)
    except team.TeamError:
        pass  # telling is a courtesy: the pause itself already happened


def _stopped_agents(agent_id, budgets):
    """This agent, plus every agent of its work session a session budget stopped."""
    agents = {agent_id}
    if any(b["kind"] == "session" for b in budgets):
        prefix = config_store.conductor_id(workspace_of(agent_id))
        agents |= {aid for aid, st in hub.all_states().items()
                   if (aid == prefix or aid.startswith(prefix + "-")) and st["status"] == "over_budget"}
    window_scopes = {b["scope"] for b in budgets if b["kind"] == "window"}
    if window_scopes:
        agents |= {aid for aid, st in hub.all_states().items() if st["status"] == "over_budget"
                   and window_scopes & {scope for scope, _ in _scopes(aid, st.get("session_id"))}}
    return agents


def extend(agent_id, amount=None):
    """Raise every budget that stopped this agent by another default amount (or `amount`
    dollars) past what's spent, and let the agents it stopped carry on. Returns a message."""
    import runner
    import team
    config = config_store.load()
    base = limits(config)
    raised = [b for b in state(agent_id, config=config) if _over(b)]
    if not raised:
        return "Nothing to extend: its budgets have room."
    more = float(amount) if amount else None
    if more is not None and more <= 0:
        raise ValueError("Extend by a positive amount of dollars")
    new_limits = []
    for budget in raised:
        own = _window_of(budget["scope"], config)["limit"] if budget["kind"] == "window" \
            else base[budget["kind"]]
        step = own * WINDOW_EXTEND if budget["kind"] == "window" else own
        limit = max(budget["limit"], budget["spent"]) + (more or step)
        hub.set_budget(budget["scope"], extra=limit - own, warned=0, paused_at=None)
        new_limits.append(f"{LABELS[budget['kind']]} now ${limit:.2f}")
    resumed = []
    for aid in sorted(_stopped_agents(agent_id, raised)):
        if hub.get_state(aid)["status"] != "over_budget" or blocked(aid):
            continue
        if runner.is_running(aid):
            hub.set_state(aid, status="idle", detail="")
            hub.queue_text(aid, RESUME_TEXT)
            team.pump(aid, auto_start=False)
            resumed.append(aid)
        else:
            hub.set_state(aid, status="stopped", detail="")
    activity.log(activity.actor(), "budget.extended", f"{agent_id}: {', '.join(new_limits)}",
                 target=agent_id, workspace=workspace_of(agent_id))
    return f"Extended ({', '.join(new_limits)})." + \
        (f" Carrying on: {', '.join(resumed)}." if resumed else "")


def stop(agent_id):
    """End the agents a budget stopped (this one, or every one in its work session the
    session budget stopped) and fail their open tasks. Returns a message."""
    import team
    paused = [b for b in state(agent_id) if b["paused"]]
    if not paused:
        return "Its budgets haven't stopped it."
    agents = sorted(_stopped_agents(agent_id, paused))
    for aid in agents:
        for task in hub.open_tasks_for(aid):
            team.finish(task["id"], STOPPED_RESULT, status="failed")
        team.stop(aid)
    activity.log(activity.actor(), "budget.stopped", f"Stopped {', '.join(agents)}",
                 target=agent_id, workspace=workspace_of(agent_id))
    return f"Stopped {', '.join(agents)}. Their open tasks failed."
