"""What's happening now, for the Live Map: what each agent is doing in plain words, and each
task's steps over time.

The PreToolUse hook (lago_hook.py) records every tool call here, with a sentence a person
reads at a glance ("is editing checkout.ts", "is running the tests"). Calls are kept for
KEEP_DAYS. timeline() lays each task's pipeline out against time from its steps' tasks.
files() is a project's file tree with who read or edited each file lately. handoffs() is
work changing hands on a project's map: a lead handing a step out, an agent reporting back.
replay() is a finished plan's history, to play back.
"""

import json
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import agentdefs
import budgets
import changes
import config_store
import features
import hub

KEEP_DAYS = 3
SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id  TEXT NOT NULL,
    tool      TEXT NOT NULL,
    subject   TEXT NOT NULL DEFAULT '',
    sentence  TEXT NOT NULL,
    at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_calls_at ON tool_calls (at);
"""
TEST_COMMAND = re.compile(r"\b(pytest|unittest|jest|vitest|mocha|go test|cargo test|npm (run )?test|"
                          r"yarn test|pnpm test|phpunit|rspec|dotnet test|mvn test|gradle test)\b")
INSTALL_COMMAND = re.compile(r"\b(npm (i|install|ci)|pip3? install|yarn add|pnpm (add|install)|"
                             r"apt(-get)? install|cargo add|go get|poetry add|uv (add|pip install))\b")
_ready = False


def _ensure():
    global _ready
    if not _ready:
        with hub.connect() as conn:
            conn.executescript(SCHEMA)
        _ready = True


def _name(path):
    return Path(str(path)).name or str(path)


def _short(text, limit=60):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def sentence(tool, tool_input):
    """What a tool call is doing, as the end of a sentence about the agent."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or ""
    if tool in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"is editing {_name(path)}"
    if tool == "Write":
        return f"is writing {_name(path)}"
    if tool == "Read":
        return f"is reading {_name(path)}"
    if tool in ("Grep", "Glob"):
        return f"is searching the code for {_short(tool_input.get('pattern', ''), 40)}"
    if tool == "Bash":
        command = " ".join(str(tool_input.get("command", "")).split())
        if TEST_COMMAND.search(command):
            return "is running the tests"
        if INSTALL_COMMAND.search(command):
            return "is installing packages"
        if re.search(r"\bgit commit\b", command):
            return "is committing its changes"
        if re.search(r"\bgit push\b", command):
            return "is pushing to the repository"
        if re.search(r"\b(npm run build|yarn build|pnpm build|make|cargo build|go build|tsc)\b", command):
            return "is building the project"
        return f"is running “{_short(tool_input.get('description') or command, 50)}”"
    if tool == "WebSearch":
        return f"is searching the web for {_short(tool_input.get('query', ''), 40)}"
    if tool == "WebFetch":
        return f"is reading {urlsplit(str(tool_input.get('url', ''))).hostname or 'a web page'}"
    if tool in ("Task", "Agent"):
        return f"is asking a helper: {_short(tool_input.get('description', ''), 40)}"
    if tool == "TodoWrite":
        return "is updating its to-do list"
    if tool == "Skill":
        return f"is loading the {tool_input.get('skill') or tool_input.get('command') or ''} skill".replace("  ", " ")
    if tool.startswith("mcp__lago__"):
        action = tool.removeprefix("mcp__lago__")
        if action == "delegate":
            return f"is handing “{_short(tool_input.get('title') or tool_input.get('task', ''), 40)}” to {tool_input.get('agent', 'an agent')}"
        if action == "wait_for_tasks":
            return "is waiting for its team"
        if action == "complete_task":
            return "is reporting back"
        if action == "run_step":
            return f"is starting step {tool_input.get('step', '')}".rstrip()
        if action == "propose_pipeline":
            return "is drafting a plan"
        if action == "tell_human":
            return "is telling you something"
        return f"is using Lago's {action.replace('_', ' ')}"
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        server = parts[1] if len(parts) > 1 else "a connector"
        return f"is using {server}" + (f" ({parts[2].replace('_', ' ')})" if len(parts) > 2 else "")
    return f"is using {tool}"


