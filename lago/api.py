"""The methods the web UI can call. Each returns JSON-serialisable data.

Methods listed in UI_THREAD run on the GTK thread (they touch GTK widgets);
everything else runs in a worker thread so the window never freezes.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import accounts
import activity
import agentdefs
import agent_drafts
import attachments
import background
import backups
import budgets
import cache
import codex_runtime
import company
import config_store
import dashboard
import decisions
import desktop
import documents
import evals
import features
import ghcatalog
import backlog
import paperclip_import
import paperclip_local
import github
import goals
import holds
import hub
import listen
import live
import member_accounts
import memory
import mic
import notifications
import notify
import onboarding
import open_tasks
import outcomes
import personas
import portability
import publish
import pull_requests
import redact
import changes
import connectors
import toolrules
import browser_live
import readstate
import secret_requests
import questions
import revisions
import routines
import schedules
import runner
import server
import services
import skill_sources
import spending
import statuscards
import swarm
import team
import tickets
import transcripts
import vault
import voice
import voice_setup

UI_THREAD = {"show_terminal", "hide_terminal", "clipboard_files"}

AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
# Before Claude Code's SessionStart hook fires, anything on screen is a question
# it asks first (trust this folder, enable MCP servers, log in…).
STARTUP_GRACE_SECONDS = 5
ACTIVITY_LIMIT = 200
WELCOME_AGAIN_SECONDS = 600  # reopening the app within this doesn't greet you again


class ApiError(Exception):
    """A problem worth showing to the user as-is."""


def _agent(config, agent_id):
    agent = config["agents"].get(agent_id)
    if agent is None:
        raise ApiError(f"There's no agent called {agent_id!r}")
    return agent


def _settle_after_interrupt(agent_id, wait=3.0):
    """Claude Code fires no Stop hook when interrupted: once its log shows the turn ended (or
    it was never in one, e.g. a message lost to a prompt on its screen), mark it idle and hand
    it what's waiting."""
    session = hub.get_state(agent_id).get("session_id")
    deadline = time.time() + wait
    while session:
        if memory.between_turns(session) or time.time() >= deadline:
            break
        time.sleep(0.5)
    if hub.get_state(agent_id)["status"] in ("working", "waiting_on_team", "needs_approval", "needs_input"):
        hub.set_state(agent_id, status="idle", detail="")
        team.pump(agent_id, auto_start=False)


def _call(work):
    try:
        return work()
    except (team.TeamError, services.ServiceError, server.ServerError, accounts.AccountError,
            revisions.RevisionError, backups.BackupError, routines.RoutineError, schedules.ScheduleError,
            holds.HoldError, decisions.DecisionError, questions.QuestionError,
            goals.GoalError, portability.PortabilityError, skill_sources.SkillSourceError,
            statuscards.CardError, documents.DocumentError, tickets.TicketError,
            pull_requests.PullRequestError, vault.VaultError, company.AccessError,
            changes.ChangesError, toolrules.RuleError, secret_requests.SecretRequestError,
            connectors.ConnectorError, backlog.BacklogError) as error:
        raise ApiError(str(error)) from error
    except github.GitHubError as error:
        raise ApiError(f"GitHub: {error}") from error


def _connector_change(work):
    """A connector change, then Lago restarted to match once he's idle (connectors.py)."""
    result = _call(work)
    connectors.refresh_lago_soon()
    return result


# Statuses whose terminal is worth showing: it holds the question or the crash.
SCREEN_STATUSES = ("needs_approval", "needs_input", "exited")


def _live_status(state, running, now):
    """The status to show: the hub's record, corrected by whether tmux still has it."""
    status = state["status"]
    if not running:
        return "exited" if status == "exited" else "stopped"
    if status in ("starting", "stopped") and now - (state.get("started_at") or now) > STARTUP_GRACE_SECONDS:
        return "needs_input"
    return status  # "exited" while tmux lives: the crash screen waits for Enter


