"""Where Lago Agents keeps things, and loading/saving the organization (config.json).

The app (this folder) holds nothing of yours. Your own things live in DATA_DIR
(~/.config/lago-agents, or $LAGO_AGENTS_HOME): config.json, the swarm catalog's agents,
tests, MCP servers and pipelines (catalog/), your Claude Code agent definitions
(claude-agents/), skills (claude-skills/) and plugin list (claude-plugins/plugins.json).
Keep it in its own private git repository if you want it versioned.

config.json holds what you edit: teams, agents, projects and which swarm agents are
switched off. The swarm team itself (each work session's Conductor and the agents it
woke) is added on load from the hub and never saved: sessions and agents come and go
while the app runs.
"""

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LAGO_AGENTS_HOME") or
                Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "lago-agents")
CONFIG_PATH = DATA_DIR / "config.json"
# A first start has no config.json yet: the welcome wizard (onboarding.py) fills it in.
EMPTY_CONFIG = {"teams": {}, "agents": {}, "projects": {}, "swarm": {}, "features": {}}  # extras off (features.py)
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") \
    / "lago-agents"
STATE_DIR.mkdir(parents=True, exist_ok=True)
# Conversations, attachments and backups: yours alone, whatever the umask or the parent's mode.
for _private in (STATE_DIR, DATA_DIR):
    try:
        if _private.is_dir() and _private.stat().st_mode & 0o077:
            _private.chmod(0o700)
    except OSError:
        pass

SWARM_TEAM = "swarm"
# The one session you talk to about all your work ("Lago" in the app): it opens and follows
# work sessions. Its id stays "orchestrator".
ORCHESTRATOR_ID = "orchestrator"
ORCHESTRATOR_NAME = "Lago"
ORCHESTRATOR_DIR = STATE_DIR / "orchestrator"
ORCHESTRATOR_DEFAULT_MODEL = "opus"
# How much Lago decides for you (`orchestrator.mode`): "check" brings every decision to you,
# ready to answer; "decide" has him take the routine and important ones himself, logged for
# you to review, and bring you only the critical ones. You pick it when the app opens.
LAGO_MODES = ("check", "decide")
DECIDING_MODELS = ("opus", "fable")  # Decide mode needs one of these for its judgment
# Present while the running Orchestrator was started with your password: it and the work
# sessions it opens skip permission checks. Removed whenever the Orchestrator stops.
ORCHESTRATOR_UNRESTRICTED = STATE_DIR / "orchestrator.unrestricted"
# Present while Lago is restarted into a fresh conversation (memory.rotate), so the hooks
# that run as the old one ends leave his unrestricted mode alone.
ORCHESTRATOR_ROTATING = STATE_DIR / "orchestrator.rotating"
# Touched when a flagged conversation sent Lago to a fresh one, so a second flag soon after
# waits for you instead of starting him over and over.
ORCHESTRATOR_FLAG_RESTART = STATE_DIR / "orchestrator.flag-restart"
# Holds the app's pid once you've typed your password: until the app closes, sessions that
# skip permission checks open without asking for it again.
BYPASS_UNLOCKED = STATE_DIR / "bypass.unlocked"
RESERVED_IDS = {SWARM_TEAM, ORCHESTRATOR_ID}
# Work sessions that belong to no project: the name they show, and where their folders go
# when you don't pick one.
NO_PROJECT = "No project"
SESSIONS_DIR = Path.home() / "lago-work"
# Swarm agent ids: `w<N>` is work session N's Conductor, `w<N>-<agent>` the agents it wakes.
SWARM_ID = re.compile(r"^w\d+(-|$)")
# Agents Lago runs directly, one task each, outside any work session: `r<N>-<agent>`.
RUN_ID = re.compile(r"^r\d+-")
# A permission prompt in a session Lago runs goes to him first, quietly; you're asked only if
# he passes it on (it's critical) or hasn't answered it within this long.
LAGO_REVIEW_SECONDS = 90