SHELL_READ, SHELL_EDIT = "Bash:read", "Bash:edit"  # files a shell command read or changed; not shown as activity
SHELL_ONLY = (SHELL_READ, SHELL_EDIT)
READERS = {"cat", "head", "tail", "less", "more", "nl", "wc", "bat", "grep", "rg", "egrep", "diff", "sed", "awk", "jq"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)
MAX_SHELL_FILES = 20


def shell_files(command, cwd):
    """The files a shell command reads and changes, as absolute paths: {"read": [...], "edit":
    [...]}. Agents often use cat, sed or a redirect instead of the file tools. A read counts only
    for a file that exists; an edit is a redirect's target, `tee`, or `sed -i`. Heredoc bodies
    are text, not commands; `cd` moves where relative paths start."""
    import shlex
    found = {"read": [], "edit": []}
    if not cwd:
        return found
    here = Path(cwd)
    text = HEREDOC.sub(lambda m: m.group(0).split("\n", 1)[0], str(command))
    for segment in re.split(r"&&|\|\||;|\||\n", text):
        try:
            words = shlex.split(segment, comments=True)
        except ValueError:
            continue
        if not words:
            continue
        if words[0] == "cd" and len(words) > 1:
            here = here / Path(words[1]).expanduser()
            continue
        edits, args, i = [], [], 0
        while i < len(words):
            word = words[i]
            redirect = re.fullmatch(r"\d?(>>?|&>)(.*)", word)
            if redirect:
                target = redirect.group(2) or (words[i + 1] if i + 1 < len(words) else "")
                i += 1 if redirect.group(2) else 2
                if target and not target.startswith("&") and target != "/dev/null":
                    edits.append(target)
                continue
            if word.startswith("<"):
                i += 1 if len(word) > 1 else 2
                continue
            args.append(word)
            i += 1
        def at(path, here=here):
            return here / Path(path).expanduser()
        name = Path(args[0]).name if args else ""
        if name == "tee" or (name in ("sed", "perl") and any(re.fullmatch(r"-[a-zA-Z]*i.*", a) for a in args[1:])):
            edits += [a for a in args[1:] if not a.startswith("-") and (name == "tee" or at(a).is_file())]
        elif name in READERS:
            found["read"] += [str(at(a).resolve()) for a in args[1:] if not a.startswith("-") and at(a).is_file()]
        found["edit"] += [str(at(a).resolve()) for a in edits]
    return {kind: list(dict.fromkeys(paths))[:MAX_SHELL_FILES] for kind, paths in found.items()}


def record(agent_id, tool, tool_input, subject="", cwd=None):
    """Note a tool call (the PreToolUse hook), and for a shell command the files it reads and
    changes (shell_files). Now and then drops calls older than KEEP_DAYS."""
    _ensure()
    now = time.time()
    touched = shell_files((tool_input or {}).get("command", ""), cwd) if tool == "Bash" and isinstance(tool_input, dict) else {}
    with hub.connect() as conn:
        conn.execute("INSERT INTO tool_calls (agent_id, tool, subject, sentence, at) VALUES (?, ?, ?, ?, ?)",
                     (agent_id, tool, str(subject)[:500], sentence(tool, tool_input), now))
        for kind, paths in touched.items():
            conn.executemany("INSERT INTO tool_calls (agent_id, tool, subject, sentence, at) VALUES (?, ?, ?, '', ?)",
                             [(agent_id, SHELL_EDIT if kind == "edit" else SHELL_READ, path, now) for path in paths])
        if random.random() < 0.01:
            conn.execute("DELETE FROM tool_calls WHERE at < ?", (now - KEEP_DAYS * 86400,))


