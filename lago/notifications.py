"""What needs you, for the bell at the top of the app: each item says what to decide and
links to the page where you decide it.

Items come from live state, so they leave the list once dealt with:
- a plan waiting for approval (until approved, changed or rejected);
- a session asking permission or a question in its terminal, waiting for your reply, or
  crashed (until its status changes);
- what a work session reported that needs an answer (a question, a result to check, being
  blocked, waiting, a failure), until someone (you or Lago) messages that session;
- a finished result you haven't seen yet;
- a session whose whole task is done, until you close it (or it reports something newer);
- a decision Lago took for you in Decide mode, until you look at it (not urgent);
- a session that used its budget and stopped (until you extend or stop it);
- an account that ran out of usage in the last ALERT_HOURS.
Dismissing one hides it; its key includes what changed, so the next one shows again.
"""

import time

import config_store
import hub

ASK_KINDS = ("question", "gate", "blocked", "waiting", "failed")
AGENT_KINDS = {"needs_approval": "permission", "needs_input": "input", "needs_you": "reply",
               "over_budget": "budget", "exited": "crashed"}
ALERT_HOURS = 12
FORGET_DISMISSED_DAYS = 30


def build(config, agents_live, alert=None, now=None):
    """[{"key", "kind", "title", "context", "text", "href", "at", "urgent"}], most urgent and
    newest first. `context` names the session or plan it's about."""
    now = now or time.time()
    items = []
    sessions = {ws["id"]: ws for ws in hub.workspaces("status = 'open'")}
    for pipeline in hub.pipelines("status = 'draft'"):
        items.append(_item(f"pipeline:{pipeline['id']}", "plan",
                           f"Plan #{pipeline['id']} waiting for your approval", pipeline["title"],
                           pipeline.get("goal") or "", f"#pipeline/{pipeline['id']}", pipeline["updated_at"],
                           project=pipeline.get("project")))
    states = hub.all_states()
    for agent_id, live in agents_live.items():
        kind = AGENT_KINDS.get(live["status"])
        agent = config["agents"].get(agent_id) or {}
        if not kind or (agent.get("workspace") and agent["workspace"] not in sessions):
            continue
        review = (states.get(agent_id) or {}).get("lago_review")
        if kind == "permission" and review and now - review < config_store.LAGO_REVIEW_SECONDS:
            continue  # Lago is deciding it; you're asked only if he passes it on or runs out of time
        name, context, href = _where(agent_id, agent, sessions)
        title = {"permission": f"{name} asks permission", "input": f"{name} is asking something",
                 "reply": f"{name} is waiting for your reply", "budget": f"{name} used its budget",
                 "crashed": f"{name} stopped unexpectedly"}[kind]
        items.append(_item(f"agent:{agent_id}:{live['status']}:{int(live.get('since') or 0)}", kind,
                           title, context, live.get("detail") or "", href, live.get("since") or now,
                           project=agent.get("project") or (sessions.get(agent.get("workspace")) or {}).get("project")))
    for event in hub.events("created_at > ?", (now - 7 * 86400,), limit=200):
        session = sessions.get(event["workspace"])
        if not session:
            continue
        if event["kind"] in ASK_KINDS:
            prompted = (states.get(config_store.conductor_id(session["id"])) or {}).get("prompted_at") or 0
            if prompted > event["created_at"]:
                continue  # the session got a message since: answered
        elif not (event["kind"] == "solved" or (event["kind"] == "done" and not event["seen"]
                                                  and not session.get("quiet"))):
            continue
        items.append(_item(f"event:{event['id']}", event["kind"], _event_title(event["kind"], session),
                           session["title"], event["text"], f"#work/{session['id']}", event["created_at"],
                           project=session.get("project")))
    for event in hub.events("kind = 'decided' AND seen = 0 AND created_at > ?", (now - 3 * 86400,), limit=30):
        session = sessions.get(event["workspace"]) or hub.get_workspace(event["workspace"]) or {"title": ""}
        items.append(_item(f"decided:{event['id']}", "decided", "Lago decided for you",
                           session["title"], event["text"], f"#work/{event['workspace']}", event["created_at"]))
    for event in hub.events("workspace IS NULL AND kind = 'news' AND seen = 0 AND created_at > ?",
                            (now - 3 * 86400,), limit=20):
        items.append(_item(f"event:{event['id']}", "news", event["text"].split("\n")[0][:120],
                           "Lago", event["text"], "#orchestrator", event["created_at"]))
    import secret_requests
    for req in secret_requests.requests("pending"):
        items.append(_item(f"secret:{req['id']}", "secret", f"{req['agent_name']} asks for {req['env']}",
                           "A secret", req["reason"], "#permissions", req["created_at"], project=req["project"]))
    import agent_drafts
    for draft in agent_drafts.drafts("ready"):
        origin = f"from {draft['repo']}" if draft["repo"] else f"proposed by {draft.get('from_agent') or 'an agent'}"
        items.append(_item(f"draft:{draft['id']}", "plan", f"New agent {origin} to approve",
                           "Swarm", draft["agent_summary"], "#swarm", draft["created_at"]))
    import skill_sources
    for draft in skill_sources.drafts("ready"):
        items.append(_item(f"skill:{draft['id']}", "plan", f"New skill {draft['name']} to review",
                           "Swarm", f"From {draft['repo']} at {draft['commit'][:10]}", "#swarm",
                           draft["created_at"]))
    import evals
    for proposal in evals.ready_proposals():
        items.append(_item(f"proposal:{proposal['id']}", "plan",
                           f"A better prompt for {proposal['catalog_id']} passed its tests",
                           "Swarm", proposal["reason"], f"#swarm/{proposal['catalog_id']}",
                           proposal["updated_at"]))
    for proposal in hub.proposals("status = 'failed' AND updated_at > ?", (now - 3 * 86400,)):
        items.append(_item(f"proposal:{proposal['id']}:failed", "done",
                           f"A rewrite of {proposal['catalog_id']} didn't pass its tests",
                           "Swarm", "It keeps its current prompt.", f"#swarm/{proposal['catalog_id']}",
                           proposal["updated_at"]))
    if alert and alert.get("kind") in ("login", "out") and _alert_at(alert) > now - ALERT_HOURS * 3600:
        items.append(_item(f"account:{alert['id']}", "account", "An account ran out of usage", "",
                           alert["text"], f"#accounts/login/{alert['account']}" if alert["kind"] == "login"
                           else "#accounts", _alert_at(alert)))
    hidden = _dismissed()
    items = [i for i in _newest_per_session(items) if i["key"] not in hidden]
    return sorted(items, key=lambda i: (not i["urgent"], -(i["at"] or 0)))


