"""What agents have cost, by day, project and agent, from the budget hooks' `spend` table.

Each row is one agent conversation, priced at API rates (token_usage.PRICES) when its hook
last measured it; a conversation counts on the day it was last measured. Lago himself has
no budget, so his own conversations aren't measured here.
"""

import time
from collections import defaultdict
from datetime import datetime

import hub


def _agent_key(agent_id, catalog):
    """Swarm copies (`w12-implementer-2`, `r3-web-researcher`) count as their catalog agent."""
    if agent_id in catalog:
        return catalog[agent_id]
    if agent_id.startswith("w") and "-" not in agent_id:
        return "conductor"
    return agent_id


def summary(days=30, now=None):
    now = now or time.time()
    since = now - days * 86400
    with hub.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.agent_id, s.workspace, s.cost, s.checked_at, w.project "
            "FROM spend s LEFT JOIN workspaces w ON w.id = s.workspace WHERE s.checked_at >= ?",
            (since,))]
        catalog = {r["agent_id"]: r["catalog_id"]
                   for r in conn.execute("SELECT agent_id, catalog_id FROM swarm_agents")}
    by_day, by_project, by_agent, by_session = (defaultdict(float) for _ in range(4))
    for row in rows:
        cost = row["cost"] or 0
        by_day[datetime.fromtimestamp(row["checked_at"]).strftime("%Y-%m-%d")] += cost
        by_project[row["project"] if row["workspace"] is not None else "(team agents and runs)"] += cost
        by_agent[_agent_key(row["agent_id"], catalog)] += cost
        if row["workspace"] is not None:
            by_session[row["workspace"]] += cost
    ranked = lambda d: sorted(({"key": k or "(no project)", "cost": round(v, 4)} for k, v in d.items()),
                              key=lambda r: -r["cost"])
    days_list = [{"key": datetime.fromtimestamp(now - i * 86400).strftime("%Y-%m-%d")}
                 for i in range(days - 1, -1, -1)]
    for d in days_list:
        d["cost"] = round(by_day.get(d["key"], 0.0), 4)
    return {"days": days, "total": round(sum(r["cost"] or 0 for r in rows), 4),
            "conversations": len(rows), "by_day": days_list, "by_project": ranked(by_project),
            "by_agent": ranked(by_agent)[:20], "by_session": ranked(by_session)[:10]}


def window_spent(since, agent=None, project=None):
    """Dollars spent since a time, optionally for one catalog agent or one project.
    Budget windows (budgets.py) are checked against this."""
    with hub.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.agent_id, s.cost, w.project FROM spend s "
            "LEFT JOIN workspaces w ON w.id = s.workspace WHERE s.checked_at >= ?", (since,))]
        catalog = {r["agent_id"]: r["catalog_id"]
                   for r in conn.execute("SELECT agent_id, catalog_id FROM swarm_agents")}
    total = 0.0
    for row in rows:
        if agent and _agent_key(row["agent_id"], catalog) != agent and row["agent_id"] != agent:
            continue
        if project and row["project"] != project:
            continue
        total += row["cost"] or 0
    return total
