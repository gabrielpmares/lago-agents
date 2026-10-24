"""Standing tool permissions: what an agent may always do, must always ask for, or may never do.

Instead of approving the same thing again and again, a rule says it once:

    {"agent": "qa-engineer", "project": "", "tool": "Bash", "pattern": "npm test*", "decision": "allow"}

- `agent`: "*" for every agent, or an agent's id, its catalog agent (`qa-engineer`, for every
  copy the swarm wakes) or its definition;
- `project`: "" for any, or a project's id;
- `tool`: the tool's name (`Bash`, `Edit`, `WebFetch`, `mcp__github__*`…), "*" for any;
- `pattern`: matched (shell-style, `*` and `?`) against what the call is about: a Bash command,
  the file of Edit/Write/Read, a WebFetch URL; "*" or "" for anything;
- `decision`: allow, ask or deny.

An allow rule is read narrowly, so it can't be stretched: a Bash command that chains or
redirects (`;`, `&`, `|`, `$`, a backquote, `<`, `>`, a new line) matches it only if its pattern
has that character too, so `npm test*` allows `npm test -- --watch` but not
`npm test; curl … | sh`; and a file's path is normalized first, so `src/*` doesn't allow
`src/../.ssh/authorized_keys`. Deny and ask rules match as written.

The Claude Code hook (lago_hook.py, PreToolUse) asks decide() before each tool call. The most
specific matching rule wins (a named agent over every agent, a project over any, a pattern over
none); on a tie, deny beats ask beats allow. Nothing matching: Claude Code's own permissions
apply as before. Each use is counted, so you see which rules work.
"""

import fnmatch
import json
import os
import time

import config_store
import hub

DECISIONS = ("allow", "ask", "deny")
SUBJECT_KEYS = {"Bash": "command", "Edit": "file_path", "MultiEdit": "file_path", "Write": "file_path",
                "Read": "file_path", "NotebookEdit": "notebook_path", "WebFetch": "url",
                "Glob": "pattern", "Grep": "pattern", "WebSearch": "query"}
FILE_TOOLS = {"Edit", "MultiEdit", "Write", "Read", "NotebookEdit"}
SHELL_CONTROL = set(";&|$`<>\n")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL DEFAULT '*',
    project     TEXT NOT NULL DEFAULT '',
    tool        TEXT NOT NULL,
    pattern     TEXT NOT NULL DEFAULT '*',
    decision    TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_by  TEXT,
    created_at  REAL NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    last_hit    REAL
);
"""


class RuleError(Exception):
    pass


def _ensure():
    with hub.connect() as conn:
        conn.executescript(SCHEMA)


def subject(tool, tool_input):
    """What a call is about, for patterns: its command, file or URL."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    return str(tool_input.get(SUBJECT_KEYS.get(tool, ""), "") or "")


def rules():
    _ensure()
    with hub.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tool_rules ORDER BY agent, tool, pattern")]


def add(tool, decision, agent="*", project="", pattern="*", note="", by=None):
    tool, pattern = (tool or "").strip(), (pattern or "").strip() or "*"
    agent, project = (agent or "").strip() or "*", (project or "").strip()
    if decision not in DECISIONS:
        raise RuleError("A rule allows, asks or denies")
    if not tool:
        raise RuleError("Name the tool, like Bash or Edit (or * for any)")
    if project and project not in config_store.load().get("projects", {}):
        raise RuleError(f"There's no project {project!r}")
    _ensure()
    import activity
    with hub.connect() as conn:
        same = conn.execute("SELECT id FROM tool_rules WHERE agent = ? AND project = ? AND tool = ? AND pattern = ?",
                            (agent, project, tool, pattern)).fetchone()
        if same:
            conn.execute("UPDATE tool_rules SET decision = ?, note = ? WHERE id = ?", (decision, note, same["id"]))
            rule_id = same["id"]
        else:
            rule_id = conn.execute("INSERT INTO tool_rules (agent, project, tool, pattern, decision, note, created_by, created_at) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                   (agent, project, tool, pattern, decision, note, by or activity.actor(), time.time())).lastrowid
    activity.log(by or activity.actor(), "permission.rule", f"{decision} {tool} {pattern} for {agent}"
                 + (f" in {project}" if project else ""), target=f"rule {rule_id}", project=project or None)
    return rule_id


def remove(rule_id):
    _ensure()
    with hub.connect() as conn:
        if not conn.execute("DELETE FROM tool_rules WHERE id = ?", (int(rule_id),)).rowcount:
            raise RuleError("That rule no longer exists")


def _names(agent_id):
    """Every name a rule may use for this agent: its id, catalog agent and definition."""
    config = config_store.load()
    agent = config["agents"].get(agent_id) or {}
    names = {agent_id}
    for key in ("catalog_id", "definition"):
        if agent.get(key):
            names.add(agent[key])
    return names, agent.get("project") or ""


def _allows(rule, tool, what):
    """Whether an allow rule's pattern covers the call, read narrowly (see the top)."""
    if tool in FILE_TOOLS and what:
        what = os.path.normpath(what)
    if tool == "Bash" and (SHELL_CONTROL & set(what)) - set(rule["pattern"]):
        return False
    return fnmatch.fnmatchcase(what, rule["pattern"])


def decide(agent_id, tool, tool_input):
    """The matching rule as {"id", "decision", ...}, or None."""
    _ensure()
    names, project = _names(agent_id)
    what = subject(tool, tool_input)
    best = None
    for rule in rules():
        if rule["agent"] != "*" and rule["agent"] not in names:
            continue
        if rule["project"] and rule["project"] != project:
            continue
        if rule["tool"] != "*" and not fnmatch.fnmatchcase(tool, rule["tool"]):
            continue
        if rule["pattern"] not in ("", "*"):
            if rule["decision"] == "allow" and not _allows(rule, tool, what):
                continue
            if rule["decision"] != "allow" and not fnmatch.fnmatchcase(what, rule["pattern"]):
                continue
        rank = (rule["agent"] != "*", bool(rule["project"]), rule["pattern"] not in ("", "*"),
                rule["tool"] != "*", DECISIONS.index(rule["decision"]))
        if best is None or rank > best[0]:
            best = (rank, rule)
    if best is None:
        return None
    with hub.connect() as conn:
        conn.execute("UPDATE tool_rules SET hits = hits + 1, last_hit = ? WHERE id = ?", (time.time(), best[1]["id"]))
    return best[1]


def suggestion(agent_id):
    """A rule to offer from the call the agent is asking about now: {"agent", "tool", "pattern", ...}."""
    state = hub.get_state(agent_id)
    try:
        last = json.loads(state.get("last_tool") or "{}")
    except ValueError:
        last = {}
    if not last.get("tool"):
        return None
    config = config_store.load()
    agent = config["agents"].get(agent_id) or {}
    what = last.get("subject") or ""
    pattern = what
    if last["tool"] == "Bash" and what:
        words = what.split()
        pattern = " ".join(words[:2]) + "*" if len(words) > 1 else words[0] + "*"
    return {"agent": agent["definition"] if agent.get("swarm") and agent.get("definition") else agent_id, "agent_name": agent.get("name") or agent_id,
            "project": agent.get("project") or "", "tool": last["tool"], "subject": what, "pattern": pattern or "*"}