# Files a message names that the app opens (open_file) or shows (read_file).
RUNNABLE_SUFFIXES = {".desktop", ".appimage", ".run", ".bin", ".exe", ".deb", ".rpm"}
VIEWABLE_SUFFIXES = {".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv",
                     ".log", ".html", ".pdf", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"}
FILE_VIEW_LIMIT = 1_000_000


def _live_all(states, running):
    """{agent id: live status} for every agent the hub knows."""
    now = time.time()
    return {aid: _live_status(state, aid in running, now) for aid, state in states.items()}


def _validate_projects(projects):
    if not isinstance(projects, dict):
        raise ApiError("Projects must be an object")
    for project_id, project in projects.items():
        if not AGENT_ID.match(project_id):
            raise ApiError(f"Project id {project_id!r} can only use lowercase letters, digits and dashes")
        name = project.get("name") or project_id
        if not str(project.get("cwd") or "").strip():
            raise ApiError(f"{name} needs a project folder")
        if not isinstance(project.get("vars", {}), dict) or \
                not all(isinstance(v, str) for v in project.get("vars", {}).values()):
            raise ApiError(f"{name}: variables must be text")
        for key in ("context", "agents", "links", "services"):
            if not isinstance(project.get(key, []), list):
                raise ApiError(f"{name}: {key} must be a list")
        for link in project.get("links", []):
            if not str(link.get("url", "")).startswith(("http://", "https://")):
                raise ApiError(f"{name}: link {link.get('name', '')!r} needs an http(s) URL")
        service_ids = [str(sv.get("id", "")) for sv in project.get("services", [])]
        for service_id in service_ids:
            if not AGENT_ID.match(service_id):
                raise ApiError(f"{name}: service id {service_id!r} can only use lowercase letters, "
                               "digits and dashes")
        if len(set(service_ids)) != len(service_ids):
            raise ApiError(f"{name}: two services have the same id")
        for service in project.get("services", []):
            if service.get("port") not in (None, "") and not str(service["port"]).isdigit():
                raise ApiError(f"{name}: {service.get('name', service['id'])}'s port must be a number")
        gh = project.get("github", {})
        if not isinstance(gh, dict) or not isinstance(gh.get("repos", []), list) \
                or not isinstance(gh.get("boards", []), list):
            raise ApiError(f"{name}: GitHub settings must have lists of repos and boards")
        for board in gh.get("boards", []):
            if not board.get("owner") or not str(board.get("number", "")).isdigit():
                raise ApiError(f"{name}: each GitHub board needs an owner and a number")
        server_list = project.get("servers", [])
        if not isinstance(server_list, list):
            raise ApiError(f"{name}: servers must be a list")
        server_ids = [str(sv.get("id", "")) for sv in server_list]
        if len(set(server_ids)) != len(server_ids) or not all(AGENT_ID.match(i) for i in server_ids):
            raise ApiError(f"{name}: each server needs its own id (lowercase letters, digits, dashes)")
        for srv in server_list:
            if not isinstance(srv.get("watch", []), list):
                raise ApiError(f"{name}: server settings are malformed")
            if srv.get("role", "production") not in ("production", "local"):
                raise ApiError(f"{name}: a server's role is production or local")
            if srv.get("path") and not str(srv["path"]).startswith("/"):
                raise ApiError(f"{name}: the code folder on the server must be an absolute path")
        if not isinstance(project.get("mcp_servers", {}), dict):
            raise ApiError(f"{name}: MCP servers must be a JSON object of name → server")
        rules = project.get("merge_policy")
        if rules is not None and (not isinstance(rules, dict)
                                  or rules.get("who", "human") not in ("human", "conductor")
                                  or rules.get("method", "squash") not in pull_requests.METHODS):
            raise ApiError(f"{name}: merge_policy takes enabled, require_checks, require_approval, "
                           "method (squash, merge or rebase) and who (human or conductor)")
        for area_id, area in (project.get("areas") or {}).items():
            if not AGENT_ID.match(area_id):
                raise ApiError(f"{name}: area id {area_id!r} can only use lowercase letters, "
                               "digits and dashes")
            if not isinstance(area.get("paths", []), list) or not isinstance(area.get("agents", []), list):
                raise ApiError(f"{name}: an area's paths and agents must be lists")


def _validate(config):
    agents, teams = config.get("agents"), config.get("teams")
    if not isinstance(agents, dict) or not isinstance(teams, dict):
        raise ApiError("Config is missing teams or agents")
    _validate_projects(config.get("projects", {}))
    for reserved in config_store.RESERVED_IDS:
        if reserved in agents or reserved in teams:
            raise ApiError(f"{reserved!r} is reserved for the swarm. Pick another id.")
    for agent_id in agents:
        if config_store.SWARM_ID.match(agent_id) or config_store.RUN_ID.match(agent_id):
            raise ApiError(f"Ids like {agent_id!r} (w or r + a number) are kept for the swarm. "
                           "Pick another id.")
    for agent_id, agent in agents.items():
        if not AGENT_ID.match(agent_id):
            raise ApiError(f"Agent id {agent_id!r} can only use lowercase letters, digits and dashes")
        if agent.get("team") not in teams:
            raise ApiError(f"{agent.get('name', agent_id)} belongs to a team that doesn't exist")
        lead = agent.get("reports_to")
        if lead:
            if lead not in agents:
                raise ApiError(f"{agent['name']} reports to an agent that doesn't exist")
            if agents[lead].get("team") != agent.get("team"):
                raise ApiError(f"{agent['name']} and {agents[lead]['name']} are in different teams")
        seen, cursor = {agent_id}, lead
        while cursor:
            if cursor in seen:
                raise ApiError(f"{agent['name']} ends up reporting to itself")
            seen.add(cursor)
            cursor = agents.get(cursor, {}).get("reports_to")
    for team_id, info in teams.items():
        if not AGENT_ID.match(team_id):
            raise ApiError(f"Team id {team_id!r} can only use lowercase letters, digits and dashes")
        roots = [a for a in agents.values() if a.get("team") == team_id and not a.get("reports_to")]
        if len(roots) > 1:
            raise ApiError(f"{info.get('name', team_id)} has {len(roots)} agents without a lead. "
                           "A team has exactly one lead; everyone else reports to someone.")


def _check_password(password):
    """Your Linux password, checked by sudo (it's never stored or logged). Guards sessions
    that run without permission checks. Asked once: until the app closes it stays unlocked."""
    if config_store.bypass_unlocked():
        return
    if not password:
        raise ApiError("Enter your password to skip permissions")
    try:
        proc = subprocess.run(["sudo", "-S", "-k", "-p", "", "true"], input=password + "\n",
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ApiError(f"Couldn't check the password: {error}") from error
    if proc.returncode != 0:
        raise ApiError("Wrong password")
    config_store.unlock_bypass()


LAGO_MODELS = ["haiku", "sonnet", "opus", "fable"]
APPEARANCES = ("auto", "light", "dark")  # the switch under the sidebar
# Lago's own tool calls shown in his conversation, as short notes.
LAGO_ACTIONS = {"app", "run_agent", "create_project", "open_work_session", "message_session", "close_work_session", "remember", "schedule", "cancel_schedule"}
CONVERSATION_LIMIT = 80
ACTIVITY_STEPS = 6


def _conversation(session_id):
    """Lago's current conversation for the chat view: what you said, what he said, notes Lago
    Agents typed in (reports, the greeting), and the actions he took."""
    path = transcripts.find(session_id)
    out = []
    for event in transcripts.read(path)["events"] if path else []:
        if event["sub"]:
            continue
        if event["kind"] == "say":
            out.append({"who": "lago", "text": event["text"], "at": event["at"],
                        "spoken": voice.spoken_line(event["text"]),
                        "category": voice.reply_category(event["text"])})
        elif event["kind"] == "prompt":
            text = event["text"]
            if text.startswith("[Lago") and "\n\n(spoken) " in text:  # "Hey Chip, ...": who you called, then what you said
                note, _, said = text.partition("\n\n")
                out.append({"who": "note", "text": note, "at": event["at"]})
                text = said
            if text.startswith("[Lago"):
                out.append({"who": "note", "text": text, "at": event["at"]})
            else:
                spoken = text.startswith("(spoken) ")
                text, paths = attachments.split(text.removeprefix("(spoken) "))
                out.append({"who": "you", "text": text, "at": event["at"], "voice": spoken,
                            "id": event.get("uuid"), "files": [_file(p) for p in paths]})
        elif event["kind"] == "tool" and re.sub(r"^(mcp__lago__|lago__|lago\.)", "", event["name"]) in LAGO_ACTIONS:
            out.append({"who": "action", "text": event["text"], "at": event["at"]})
    return out[-CONVERSATION_LIMIT:]


def _file(path):
    """A file you sent Lago, for the chat: shown as a picture or as a named file."""
    return {"path": path, "url": Path(path).as_uri(), "name": attachments.display_name(path),
            "image": attachments.is_image(path), "kept": Path(path).exists()}


def _activity(session_id):
    """What Lago has done so far this turn, newest last: [{"text", "done"}]. Shown while he
    works, as Claude Code shows each tool call."""
    path = transcripts.find(session_id)
    events = transcripts.read(path)["events"] if path else []
    turn = []
    for event in reversed(events):
        if event["sub"]:
            continue
        if event["kind"] == "prompt":
            break
        turn.append(event)
    turn.reverse()
    finished = {e["id"] for e in turn if e["kind"] == "result"}
    steps = [{"text": e.get("live") or e["text"], "done": e["id"] in finished}
             for e in turn if e["kind"] == "tool"]
    return steps[-ACTIVITY_STEPS:]


RUN_SHOWN_FOR = 2 * 3600  # finished runs stay in Lago's sidebar this long


def _agent_runs(states, running, now):
    """Agents Lago ran directly (no work session): open ones, and ones that finished in the
    last RUN_SHOWN_FOR seconds and you haven't closed, newest first, with what each is doing
    right now."""
    closed = notifications._dismissed()
    runs = []
    rows = [row for row in hub.swarm_agents()
            if not row["workspace"] and config_store.RUN_ID.match(row["agent_id"])]
    latest = hub.latest_tasks(row["agent_id"] for row in rows)
    for row in rows:
        task = latest.get(row["agent_id"])
        if not task:
            continue
        open_ = task["status"] in hub.OPEN_TASK
        state = states.get(row["agent_id"]) or {"status": "stopped"}
        # A finished run stays a while after its agent last did anything: a task closed long
        # after the run stopped (the watchdog's cleanup) is old news, not a new result.
        last_active = min(task["updated_at"], state.get("updated_at") or task["updated_at"])
        if not open_ and (now - last_active > RUN_SHOWN_FOR or f"run:{task['id']}" in closed):
            continue
        live = _live_status(state, row["agent_id"] in running, now)
        runs.append({"agent_id": row["agent_id"], "catalog_id": row["catalog_id"],
                     "name": row["name"].split(" · ")[0], "task_id": task["id"],
                     "title": task["title"], "status": task["status"], "live": live,
                     "detail": state.get("detail", "") if open_ else "",
                     "started_at": task["created_at"], "updated_at": task["updated_at"],
                     "result": "" if open_ else team.clip(task["result"], 600),
                     "steps": _activity(state.get("session_id")) if open_ and state.get("session_id") else [],
                     "test": bool(row.get("variant")) or "/evals/" in (row.get("folder") or "")})
    return sorted(runs, key=lambda r: (r["status"] not in hub.OPEN_TASK, -r["started_at"]))


def _schedule(row):
    return {"id": row["id"], "text": row["text"], "next_at": row["next_at"],
            "repeat": schedules.describe(row), "kind": row["repeat"],
            "every_minutes": row["every_minutes"], "last_at": row.get("last_at")}


def _with_files(text, files, keep):
    """The message Lago gets: the files saved and named at its top, then the text."""
    text = (text or "").strip()
    keep = [str(p) for p in keep or ()]
    if any(not attachments.kept(p) for p in keep):
        raise ApiError("A file from the earlier message is no longer kept; attach it again")
    if not text and not files and not keep:
        raise ApiError("Write or say something first")
    try:
        saved = attachments.save(list(files)) if files else []
    except attachments.AttachmentError as error:
        raise ApiError(str(error)) from error
    return attachments.message(text, keep + saved) if keep or saved else text


def _write_down_last_conversation():
    """Before Lago starts afresh, summarize the conversation he leaves, so his notes are
    ready for the new one (takes a few seconds with Haiku)."""
    oid = config_store.ORCHESTRATOR_ID
    previous = hub.get_state(oid).get("session_id")
    if runner.is_running(oid) or not previous or memory.summarized(previous) \
            or not memory.rotation_due(previous):
        return
    try:
        memory.summarize(previous)
    except Exception as error:  # his notes can wait; Lago starting can't
        print(f"lago-agents: couldn't summarize Lago's conversation: {error!r}", file=sys.stderr)


def _focus_card(config):
    agent = config_store.focus()
    entry = swarm.enabled_agents(config).get(agent) if agent else None
    if agent and entry is None:  # switched off or removed since
        config_store.set_focus(None)
    return {"id": entry["id"], "name": entry["name"], "summary": entry["summary"]} if entry else None


def _focused(text):
    """Your message, marked with the agent you're working with, so Lago never loses track."""
    agent = config_store.focus()
    return f"[Using `{agent}` only] {text}" if agent else text


def _skip_permissions(config):
    return bool((config.get("orchestrator") or {}).get("skip_permissions"))


class Api:
    def __init__(self, window):
        self.window = window
        self.welcomed = False
        attachments.start_sweeping()
        background.start_loops()  # unless the scheduler service already runs them

    # --- setup

    def init(self):
        if not runner.is_running(config_store.ORCHESTRATOR_ID):
            config_store.ORCHESTRATOR_UNRESTRICTED.unlink(missing_ok=True)  # left from a crash
        config = config_store.load()
        return {"config": config, "definitions": self.definitions(), "theme": self._theme(),
                "appearance": config.get("appearance", "auto"),
                "persona": personas.current(config), "personas": personas.listing(),
                "features": features.state(config), "feature_list": features.listing(config),
                "catalog": self.catalog(), "onboarding": onboarding.needed(config),
                "provider": codex_runtime.provider(config)}

    # --- first start (onboarding.py) and New Task

    def onboarding_claude(self):
        return onboarding.claude_status()

    def onboarding_login(self):
        return self.login_account(accounts.MAIN)

    # --- which subscription agents run on (codex_runtime.py)

    def providers(self):
        config = config_store.load()
        return {"current": codex_runtime.provider(config), "labels": codex_runtime.PROVIDERS,
                "claude": onboarding.claude_status(), "codex": codex_runtime.status()}

    def test_provider(self, provider):
        if provider == "codex":
            return codex_runtime.test()
        status = onboarding.claude_status()
        return {"ok": status["logged_in"], "message": "Claude Code is logged in." if status["logged_in"]
                else "Claude Code isn't logged in." if status["installed"] else "Claude Code isn't installed."}

    def login_provider(self, provider):
        if provider != "codex":
            return self.login_account(accounts.MAIN)
        terminal = _call(lambda: server.launch("login-codex", "", codex_runtime.login_command(),
                                               "log in to Codex with your ChatGPT account"))
        return {"ok": True, "terminal": terminal, "message": "Finish logging in in the terminal (it opens your browser)."}

    def set_provider(self, provider, verify=True):
        if provider not in codex_runtime.RUNTIMES:
            raise ApiError("Pick Claude or Codex")
        if provider == "codex" and verify:
            check = codex_runtime.test()  # its "logged in" can hide an expired token
            if not check["ok"]:
                raise ApiError(f"Codex isn't working: {check['message']} Log in, then try again.")
        config = config_store.without_swarm(config_store.load())
        config["runtime"] = {**(config.get("runtime") or {}), "default": provider}
        config_store.save(config)
        linked = codex_runtime.link_skills(config_store.DATA_DIR / "claude-skills") if provider == "codex" else []
        return {"ok": True, "message": f"New sessions run on your {codex_runtime.PROVIDERS[provider]}. "
                "Agents already running keep theirs until they restart."
                + (f" Linked {len(linked)} skills into ~/.codex/skills." if linked else "")}

    def detect_folder(self, folder):
        return onboarding.detect(folder)

    def check_hub(self, url):
        return company.check_hub((url or "").strip())

    def join_company(self, url):
        url = (url or "").strip()
        check = company.check_hub(url)
        if not check["ok"]:
            raise ApiError(check["message"])
        company.join(url)
        desktop.open_target(url)
        return {"ok": True, "message": check["message"]}

    def finish_onboarding(self, name, project=None, provider="claude", company_name=""):
        try:
            if company_name.strip():
                company.setup(company_name, company.this_machine_login())
            if provider in codex_runtime.RUNTIMES:
                self.set_provider(provider, verify=False)  # the wizard tested it just now
            project_id = onboarding.finish(name, project)
        except ValueError as error:
            raise ApiError(str(error)) from error
        except team.TeamError as error:
            raise ApiError(str(error)) from error
        return {"ok": True, "project": project_id, "config": config_store.load()}

    def dismiss_start_card(self):
        onboarding.dismiss_start_card()
        return {"ok": True}

    def new_task(self, text, project="", agent=""):
        """One way to ask for anything. Lago decides by default; with a project, a work
        session opens on it (followed by Lago); with an agent, it runs the task directly and
        its result goes to Lago. Returns where to look: {"href", "message"}."""
        text = (text or "").strip()
        if not text:
            raise ApiError("Describe the task")
        config = config_store.load()
        who = activity.person()
        member = bool(who) and not company.is_owner(who["login"])
        # Only the owner's own work skips permission checks; members' always asks.
        unrestricted = not member and (config_store.ORCHESTRATOR_UNRESTRICTED.exists() or config_store.bypass_ready(config))
        if member and not project and not agent:
            raise ApiError("Pick a project for it: Lago is the owner's assistant, so your tasks go to a project's lead.")
        if agent:
            _, agent_id, _ = _call(lambda: swarm.run_agent(agent, text, project_id=project or "",
                                                             unrestricted=unrestricted,
                                                             from_agent=config_store.ORCHESTRATOR_ID))
            activity.log("you", "task.new", f"{text[:200]} (to {agent})", target=agent_id, project=project or None)
            return {"ok": True, "href": f"#agent/{agent_id}", "message": "Started. Lago gets the result."}
        if project:
            workspace_id = _call(lambda: swarm.open_workspace(project, "", text, unrestricted=unrestricted,
                                                                orchestrated=True))
            return {"ok": True, "href": f"#work/{workspace_id}",
                    "message": "A work session is starting on it. Lago follows it for you."}
        self.say_to_orchestrator(f"New task: {text}")
        return {"ok": True, "href": "#orchestrator", "message": "Sent to Lago: he'll pick who does it."}

    def definitions(self):
        config = config_store.load()
        folders = {info.get("cwd") for info in config["teams"].values() if info.get("cwd")}
        found = agentdefs.discover(sorted(folders))
        return sorted(({"name": d["name"], "description": " ".join(d["description"].split()),
                        "model": d["model"], "tools": d["tools"], "scope": d["scope"],
                        "path": d["path"]} for d in found.values()),
                      key=lambda d: d["name"])

    def save_config(self, config):
        """Save teams and agents. Projects and swarm switches have their own methods, so a
        stale copy from the Teams page can't overwrite them."""
        current = config_store.load()
        config = config_store.without_swarm(config)
        config["projects"], config["swarm"] = current["projects"], current["swarm"]
        for key in ("voice", "orchestrator", "accounts", "active_account", "accounts_auto_switch", "connectors",
                    "appearance"):
            if key in current:
                config[key] = current[key]
            else:
                config.pop(key, None)
        _validate(config)
        config_store.save(config)
        return {"ok": True}

    # --- history, search, config revisions and backups

    def activity(self, workspace=None, kind=None, limit=200):
        return activity.timeline(workspace=workspace, kind=kind or None, limit=limit)

    def search(self, query, workspace=None):
        return activity.search(query, workspace=workspace)

    # --- routines

    def routines(self):
        rows = routines.all_routines()
        for row in rows:
            row["describe"] = routines.describe(row)
            row["has_hook"] = bool(row.pop("hook_hash", None))
            row["runs"] = routines.runs(row["id"], 5)
        return rows

    def save_routine(self, routine_id=None, **fields):
        def work():
            if routine_id:
                return routines.update(routine_id, **fields)
            return routines.create(**fields)
        row = _call(work)
        row.pop("hook_hash", None)
        return row

    def routine_status(self, routine_id, status):
        return _call(lambda: routines.set_status(routine_id, status))

    def run_routine(self, routine_id):
        return _call(lambda: routines.fire(routine_id, "manual", force=True))

    def routine_hook(self, routine_id):
        return {"secret": _call(lambda: routines.new_hook(routine_id))}

    def routine_runs(self, routine_id):
        return routines.runs(routine_id, 50)

    # --- Lago's reminders (schedules.py), shown with the routines

    def reminders(self):
        return [_schedule(row) for row in schedules.active()]

    def save_reminder(self, schedule_id=None, text=None, when=None, repeat="daily",
                      every_minutes=None):
        """Add a reminder for Lago, or change one. `when`: "09:00" or "2026-11-01 08:30"."""
        def work():
            if schedule_id:
                return schedules.update(schedule_id, text, when or None, repeat, every_minutes)
            if not when and repeat != "minutes":
                raise schedules.ScheduleError("Say when it first comes due (e.g. 09:00)")
            return schedules.add(text, when or None, None, repeat, every_minutes)
        return _schedule(_call(work))

    def run_reminder(self, schedule_id):
        _call(lambda: schedules.run_now(schedule_id, notify.tell_orchestrator))
        return {"ok": True, "message": "Sent to Lago."}

    def spending(self, days=30):
        return spending.summary(int(days))

    def config_revisions(self):
        return revisions.recent(100)

    def config_revision(self, revision_id):
        return _call(lambda: {"diff": revisions.diff(revision_id), **{
            k: v for k, v in revisions.get(revision_id).items() if k not in ("before", "after")}})

    def restore_config(self, revision_id, which="before"):
        return _call(lambda: revisions.restore(revision_id, which))

    def backups(self):
        return backups.backups()

    def backup_now(self):
        return _call(lambda: {"ok": True, "name": backups.backup().name})

    def restore_backup(self, name):
        return _call(lambda: backups.restore(name))

    # --- Claude accounts

    def accounts(self, refresh=False):
        """Each account: who is logged in, its plan limits from /usage, and Lago's tokens."""
        config = config_store.load()
        chosen = accounts.active(config)
        states = hub.all_states()
        tokens = accounts.lago_tokens()
        out = []
        for account_id, info in accounts.all_accounts(config).items():
            try:
                status = accounts.status(account_id, refresh=bool(refresh))
            except Exception as error:
                status = {"logged_in": False, "email": "", "plan": "", "error": str(error)}
            usage = {}
            if status.get("logged_in"):
                try:
                    usage = accounts.usage(account_id, refresh=bool(refresh))
                except Exception as error:
                    usage = {"error": str(error)}
            running = [aid for aid, s in states.items()
                       if (s.get("account") or accounts.MAIN) == account_id and runner.is_running(aid)]
            out.append({"id": account_id, "label": info.get("label") or account_id,
                        "active": account_id == chosen, "folder": str(accounts.config_dir(account_id)),
                        "status": status, "usage": usage, "lago": tokens.get(account_id, {}),
                        "running": len(running)})
        return {"accounts": out, "auto_switch": accounts.auto_switch(config), "switch_at": accounts.SWITCH_AT,
                "budgets": budgets.limits(config), "warn_at": budgets.WARN_AT,
                "windows": self.budget_windows()}

    # --- budgets

    def budget_windows(self):
        """Each window with what's been spent in it so far."""
        import spending
        out = []
        for window in budgets.windows(config_store.load()):
            kind, _, value = window["scope"].partition(":")
            spent = spending.window_spent(budgets.window_start(window["window"]),
                                          agent=value if kind == "agent" else None,
                                          project=value if kind == "project" else None)
            extra = hub.get_budget(budgets._window_key(window))["extra"]
            out.append({**window, "spent": round(spent, 2), "extended": round(extra, 2)})
        return out

    def save_budget_windows(self, windows):
        clean = []
        for w in windows or []:
            scope, window = str(w.get("scope") or "all"), w.get("window")
            try:
                limit = round(float(w.get("limit")), 2)
            except (TypeError, ValueError) as error:
                raise ApiError("A window's limit is an amount of dollars, like 50") from error
            if window not in budgets.WINDOWS:
                raise ApiError("A window is a week or a month")
            if limit <= 0:
                raise ApiError("A window needs a limit above 0 (remove it instead)")
            if scope != "all" and scope.split(":", 1)[0] not in ("project", "agent"):
                raise ApiError("A window covers everything, project:<id> or agent:<id>")
            clean.append({"scope": scope, "window": window, "limit": limit})
        config = config_store.without_swarm(config_store.load())
        config["budget_windows"] = clean
        config_store.save(config)
        return {"ok": True, "message": "Saved. Running agents get the new windows at their next check."}

    def save_budgets(self, agent, session):
        """The default budgets, in dollars at API prices: per agent conversation and per
        work session (0: no limit)."""
        try:
            values = {"agent": round(float(agent), 2), "session": round(float(session), 2)}
        except (TypeError, ValueError) as error:
            raise ApiError("Budgets are amounts of dollars, like 10 or 2.5") from error
        if min(values.values()) < 0:
            raise ApiError("A budget can't be negative (0 means no limit)")
        config = config_store.without_swarm(config_store.load())
        config["budgets"] = values
        config_store.save(config)
        return {"ok": True, "message": "Saved. Running agents get the new budgets at their next check."}

    def extend_budget(self, agent_id, amount=None):
        """Give the budgets that stopped an agent (or its work session) more room, and let
        the agents they stopped carry on."""
        _agent(config_store.load(), agent_id)
        try:
            return {"ok": True, "message": _call(lambda: budgets.extend(agent_id, amount))}
        except ValueError as error:
            raise ApiError(str(error)) from error

    def stop_over_budget(self, agent_id):
        """End the agents a budget stopped and fail their open tasks."""
        _agent(config_store.load(), agent_id)
        return {"ok": True, "message": _call(lambda: budgets.stop(agent_id))}

    def active_account(self):
        """The account new sessions start on, with its limits as last seen (for the sidebar).
        Polled every minute, so it also moves new sessions off an account that runs out."""
        config = config_store.load()
        if accounts.auto_switch(config):
            try:
                accounts.check_active(config)
                config = config_store.load()
            except Exception:
                pass  # no reading yet, or claude unavailable: the next poll tries again
        account_id = accounts.active(config)
        usage = cache.read(f"account:{account_id}:usage")[0] or {}
        return {"id": account_id, "label": accounts.all_accounts(config)[account_id].get("label"),
                "limits": usage.get("limits", [])}

    def set_auto_switch(self, on):
        config = config_store.load()
        config["accounts_auto_switch"] = bool(on)
        config_store.save(config)
        return {"ok": True, "message": "Agents move to another account when one runs out." if on
                else "Agents stay on their account when it runs out."}

    def account_alert(self):
        return accounts.alert()

    def set_active_account(self, account_id):
        config = config_store.load()
        if account_id not in accounts.all_accounts(config):
            raise ApiError(f"No account called {account_id!r}")
        config["active_account"] = account_id
        config_store.save(config)
        label = accounts.all_accounts(config)[account_id]["label"]
        return {"ok": True, "message": f"New sessions start on {label}. Running agents keep "
                                       "their account until you restart them."}

    def add_account(self, label):
        label = " ".join(str(label).split())[:40]
        if not label:
            raise ApiError("Give the account a name.")
        config = config_store.load()
        account_id = accounts.new_id(config, label)
        accounts.prepare(account_id)
        config.setdefault("accounts", {})[account_id] = {"label": label}
        config_store.save(config)
        return {"ok": True, "id": account_id, "message": f"Added {label}. Log in to it next."}

    def rename_account(self, account_id, label):
        label = " ".join(str(label).split())[:40]
        config = config_store.load()
        if account_id not in accounts.all_accounts(config) or not label:
            raise ApiError("Nothing to rename.")
        config.setdefault("accounts", {}).setdefault(account_id, {})["label"] = label
        config_store.save(config)
        return {"ok": True, "message": "Renamed."}

    def remove_account(self, account_id):
        config = config_store.load()
        if account_id == accounts.MAIN:
            raise ApiError("The main account can't be removed.")
        _call(lambda: accounts.remove(account_id))
        (config.get("accounts") or {}).pop(account_id, None)
        if config.get("active_account") == account_id:
            config["active_account"] = accounts.MAIN
        config_store.save(config)
        return {"ok": True, "message": "Removed. Its login is deleted from this machine."}

    def login_account(self, account_id):
        """Open a terminal running `claude auth login` for this account."""
        config = config_store.load()
        if account_id not in accounts.all_accounts(config):
            raise ApiError(f"No account called {account_id!r}")
        accounts.prepare(account_id)
        cache.forget(f"account:{account_id}:")
        email = accounts.all_accounts(config)[account_id].get("email", "")
        terminal = _call(lambda: server.launch(f"login-{account_id}", "", accounts.login_command(account_id, email),
                                               f"log in to the {account_id} account"))
        return {"ok": True, "terminal": terminal,
                "message": "Finish logging in in the terminal (it opens your browser)."}

    # --- connectors (connectors.py)

    def connectors(self):
        return connectors.listing()

    def connector_calls(self, limit=200):
        return connectors.calls(limit)

    def add_connector(self, catalog_id=None, name="", url="", command="", auth="none"):
        return _connector_change(lambda: connectors.add(catalog_id, name, url, command, auth))

    def connector_sign_in(self, connector_id, token="", client_id="", client_secret=""):
        return _connector_change(lambda: connectors.sign_in(connector_id, token, client_id, client_secret))

    def connector_sign_out(self, connector_id):
        return _connector_change(lambda: connectors.sign_out(connector_id))

    def remove_connector(self, connector_id):
        return _connector_change(lambda: connectors.remove(connector_id))

    def update_connector(self, connector_id, projects=None, lago=None, ask=None, blocked_tools=None):
        return _connector_change(lambda: connectors.update(connector_id, projects, lago, ask, blocked_tools))

    def connector_tools(self, connector_id):
        return _call(lambda: connectors.check(connector_id))

    def connect_assistant(self, which, on=True):
        return _call(lambda: connectors.connect_assistant(which, on))

    # --- swarm

    def catalog(self):
        """Every swarm agent's manifest, whether it's switched on, and the templates."""
        config = config_store.load()
        off = swarm.disabled(config)
        agents = []
        for entry in agentdefs.catalog().values():
            resolved = agentdefs.resolve(entry)
            agents.append({**{k: v for k, v in entry.items() if k != "body"},
                           "enabled": entry["id"] not in off,
                           "extra_instructions": bool(entry["body"]),
                           "base_path": (resolved or {}).get("base_path"),
                           "missing_base": resolved is None,
                           "model": (resolved or {}).get("model") or entry["model"],
                           "tools": (resolved or {}).get("tools") or entry["tools"]})
        cards = {c["id"]: c for c in evals.ranking()}
        for agent in agents:
            agent["eval"] = cards.get(agent["id"])
        templates = [{k: v for k, v in t.items() if k != "body"}
                     for t in agentdefs.pipeline_templates().values()]
        return {"agents": agents, "templates": templates,
                "skills": sorted(agentdefs.skills().values(), key=lambda k: k["name"]),
                "plugins": agentdefs.plugins(),
                "folder": str(agentdefs.CATALOG_AGENTS)}

    # --- evaluations (evals.py)

    def agent_evaluation(self, agent_id):
        return _call(lambda: evals.details(agent_id))

    def draft_agent_tests(self, agent_id):
        added = _call(lambda: evals.draft_tests(agent_id))
        return {"ok": True, "message": f"Drafted {added} test{'s' if added != 1 else ''}. "
                "Read them over: the gate is only as good as its tests."
                if added else f"The suite already has {evals.MIN_TESTS} or more tests."}

    def revert_proposal(self, proposal_id):
        return {"ok": True, "message": _call(lambda: evals.revert(proposal_id))}

    def rate_task(self, task_id, verdict, reason=""):
        return {"ok": True, "message": _call(lambda: evals.rate(task_id, verdict, "human", reason))}

    def close_runs(self, task_ids):
        """Hide finished agent runs from Lago's side panel (their results stay in History)."""
        for task_id in task_ids or []:
            task = hub.get_task(int(task_id))
            if task and task["status"] not in hub.OPEN_TASK:
                notifications.dismiss(f"run:{task['id']}")
        return {"ok": True}

    def judge_session(self, workspace_id, accepted, reason=""):
        return {"ok": True, "message": _call(lambda: outcomes.judge_session(workspace_id,
                                                                            bool(accepted), reason))}

    def save_agent_tests(self, agent_id, tests):
        saved = _call(lambda: evals.save_tests(agent_id, tests))
        return {"ok": True, "tests": saved,
                "message": f"Saved {len(saved)} test{'s' if len(saved) != 1 else ''}."}

    def run_agent_tests(self, agent_id):
        runs = _call(lambda: evals.run_tests(agent_id))
        return {"ok": True, "message": f"Queued {len(runs)} test run{'s' if len(runs) != 1 else ''}. "
                f"Up to {evals.MAX_RUNNING} run at a time, graded as they finish."}

    def improve_agent(self, agent_id):
        proposal = _call(lambda: evals.propose(agent_id))
        return {"ok": True, "message": f"Improvement #{proposal} started: the test suite is "
                "filled, the agent-designer drafts a prompt, and it only reaches you if it "
                "passes every test without losing ground."}

    def decide_proposal(self, proposal_id, approve):
        return {"ok": True, "message": _call(lambda: evals.decide(proposal_id, bool(approve)))}

    def agent_drafts(self):
        return {"drafts": [agent_drafts.get(d["id"]) for d in agent_drafts.drafts()[:20]]}

    def dismiss_agent_draft(self, draft_id):
        """Hide a failed or installed draft from the Agents page."""
        _call(lambda: agent_drafts.dismiss(draft_id))
        return {"ok": True}

    def decide_agent_draft(self, draft_id, approve):
        message = _call(lambda: agent_drafts.decide(draft_id, bool(approve)))
        return {"ok": True, "message": message}

    def set_swarm_agent(self, agent_id, enabled):
        config = config_store.without_swarm(config_store.load())
        if agent_id not in agentdefs.catalog():
            raise ApiError(f"There's no swarm agent {agent_id!r}")
        off = set(config["swarm"].get("disabled", []))
        off.discard(agent_id) if enabled else off.add(agent_id)
        config["swarm"]["disabled"] = sorted(off)
        config_store.save(config)
        return {"ok": True, "message": "Switched on." if enabled else
                "Switched off. The Conductor won't use it."}

    def save_projects(self, projects):
        _validate_projects(projects)
        config = config_store.without_swarm(config_store.load())
        config["projects"] = projects
        config_store.save(config)
        return {"ok": True, "message": "Saved. The Conductor sees the change on its next tool call."}

    # --- projects and work sessions

    def projects(self):
        """Project cards: the basics, open work sessions and anything waiting for you."""
        config = config_store.load()
        running = runner.running_agents()
        open_ws = hub.workspaces("status = 'open'")
        waiting = hub.pipelines("status = 'draft'")
        cards = []
        for pid, project in config["projects"].items():
            mine = [w for w in open_ws if w["project"] == pid]
            cards.append({
                "id": pid, "name": project.get("name", pid), "icon": project.get("icon", "cube"),
                "description": project.get("description", ""), "cwd": project.get("cwd", ""),
                "repo": project.get("repo", ""), "areas": len(project.get("areas") or {}),
                "services": len(project.get("services") or []),
                "boards": len(github.boards(project)),
                "open_sessions": len(mine),
                "active_sessions": sum(config_store.conductor_id(w["id"]) in running for w in mine),
                "waiting": sum(p["project"] == pid for p in waiting),
            })
        return cards

    def project(self, project_id):
        config = config_store.load()
        project = config["projects"].get(project_id)
        if project is None:
            raise ApiError(f"There's no project {project_id!r}")
        running = runner.running_agents()
        states = hub.all_states()
        pipelines = hub.pipelines("project = ?", (project_id,), limit=200)
        sessions = []
        live = _live_all(states, running)
        for ws in hub.workspaces("project = ?", (project_id,), limit=100):
            cid = config_store.conductor_id(ws["id"])
            state = states.get(cid) or {"status": "stopped"}
            mine = [p for p in pipelines if p["workspace"] == ws["id"]]
            latest = mine[0] if mine else None
            sessions.append({**ws, "conductor": cid,
                             "live": notifications.session_status(ws, live, states),
                             "detail": state.get("detail", "") if cid in running else "",
                             "pipeline": {"id": latest["id"], "title": latest["title"],
                                          "status": latest["status"]} if latest else None,
                             "waiting": sum(p["status"] == "draft" for p in mine)})
        return {"id": project_id, "project": project, "sessions": sessions,
                "repos": github.repos(project), "boards": github.boards(project)}

    def open_workspace(self, project, area="", text="", unrestricted=False, password="", folder=""):
        """A work session on a project, or (project "") on none, in `folder` or its own."""
        if unrestricted:
            _check_password(password)
        workspace_id = _call(lambda: swarm.open_workspace(project or "", area or "", text or "",
                                                          bool(unrestricted), folder=folder or ""))
        return {"ok": True, "message": "Starting the Conductor…", "workspace": workspace_id}

    def free_sessions(self):
        """Open work sessions that belong to no project."""
        running, states, now = runner.running_agents(), hub.all_states(), time.time()
        out = []
        for ws in hub.workspaces("project = '' AND status = 'open'"):
            cid = config_store.conductor_id(ws["id"])
            out.append({"id": ws["id"], "title": ws["title"], "folder": ws.get("folder") or "",
                        "live": _live_status(states.get(cid) or {"status": "stopped"},
                                             cid in running, now)})
        return out

    def close_workspace(self, workspace_id):
        _call(lambda: swarm.close_workspace(int(workspace_id)))
        return {"ok": True, "message": "Work session closed. Its agents are asleep."}

    def reopen_workspace(self, workspace_id, password=""):
        if _call(lambda: swarm.get_workspace(int(workspace_id))).get("unrestricted"):
            _check_password(password)
        _call(lambda: swarm.reopen_workspace(int(workspace_id)))
        return {"ok": True, "message": "Reopened. The Conductor resumes the conversation."}

    def hold_session(self, workspace_id, on, reason=""):
        """Pause a work session and every agent in it, or let it carry on."""
        if on:
            stopped = _call(lambda: holds.hold(workspace_id, reason))
            return {"ok": True, "message": f"Session #{workspace_id} is on hold"
                    + (f" ({len(stopped)} interrupted)." if stopped else ".")}
        _call(lambda: holds.release(workspace_id))
        return {"ok": True, "message": f"Session #{workspace_id} is carrying on."}

    # --- secrets (names only; values stay in the keyring)

    def secrets(self, project=None):
        return vault.secrets(project)

    def save_secret(self, name, env, value, project=""):
        return _call(lambda: vault.save(name, env, value, project))

    def remove_secret(self, name, project=""):
        return _call(lambda: vault.remove(name, project))

    def secret_access(self):
        return vault.access_log()

    # --- pull requests

    def pr_check(self, repo, number):
        allowed, reasons, now = _call(lambda: pull_requests.check(repo, int(number)))
        return {"allowed": allowed, "reasons": reasons, **now,
                "reviews": pull_requests.reviews(repo, int(number))}

    def merge_pr(self, repo, number):
        return _call(lambda: pull_requests.merge(repo, int(number)))

    # --- a work session's documents

    def session_documents(self, workspace_id):
        return _call(lambda: documents.sync(workspace_id))

    def document(self, doc_id, number=None):
        def work():
            doc = documents.get(doc_id)
            rev = documents.revision(doc_id, number)
            return {**doc, "revision": rev, "diff": documents.diff(doc_id, rev["number"]),
                    "comments": documents.comments(doc_id)}
        return _call(work)

    def comment_document(self, doc_id, text, quote=""):
        return _call(lambda: documents.comment(doc_id, text, quote))

    # --- status cards and project summaries

    def status_cards(self):
        return statuscards.cards()

    def add_status_card(self, title, project="", query=""):
        return _call(lambda: statuscards.add(title, project, query))

    def refresh_status_card(self, card_id):
        return _call(lambda: statuscards.refresh(card_id, force=True)[0])

    def archive_status_card(self, card_id):
        return _call(lambda: statuscards.archive(card_id))

    def project_summary(self, project, refresh=False):
        card = statuscards.project_summary(project)
        if refresh or not card["generated_at"]:
            card = _call(lambda: statuscards.refresh(card["id"], force=refresh)[0])
        return card

    # --- skills from git

    def fetch_skill(self, repo, path="", ref=""):
        draft = _call(lambda: skill_sources.fetch(repo, path, ref))
        return {"ok": True, "id": draft["id"], "message": f"Fetched {draft['name']}: review it before installing."}

    def github_agents(self, topic="", query=""):
        """Lago's GitHub list: trending by topic, or the matches for a search."""
        cards = ghcatalog.search(query, limit=40) if (query or "").strip() \
            else ghcatalog.trending(topic, limit=40)
        import ghimport
        have = ghcatalog.installed()
        cards = [{**c, "have": have.get(c["repo"].lower())} for c in cards]
        return {"topics": ghcatalog.TOPICS, "kinds": ghcatalog.KIND_NAMES, "cards": cards,
                "careful": ghimport.careful(),
                "refreshed_at": float(ghcatalog._meta("refreshed_at") or 0),
                "listed": len(ghcatalog.cards())}

    def explain_github_agent(self, repo, refresh=False):
        """What a repo on the list does, risk and reward, and where it fits your projects."""
        return _call(lambda: ghcatalog.explain(repo, bool(refresh)))

    def set_careful_import(self, on):
        """Import from GitHub through the builder's sandbox test and a draft you approve."""
        config = config_store.load()
        config.setdefault("swarm", {})["careful_import"] = bool(on)
        config_store.save(config)
        return {"ok": True, "message": "Careful import on: each agent is sandbox-tested and waits "
                "for your approval." if on else "Careful import off: agents install in one step."}

    def refresh_github_agents(self):
        started = ghcatalog.refresh_later()
        return {"ok": True, "message": "Refreshing from GitHub: it takes a few minutes."
                if started else "Already refreshing."}

    def import_github_agent(self, repo, notes=""):
        done = _call(lambda: ghcatalog.import_repo(repo, notes))
        if isinstance(done, str):
            return {"ok": True, "message": done}
        return {"ok": True, "message": f"The GitHub Agent Builder is checking {repo} (task "
                f"#{done}). Its draft appears on the Agents tab for your approval; nothing is "
                "installed before that."}

    def prepare_publish(self, agent_id):
        """The scrubbed files of an agent's public repository, for you to read first."""
        return _call(lambda: publish.prepare(agent_id))

    def publish_agent(self, agent_id, checked=False):
        return {"ok": True, "message": _call(lambda: publish.publish(agent_id, bool(checked)))}

    def skill_drafts(self):
        return [skill_sources.read(d["id"]) for d in skill_sources.drafts("ready")]

    def decide_skill(self, draft_id, approve):
        return {"ok": True, "message": _call(lambda: skill_sources.decide(draft_id, bool(approve)))}

    # --- bringing everything over from Paperclip (paperclip_import.py, paperclip_local.py)

    def paperclip_find(self, extra=""):
        """Paperclip installs on this computer (and in `extra`, a folder you point at), with
        their backups."""
        return {"instances": paperclip_local.instances([extra] if extra else [])}

    def paperclip_fresh_backup(self, instance):
        """Have Paperclip back itself up now, so the import has today's last changes."""
        try:
            path = paperclip_local.fresh_backup(instance)
        except (paperclip_local.LocalError, OSError, subprocess.SubprocessError) as error:
            raise ApiError(str(error)) from None
        return {"ok": True, "message": "Paperclip made a fresh backup.", "backup": str(path)}

    def paperclip_stage(self, zip=None, folder=None, github=None, instance=None, backup=None):
        """Get an import ready: Paperclip on this computer (`instance`, optionally a `backup`),
        a backup file, the exported zip (a data URL), a folder, or a GitHub repository."""
        return {"token": _call(lambda: paperclip_import.stage(data_url=zip, folder=folder, github=github,
                                                              instance=instance, backup=backup))}

    def paperclip_preview(self, token):
        return _call(lambda: paperclip_import.preview(token))

    def paperclip_apply(self, token, company, agents=(), skills=(), goals=(), projects=None, routines=(), heartbeats=(),
                        tasks="open", secrets=(), secret_values=False, history=False, documents=()):
        return _call(lambda: paperclip_import.apply(
            token, company, agents=agents, skills=skills, goals=goals, projects=projects or {}, routines=routines,
            heartbeats=heartbeats, tasks=tasks, secrets=secrets, secret_values=bool(secret_values), history=bool(history),
            documents=documents))

    def backlog(self, which="open", project=None):
        """Tasks waiting to be started, and finished ones kept as history (backlog.py)."""
        return {"items": backlog.items(which, project), "counts": backlog.counts()}

    def backlog_item(self, item_id):
        item = _call(lambda: backlog.get(item_id))
        return {**item, "start_text": backlog.start_text(item)}

    def backlog_status(self, item_id, status, workspace=None):
        _call(lambda: backlog.set_status(item_id, status, workspace))
        return {"ok": True}

    # --- export and import

    def export_team(self, path="", teams=(), catalog=(), skills=(), templates=()):
        from datetime import datetime
        path = path or str(config_store.SESSIONS_DIR / "exports" /
                           f"lago-team-{datetime.now():%Y%m%d-%H%M}.tar.gz")
        manifest = _call(lambda: portability.export(path, teams, catalog, skills, templates))
        return {"ok": True, "path": path, "files": len(manifest["files"]),
                "message": f"Exported to {path}"}

    def preview_import(self, path):
        return _call(lambda: portability.preview(path))

    def apply_import(self, path):
        plan = _call(lambda: portability.apply(path))
        return {"ok": True, "message": f"Imported {len(plan['new'])} new files, {len(plan['teams_new'])} "
                f"teams and {len(plan['agents_new'])} agents."}

    # --- goals

    def goals(self, project=None, include_closed=False):
        out = []
        for goal in goals.all_goals(project, include_closed):
            progress = goals.progress(goal["id"])
            out.append({**goal, "counts": progress["counts"], "sessions": progress["sessions"]})
        return out

    def save_goal(self, goal_id=None, **fields):
        if goal_id:
            return _call(lambda: goals.update(goal_id, **fields))
        return _call(lambda: goals.add(fields.get("title"), fields.get("project", ""),
                                       fields.get("description", ""), fields.get("parent_id"),
                                       fields.get("target", "")))

    def link_goal(self, workspace_id, goal_id=None):
        _call(lambda: goals.link(workspace_id, goal_id))
        return {"ok": True}

    def answer_question(self, event_id, answers, note=""):
        return _call(lambda: questions.answer(event_id, answers, note))

    def judge_decision(self, event_id, verdict, note=""):
        return _call(lambda: decisions.judge(event_id, int(verdict), note))

    def set_session_quiet(self, workspace_id, on):
        """A quiet session speaks and rings the bell only for what needs you."""
        ws = _call(lambda: swarm.get_workspace(workspace_id))
        hub.update_workspace(ws["id"], quiet=1 if on else 0)
        return {"ok": True, "message": f"Session #{ws['id']} is {'quiet' if on else 'no longer quiet'}."}

    def workspace(self, workspace_id):
        workspace = _call(lambda: swarm.get_workspace(int(workspace_id)))
        config = config_store.load()
        cid = config_store.conductor_id(workspace["id"])
        state = hub.get_state(cid)
        running = runner.running_agents()
        status = _live_status(state, cid in running, time.time())
        pipelines = [self._pipeline_view(swarm.sync(p))
                     for p in hub.pipelines("workspace = ?", (workspace["id"],), limit=50)]
        agents = [aid for aid, a in config["agents"].items()
                  if a.get("workspace") == workspace["id"] and not a.get("conductor")]
        latest = hub.events("workspace = ? AND kind NOT IN ('update', 'decided')", (workspace["id"],), limit=1)
        states = hub.all_states()
        return {**workspace, "conductor": cid, "status_live": status,
                "activity": notifications.session_status(workspace, _live_all(states, running), states),
                "solved": bool(latest) and latest[0]["kind"] == "solved",
                "detail": state["detail"] if cid in running or status == "exited" else "",
                "screen": runner.capture(cid, 30) if status in SCREEN_STATUSES else "",
                "pipelines": pipelines, "agents": agents,
                "budget": budgets.session_state(workspace["id"], config),
                "goal": goals.get(workspace["goal_id"])["title"] if workspace.get("goal_id") else "",
                "questions": [{**e, "form": json.loads(e["form"])} for e in hub.events(
                    "workspace = ? AND kind = 'question' AND form IS NOT NULL AND answered_at IS NULL",
                    (workspace["id"],), limit=5)]}

    def session_changes(self, workspace_id):
        """The files a work session changed (changes.py)."""
        return _call(lambda: changes.summary(int(workspace_id)))

    def session_change_diff(self, workspace_id, path, branch=None):
        return {"diff": _call(lambda: changes.diff(int(workspace_id), path, branch or None))}

    def browser_view(self, agent_id):
        """What the agent's browser shows now, if it has one open (browser_live.py)."""
        return browser_live.view(agent_id)

    # --- standing tool permissions (toolrules.py)

    def tool_rules(self):
        return {"rules": toolrules.rules(), "projects": {pid: p.get("name", pid) for pid, p in config_store.load()["projects"].items()}}

    def add_tool_rule(self, tool, decision, agent="*", project="", pattern="*", note=""):
        rule_id = _call(lambda: toolrules.add(tool, decision, agent, project, pattern, note))
        return {"ok": True, "id": rule_id, "message": "Saved. It applies from the next tool call."}

    def remove_tool_rule(self, rule_id):
        _call(lambda: toolrules.remove(rule_id))
        return {"ok": True, "message": "Removed."}

    def secret_requests(self):
        return {"pending": secret_requests.requests("pending"),
                "recent": [r for r in secret_requests.requests(None) if r["status"] != "pending"][:10]}

    def give_secret(self, request_id, value, for_all=False):
        _call(lambda: secret_requests.give(int(request_id), value, bool(for_all)))
        return {"ok": True, "message": "Saved in the vault. The agent was told how to load it."}

    def decline_secret(self, request_id, note=""):
        _call(lambda: secret_requests.decline(int(request_id), note))
        return {"ok": True, "message": "Declined. The agent was told."}

    def tool_rule_suggestion(self, agent_id):
        return toolrules.suggestion(agent_id)

    def allow_always(self, agent_id, tool, pattern="*", agent="*", project=""):
        """Approve what the agent is asking now, and from now on (a standing rule)."""
        _call(lambda: toolrules.add(tool, "allow", agent, project, pattern))
        self.press(agent_id, "approve")
        return {"ok": True, "message": "Approved, now and from now on."}

    # --- orchestrator and voice

    def orchestrator(self):
        """The Orchestrator's status, the open work sessions and what they reported."""
        config = config_store.load()
        oid = config_store.ORCHESTRATOR_ID
        running = runner.running_agents()
        states = hub.all_states()
        now = time.time()
        state = states.get(oid) or hub.get_state(oid)
        status = _live_status(state, oid in running, now)
        if status == "working" and now - (state.get("updated_at") or now) > 20 \
                and memory.between_turns(state.get("session_id")):
            status, state = "idle", {**state, "detail": ""}  # a stale record: his turn ended
        sessions = []
        live = _live_all(states, running)
        for ws in hub.workspaces("status = 'open'"):
            cid = config_store.conductor_id(ws["id"])
            latest = hub.pipelines("workspace = ?", (ws["id"],), limit=1)
            event = hub.events("workspace = ?", (ws["id"],), limit=1)
            sessions.append({**ws, "conductor": cid,
                             "live": notifications.session_status(ws, live, states),
                             "detail": (states.get(cid) or {}).get("detail", "") if cid in running else "",
                             "pipeline": {k: latest[0][k] for k in ("id", "title", "status")} if latest else None,
                             "event": event[0] if event else None})
        return {"status": status, "detail": state["detail"] if oid in running or status == "exited" else "",
                "screen": runner.capture(oid, 30) if status in SCREEN_STATUSES else "",
                "conversation": _conversation(state.get("session_id")),
                "activity": _activity(state.get("session_id")) if status == "working" else [],
                "schedules": [_schedule(row) for row in schedules.active()],
                "speaking": voice.playing(), "voice_queued": voice.speaking(), "rotating": config_store.rotating(),
                "model": config_store.orchestrator_model(config), "models": LAGO_MODELS,
                "mode": config_store.lago_mode(config), "start_card": bool(config.get("start_card")),
                "user_name": config.get("user_name") or "",
                "days": [{"day": d["day"], "headlines": d["headlines"]} for d in memory.days(3)],
                "sessions": sessions, "events": decisions.with_verdicts(hub.events(limit=40)),
                "runs": _agent_runs(states, running, now),
                "voice": voice.settings(config), "voices": voice.voices(), "voice_setup": voice_setup.status(),
                "effects": [{"id": k, "label": v} for k, v in voice.EFFECTS.items()],
                "wake_sounds": [{"id": k, "label": v} for k, v in voice.WAKE_SOUNDS.items()
                                if not k.startswith("/") or Path(k).is_file()],  # the desktop's own sounds vary
                "chime_categories": [{"id": k, "label": v} for k, v in voice.CATEGORIES.items()],
                "kinds": list(voice.PHRASES), "user_name": config.get("user_name") or "",
                "mic": features.on(config, "voice") and mic.available(), "recording": mic.recording(),
                "unrestricted": oid in running and config_store.ORCHESTRATOR_UNRESTRICTED.exists(),
                "skip_permissions": _skip_permissions(config), "focus": _focus_card(config)}

    def lago_memory(self):
        """What Lago remembers: the about-you note and his summaries of past days."""
        return {"about": memory.about(), "threads": memory.threads(), "folder": str(memory.MEMORY_DIR),
                "days": [{"day": d["day"], "text": d["text"]} for d in memory.days(30)]}

    def save_lago_memory(self, about, threads=None):
        memory.save_about(about if isinstance(about, str) else "")
        if isinstance(threads, str):
            memory.save_threads(threads)
        return {"ok": True, "message": "Saved. Lago reads it when his next conversation starts."}

    def set_lago_mode(self, mode, tell=True):
        """Check with me (every decision comes to you) or Decide for me (Lago takes the routine
        and important ones himself, logged; only critical ones come to you)."""
        if mode not in config_store.LAGO_MODES:
            raise ApiError(f"Unknown mode {mode!r}")
        config = config_store.without_swarm(config_store.load())
        config["orchestrator"] = {**(config.get("orchestrator") or {}), "mode": mode}
        config_store.save(config)
        if tell and runner.is_running(config_store.ORCHESTRATOR_ID):
            rules = " ".join(team.MODE_RULES[mode][1:])
            notify.tell_orchestrator(f"[Lago] Mode: {team.MODE_RULES[mode][0][9:]}. {rules}", start=False)
        on_opus = config_store.orchestrator_model(config_store.load()) in config_store.DECIDING_MODELS
        return {"ok": True, "message": "Lago decides for you now, and brings you only the critical decisions."
                + ("" if on_opus else " He moves to Opus when he next starts.")
                if mode == "decide" else "Lago brings every decision to you now."}

    def set_lago_model(self, model):
        if model not in LAGO_MODELS:
            raise ApiError(f"Unknown model {model!r}")
        config = config_store.without_swarm(config_store.load())
        config["orchestrator"] = {**(config.get("orchestrator") or {}), "model": model}
        config_store.save(config)
        return {"ok": True, "message": "Saved. Lago switches model when he next starts."}

    def set_persona(self, persona, tell=True):
        """Lago, Chip or Atlas: the same assistant and memory with another name, face and voice.
        Sets his voice and effect to the persona's (you can still change them after), and
        "Hey <name>" as the wake phrase when it was the old name's."""
        if persona not in personas.PERSONAS:
            raise ApiError(f"Unknown persona {persona!r}")
        config = config_store.without_swarm(config_store.load())
        old_name = personas.name(config)
        chosen = personas.PERSONAS[persona]
        config_store.save(personas.use(config, persona))
        if tell and old_name != chosen["name"] and runner.is_running(config_store.ORCHESTRATOR_ID):
            notify.tell_orchestrator(personas.switch_note(persona), start=False)
        return {"ok": True, "persona": persona, "message": f"You're talking to {chosen['name']} now."}

    def voice_state(self):
        """Whether his voice is coming out now, and whether any is still to come (queued or
        being made). The UI asks every 150 ms while something is queued, for his mouth."""
        return {"playing": voice.playing(), "queued": voice.speaking()}

    def preview_persona(self, persona):
        """The persona introduces himself aloud, in his voice and effect."""
        if persona not in personas.PERSONAS:
            raise ApiError(f"Unknown persona {persona!r}")
        config = config_store.load()
        chosen = personas.PERSONAS[persona]
        settings = {**voice.settings(config), "voice": chosen["voice"], "effect": chosen["effect"], "chime": False}
        voice.hush()  # you asked to hear him now: not after what's being said, or the last preview
        if not voice.say(personas.intro(persona, config.get("user_name") or ""), settings, "progress"):
            raise ApiError("Couldn't play his voice: check that the offline voices are installed.")
        return {"ok": True}

    def welcome(self, password="", restricted=False, mode=None, first=False):
        """Once per app start: say hello aloud and have the Orchestrator give you the day's
        picture and ask what you'll work on. Skipped if you were greeted minutes ago. When
        the Orchestrator should skip permissions and isn't running, the first call returns
        needs_password; call again with the password, or with restricted=True."""
        if self.welcomed or onboarding.needed(config_store.load()):
            return {"ok": True}  # the welcome wizard comes first
        if mode:
            self.set_lago_mode(mode, tell=False)
        config = config_store.load()
        oid = config_store.ORCHESTRATOR_ID
        unrestricted = not runner.is_running(oid) and _skip_permissions(config) and not restricted
        if unrestricted:
            if not password and not config_store.bypass_unlocked():
                return {"needs_password": True}
            _check_password(password)  # before he says anything: a wrong one is asked again
        settings = voice.settings(config)
        stamp = config_store.STATE_DIR / "last-welcome"
        try:
            recent = time.time() - float(stamp.read_text()) < WELCOME_AGAIN_SECONDS
        except (OSError, ValueError):
            recent = False
        greet = not first and settings["greet"] and not recent
        if greet and settings["enabled"] and not voice.quiet_now(settings):
            # Said first: writing down the last conversation can take seconds (Haiku).
            voice.say(voice.greeting(config), settings)
        _write_down_last_conversation()
        if unrestricted:
            self.start_orchestrator(password=password, unrestricted=True)
        self.welcomed = True
        if first:  # straight after the welcome wizard
            stamp.write_text(str(time.time()))
            name = config.get("user_name") or "The human"
            _call(lambda: notify.tell_orchestrator(
                f"[Lago] {name} just set Lago Agents up for the first time"
                + (f" and added their first project ({', '.join(p.get('name', k) for k, p in config['projects'].items())})"
                   if config["projects"] else "")
                + ". Greet them by name in two or three short sentences: who you are and what you can do "
                "for them (run their work with a team of agents, keep track of it, decide routine things "
                "for them). The app shows them two choices under your message: \"Plan something with "
                "me\" or \"I have a task in mind\". Don't list features; end by asking what they'd like "
                "to do first."))
            return {"ok": True}
        if not greet:
            return {"ok": True}
        stamp.write_text(str(time.time()))
        import lago_mcp  # the same lines his work_sessions tool gives, saving him that call
        name = config.get("user_name") or "The human"
        _call(lambda: notify.tell_orchestrator(
            f"[Lago] {name} just opened Lago Agents ({time.strftime('%A %H:%M')}). You already "
            "greeted them aloud and asked what they'll work on today. Give the short status "
            "(what finished, what needs them now in these sessions) and the one or two pieces "
            "of work from \"Where things stand\" most worth picking up, then ask what they "
            "want to work on today. For each open task below that's stuck (stopped, quiet, "
            "waiting on usage that has room again, or waiting on them), say in one line where "
            "it stands and ask whether to carry on or abandon it.\n\nWork sessions now:\n"
            + lago_mcp.tool_work_sessions() + "\n\nOpen runs now:\n" + open_tasks.brief()))
        return {"ok": True}

    def start_orchestrator(self, password="", unrestricted=None, mode=None):
        """Start the Orchestrator, or restart it (keeping its conversation) to change mode.
        Unrestricted needs your password: it and its work sessions skip permission checks.
        `mode` (check or decide) is saved first, so he starts in it."""
        oid = config_store.ORCHESTRATOR_ID
        if unrestricted is None:
            unrestricted = _skip_permissions(config_store.load())
        if unrestricted:
            _check_password(password)
        if mode:
            self.set_lago_mode(mode, tell=False)
        if runner.is_running(oid):
            team.stop(oid)
        _write_down_last_conversation()
        if unrestricted:
            config_store.ORCHESTRATOR_UNRESTRICTED.touch()
        _call(lambda: team.start(oid))
        return {"ok": True, "message": "Starting Lago without permission checks."
                if unrestricted else "Starting Lago."}

    def restart_lago(self):
        """Lago is stuck: end his session for good and start him again on the same conversation,
        keeping his mode. Nothing he was in the middle of finishes."""
        _call(team.restart_orchestrator)
        return {"ok": True, "message": "Restarting Lago. He keeps this conversation and his notes."}

    def bypass_unlocked(self):
        """Whether your password was already given since the app opened (no need to ask)."""
        return config_store.bypass_unlocked()

    def new_conversation(self):
        """Lago starts a fresh conversation from his notes (the current one is written down)."""
        memory.rotate_later(clear=True)
        config_store.set_focus(None)
        return {"ok": True, "message": "Starting a new conversation. He keeps his notes."}

    def set_skip_permissions(self, on):
        """Whether the Orchestrator asks for your password to run without permission checks."""
        config = config_store.without_swarm(config_store.load())
        config["orchestrator"] = {**(config.get("orchestrator") or {}), "skip_permissions": bool(on)}
        config_store.save(config)
        return {"ok": True}

    def set_lago_focus(self, agent_id=None):
        """Work with one swarm agent through Lago, or (None) stop. He's told, and asks what
        it should do."""
        was = config_store.focus()
        if agent_id:
            entry = swarm.enabled_agents(config_store.load()).get(agent_id)
            if entry is None:
                raise ApiError(f"There's no switched-on swarm agent {agent_id!r}")
            config_store.set_focus(agent_id)
            note = (f"[Lago] {config_store.load().get('user_name') or 'The human'} picked "
                    f"{entry['name']} on the Talk to Lago page.\n{swarm.focus_brief(entry)}\n"
                    "Ask them, in one short line, what it should do.")
        else:
            config_store.set_focus(None)
            if not was:
                return {"ok": True}
            note = f"[Lago] They stopped using `{was}`: back to normal."
        _call(lambda: notify.tell_orchestrator(note))
        return {"ok": True}

    def say_to_orchestrator(self, text, spoken=False, files=(), keep=()):
        """Send Lago what you typed or said, with the files you attached (data URLs) and
        ones you sent before and are sending again (`keep`, paths: an edited message's)."""
        text = _focused(_with_files(text, files, keep))
        onboarding.dismiss_start_card()
        oid = config_store.ORCHESTRATOR_ID
        status = hub.get_state(oid)["status"] if runner.is_running(oid) else "stopped"
        _call(lambda: notify.tell_orchestrator(f"(spoken) {text}" if spoken else text))
        if status in ("stopped", "starting", "connecting"):
            return {"ok": True, "message": "Lago is waking up: he gets your message as soon "
                    "as his tools are connected."}
        return {"ok": True, "message": "Queued: Lago reads it when he finishes "
                "what it's doing." if status != "idle" else "Sent."}

    def rewind_lago(self, message_id, text, files=(), keep=()):
        """Edit one of your messages, as Esc Esc does in Claude Code: Lago goes back to just
        before it, forgetting it and everything after, and gets the new version instead.
        His session restarts from a copy of the conversation cut there (the old one is
        kept, untouched, as Claude Code keeps a rewound one)."""
        oid = config_store.ORCHESTRATOR_ID
        text = _focused(_with_files(text, files, keep))
        if config_store.rotating():
            raise ApiError("Lago is writing down the last conversation; try again in a moment")
        previous = hub.get_state(oid).get("session_id")
        try:
            forked = transcripts.fork_before(previous, message_id)
        except transcripts.TranscriptError as error:
            raise ApiError(str(error)) from error
        unrestricted = config_store.ORCHESTRATOR_UNRESTRICTED.exists()
        if runner.is_running(oid):
            team.stop(oid)
            time.sleep(1)  # let the old session's exit hooks finish first
        if unrestricted:
            config_store.ORCHESTRATOR_UNRESTRICTED.touch()
        hub.set_state(oid, session_id=forked)
        _call(lambda: team.start(oid, initial_text=text, keep_session=True,
                                 fresh=forked is None, carry=False))
        return {"ok": True, "message": "Sent the edited message."}

    def clipboard_files(self):
        """What Ctrl+V should attach for Lago: pictures or files on the system clipboard."""
        import clipboard  # GTK: only the app's main thread calls this
        return {"files": clipboard.files()}

    def cancel_schedule(self, schedule_id):
        try:
            schedules.cancel(schedule_id)
        except schedules.ScheduleError as error:
            raise ApiError(str(error)) from error
        return {"ok": True, "message": "Reminder cancelled."}

    def open_attachment(self, path):
        """Open a file you sent Lago in its usual app."""
        if not attachments.kept(path):
            raise ApiError("That file is no longer kept")
        if not desktop.open_target(str(path)):
            raise ApiError("Couldn't open it")
        return {"ok": True}

    def set_talk(self, on):
        """Lago saying the Orchestrator's replies aloud: on or off (off also hushes him)."""
        config = config_store.without_swarm(config_store.load())
        config["voice"] = {**voice.settings(config), "talk": bool(on)}
        config_store.save(config)
        if not on:
            voice.hush()
        return {"ok": True, "message": "Lago talks again." if on else "Lago is quiet now."}

    def mic_start(self):
        if not features.on(config_store.load(), "voice"):
            raise ApiError("Voice is off (Settings, Features)")
        try:
            mic.start()
        except mic.MicError as error:
            raise ApiError(str(error)) from error
        return {"ok": True}

    def mic_stop(self, send=True):
        try:
            text = mic.stop_and_transcribe()
        except mic.MicError as error:
            raise ApiError(str(error)) from error
        if text and send:
            self.say_to_orchestrator(text, spoken=True)
        return {"text": text, "sent": bool(text and send)}

    def mic_cancel(self):
        mic.cancel()
        return {"ok": True}

    def mark_events_seen(self):
        hub.mark_events_seen()
        return {"ok": True}

    def save_voice(self, settings):
        if not isinstance(settings, dict):
            raise ApiError("Voice settings must be an object")
        clean = {**voice.DEFAULTS, **{k: v for k, v in settings.items() if k in voice.DEFAULTS}}
        for key in ("quiet_from", "quiet_to"):
            if clean[key] and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(clean[key])):
                raise ApiError("Quiet hours are times like 22:30")
        if bool(clean["quiet_from"]) != bool(clean["quiet_to"]):
            raise ApiError("Quiet hours need both a start and an end")
        try:
            clean["rate"] = max(-100, min(100, int(clean["rate"])))
        except (TypeError, ValueError) as error:
            raise ApiError("The speed must be a number from -100 to 100") from error
        try:
            clean["volume"] = max(voice.VOLUME_MIN, min(voice.VOLUME_MAX, int(clean["volume"])))
        except (TypeError, ValueError) as error:
            raise ApiError(f"The volume must be a number from {voice.VOLUME_MIN} to {voice.VOLUME_MAX}") from error
        clean["kinds"] = [k for k in clean["kinds"] if k in voice.PHRASES]
        clean["kinds_known"] = list(voice.PHRASES)  # what you chose from, so new kinds can be told apart
        chimes = settings.get("chimes") if isinstance(settings.get("chimes"), dict) else {}
        clean["chimes"] = {category: chimes[category] if chimes.get(category) in voice.WAKE_SOUNDS else default
                           for category, default in voice.DEFAULTS["chimes"].items()}
        if clean["effect"] not in voice.EFFECTS:
            raise ApiError(f"Unknown voice effect {clean['effect']!r}")
        clean["enabled"], clean["chime"] = bool(clean["enabled"]), bool(clean["chime"])
        clean["wake"], clean["wake_personas"] = bool(clean["wake"]), bool(clean["wake_personas"])
        for key in ("wake_sound", "sent_sound"):
            if clean[key] not in voice.WAKE_SOUNDS:
                raise ApiError(f"Unknown sound {clean[key]!r}")
        clean["wake_phrase"] = " ".join(str(clean["wake_phrase"]).split())[:40] or "Hey Lago"
        config = config_store.without_swarm(config_store.load())
        was_listening = voice.settings(config)["wake"]
        if clean["wake"] != was_listening or (clean["wake"] and not listen.service_running()):
            try:  # nothing that can hear runs unless you switched it on
                listen.set_service(clean["wake"])
            except listen.ListenError as error:
                raise ApiError(str(error)) from error
        config["voice"] = clean
        config_store.save(config)
        return {"ok": True, "message": "Saved."}

    def lago_say(self, text, category="progress"):
        """Say one of Lago's spoken lines again."""
        voice.hush()
        category = category if category in voice.CATEGORIES else "progress"
        if not voice.say(team.clip(str(text or "").strip(), 300), voice.settings(config_store.load()), category):
            raise ApiError("No voice is installed.")
        return {"ok": True}

    def play_wake_sound(self, choice):
        voice.play_file(voice.wake_sound(choice if choice in voice.WAKE_SOUNDS else "soft"))
        return {"ok": True}

    def test_voice(self, settings=None, kind="done"):
        config = config_store.load()
        current = {**voice.settings(config), **(settings or {})}
        project = next(iter(config["projects"].values()), {}).get("name", "your project")
        if not voice.say(voice.phrase(config, kind, project), current, voice.KIND_CATEGORY.get(kind, "progress")):
            raise ApiError("No voice is installed: install Lago's voices in his settings.")
        return {"ok": True, "message": "Speaking…"}

    def install_voices(self):
        """Lago's own voices and the speech engines, downloaded in the background."""
        voice_setup.start()
        return {"ok": True, **voice_setup.status()}

    def voice_setup_status(self):
        return voice_setup.status()

    # --- services

    def _project_config(self, project_id):
        project = config_store.load()["projects"].get(project_id)
        if project is None:
            raise ApiError(f"There's no project {project_id!r}")
        return project

    def services_status(self, project_id, refresh=False):
        return services.statuses(self._project_config(project_id), refresh=bool(refresh))

    def service_action(self, project_id, service_id, action):
        project = self._project_config(project_id)
        log = _call(lambda: services.service_action(project_id, project, service_id, action))
        cache.forget("services:")  # its status is about to change
        return {"ok": True, "message": f"{'Starting' if action == 'start' else 'Stopping'}… "
                                       f"Output goes to {log}"}

    def project_command(self, project_id, which):
        project = self._project_config(project_id)
        if which not in ("start_all", "stop_all"):
            raise ApiError(f"Unknown command {which!r}")
        command = (project.get("commands") or {}).get(which)
        log = _call(lambda: services.run(project_id, project, which, command))
        return {"ok": True, "message": f"Running. Output goes to {log}"}

    def service_logs(self, project_id, service_id):
        project = self._project_config(project_id)
        return _call(lambda: services.logs(project_id, project, service_id))

    # --- server

    def server_status(self, project_id, server_id="", refresh=False):
        project = self._project_config(project_id)
        return _call(lambda: server.status(project_id, project, server_id or None, refresh=bool(refresh)))

    def server_connect(self, project_id, server_id=""):
        project = self._project_config(project_id)
        session = _call(lambda: server.open_shell(project_id, project, server_id or None))
        return {"ok": True, "message": "Connecting…", "terminal": session}

    def server_deploy_preview(self, project_id, server_id=""):
        project = self._project_config(project_id)
        return {"script": _call(lambda: server.deploy_script(project, server_id or None))}

    def server_deploy(self, project_id, server_id=""):
        project = self._project_config(project_id)
        session = _call(lambda: server.deploy(project_id, project, server_id or None))
        return {"ok": True, "message": "Deploy started. Watch it in the terminal.", "terminal": session}

    def server_open_site(self, project_id, server_id=""):
        project = self._project_config(project_id)
        result = _call(lambda: server.open_site(project_id, project, server_id or None))
        return {"ok": True, "message": ("Started the local server and opened " if result["started"]
                                        else "Opened ") + result["url"]}

    def terminal_running(self, terminal_id):
        return {"running": runner.is_running(terminal_id)}

    # --- GitHub

    def github_repos(self, project_id, refresh=False):
        project = self._project_config(project_id)
        result = []
        for repo in github.repos(project):
            try:
                result.append(github.repo_activity(repo, refresh=bool(refresh)))
            except github.GitHubError as error:
                result.append({"repo": repo, "error": str(error)})
        return result

    def github_board(self, project_id, owner, number, refresh=False):
        project = self._project_config(project_id)
        if not any(b["owner"] == owner and int(b["number"]) == int(number)
                   for b in github.boards(project)):
            raise ApiError("That board isn't part of this project")
        return _call(lambda: github.board(owner, int(number), refresh=bool(refresh)))

    def pipelines(self):
        return [self._pipeline_view(swarm.sync(p)) for p in hub.pipelines(limit=100)]

    def pipeline(self, pipeline_id):
        return self._pipeline_view(_call(lambda: swarm.get(int(pipeline_id))))

    def decide_pipeline(self, pipeline_id, decision, note=""):
        _call(lambda: swarm.decide(int(pipeline_id), decision, note))
        return {"ok": True, "message": {"approve": "Approved. The Conductor starts running it.",
                                        "changes": "Sent back to the Conductor for changes.",
                                        "reject": "Rejected."}[decision]}

    def cancel_pipeline(self, pipeline_id):
        _call(lambda: swarm.cancel(int(pipeline_id)))
        return {"ok": True, "message": "Cancelled. Its running agents were stopped."}

    @staticmethod
    def _pipeline_view(pipeline):
        for step in pipeline["steps"]:
            step["agent_id"] = swarm.instance_id(pipeline["workspace"], step["agent"])
            task = hub.get_task(step["task_id"]) if step["task_id"] else None
            step["task_status"] = task["status"] if task else None
        return pipeline

    # --- live state

    def dashboard(self, days=dashboard.DAYS):
        """The Dashboard's charts and lists: tasks, spend and success rate per day."""
        return dashboard.summary(int(days))

    def overview(self):
        config = config_store.load()
        running = runner.running_agents()
        states = hub.all_states()
        now = time.time()
        open_tasks = hub.tasks("status IN ('queued', 'sent', 'working')", limit=1000)
        by_agent = {}
        for task in open_tasks:
            by_agent.setdefault(task["to_agent"], []).append(task)
        agents = {}
        for agent_id in config["agents"]:
            state = states.get(agent_id) or hub.get_state(agent_id)
            mine = by_agent.get(agent_id, [])
            current = next((t for t in mine if t["id"] == state.get("current_task")), None) \
                or next((t for t in mine if t["status"] == "working"), None)
            status = _live_status(state, agent_id in running, now)
            human_waiting = status == "idle" and any(t["from_agent"] is None for t in mine)
            agents[agent_id] = {
                "status": "needs_you" if human_waiting else status,
                "detail": state["detail"] if agent_id in running or status == "exited" else "",
                "since": state.get("updated_at"),
                "started_at": state.get("started_at") if agent_id in running else None,
                "open_tasks": len(mine),
                "task": {"id": current["id"], "title": current["title"]} if current else None,
            }
        swarm_agents = {aid: a for aid, a in config["agents"].items() if a.get("swarm")}
        waiting = len(hub.pipelines("status = 'draft'"))
        unseen = len(hub.events("seen = 0 AND kind != 'update'", limit=99))
        alert = accounts.alert()
        return {"agents": agents, "open_tasks": len(open_tasks), "swarm_agents": swarm_agents,
                "pipelines_waiting": waiting, "reports_unseen": unseen,
                "lago_speaking": voice.playing(), "lago_voice_queued": voice.speaking(),
                "account_alert": alert, "persona": personas.current(config),
                "notifications": notifications.route(notifications.build(config, agents, alert, now),
                                                     readstate.viewer(), company.handlers(config)),
                "open_sessions": notifications.open_sessions(agents)}

    def dismiss_notification(self, key):
        notifications.dismiss(key)
        return {"ok": True}

    def notification_opened(self, key):
        notifications.opened(key)
        return {"ok": True}

    def agent_detail(self, agent_id):
        config = config_store.load()
        agent = _agent(config, agent_id)
        state = hub.get_state(agent_id)
        running = runner.is_running(agent_id)
        status = _live_status(state, running, time.time())
        path = transcripts.find(state.get("session_id"))
        transcript = transcripts.read(path) if path else {"events": [], "cost": None, "tokens": 0,
                                                           "input_tokens": 0, "model": ""}
        tasks = hub.tasks_involving(agent_id, limit=60)
        spent = hub.get_spend(state["session_id"]) if state.get("session_id") else None
        if path and spent is not None and transcript.get("api_cost", 0) > spent["cost"] + 0.005:
            # The log is ahead of the last budget check (the hooks measure between turns).
            hub.record_spend(state["session_id"], agent_id, spent["workspace"], transcript["api_cost"])
        if status == "idle" and any(t["to_agent"] == agent_id and t["from_agent"] is None
                                    and t["status"] in hub.OPEN_TASK for t in tasks):
            status = "needs_you"
        return {
            "status": status,
            "detail": state["detail"] if running or status == "exited" else "",
            "since": state.get("updated_at"),
            "started_at": state.get("started_at") if running else None,
            "session_id": state.get("session_id"),
            "cwd": str(config_store.team_cwd(config, agent)),
            "events": transcript["events"][-ACTIVITY_LIMIT:],
            "cost": transcript["cost"],
            "tokens": transcript["tokens"],
            "account_label": accounts.all_accounts(config).get(
                state.get("account") or accounts.MAIN, {}).get("label", state.get("account")),
            "input_tokens": transcript["input_tokens"],
            "model": transcript["model"],
            "tasks": tasks,
            "screen": runner.capture(agent_id, 30)
            if status in SCREEN_STATUSES else "",
            "budgets": budgets.state(agent_id, state.get("session_id"), config),
        }

    def terminal_snapshot(self, agent_id, lines=200):
        """An agent's screen and recent scrollback, with colours (ANSI): the live map's view of
        its output. (The phone server's version also tracks narrowed terminals.)"""
        if agent_id not in config_store.load()["agents"]:
            raise ApiError(f"No agent {agent_id!r}")
        if not runner.is_running(agent_id):
            return {"running": False, "text": ""}
        lines = max(0, min(int(lines), 2000))
        ok, text = runner.tmux("capture-pane", "-p", "-e", "-t", f"={runner.session_name(agent_id)}:", "-S", f"-{lines}")
        return {"running": ok, "text": redact.text(text) if ok else ""}

    def terminal_wall(self, project=None, lines=30):
        """Every running agent's screen in a project, for the Live Map's wall of terminals: the
        agents its map shows, each with the bottom of its screen in colour (ANSI), secrets hidden."""
        lines = max(5, min(int(lines), 120))
        graph = self.project_map(project or "")
        tasks = {n["id"]: n["label"] for n in graph["nodes"] if n["kind"] == "task"}
        out = []
        for node in graph["nodes"]:
            if node["kind"] not in ("lead", "agent", "run") or not node["running"]:
                continue
            ok, text = runner.tmux("capture-pane", "-p", "-e", "-t", f"={runner.session_name(node['id'])}:")
            if not ok:
                continue
            screen = text.rstrip("\n").split("\n")
            while screen and not screen[-1].strip():
                screen.pop()
            lead = next((n for n in graph["nodes"] if n["id"] == node["parent"]), {})
            out.append({"id": node["id"], "label": node["label"], "kind": node["kind"], "status": node["status"],
                        "detail": node["detail"], "task": tasks.get(node["parent"]) or tasks.get(lead.get("parent")) or "",
                        "text": redact.text("\n".join(screen[-lines:]))})
        return out

    def live_activity(self, project=None, after=0, limit=40):
        """What agents are doing, newest first, in plain words (live.py)."""
        return live.recent(config_store.load(), after=after, limit=limit, project=project)

    def timeline(self, project=None, hours=24):
        """Each task's plan against time: running, waiting for approval, or finished lately."""
        hours = max(1, min(int(hours), 24 * 14))
        return live.timeline(config_store.load(), project=project, hours=hours)

    def replay(self, pipeline_id):
        """A plan's history to play back on the Live Map: its steps and what each agent did."""
        try:
            return live.replay(config_store.load(), int(pipeline_id))
        except KeyError:
            raise ApiError(f"There's no plan #{pipeline_id}") from None

    def file_activity(self, project, hours=24, tree=True):
        """A project's files, lit by what agents read and edited lately (live.py)."""
        config = config_store.load()
        if project not in config["projects"]:
            raise ApiError(f"There's no project {project!r}")
        return live.files(config, project, hours=max(1, min(int(hours), 24 * live.KEEP_DAYS)), with_tree=bool(tree))

    def project_map(self, project_id):
        """A project as a live graph: its open tasks, each task's lead and the agents it woke,
        and agents running alone in it, with what each is doing now."""
        config = config_store.load()
        project_id = project_id or ""
        if project_id and project_id not in config["projects"]:
            raise ApiError(f"There's no project {project_id!r}")
        states, running, now = hub.all_states(), runner.running_agents(), time.time()
        statuses = _live_all(states, running)
        open_tasks = {}
        for task in hub.tasks("status IN ('queued', 'blocked', 'sent', 'working')", limit=1000):
            open_tasks.setdefault(task["to_agent"], []).append(task)

        def agent_node(agent_id, label, kind, parent):
            state = states.get(agent_id) or {}
            mine = open_tasks.get(agent_id, [])
            current = next((t for t in mine if t["id"] == state.get("current_task")), None) or (mine[0] if mine else None)
            return {"id": agent_id, "kind": kind, "label": label, "parent": parent,
                    "status": statuses.get(agent_id, "stopped"), "running": agent_id in running,
                    "detail": state.get("detail", "") if agent_id in running else "",
                    "task": {"id": current["id"], "title": current["title"]} if current else None,
                    "since": state.get("updated_at"), "codex": state.get("account") == "codex"}
        nodes, edges = [], []
        name = config["projects"].get(project_id, {}).get("name", project_id) if project_id else config_store.NO_PROJECT
        nodes.append({"id": "project", "kind": "project", "label": name})
        catalog = agentdefs.catalog()
        for ws in hub.workspaces("status = 'open' AND project = ?", (project_id,)):
            tid = f"task-{ws['id']}"
            question = hub.events("workspace = ? AND kind = 'question' AND form IS NOT NULL AND answered_at IS NULL",
                                  (ws["id"],), limit=1)
            nodes.append({"id": tid, "kind": "task", "label": ws["title"], "workspace": ws["id"],
                          "status": "needs_you" if question else notifications.session_status(ws, statuses, states),
                          "held": bool(ws.get("held_at")), "href": f"#work/{ws['id']}"})
            edges.append({"from": "project", "to": tid})
            lead = config_store.conductor_id(ws["id"])
            nodes.append(agent_node(lead, "Lead", "lead", tid))
            edges.append({"from": tid, "to": lead})
            for row in hub.swarm_agents():
                if row["workspace"] == ws["id"]:
                    label = (catalog.get(row["catalog_id"]) or {}).get("name") or row["catalog_id"]
                    nodes.append(agent_node(row["agent_id"], label, "agent", lead))
                    edges.append({"from": lead, "to": row["agent_id"]})
        for row in hub.swarm_agents():
            if row["workspace"] or (row["project"] or "") != project_id or not config_store.RUN_ID.match(row["agent_id"]):
                continue
            if row["agent_id"] not in running and not open_tasks.get(row["agent_id"]):
                continue  # finished runs drop off the map
            label = (catalog.get(row["catalog_id"]) or {}).get("name") or row["catalog_id"]
            nodes.append(agent_node(row["agent_id"], label, "run", "project"))
            edges.append({"from": "project", "to": row["agent_id"]})
        return {"project": project_id, "name": name, "nodes": nodes, "edges": edges, "at": now,
                "handoffs": live.handoffs(nodes, now=now)}

    def my_tasks(self, days=14):
        """Your work as one list: every work session (shown as a task) and every agent Lago or
        New Task ran directly, open ones first, then those finished in the last `days`."""
        now = time.time()
        states = hub.all_states()
        running = runner.running_agents()
        live = _live_all(states, running)
        config = config_store.load()
        out = []
        for ws in hub.workspaces(limit=200):
            if ws["status"] == "closed" and now - ws["updated_at"] > days * 86400:
                continue
            event = hub.events("workspace = ? AND kind NOT IN ('update', 'decided')", (ws["id"],), limit=1)
            status = "closed" if ws["status"] == "closed" else notifications.session_status(ws, live, states)
            project = config["projects"].get(ws["project"], {}).get("name", ws["project"]) if ws["project"] \
                else config_store.NO_PROJECT
            out.append({"kind": "session", "id": ws["id"], "item": f"session:{ws['id']}", "title": ws["title"], "where": project, "project": ws["project"] or "",
                        "status": status, "open": ws["status"] == "open", "href": f"#work/{ws['id']}",
                        "last": (event[0]["text"].split("\n")[0][:160] if event else ""),
                        "at": ws["updated_at"], "created_at": ws["created_at"]})
        standing = {r["task_id"]: r for r in open_tasks.runs(now)}
        for run in _agent_runs(states, running, now):
            if run["test"]:
                continue
            where = standing.get(run["task_id"])
            out.append({"kind": "run", "standing": {k: where[k] for k in ("state", "says", "last", "can_continue")} if where else None, "id": run["task_id"], "item": f"agent:{run['agent_id']}", "title": run["title"], "where": run["name"],
                        "project": (config["agents"].get(run["agent_id"]) or {}).get("project") or "",
                        "status": run["live"] if run["status"] in hub.OPEN_TASK else run["status"],
                        "open": run["status"] in hub.OPEN_TASK, "href": f"#agent/{run['agent_id']}",
                        "last": run["result"].split("\n")[0][:160], "at": run["updated_at"],
                        "created_at": run["started_at"]})
        readstate.annotate(out)
        return sorted(out, key=lambda t: (not t["open"], -(t["at"] or 0)))

    def continue_task(self, task_id):
        """A run that stopped before finishing carries on, on its own conversation."""
        return {"ok": True, "message": _call(lambda: open_tasks.continue_run(task_id))}

    def abandon_task(self, task_id, reason=""):
        """Stop a run and close its task as cancelled."""
        return {"ok": True, "message": _call(lambda: open_tasks.abandon(task_id, reason))}

    def ask_lago_about_task(self, task_id):
        """Lago looks into one open task and tells you where it stands and what he'd do."""
        task = hub.get_task(int(task_id))
        if task is None:
            raise ApiError(f"There's no task #{task_id}")
        name = config_store.load().get("user_name") or "The human"
        _call(lambda: notify.tell_orchestrator(
            f"[Lago] {name} asks about task #{task['id']} \"{task['title']}\" on the Tasks page. "
            "Look at it (open_tasks, session_detail or the run's log), then tell them in a few "
            "lines where it stands and recommend one of: let it carry on, continue it now, or "
            "abandon it. Do what they choose; abandon only on their word."))
        return {"ok": True, "message": "Asked Lago. He'll answer in Talk to Lago."}

    def mark_read(self, items):
        """Mark work sessions or agents' runs (`session:<id>`, `agent:<id>`) read for whoever looks."""
        readstate.mark([str(i) for i in (items or [])][:500])
        return {"ok": True}

    def tasks(self):
        rows = hub.tasks(limit=300)
        with hub.connect() as conn:
            counts = dict(conn.execute("SELECT task_id, COUNT(*) FROM task_comments GROUP BY task_id").fetchall())
        for t in rows:
            t["comments"] = counts.get(t["id"], 0)
            t["labels"] = tickets.labels(t)
            t["blocked_by"] = tickets.blocked_by(t)
        return rows

    def task_comments(self, task_id):
        return tickets.comments(task_id)

    def comment_task(self, task_id, text, reopen=False):
        to = _call(lambda: tickets.comment(task_id, text, author="you", reopen=reopen))
        config = config_store.load()
        return {"ok": True, "message": f"Sent to {team.label(config, to)}." if to else "Comment saved."}

    def set_task_labels(self, task_id, labels):
        return _call(lambda: tickets.set_labels(task_id, labels))

    # --- control

    def start_agent(self, agent_id, fresh=False):
        _agent(config_store.load(), agent_id)
        _call(lambda: team.start(agent_id, fresh=bool(fresh)))
        return {"ok": True, "message": "Starting."}

    def stop_agent(self, agent_id):
        team.stop(agent_id)
        return {"ok": True, "message": "Stopped."}

    def start_team(self, team_id):
        config = config_store.load()
        lead = config_store.team_lead(config, team_id)
        if lead is None:
            raise ApiError("This team has no lead yet")
        if runner.is_running(lead):
            return {"ok": True, "message": "The lead is already running."}
        _call(lambda: team.start(lead))
        return {"ok": True, "message": f"Starting {config['agents'][lead]['name']}. "
                                       "Team members start when they get a task."}

    def stop_team(self, team_id):
        config = config_store.load()
        stopped = 0
        for agent_id, agent in config["agents"].items():
            if agent.get("team") == team_id and runner.is_running(agent_id):
                team.stop(agent_id)
                stopped += 1
        return {"ok": True, "message": f"Stopped {stopped} agent{'s' if stopped != 1 else ''}."}

    def give_task(self, agent_id, title, body):
        _agent(config_store.load(), agent_id)
        title, body = (title or "").strip(), (body or "").strip()
        if not body:
            raise ApiError("Describe the task first")
        task_id = _call(lambda: team.assign(agent_id, title or body.splitlines()[0][:80], body))
        return {"ok": True, "message": f"Task #{task_id} sent.", "task_id": task_id}

    def send_message(self, agent_id, text):
        _agent(config_store.load(), agent_id)
        if not (text or "").strip():
            raise ApiError("Write a message first")
        if not runner.is_running(agent_id):
            raise ApiError("This agent isn't running. Start it, or give it a task instead.")
        _call(lambda: team.message(agent_id, text.strip()))
        status = hub.get_state(agent_id)["status"]
        return {"ok": True, "message": "Sent." if status == "idle" else
                "Queued. It arrives when the agent finishes its current step."}

    def press(self, agent_id, key):
        """Answer the agent's prompt: approve (Enter), deny or interrupt (Escape)."""
        keys = {"approve": ["Enter"], "deny": ["Escape"], "interrupt": ["Escape"]}
        if key not in keys:
            raise ApiError(f"Unknown key {key!r}")
        if not runner.is_running(agent_id):
            raise ApiError("This agent isn't running")
        if hub.get_state(agent_id).get("account") == "codex":
            if key != "interrupt":
                raise ApiError("Agents on Codex never stop to ask permission.")
            keys[key] = ["C-c"]  # codex_session.py stops the running turn
        ok, output = runner.keys(agent_id, *keys[key])
        if not ok:
            raise ApiError(output)
        if key in ("approve", "deny"):
            hub.set_state(agent_id, status="working", detail="", lago_review=None)
        elif hub.get_state(agent_id).get("account") != "codex":
            _settle_after_interrupt(agent_id)
        return {"ok": True, "message": {"approve": "Approved.", "deny": "Denied.",
                                        "interrupt": "Interrupted."}[key]}

    def set_task(self, task_id, status, result=""):
        task = hub.get_task(int(task_id))
        if task is None:
            raise ApiError("That task no longer exists")
        if status == "cancelled":
            hub.update_task(task["id"], status="cancelled")
            tickets.release(task["id"])  # tells whoever handed out tasks waiting on it
            return {"ok": True, "message": f"Task #{task_id} cancelled."}
        if status in ("done", "failed"):
            team.finish(task["id"], result or "Marked by you in Lago Agents.", status)
            return {"ok": True, "message": f"Task #{task_id} marked {status}."}
        raise ApiError(f"Unknown task status {status!r}")

    # --- terminal (GTK)

    def show_terminal(self, agent_id):
        if not runner.is_running(agent_id):
            self.window.hide_terminal()
            return {"shown": False}
        self.window.show_terminal(agent_id)
        return {"shown": True}

    def hide_terminal(self):
        self.window.hide_terminal()
        return {"ok": True}

    def open_external(self, agent_id):
        if not runner.is_running(agent_id):
            raise ApiError("This agent isn't running")
        command = " ".join(runner.attach_argv(agent_id))
        argv = desktop.terminal_argv(command)
        if not argv:
            raise ApiError(f"No terminal app found (set $TERMINAL). Attach with: {command}")
        try:
            subprocess.Popen(argv, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            raise ApiError(f"Couldn't open a terminal: {error}") from error
        return {"ok": True, "message": "Opened in a terminal window."}

    def _find_file(self, path, sessions=None):
        """A file named in a message, or None. Absolute and ~ paths are taken as they are; a
        relative one is looked for in the folders of the work sessions the message mentions,
        then each project's folder, then Lago's. Only files in your home folder count."""
        raw = re.sub(r":\d+(?::\d+)?$", "", str(path or "").strip().strip("`'\"").rstrip(".,;)"))
        if not raw:
            return None
        wanted = Path(os.path.expanduser(raw))
        config = config_store.load()
        bases = []
        if not wanted.is_absolute():
            for number in sessions or []:
                try:
                    ws = hub.get_workspace(int(number))
                except (TypeError, ValueError):
                    continue
                if ws:
                    project = config["projects"].get(ws["project"])
                    bases.append(swarm.project_cwd(project) if project else Path(ws.get("folder") or "~").expanduser())
            bases += [swarm.project_cwd(p) for p in config["projects"].values()]
            bases.append(config_store.ORCHESTRATOR_DIR)
        home = Path.home().resolve()
        for candidate in ([base / wanted for base in bases] if bases else [wanted]):
            candidate = candidate.resolve()
            if candidate.is_file() and home in candidate.parents:
                return candidate
        return None

    def open_file(self, path, sessions=None):
        """Open a file a message names (a design doc, a report) in its usual app."""
        found = self._find_file(path, sessions)
        if found is None:
            raise ApiError(f"Couldn't find {path} in the sessions' or projects' folders")
        if found.suffix.lower() in RUNNABLE_SUFFIXES or (os.access(found, os.X_OK) and found.suffix.lower() not in VIEWABLE_SUFFIXES):
            raise ApiError(f"{found.name} is a program: open it from your file manager if you mean to")
        if not desktop.open_target(str(found)):
            raise ApiError("Couldn't open it")
        return {"ok": True, "message": f"Opened {found.name}", "path": str(found)}

    def read_file(self, path, sessions=None):
        """A text file a message names, to show in the app (on the phone, where it can't open
        on the computer): {"path", "name", "text", "markdown"}."""
        found = self._find_file(path, sessions)
        if found is None:
            raise ApiError(f"Couldn't find {path} in the sessions' or projects' folders")
        if found.stat().st_size > FILE_VIEW_LIMIT:
            raise ApiError(f"{found.name} is too big to show here")
        try:
            text = found.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ApiError(f"{found.name} isn't a text file") from error
        return {"path": str(found), "name": found.name, "text": text,
                "markdown": found.suffix.lower() in (".md", ".markdown")}

    def open_url(self, url):
        """Open a link in your browser (the app window only shows its own pages)."""
        if not str(url).startswith(("http://", "https://")):
            raise ApiError("Only web links can be opened")
        if not desktop.open_target(url):
            raise ApiError("Couldn't open the browser")
        return {"ok": True}

    # --- your phone (remote.py). Named remote_*, so the phone itself can't call them.

    # --- your own subscription, as a member in a browser (member_accounts.py)

    def _me(self):
        who = activity.person()
        if not who:
            raise ApiError("At this computer you use the Accounts page")
        return who

    def my_subscription(self):
        who = self._me()
        return {**member_accounts.status(who["login"]), "provider": codex_runtime.provider(config_store.load())}

    def connect_my_subscription(self, provider):
        who = self._me()
        try:
            member_accounts.connect(who["login"], who["name"], provider)
        except member_accounts.MemberAccountError as error:
            raise ApiError(str(error)) from error
        return {"ok": True}

    def my_login_screen(self):
        return member_accounts.screen(self._me()["login"])

    def my_login_send(self, text):
        try:
            return member_accounts.send(self._me()["login"], text)
        except member_accounts.MemberAccountError as error:
            raise ApiError(str(error)) from error

    # --- the company's people (company.py) and browser access

    def people(self):
        import remote
        conf = remote.settings()
        return {"company": company.company(), "members": company.members(), "blocked": company.blocked(),
                "handlers": company.handlers(),
                "projects": {pid: p.get("name", pid) for pid, p in config_store.load()["projects"].items()},
                "web": {"on": conf["web"], "via": conf["web_via"], "url": remote.web_url(conf), "running": remote.service_state()}}

    def block_person(self, login):
        _call(lambda: company.block(login))
        return {"ok": True, "message": f"{login} is kept out: they see only \"not available\"."}

    def unblock_person(self, login):
        _call(lambda: company.unblock(login))
        return {"ok": True, "message": f"{login} can ask to join again."}

    def use_this_tailnet(self):
        """Serve browser access on this computer's own tailnet (not a separate company one)."""
        import company_tailnet
        import remote
        if not company.owner():
            ip = remote.tailscale_ip()
            me = company.whois(ip) if ip else None
            if me:
                company.setup(company.company()["name"], me["login"])
        try:
            if company_tailnet.SOCKET.exists():
                company_tailnet.unserve_web()
                company_tailnet.stop()
            remote.set_web_via("tailnet")
            remote.set_web(True)
        except (company_tailnet.TailnetError, remote.RemoteError) as error:
            raise ApiError(str(error)) from error
        return {"ok": True, "message": f"Browser access is on: {remote.web_url()}. People you keep out see only \"not available\"."}

    def admit_person(self, login, role="watch", admin=False):
        return _call(lambda: company.admit(login, role, bool(admin)))

    def set_person_role(self, login, project, role=None):
        _call(lambda: company.set_project_role(login, project, role or None))
        return {"ok": True}

    def remove_person(self, login):
        _call(lambda: company.remove(login))
        return {"ok": True, "message": f"{login} no longer has access."}

    def set_project_handler(self, project, login=""):
        _call(lambda: company.set_handler(project, login))
        return {"ok": True, "message": f"{login}'s bell gets this project's questions and approvals." if login
                else "You get this project's questions and approvals again."}

    def set_company_name(self, name):
        return company.setup(name, company.owner())

    def company_network(self):
        """The company's own tailnet: whether it's running and logged in, and its address."""
        import company_tailnet
        import remote
        return {**company_tailnet.status(), "via": remote.settings()["web_via"]}

    def company_network_login(self):
        import company_tailnet
        try:
            return {"ok": True, "url": company_tailnet.login()}
        except company_tailnet.TailnetError as error:
            raise ApiError(str(error)) from error

    def company_network_use(self):
        """Serve browser access on the company tailnet only, with you as its owner there too."""
        import company_tailnet
        import remote
        info = company_tailnet.status()
        if info["state"] != "Running":
            raise ApiError("Log the company network in first")
        company.add_owner_login(info["login"])
        try:
            company_tailnet.serve_web()
            remote.set_web_via("company")
            remote.set_web(True)
        except (company_tailnet.TailnetError, remote.RemoteError) as error:
            raise ApiError(str(error)) from error
        return {"ok": True, "message": f"Browser access is on, only on the company tailnet: {info['url']}"}

    def set_web_access(self, on):
        import remote
        try:
            remote.set_web(bool(on))
        except remote.RemoteError as error:
            raise ApiError(str(error)) from error
        url = remote.web_url()
        return {"ok": True, "message": f"People in your company can open {url} on the tailnet." if on else "Browser access is off."}

    def remote_status(self):
        import remote
        return remote.status()

    def remote_set_password(self, password):
        import remote
        try:
            remote.set_password(password)
        except remote.RemoteError as error:
            raise ApiError(str(error)) from error
        return {"ok": True, "message": "Phone password set. Phones keep their sessions until they lapse; "
                "remove a device to sign it out now."}

    def remote_set_enabled(self, on):
        import remote
        try:
            remote.set_enabled(bool(on))
        except remote.RemoteError as error:
            raise ApiError(str(error)) from error
        return {"ok": True, "message": "Phone access is on." if on else "Phone access is off."}

    def remote_pair(self):
        """Open pairing for ten minutes. The QR code holds the address, the certificate's
        fingerprint and the one-time code."""
        import urllib.parse
        import remote
        try:
            remote.ensure_certificate([remote.listen_address(), remote.tailscale_ip()])
            info = remote.start_pairing()
        except remote.RemoteError as error:
            raise ApiError(str(error)) from error
        url = remote.server_url()
        if not url:
            remote.cancel_pairing()
            raise ApiError("Tailscale isn't up on this computer, so the phone has no way to reach it")
        qr = "lago://pair?" + urllib.parse.urlencode({"u": url, "f": remote.fingerprint(), "c": info["code"]})
        return {**info, "url": url, "fingerprint": remote.pretty_fingerprint(remote.fingerprint()),
                "qr": qr, "restart": not remote.service_state()["active"]}

    def remote_cancel_pair(self):
        import remote
        remote.cancel_pairing()
        return {"ok": True}

    def remote_publish_app(self, notes=""):
        """Build the Android app with the next version number and publish it: paired phones
        update themselves. Runs android/build-apk.sh in a terminal you can watch."""
        import shlex
        script = Path(__file__).resolve().parent.parent / "android" / "build-apk.sh"
        if not script.exists():
            raise ApiError("android/build-apk.sh is missing")
        command = f"cd {shlex.quote(str(script.parent))} && ./build-apk.sh" + (f" -m {shlex.quote(notes)}" if notes else "")
        terminal = _call(lambda: server.launch("app-publish", "", command, "build and publish the phone app"))
        return {"ok": True, "terminal": terminal, "message": "Building the app; phones update when it's published."}

    def remote_revoke(self, device_id):
        import remote
        try:
            remote.revoke(device_id)
        except remote.RemoteError as error:
            raise ApiError(str(error)) from error
        return {"ok": True, "message": "Removed. That phone is signed out."}

    # --- other Claude sessions

    def sessions(self):
        """Claude Code sessions on this machine that Lago Agents didn't start."""
        found = []
        for proc in desktop.claude_processes():
            words = proc["args"]
            if proc["lago"] or any(w in ("mcp", "--version", "-p", "--print") for w in words[1:2]):
                continue
            found.append({"pid": proc["pid"], "cwd": proc["cwd"], "project": Path(proc["cwd"]).name if proc["cwd"] else "",
                          "args": " ".join(words[1:])[:200], "started": proc["started"]})
        return sorted(found, key=lambda s: -s["started"])

    def stop_session(self, pid):
        if not isinstance(pid, int) or pid <= 1:
            raise ApiError("Invalid process id")
        try:
            if desktop.process_name(pid) != "claude":
                raise ApiError("That process isn't a Claude Code session anymore")
            os.kill(pid, signal.SIGTERM)
        except OSError as error:
            raise ApiError(str(error)) from error
        return {"ok": True, "message": "Session stopped."}

    # --- helpers

    def set_feature(self, feature, on):
        """Switch an extra on or off (features.py). Off also stops what it runs by itself."""
        try:
            config = features.switch(feature, on)
        except ValueError as error:
            raise ApiError(str(error)) from error
        if not on:
            self._stop_feature(feature)
        elif feature == "voice" and voice.settings(config)["wake"]:
            try:  # "Hey Lago" was on before Voice was switched off
                listen.set_service(True)
            except listen.ListenError as error:
                raise ApiError(f"Switched on, but the listener didn't start: {error}") from error
        name = features.FEATURES[feature][0]
        return {"ok": True, "message": f"{name} is {'on' if on else 'off'}."}

    @staticmethod
    def _stop_feature(feature):
        import remote  # remote imports this module for its Api
        try:
            if feature == "voice":
                voice.hush()
                listen.set_service(False)
            elif feature == "phone" and remote.service_state()["enabled"] and not remote.settings()["web"]:
                remote.set_enabled(False)
            elif feature == "people" and remote.settings()["web"]:
                remote.set_web(False)
        except (listen.ListenError, remote.RemoteError) as error:
            raise ApiError(f"Switched off, but couldn't stop it: {error}") from error

    def set_appearance(self, appearance):
        """Light, dark, or auto (follow the desktop), from the switch under the sidebar.
        The terminal pane changes with it."""
        if appearance not in APPEARANCES:
            raise ApiError(f"Unknown appearance {appearance!r}")
        config = config_store.without_swarm(config_store.load())
        config["appearance"] = appearance
        config_store.save(config)
        theme = self._theme()
        if hasattr(self.window, "set_terminal_theme"):
            self.window.set_terminal_theme(theme or "dark")
        return {"ok": True, "theme": theme}

    @staticmethod
    def _theme():
        """The look picked on the sidebar switch, or the desktop's when it's on Auto."""
        chosen = config_store.load().get("appearance", "auto")
        if chosen in ("light", "dark"):
            return chosen
        return desktop.color_scheme()