def _item(key, kind, title, context, text, href, at, project=None):
    return {"key": key, "kind": kind, "title": title, "context": context,
            "text": " ".join(str(text).split())[:240], "href": href, "at": at, "project": project or "",
            "urgent": kind not in ("done", "solved", "news", "decided")}


def route(items, viewer, handlers):
    """Who each item is for. A project with a handler (`handler` in its settings: a member's
    login) sends what needs someone to that person: it stays urgent for them and shows,
    calmer and labelled, for everyone else. `viewer` is a login, or "owner"."""
    for item in items:
        handler = handlers.get(item.get("project") or "")
        if not handler:
            continue
        item["for"] = handler
        if viewer != handler:
            item["urgent"] = False
    return sorted(items, key=lambda i: (not i["urgent"], -(i["at"] or 0)))


def _where(agent_id, agent, sessions):
    """(who, which session, link) for an agent."""
    if agent_id == config_store.ORCHESTRATOR_ID:
        return "Lago", "", "#orchestrator"
    session = sessions.get(agent.get("workspace"))
    if session:
        who = "Session" if agent.get("conductor") else f"{agent.get('name', agent_id)} in session"
        return f"{who} #{session['id']}", session["title"], f"#work/{session['id']}"
    return agent.get("name", agent_id), "", f"#agent/{agent_id}"


def _event_title(kind, session):
    what = {"question": "has a question", "gate": "has a result to check", "blocked": "is blocked",
            "waiting": "is waiting for you", "failed": "failed", "done": "finished a stage",
            "solved": "is fully done: close it?"}[kind]
    return f"Session #{session['id']} {what}"


def _newest_per_session(items):
    """One report per session, the newest (it says what's current), and no "waiting for your
    reply" for a session whose report already says what it waits for."""
    reported, out = set(), []
    for item in sorted(items, key=lambda i: -(i["at"] or 0)):
        if item["key"].startswith("event:"):
            if item["href"] in reported:
                continue
            reported.add(item["href"])
        out.append(item)
    return [i for i in out if not (i["kind"] == "reply" and i["href"] in reported)]


def _alert_at(alert):
    try:
        return float(alert["id"])
    except (KeyError, TypeError, ValueError):
        return 0


def _dismissed():
    with hub.connect() as conn:
        conn.execute("DELETE FROM dismissed WHERE at < ?", (time.time() - FORGET_DISMISSED_DAYS * 86400,))
        return {row["key"] for row in conn.execute("SELECT key FROM dismissed")}


def dismiss(key):
    with hub.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO dismissed (key, at) VALUES (?, ?)", (str(key), time.time()))


def opened(key):
    """You followed a notification: a finished result counts as seen."""
    if str(key).startswith(("event:", "decided:")):
        with hub.connect() as conn:
            conn.execute("UPDATE events SET seen = 1 WHERE id = ? AND kind IN ('done', 'decided')",
                         (int(str(key).split(":")[1]),))


def session_status(ws, live, states=None):
    """What a work session as a whole is doing, as one status. Its Conductor's own status
    ("idle") would hide agents working for it, a plan or a question waiting for you, or a
    finished task. `live` maps agent ids to their live status."""
    cid = config_store.conductor_id(ws["id"])
    conductor = live.get(cid, "stopped")
    agents = [st for aid, st in live.items() if aid.startswith(cid + "-")]
    if conductor in ("needs_approval", "needs_input", "over_budget", "exited", "starting"):
        return conductor
    for status in ("needs_approval", "over_budget", "needs_input"):
        if status in agents:
            return status
    if conductor in ("working", "waiting_on_team"):
        return conductor
    if any(st in ("working", "waiting_on_team", "starting") for st in agents):
        return "agents_working"
    if hub.pipelines("workspace = ? AND status = 'draft'", (ws["id"],), limit=1):
        return "plan_waiting"
    event = hub.events("workspace = ? AND kind NOT IN ('update', 'decided')", (ws["id"],), limit=1)
    if event:
        prompted = ((states if states is not None else hub.all_states()).get(cid) or {}).get("prompted_at") or 0
        if event[0]["kind"] == "solved":
            return "solved"
        if event[0]["kind"] in ASK_KINDS and prompted < event[0]["created_at"]:
            return "needs_you"
    return conductor


def open_sessions(agents_live):
    """The open work sessions, for the tabs at the top: [{"id", "title", "project", "status"}]."""
    live = {aid: l["status"] for aid, l in agents_live.items()}
    states = hub.all_states()
    return [{"id": ws["id"], "title": ws["title"], "project": ws["project"],
             "status": session_status(ws, live, states)}
            for ws in sorted(hub.workspaces("status = 'open'"), key=lambda w: w["id"])]