def _role(agent_id, names):
    """What a session's agent is, from its id, once it's gone from the config: "Lead" for
    w<N>, the catalog agent's name for w<N>-<agent>[-<copy>]; None for anything else."""
    if re.fullmatch(r"w\d+", agent_id):
        return "Lead"
    match = re.fullmatch(r"w\d+-(.+?)(?:-\d+)?", agent_id)
    if not match:
        return None
    return names.get(match.group(1)) or match.group(1).replace("-", " ").capitalize()


def _agent_project(config, agent_id, sessions):
    agent = config["agents"].get(agent_id) or {}
    if agent.get("project") is not None and agent.get("swarm"):
        return agent.get("project") or ""
    return (sessions.get(agent.get("workspace")) or {}).get("project") or agent.get("project") or ""


def recent(config, after=0, limit=40, project=None):
    """The newest tool calls, newest first: [{"id", "agent_id", "agent", "project", "sentence",
    "tool", "subject", "at"}]. `after` returns only calls newer than that id; `project` ("" is
    work outside projects) keeps that project's."""
    _ensure()
    sessions = {ws["id"]: ws for ws in hub.workspaces("1 = 1")}
    out, names = [], None
    with hub.connect() as conn:
        rows = conn.execute("SELECT * FROM tool_calls WHERE id > ? AND tool NOT IN (?, ?) ORDER BY id DESC LIMIT ?",
                            (int(after or 0), *SHELL_ONLY, max(1, min(int(limit), 500)) * (4 if project is not None else 1)))
        for row in rows:
            row = dict(row)
            where = _agent_project(config, row["agent_id"], sessions)
            if project is not None and where != project:
                continue
            agent = config["agents"].get(row["agent_id"]) or {}
            if not agent.get("name") and names is None:
                names = {cid: e.get("name") for cid, e in agentdefs.catalog().items()}
            out.append({**row, "agent": agent.get("name") or _role(row["agent_id"], names or {}) or row["agent_id"], "project": where})
            if len(out) >= limit:
                break
    return out


STEP_ENDED = ("done", "failed", "skipped")


def _laid(conn, pipeline, names):
    """One plan with its steps placed in time, as timeline() and replay() show it."""
    steps = pipeline["steps"] if isinstance(pipeline["steps"], list) else json.loads(pipeline["steps"] or "[]")
    laid = []
    for step in steps:
        task = None
        if step.get("task_id"):
            task = conn.execute("SELECT created_at, updated_at, status FROM tasks WHERE id = ?",
                                (step["task_id"],)).fetchone()
        status = step.get("status") or "pending"
        start = task["created_at"] if task else None
        end = (task["updated_at"] if task else None) if status in STEP_ENDED else None
        laid.append({"id": step.get("id"), "title": step.get("title") or step.get("id"),
                     "agent": step.get("agent"), "agent_name": names.get(step.get("agent")) or step.get("agent"),
                     "status": status, "gate": bool(step.get("gate")), "start": start, "end": end})
    return {"id": pipeline["id"], "workspace": pipeline.get("workspace"), "title": pipeline["title"],
            "project": pipeline.get("project") or "", "status": pipeline["status"],
            "created_at": pipeline["created_at"], "updated_at": pipeline["updated_at"], "steps": laid}


def timeline(config, project=None, hours=24, now=None):
    """Each task's plan against time: running, waiting for approval, or finished within `hours`.
    [{"id", "workspace", "title", "project", "status", "created_at", "updated_at", "steps":
    [{"id", "title", "agent", "agent_name", "status", "gate", "start", "end"}], "cost"}], newest
    first. A step starts when its task was handed out and ends when the task finished; one
    still running has no end. "cost" is what the task's work session has spent at API prices,
    against its budget ({"spent", "limit" (0: none), "paused"}), while the Accounts & Budgets
    extra is on; None otherwise."""
    now = now or time.time()
    since = now - hours * 3600
    rows = hub.pipelines("status IN ('draft', 'changes_requested', 'approved', 'running') OR updated_at > ?",
                         (since,))
    names = {cid: entry.get("name") for cid, entry in agentdefs.catalog().items()}
    costs = {}
    if features.on(config, "budgets"):
        for workspace in {p.get("workspace") for p in rows if p.get("workspace")}:
            costs[workspace] = budgets.session_state(workspace, config)
    out = []
    with hub.connect() as conn:
        for pipeline in rows:
            if project is not None and (pipeline.get("project") or "") != project:
                continue
            out.append({**_laid(conn, pipeline, names), "cost": costs.get(pipeline.get("workspace"))})
    return sorted(out, key=lambda p: -(p["updated_at"] or 0))


