"""The Dashboard's numbers over time: tasks by outcome, spend and success rate per day for
the last DAYS days, this month's spend against its budget, and the latest tasks and activity.
What's live (who is working, who needs you) comes from Api.overview, polled separately.
"""

import time
from collections import defaultdict
from datetime import datetime, timedelta

import activity
import budgets
import config_store
import hub
import spending

DAYS = 14
RECENT = 8
# Activity the dashboard leaves out: noise next to the work itself.
QUIET_KINDS = ("skill.installed", "config.changed", "connector.call")


def _day(at):
    return datetime.fromtimestamp(at).strftime("%Y-%m-%d")


def days(count=DAYS, now=None):
    """[{"key": "2026-10-23", "done", "failed", "other", "cost", "rate"}], oldest first.
    A task counts on the day it was created; `rate` is done / (done + failed), None
    on a day with neither."""
    now = now or time.time()
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    keys = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(count - 1, -1, -1)]
    since = (today - timedelta(days=count - 1)).timestamp()
    counts = defaultdict(lambda: {"done": 0, "failed": 0, "other": 0})
    with hub.connect() as conn:
        for row in conn.execute("SELECT status, created_at FROM tasks WHERE created_at >= ?", (since,)):
            bucket = row["status"] if row["status"] in ("done", "failed") else "other"
            counts[_day(row["created_at"])][bucket] += 1
    costs = {d["key"]: d["cost"] for d in spending.summary(count, now)["by_day"]}
    out = []
    for key in keys:
        c = counts[key]
        finished = c["done"] + c["failed"]
        out.append({"key": key, **c, "cost": round(costs.get(key, 0.0), 2),
                    "rate": round(c["done"] / finished, 3) if finished else None})
    return out


def month(config=None, now=None):
    """{"spent", "budget"}: dollars since the 1st, and the monthly budget for everything
    (0 when there's none)."""
    config = config or config_store.load()
    budget = next((w["limit"] for w in budgets.windows(config)
                   if w["scope"] == "all" and w["window"] == "month"), 0)
    return {"spent": round(spending.window_spent(budgets.window_start("month", now)), 2),
            "budget": budget}


def recent_tasks(limit=RECENT):
    with hub.connect() as conn:
        rows = conn.execute("SELECT id, title, status, to_agent, from_agent, updated_at FROM tasks "
                            "ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def recent_activity(limit=RECENT):
    rows = [r for r in activity.timeline(limit=limit * 6) if r["kind"] not in QUIET_KINDS]
    return [{k: r[k] for k in ("id", "actor", "kind", "text", "created_at")} for r in rows[:limit]]


def summary(count=DAYS, now=None):
    series = days(count, now)
    with hub.connect() as conn:
        blocked = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'blocked'").fetchone()[0]
    done = sum(d["done"] for d in series)
    failed = sum(d["failed"] for d in series)
    return {"days": series, "month": month(now=now), "blocked": blocked,
            "done": done, "failed": failed,
            "rate": round(done / (done + failed), 3) if done + failed else None,
            "recent_tasks": recent_tasks(), "recent_activity": recent_activity()}
