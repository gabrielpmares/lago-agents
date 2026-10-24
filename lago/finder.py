"""Finding an agent for a task: the ones you already used for this kind of job come first.

history() looks through past tasks for ones like a new task (words shared with their title and
brief) and groups them by the swarm agent that did them, with how each went: finished or
failed, and the thumbs up or down it got (your own rating over the Conductor's or Lago's).
Shared words only suggest the same job. Lago reads the examples and confirms before relying on
one: "review a pull request" and "review a contract" share a word, not a job.

Test runs (evals.py) aren't history: they're the agent's exam, not your work.
"""

import re

import hub

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "onto", "our", "your", "their",
    "its", "are", "was", "were", "has", "have", "had", "will", "would", "should", "could", "can",
    "not", "but", "all", "any", "each", "every", "some", "one", "two", "new", "then", "than",
    "when", "what", "which", "who", "how", "why", "where", "there", "here", "them", "they", "you",
    "please", "make", "need", "needs", "want", "wants", "use", "using", "get", "give", "let",
    "also", "just", "about", "after", "before", "only", "more", "most", "very", "out", "via",
    "task", "work", "job", "done", "agent", "lago",
}
MIN_SHARED = 2       # words a past task must share with the new one (fewer if the query is short)
SCAN = 3000          # most recent finished tasks looked at
BODY_CHARS = 1500    # of a task's brief, compared besides its title
EXAMPLES = 3


def words(text):
    """The words that say what a task is about: lowercase, 3+ letters, no filler."""
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (text or "").lower())
            if w not in STOPWORDS}


def _verdict(rows):
    """1, -1 or 0 for one task's ratings: yours alone when you gave one."""
    human = [r for r in rows if r["rater"] == "human"]
    total = sum(r["verdict"] for r in human or rows)
    return (total > 0) - (total < 0)


def history(query, project=""):
    """The agents that did tasks like `query`, best first: [{catalog_id, similar, done, failed,
    ups, downs, same_project, last_at, examples: [{task_id, title, status, verdict, shared}]}]."""
    wanted = words(query)
    if not wanted:
        return []
    need = min(MIN_SHARED, len(wanted))
    agents = {r["agent_id"]: r for r in hub.swarm_agents()}
    tests = {r["task_id"] for r in hub.test_runs(limit=100000) if r["task_id"]}
    ratings = {}
    for row in hub.ratings(limit=100000):
        ratings.setdefault(row["task_id"], []).append(row)
    found = {}
    for task in hub.tasks("status IN ('done', 'failed')", limit=SCAN):  # newest first
        agent = agents.get(task["to_agent"])
        if agent is None or task["id"] in tests:
            continue
        shared = wanted & words(f"{task['title']} {task['body'][:BODY_CHARS]}")
        if len(shared) < need:
            continue
        verdict = _verdict(ratings.get(task["id"], []))
        mine = found.setdefault(agent["catalog_id"], {
            "catalog_id": agent["catalog_id"], "similar": 0, "done": 0, "failed": 0, "ups": 0,
            "downs": 0, "same_project": 0, "last_at": task["updated_at"], "best": 0.0,
            "examples": []})
        mine["similar"] += 1
        mine["done" if task["status"] == "done" else "failed"] += 1
        mine["ups"] += verdict > 0
        mine["downs"] += verdict < 0
        mine["same_project"] += bool(project) and agent["project"] == project
        mine["best"] = max(mine["best"], len(shared) / len(wanted))
        if len(mine["examples"]) < EXAMPLES:
            mine["examples"].append({"task_id": task["id"], "title": task["title"],
                                     "status": task["status"], "verdict": verdict,
                                     "shared": sorted(shared)})
    return sorted(found.values(), key=_rank)


def _rank(found):
    """Closest match first, then the better record, then the more recent."""
    record = found["ups"] - found["downs"] + 0.5 * (found["done"] - found["failed"])
    return (-round(found["best"], 1), -record, -found["same_project"], -found["last_at"])


def history_text(found):
    """history() as lines for Lago and the Conductor."""
    lines = []
    for f in found:
        record = [f"{f['similar']} similar task{'s' * (f['similar'] != 1)}",
                  f"{f['done']} finished"]
        if f["failed"]:
            record.append(f"{f['failed']} failed")
        if f["ups"] or f["downs"]:
            record.append(f"{f['ups']} up, {f['downs']} down")
        if f["same_project"]:
            record.append(f"{f['same_project']} in this project")
        lines.append(f"- `{f['catalog_id']}`: {', '.join(record)}")
        for ex in f["examples"]:
            mark = {1: " (thumbs up)", -1: " (thumbs down)"}.get(ex["verdict"], "")
            failed = " (failed)" if ex["status"] == "failed" else ""
            lines.append(f"  #{ex['task_id']} {ex['title']}{failed}{mark}")
    return "\n".join(lines)