READS = ("Read", SHELL_READ)
EDITS = ("Edit", "MultiEdit", "Write", "NotebookEdit", SHELL_EDIT)
CONFLICT_WINDOW = 15 * 60  # two agents editing one file this close together may be stepping on each other
MAX_TREE = 5000
MAX_TOUCHES = 30  # per file
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".worktrees"}
WORKTREE = re.compile(r"^\.worktrees/(p\d+-[a-z0-9-]+)/(.+)$")


def _tree(folder):
    """The project's files, relative to its folder: git's list (tracked and new, minus what it
    ignores) when it's a repository, else a walk that skips build and dependency folders."""
    if changes.repo_root(folder):
        listed = changes._git(folder, "ls-files", "--cached", "--others", "--exclude-standard", check=False).splitlines()
        return sorted({p for p in listed if not p.startswith(".worktrees/")})
    found = []
    for top, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel = Path(top).relative_to(folder)
        found += [str(rel / n) if str(rel) != "." else n for n in sorted(names)]
        if len(found) > MAX_TREE:
            break
    return found


def files(config, project, hours=24, now=None, with_tree=True):
    """A project's files and who touched them lately: {"folder", "repo_prefix", "files": [path],
    "more", "touched": {path: {"touches": [{"agent_id", "agent", "kind": "read"|"edit", "at",
    "workspace", "branch"}], "read_at", "edit_at", "conflict"}}}. Paths are relative to the
    project's folder; edits in a step's worktree count for the same file, with its branch.
    `repo_prefix` is the folder's path inside its repository, for the session's diff. Without
    `with_tree`, "files" is None (the page already has it)."""
    import swarm
    folder = swarm.project_cwd(config["projects"][project]).resolve()
    if not folder.is_dir():
        return {"folder": str(folder), "repo_prefix": "", "files": [], "more": 0, "touched": {}, "missing": True}
    root = changes.repo_root(folder)
    prefix = str(folder.relative_to(root.resolve())) if root and folder.is_relative_to(root.resolve()) else ""
    prefix = "" if prefix == "." else prefix
    now = now or time.time()
    _ensure()
    tools = READS + EDITS
    places = [f"{folder}/%"] + ([f"{root}/.worktrees/%"] if root else [])
    with hub.connect() as conn:
        rows = conn.execute(f"SELECT agent_id, tool, subject, at FROM tool_calls WHERE at > ? AND tool IN ({','.join('?' * len(tools))}) "
                            f"AND ({' OR '.join(['subject LIKE ?'] * len(places))}) ORDER BY at DESC",
                            (now - hours * 3600, *tools, *places))
        rows = [dict(r) for r in rows]
    touched, names = {}, None
    for row in rows:
        path, branch = Path(row["subject"]), None
        if not path.is_absolute():
            continue
        if root and path.is_relative_to(root / ".worktrees"):
            match = WORKTREE.match(str(path.relative_to(root)))
            inside = match and Path(match.group(2))
            if not inside or (prefix and not inside.is_relative_to(prefix)):
                continue
            rel, branch = str(inside.relative_to(prefix) if prefix else inside), f"lago/{match.group(1)}"
        elif path.is_relative_to(folder):
            rel = str(path.relative_to(folder))
        else:
            continue
        agent = config["agents"].get(row["agent_id"]) or {}
        if not agent.get("name"):
            names = names if names is not None else {cid: e.get("name") for cid, e in agentdefs.catalog().items()}
        kind = "edit" if row["tool"] in EDITS else "read"
        entry = touched.setdefault(rel, {"touches": [], "read_at": None, "edit_at": None, "conflict": False})
        entry[f"{kind}_at"] = entry[f"{kind}_at"] or row["at"]
        if len(entry["touches"]) < MAX_TOUCHES:
            entry["touches"].append({"agent_id": row["agent_id"], "agent": agent.get("name") or _role(row["agent_id"], names) or row["agent_id"], "kind": kind,
                                     "at": row["at"], "workspace": agent.get("workspace"), "branch": branch})
    for entry in touched.values():
        edits = [t for t in entry["touches"] if t["kind"] == "edit"]
        entry["conflict"] = any(a["agent_id"] != b["agent_id"] and a["at"] - b["at"] <= CONFLICT_WINDOW
                                for a, b in zip(edits, edits[1:]))
    tree = _tree(folder) if with_tree else None
    return {"folder": str(folder), "repo_prefix": prefix, "files": tree[:MAX_TREE] if tree is not None else None,
            "more": max(0, len(tree) - MAX_TREE) if tree else 0, "touched": touched}