def orchestrator_model(config):
    """Opus unless you pick: keeping your work's thread across conversations, and knowing
    what to suggest next, needs the stronger model. Decide mode never runs on less than Opus."""
    model = (config.get("orchestrator") or {}).get("model") or ORCHESTRATOR_DEFAULT_MODEL
    if lago_mode(config) == "decide" and model not in DECIDING_MODELS:
        return ORCHESTRATOR_DEFAULT_MODEL
    return model


def lago_mode(config):
    mode = (config.get("orchestrator") or {}).get("mode")
    return mode if mode in LAGO_MODES else "check"


# The one swarm agent you're working with on the Talk to Lago page, if any: while it's set,
# Lago hands everything to that agent and nothing else (lago_mcp.py and lago_hook.py
# enforce it).
FOCUS_FILE = ORCHESTRATOR_DIR / "focus.json"
# What Lago can't use in focus mode: his own research and other ways to get work done.
FOCUS_BLOCKED = {"WebSearch", "WebFetch", "mcp__lago__open_work_session",
                 "mcp__lago__mail_inbox", "mcp__lago__mail_read"}


def focus():
    """The agent id Lago is focused on, or None."""
    try:
        return json.loads(FOCUS_FILE.read_text()).get("agent") or None
    except (OSError, ValueError):
        return None


def set_focus(agent_id=None):
    if agent_id:
        FOCUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        FOCUS_FILE.write_text(json.dumps({"agent": agent_id, "since": time.time()}))
    else:
        FOCUS_FILE.unlink(missing_ok=True)


def rotating():
    try:
        return time.time() - ORCHESTRATOR_ROTATING.stat().st_mtime < 600
    except OSError:
        return False


def drop_unrestricted():
    """The Orchestrator stopped: its next run checks permissions again (unless it's only
    being restarted into a fresh conversation)."""
    if not rotating():
        ORCHESTRATOR_UNRESTRICTED.unlink(missing_ok=True)


def unlock_bypass():
    """Your password was checked: remember it until this app closes."""
    BYPASS_UNLOCKED.write_text(str(os.getpid()))


def lock_bypass():
    BYPASS_UNLOCKED.unlink(missing_ok=True)


def bypass_unlocked():
    """Whether you typed your password since the app last opened (the app is still running)."""
    try:
        pid = int(BYPASS_UNLOCKED.read_text())
    except (OSError, ValueError):
        return False
    import desktop
    return any(Path(word).name in ("lago_agents.py", "app_server.py") for word in desktop.process_command(pid))


def bypass_ready(config):
    """Lago should skip permission checks (Settings) and you already gave your password: he
    and the work sessions he opens run unrestricted without asking again."""
    return bool((config.get("orchestrator") or {}).get("skip_permissions")) and bypass_unlocked()


def conductor_id(workspace_id):
    return f"w{workspace_id}"


def load():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError:
        config = json.loads(json.dumps(EMPTY_CONFIG))
    for key in ("teams", "agents", "projects"):
        config.setdefault(key, {})
    config.setdefault("swarm", {})
    _add_swarm(config)
    return config