HANDING_OUT = ("mcp__lago__delegate", "mcp__lago__run_step")
REPORTING = ("mcp__lago__complete_task",)
HANDOFF_SECONDS = 30


def handoffs(nodes, seconds=HANDOFF_SECONDS, now=None):
    """Work that changed hands in the last `seconds` between nodes of a project map
    (Api.project_map): [{"id", "from", "to", "kind": "hand_out" | "report", "at"}], oldest
    first. A hand-out goes to whoever got the task it made (the call comes just before the
    task exists, so it shows once the task does); a report goes up to the node's parent."""
    now = now or time.time()
    by_id = {n["id"]: n for n in nodes}
    _ensure()
    tools = HANDING_OUT + REPORTING
    out = []
    with hub.connect() as conn:
        rows = conn.execute(f"SELECT id, agent_id, tool, at FROM tool_calls WHERE at > ? AND tool IN ({','.join('?' * len(tools))}) "
                            "ORDER BY id", (now - seconds, *tools)).fetchall()
        for row in rows:
            node = by_id.get(row["agent_id"])
            if not node:
                continue
            if row["tool"] in REPORTING:
                target = node.get("parent")
            else:
                task = conn.execute("SELECT to_agent FROM tasks WHERE from_agent = ? AND created_at BETWEEN ? AND ? "
                                    "ORDER BY created_at LIMIT 1", (row["agent_id"], row["at"], row["at"] + 20)).fetchone()
                target = task["to_agent"] if task else None
            if target in by_id and target != row["agent_id"]:
                out.append({"id": row["id"], "from": row["agent_id"], "to": target,
                            "kind": "report" if row["tool"] in REPORTING else "hand_out", "at": row["at"]})
    return out


REPLAY_EVENTS = 5000