def _add_swarm(config):
    """Add the swarm team: each work session's Conductor and the agents it woke."""
    import hub  # hub imports this module for STATE_DIR

    swarm = config["swarm"]
    config["teams"][SWARM_TEAM] = {
        "name": "Tasks", "icon": "hexagon", "cwd": "~", "swarm": True,
        "description": "One lead per task you're working on, and the agents it wakes."}
    folders = {}
    for ws in hub.workspaces():
        project = config["projects"].get(ws["project"], {})
        # A session with no project works in its own folder.
        folders[ws["id"]] = project.get("cwd") if ws["project"] else ws.get("folder")
        where = project.get("name", ws["project"]) if ws["project"] else NO_PROJECT
        area = (project.get("areas") or {}).get(ws["area"] or "", {})
        if area:
            where += f" / {area.get('name', ws['area'])}"
        conductor = {"name": f"Lead · {where}", "team": SWARM_TEAM, "definition": "",
                     "reports_to": None, "swarm": True, "conductor": True,
                     "workspace": ws["id"], "project": ws["project"],
                     "closed": ws["status"] == "closed", "cwd": folders[ws["id"]] or "~",
                     "unrestricted": bool(ws.get("unrestricted")),
                     "orchestrated": bool(ws.get("orchestrated")),
                     "account": ws.get("account"), "codex_home": ws.get("codex_home")}
        for key in ("model", "permission_mode"):
            if swarm.get(f"conductor_{key}"):
                conductor[key] = swarm[f"conductor_{key}"]
        config["agents"][conductor_id(ws["id"])] = conductor
    ORCHESTRATOR_DIR.mkdir(exist_ok=True)
    orchestrator = {"name": ORCHESTRATOR_NAME, "team": SWARM_TEAM, "definition": "",
                    "reports_to": None, "swarm": True, "orchestrator": True,
                    "cwd": str(ORCHESTRATOR_DIR),
                    "unrestricted": ORCHESTRATOR_UNRESTRICTED.exists(),
                    "model": orchestrator_model(config)}
    if swarm.get("conductor_permission_mode"):
        orchestrator["permission_mode"] = swarm["conductor_permission_mode"]
    config["agents"][ORCHESTRATOR_ID] = orchestrator
    unrestricted = {ws["id"] for ws in hub.workspaces() if ws.get("unrestricted")}
    whose = {ws["id"]: (ws.get("account"), ws.get("codex_home")) for ws in hub.workspaces()}
    for row in hub.swarm_agents():
        if row["agent_id"] in config["agents"]:
            continue
        project = config["projects"].get(row["project"], {})
        agent = {"name": row["name"], "team": SWARM_TEAM, "definition": row["catalog_id"],
                 "reports_to": conductor_id(row["workspace"]) if row["workspace"] else None,
                 "swarm": True, "workspace": row["workspace"], "project": row["project"],
                 "cwd": folders.get(row["workspace"]) or row.get("folder") or project.get("cwd") or "~",
                 "unrestricted": row["workspace"] in unrestricted or bool(row.get("unrestricted")),
                 "direct": not row["workspace"],
                 # whose subscription it runs on: the run's own, else its work session's
                 "account": row.get("account") or whose.get(row["workspace"], (None, None))[0],
                 "codex_home": row.get("codex_home") or whose.get(row["workspace"], (None, None))[1]}
        if row.get("model"):
            agent["model"] = row["model"]
        if row.get("variant"):
            agent["variant"] = row["variant"]
        if project.get("permission_mode") or swarm.get("permission_mode"):
            agent["permission_mode"] = project.get("permission_mode") or swarm["permission_mode"]
        config["agents"][row["agent_id"]] = agent


def without_swarm(config):
    """The part of the config that belongs in config.json."""
    config = dict(config)
    config["teams"] = {tid: t for tid, t in config.get("teams", {}).items() if not t.get("swarm")}
    config["agents"] = {aid: a for aid, a in config.get("agents", {}).items() if not a.get("swarm")}
    return config


def save(config, source=None, restored_from=None):
    """Write atomically so a crash mid-save can't leave a half-written config. Each change
    is kept as a revision (revisions.py), so it can be undone."""
    import revisions
    config = without_swarm(config)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as file:
            before = json.load(file)
    except (OSError, ValueError):
        before = {}
    CONFIG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_PATH.parent, prefix=".config-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(config, file, indent=2, ensure_ascii=False)
        file.write("\n")
    os.replace(tmp, CONFIG_PATH)
    try:
        revisions.record(before, json.loads(json.dumps(config)), source, restored_from)
    except Exception:  # keeping history must never stop a save
        logging.getLogger("lago.config").exception("keeping a config revision failed")


def team_cwd(config, agent):
    """The folder an agent works in: its own override, else its team's."""
    team = config["teams"].get(agent.get("team"), {})
    return Path(os.path.expanduser(agent.get("cwd") or team.get("cwd") or "~"))


def reports_of(config, agent_id):
    return [aid for aid, a in config["agents"].items() if a.get("reports_to") == agent_id]


def team_lead(config, team_id):
    for aid, agent in config["agents"].items():
        if agent.get("team") == team_id and not agent.get("reports_to"):
            return aid
    return None