def _plan_agents(conn, pipeline):
    """Everyone who worked on a plan: whoever its steps' tasks went to and came from, and its
    work session's lead and agents."""
    steps = pipeline["steps"] if isinstance(pipeline["steps"], list) else json.loads(pipeline["steps"] or "[]")
    found = set()
    for task_id in [st["task_id"] for st in steps if st.get("task_id")]:
        row = conn.execute("SELECT to_agent, from_agent FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row:
            found |= {row["to_agent"], row["from_agent"]} - {None}
    if pipeline.get("workspace"):
        lead = config_store.conductor_id(pipeline["workspace"])
        found.add(lead)
        found |= {r["to_agent"] for r in conn.execute("SELECT to_agent FROM tasks WHERE from_agent = ?", (lead,))}
        found |= {r["agent_id"] for r in conn.execute("SELECT agent_id FROM spend WHERE workspace = ?", (pipeline["workspace"],))}
    return found


def _when(stamp):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _from_transcripts(conn, agents, start, end):
    """Tool calls read back from the agents' conversations, for when the recorded ones are gone:
    [(at, agent_id, tool, sentence)]."""
    import transcripts
    marks = ",".join("?" * len(agents))
    sessions = {r["session_id"]: r["agent_id"] for r in conn.execute(
        f"SELECT session_id, agent_id FROM account_sessions WHERE agent_id IN ({marks}) AND started_at <= ? "
        f"UNION SELECT session_id, agent_id FROM spend WHERE agent_id IN ({marks})", (*agents, end, *agents))}
    found = []
    for session_id, agent_id in sessions.items():
        path = transcripts.find(session_id)
        if not path:
            continue
        with open(path, encoding="utf-8", errors="replace") as file:
            for raw in file:
                if '"tool_use"' not in raw and '"function_call"' not in raw and '"custom_tool_call"' not in raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                at = _when(entry.get("timestamp"))
                if at is None or not start <= at <= end or entry.get("isSidechain"):
                    continue
                content = (entry.get("message") or {}).get("content")
                if isinstance(content, list):
                    found += [(at, agent_id, b.get("name", ""), sentence(b.get("name", ""), b.get("input")))
                              for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                else:  # Codex
                    found += [(at, agent_id, e["name"], sentence(e["name"], {}) if e["name"] != "Bash" else "is running a command")
                              for e in transcripts.events_from(entry) if e["kind"] == "tool"]
    return sorted(found)


def replay(config, pipeline_id, now=None):
    """A plan's history to play back: {"plan" (as timeline() lays it out, without cost),
    "start", "end", "events": [{"at", "agent_id", "agent", "sentence"}] oldest first,
    "source": "recorded" | "transcripts" | None, "more"}. The calls come from what the hook
    recorded; once those have expired, from the agents' own transcripts. A plan still running
    plays up to now."""
    pipeline = hub.get_pipeline(int(pipeline_id))
    if not pipeline:
        raise KeyError(pipeline_id)
    import redact
    names = {cid: entry.get("name") for cid, entry in agentdefs.catalog().items()}
    _ensure()
    with hub.connect() as conn:
        plan = _laid(conn, pipeline, names)
        started = [st["start"] for st in plan["steps"] if st["start"]]
        start = min(started + [plan["created_at"]])
        running = any(st["start"] and not st["end"] for st in plan["steps"]) or plan["status"] in ("approved", "running")
        end = (now or time.time()) if running else max([plan["updated_at"]] + [st["end"] or st["start"] for st in plan["steps"] if st["start"]])
        agents = sorted(_plan_agents(conn, pipeline))
        rows, source = [], None
        if agents:
            marks = ",".join("?" * len(agents))
            rows = [(r["at"], r["agent_id"], r["tool"], r["sentence"]) for r in conn.execute(
                f"SELECT at, agent_id, tool, sentence FROM tool_calls WHERE agent_id IN ({marks}) AND at BETWEEN ? AND ? "
                f"AND tool NOT IN (?, ?) ORDER BY at LIMIT ?", (*agents, start, end, *SHELL_ONLY, REPLAY_EVENTS + 1))]
            source = "recorded" if rows else None
            if not rows:
                rows = _from_transcripts(conn, agents, start, end)
                source = "transcripts" if rows else None
    def who(agent_id):  # by role: the session's agents carry its name, and may be gone from the config
        if plan["workspace"] and re.fullmatch(rf"w{plan['workspace']}(-.+)?", agent_id):
            return _role(agent_id, names)
        return (config["agents"].get(agent_id) or {}).get("name") or agent_id
    events = [{"at": at, "agent_id": agent_id, "agent": who(agent_id), "sentence": redact.text(text)}
              for at, agent_id, tool, text in rows[:REPLAY_EVENTS]]
    return {"plan": {**plan, "cost": None}, "start": start, "end": end, "events": events, "source": source,
            "more": len(rows) > REPLAY_EVENTS}
