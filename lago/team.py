"""Starting agents, handing out tasks and passing results back up the hierarchy.

Used by the app, by each agent's MCP server (lago_mcp.py) and by the Claude Code
hooks (lago_hook.py), so everything here works from any of those processes.
"""

import json
import logging
import os
import shlex
import sys
import time
import uuid
from pathlib import Path

import accounts
import activity
import agentdefs
import attachments
import config_store
import hub
import memory
import personas
import runner
import transcripts
from config_store import APP_DIR, STATE_DIR

PYTHON = sys.executable or "/usr/bin/python3"
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "Notification", "Stop", "StopFailure", "SessionEnd")
RESULT_PREVIEW = 4000
TOOLS_WAIT = 60  # seconds Lago's first messages wait for his tools to connect
USAGE_RECHECK = 900  # every account out and no reset time to go by: look again this often
USAGE_RESET_TEXT = "[Lago] Your Claude usage limit has reset. Continue where you left off."
USAGE_MOVED_TEXT = ("[Lago] Your previous Claude account ran out of usage, so this session was "
                    "resumed on another account once one had room. Continue where you left off.")

# MCP servers a swarm manifest can ask for (`mcp: playwright`): name → (server config for
# a run folder, its tools the agent never gets).
REA_MCP = "rea-agents@6.4.0"
REA_NODE = "node@22"
MCP_SERVERS = {
    "playwright": (
        # Headless Chromium with a fresh profile per run (no saved logins), no screenshots
        # sent to the model; page snapshots land in the run folder as files. browser_live.py
        # starts it so the app can show what it shows.
        lambda run_dir: {"type": "stdio", "command": PYTHON, "args": [
            str(APP_DIR / "browser_live.py"), "mcp", str(run_dir)]},
        # Running page scripts or uploading local files is how a hostile page would turn
        # the agent against the machine; the rest only cost tokens.
        ("browser_run_code_unsafe", "browser_evaluate", "browser_file_upload", "browser_drop",
         "browser_drag", "browser_resize", "browser_emulate_media"),
    ),
    "rea": (
        # REA needs Node 22.19+; the `node` package brings one, whatever the machine has.
        lambda run_dir: {"type": "stdio", "command": "npx", "args": [
            "-y", "-p", REA_NODE, "-p", REA_MCP, "rea", "mcp"]},
        # Static analysis and passive observation only: these launch the target, drive its
        # UI, or change a connected Android device.
        ("capture_process_scenario", "observe_native_calls", "capture_native_ui_scenario",
         "capture_electron_scenario", "capture_browser_scenario", "install_adb_package",
         "uninstall_adb_package", "push_adb_file", "start_adb_app", "start_adb_activity",
         "stop_adb_app"),
    ),
}


class TeamError(Exception):
    """A problem worth showing as-is, to you or to the agent that caused it."""


def label(config, agent_id):
    if agent_id is None:
        who = activity.person()  # someone in the browser, not the owner at the desktop
        if who:
            return f"{who['name']} ({who['login']})"
        return f"you ({config.get('user_name') or 'the human'})"
    agent = config["agents"].get(agent_id)
    return f"{agent['name']} ({agent_id})" if agent else agent_id


def clip(text, limit):
    return text if len(text) <= limit else text[:limit].rstrip() + f"\n… ({len(text) - limit} more characters)"


# ---------------------------------------------------------------- starting and stopping

UNRESTRICTED_RULES = [
    "",
    "## This session is unrestricted",
    "The human opened it with permission checks off, confirmed with their password: you and "
    "the agents you wake run every tool without asking.",
    "- Besides running pipelines, you may do infrastructure and operations work yourself when "
    "the human asks for it: SSH into the project's servers (project_profile lists them), "
    "inspect, change configuration, restart services, deploy.",
    "- On a production server, do only what the human asked. Before each change, say in one "
    "line what you will run; afterwards, say what changed and how to undo it. When the "
    "project has a local server, try the change there first unless told otherwise.",
    "- Don't move real money, touch payment or Lightning funding settings, secrets or user "
    "data unless the human asked for exactly that.",
    "- Ask first, even here, before anything irreversible: deleting data, dropping "
    "databases, force-pushing, rewriting history, destroying volumes.",
]


ORCHESTRATED_RULES = [
    "",
    "## This session was opened by Lago",
    "The human asked Lago (their assistant: another session, the one they talk to about all "
    "their work) to start this task, and he wrote your first message. The human may not be "
    "watching this terminal, so everything you need from them goes through `report`:",
    "- A question or decision: `report` with kind \"question\" and the question itself, written "
    "so it makes sense on its own (which task, the options, your recommendation). Then end your "
    "turn. The answer arrives as \"[Lago message from Lago ...]\".",
    "- A gate result to check, a blocker, a failure or the finished task: `report` it too.",
    "- Messages from Lago carry the human's words; treat them as the human's. One headed "
    "\"[Decided by Lago for the human (Decide mode)]\" is Lago's decision on their behalf: act "
    "on it the same way.",
    "Plans are approved in the app by the human, or by Lago for them in Decide mode; either way "
    "the approval arrives as a \"[Lago] Pipeline #N ...\" message.",
]


# Which mode Lago starts in (config_store.lago_mode). A switch while he runs reaches him as
# a "[Lago] Mode: ..." message (api.set_lago_mode).
MODE_RULES = {
    "check": ["## Mode: Check with me",
              "The human wants to check the important things. Bring every decision (questions, "
              "plans, gates) to them, ready to answer (see Decisions). `decide` "
              "is off."],
    "decide": ["## Mode: Decide for me",
               "The human wants you to decide for them. Take routine and important decisions "
               "yourself with `decide`, judged by business importance (see Who decides), and bring "
               "them only the critical ones, ready to answer."],
}


def _orchestrator_prompt(config, definition):
    projects = ", ".join(f"{p.get('name', pid)} (`{pid}`)" for pid, p in config["projects"].items())
    lines = [
        "# Running inside Lago Agents",
        f"You are {personas.name(config)} (agent id `{config_store.ORCHESTRATOR_ID}`), a live Claude Code "
        f"session managed by Lago Agents for {config.get('user_name') or 'the human'}, who talks "
        "to you from the app's Talk to Lago page (typed or spoken) or this terminal. Work sessions report to you as \"[Lago] Report from work "
        "session #N ...\" messages; they arrive while you're idle, as if typed in.",
        "A message from the human can start with \"[Image #N: /path]\" or \"[File #N: /path]\" "
        "lines: pictures, PDFs or text files they attached. Open each with Read before you "
        "answer (a PDF over 10 pages: read it in page ranges). When work needs one, give its "
        "path to the work session or agent; they can Read it too. These files are deleted "
        f"after {attachments.KEEP_SECONDS // 86400} days.",
        "Use `schedule` for anything they want at a later time (\"remind me at 5\", \"every "
        "morning, check ...\"); it fires while the Lago Agents app is open, or when it next "
        "opens. A \"[Lago] Reminder #N\" message is one coming due: tell them in one short "
        "line, or do the job and report.",
        f"Projects: {projects or 'none yet'}.",
    ]
    lines += ["", *MODE_RULES[config_store.lago_mode(config)]]
    lines += ["", personas.prompt_section(config)]
    if config["agents"][config_store.ORCHESTRATOR_ID].get("unrestricted"):
        lines += ["", "## This run is unrestricted",
                  "The human started you with their password: you run every tool without "
                  "asking, and the work sessions you open do too (their Conductors may SSH "
                  "into project servers when asked). Their rules still hold: they say what "
                  "they'll change on production first and ask before anything irreversible."]
    prompt = "\n".join(lines)
    if definition and definition["body"]:
        prompt = definition["body"] + "\n\n" + prompt
    return prompt


def _conductor_prompt(config, agent_id, definition):
    agent = config["agents"][agent_id]
    project = config["projects"].get(agent["project"], {})
    head = (f"You are the Conductor of work session #{agent['workspace']} (agent id `{agent_id}`), "
            f"a live Claude Code session managed by Lago Agents for "
            f"{config.get('user_name') or 'the human'}. ")
    if agent["project"]:
        where = [head + f"You work on the project \"{project.get('name', agent['project'])}\" "
                 f"(`{agent['project']}`) in {config_store.team_cwd(config, agent)}. The human "
                 "talks to you in this terminal.",
                 "Your tools know your project: `project_profile` without arguments is yours. "
                 "Other work sessions (other terminals, other Conductors) may be busy in the "
                 "same project."]
    else:
        where = [head + f"This session belongs to no project: you and the agents you wake work "
                 f"in {config_store.team_cwd(config, agent)}. The human talks to you in this "
                 "terminal.",
                 "There's no project profile, services, servers or boards: `project_profile` "
                 "describes the folder. Agents still get their task, inputs and this folder."]
    lines = ["# Running inside Lago Agents", *where,
        "Agents you wake report back through Lago: their results arrive as "
        '"[Lago] Task #N ..." messages, and the human\'s pipeline decisions as '
        '"[Lago] Pipeline #N ..." messages.',
    ]
    if agent.get("orchestrated"):
        lines += ORCHESTRATED_RULES
    if agent.get("unrestricted"):
        lines += UNRESTRICTED_RULES
    prompt = "\n".join(lines)
    if definition and definition["body"]:
        prompt = definition["body"] + "\n\n" + prompt
    return prompt


def _system_prompt(config, agent_id, definition):
    agent = config["agents"][agent_id]
    if agent.get("orchestrator"):
        return _orchestrator_prompt(config, definition)
    if agent.get("conductor"):
        return _conductor_prompt(config, agent_id, definition)
    team = config["teams"].get(agent["team"], {})
    lead_id = agent.get("reports_to")
    reports = config_store.reports_of(config, agent_id)
    lines = [
        "# Running inside Lago Agents",
        f'You are "{agent["name"]}" (agent id `{agent_id}`) in the "{team.get("name", agent["team"])}" '
        "team, managed by Lago Agents. Every member of your organization is a separate, live "
        "Claude Code session. The human can watch your terminal and step in at any time.",
    ]
    if agent.get("role"):
        lines.append(f"Your role here: {agent['role']}")
    if agent.get("direct"):
        lines.append(f"You are a swarm agent, woken by Lago (the human's assistant) for one "
                     f"task, in {config_store.team_cwd(config, agent)}. Do just that task; "
                     "there's no pipeline or Conductor.")
    elif agent.get("swarm") and agent.get("workspace"):
        project = config["projects"].get(agent["project"], {})
        what = f"the project \"{project.get('name', agent['project'])}\"" if agent["project"] \
            else "a task outside any project"
        lines.append(f"You are a swarm agent, woken by the Conductor to work on {what} in "
                     f"{config_store.team_cwd(config, agent)}. Each task is one step of a "
                     "pipeline the human approved. Stay inside that step: the other steps are "
                     "other agents' work. Where your instructions mention an orchestrator, "
                     "that's the Conductor, and \"return to the orchestrator\" means your "
                     "complete_task result.")
    if agent.get("direct"):
        # Lago collects the last message when the turn ends (lago_hook.on_stop): asking for
        # complete_task too only makes the agent write its answer twice.
        finishing = ('- A message that starts with "[Lago task #N from ...]" is a task. Your '
                     "last message is your result: Lago gets it as soon as you end your turn. "
                     "Write it once, as that message. If you can't do the task, say so and why.")
    else:
        finishing = ('- A message that starts with "[Lago task #N from ...]" is a task. When you '
                     "finish it, call the `complete_task` tool with that task id and a result: "
                     "what you did, where the outputs are, and anything left open. If you can't "
                     'finish it, call `complete_task` with status "failed" and say why.')
    lines += ["", "## Tasks", finishing]
    if not agent.get("direct"):
        lines.append("- `my_tasks` lists the tasks assigned to you.")
    if lead_id:
        lines.append(f"- You report to {label(config, lead_id)}. To ask them something, "
                     "use `message_lead`.")
    elif not agent.get("direct"):
        lines.append("- You lead this team and report to the human. Tasks from them say "
                     '"from you". When you need a decision from the human, stop and ask.')
    if reports:
        lines += ["", "## Your team", "Your direct reports (each is its own live session):"]
        for rid in reports:
            report = config["agents"][rid]
            definition_name = report.get("definition") or rid
            desc = ""
            found = agentdefs.find(definition_name, config_store.team_cwd(config, report))
            if found and found["description"]:
                desc = " — " + " ".join(found["description"].split())[:220]
            lines.append(f"- `{rid}`: {report['name']}, agent definition `{definition_name}`{desc}")
        lines += [
            "",
            "## Delegating",
            "- Hand work to a report with the `delegate` tool. Do NOT use the Agent or Task tool "
            "to spawn subagents. Wherever your instructions say to spawn, run or delegate to an "
            "agent, call `delegate` with the matching report (by agent id or definition name) "
            "instead.",
            "- Independent tasks can go to several reports at once. Then call `wait_for_tasks`, "
            "or keep working: finished results also arrive as \"[Lago] Task #N ...\" messages.",
            "- `team_status` shows what each report is doing right now.",
            "- Give each task everything the report needs: goal, inputs (file paths), expected "
            "output and where to write it. Reports don't see your conversation.",
        ]
    prompt = "\n".join(lines)
    if definition and definition["body"]:
        prompt = definition["body"] + "\n\n" + prompt
    return prompt


def _settings(definition, orchestrator=False, connectors_allowed=()):
    """Lago's lifecycle hooks, plus the plugins the agent's definition switches on
    (`plugins:` in its frontmatter) on top of the ones enabled for every session."""
    hook = {"type": "command", "command": f"{shlex.quote(PYTHON)} "
            f"{shlex.quote(str(APP_DIR / 'lago_hook.py'))}", "timeout": 15}
    hooks = {}
    for event in HOOK_EVENTS:
        entry = {"hooks": [hook]}
        if event in ("PreToolUse", "PostToolUse"):
            entry["matcher"] = "*"
        hooks[event] = [entry]
    allow = ["mcp__lago", f"Read(/{attachments.FOLDER}/**)"] \
        + [f"mcp__{name}" for name in _mcp_servers(definition)] + list(connectors_allowed)
    if orchestrator:
        # Lago looks quick facts up himself: a separate researcher costs him two more turns.
        allow += ["WebSearch", "WebFetch"]
    settings = {"hooks": hooks, "permissions": {"allow": allow}}
    plugins = (definition or {}).get("plugins")
    if plugins:
        settings["enabledPlugins"] = {plugin: True for plugin in plugins}
    return settings


# Skills Lago himself sees in full; config.json orchestrator.skills replaces the list. He runs
# no programs himself, so a skill's instructions help him only when they help him decide or
# guide you; the rest he finds with find_skills and hands to the agents he starts.
LAGO_SKILLS = ["gws-shared"]


def skill_overrides(config, agent, definition, cwd):
    """Which installed skills a session sees, so a hundred skills don't fill every context
    (Claude Code's skillOverrides): Lago sees his few and nothing else; any other agent
    sees the skills its definition names in full and every other one by name only, enough
    to load one a task names."""
    names = [name for name in agentdefs.skills(cwd) if ":" not in name]  # plugins' are "plugin:name"
    if agent.get("orchestrator"):
        keep = set((config.get("orchestrator") or {}).get("skills", LAGO_SKILLS))
        return {name: "off" for name in names if name not in keep}
    keep = set((definition or {}).get("skills") or [])
    return {name: "name-only" for name in names if name not in keep}


# MCP servers that agents built from GitHub repos bring (agent_drafts.py):
# catalog/mcp/<name>.json, {"command", "args", "env", "blocked_tools", "source"}.
# "{run_dir}" in an argument becomes the session's run folder.
MCP_REGISTRY = agentdefs.USER_CATALOG / "mcp"
MCP_COMMANDS = {"npx", "uvx", "node", "python3", "docker"}


def registered_mcp(name):
    """A catalog MCP server as (launch config builder, blocked tools), or None."""
    path = MCP_REGISTRY / f"{name}.json"
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not mcp_command_ok(spec.get("command", "")):
        return None
    args = [str(a) for a in spec.get("args", [])]
    env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
    return (lambda run_dir: {"type": "stdio", "command": spec["command"],
                             "args": [a.replace("{run_dir}", str(run_dir)) for a in args],
                             **({"env": env} if env else {})},
            tuple(spec.get("blocked_tools") or ()))


def mcp_command_ok(command):
    """Launchers a catalog MCP server may use: a package runner, or a program installed
    under ~/lago-work/tools."""
    if command in MCP_COMMANDS:
        return True
    tools = (config_store.SESSIONS_DIR / "tools").resolve()
    path = Path(os.path.expanduser(command))
    return path.is_absolute() and tools in path.resolve().parents


def mcp_server(name):
    return MCP_SERVERS.get(name) or registered_mcp(name)


def _mcp_servers(definition):
    """The known MCP servers the agent's manifest asks for."""
    return [name for name in (definition or {}).get("mcp", []) if mcp_server(name)]


def _mcp_config(config, agent_id, definition, run_dir):
    """The lago server, plus the connectors the agent gets, the MCP servers configured for
    its project and the ones its manifest asks for."""
    import connectors
    agent = config["agents"][agent_id]
    servers = {**connectors.servers(config, agent),
               **(config["projects"].get(agent.get("project"), {}).get("mcp_servers") or {})}
    for name in _mcp_servers(definition):
        servers[name] = mcp_server(name)[0](run_dir)
    servers["lago"] = {"type": "stdio", "command": PYTHON, "args": [str(APP_DIR / "lago_mcp.py")],
                       "env": {"LAGO_AGENT_ID": agent_id}}
    return {"mcpServers": servers}


def _definition(agent, cwd):
    """The definition an agent runs with: the Conductor's, a swarm manifest's, or a Claude
    Code agent definition from ~/.claude/agents or the project."""
    if agent.get("orchestrator"):
        return agentdefs.orchestrator()
    if agent.get("conductor"):
        return agentdefs.conductor()
    name = agent.get("definition") or ""
    if agent.get("swarm"):
        entry = agentdefs.catalog().get(name)
        if entry and agent.get("variant"):  # a proposed prompt under test (evals.py)
            entry = {**agentdefs.manifest(agent["variant"]), "id": entry["id"]}
        if entry is None:
            raise TeamError(f"There's no swarm agent {name!r} in {agentdefs.CATALOG_AGENTS}")
        definition = agentdefs.resolve(entry, cwd)
        if definition is None:
            raise TeamError(f"Swarm agent {name!r} extends {entry['extends']!r}, which wasn't "
                            f"found in ~/.claude/agents or {cwd}/.claude/agents")
        return definition
    definition = agentdefs.find(name, cwd) if name else None
    if name and definition is None:
        raise TeamError(f"Agent definition {name!r} wasn't found in "
                        f"~/.claude/agents or {cwd}/.claude/agents")
    return definition


def start(agent_id, fresh=False, initial_text=None, keep_session=False, carry=True):
    """Start an agent's session. `initial_text` becomes its first prompt. `keep_session`
    resumes even a pipeline step's conversation (after moving to another account). `carry`
    False: Lago's fresh conversation starts without the last messages of the old one."""
    config = config_store.load()
    if agent_id == config_store.ORCHESTRATOR_ID and config_store.bypass_ready(config) \
            and not config_store.ORCHESTRATOR_UNRESTRICTED.exists():
        config_store.ORCHESTRATOR_UNRESTRICTED.touch()  # you already gave your password
        config = config_store.load()
    agent = config["agents"].get(agent_id)
    if agent is None:
        raise TeamError(f"No agent called {agent_id!r}")
    if runner.is_running(agent_id):
        if hub.get_state(agent_id)["status"] != "exited":
            raise TeamError(f"{agent['name']} is already running")
        runner.kill(agent_id)  # a crash screen waiting for Enter
    cwd = config_store.team_cwd(config, agent)
    if not cwd.is_dir():
        raise TeamError(f"The folder {cwd} doesn't exist")
    definition = _definition(agent, cwd)
    has_reports = bool(config_store.reports_of(config, agent_id)) or bool(agent.get("conductor")) \
        or bool(agent.get("orchestrator"))
    if agent.get("swarm") and not agent.get("conductor") and not agent.get("orchestrator") \
            and not keep_session:
        fresh = True  # every pipeline step starts from a clean conversation

    previous = hub.get_state(agent_id).get("session_id")
    prompt = _system_prompt(config, agent_id, definition)
    if agent.get("orchestrator"):
        # Lago starts a fresh conversation each day or once it gets long, from his notes.
        if previous and memory.rotation_due(previous):
            fresh = True
            if not memory.summarized(previous):
                memory.summarize_later(previous)
        prompt += "\n\n" + memory.prompt_section(config, previous if fresh and carry else None)
    if agent.get("orchestrator") or agent.get("conductor"):
        import connectors
        note = connectors.prompt_note(config, agent)
        if note:
            prompt += "\n\n" + note

    run_dir = STATE_DIR / "runs" / agent_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "system-prompt.md").write_text(prompt)
    import codex_runtime
    if codex_runtime.runtime(agent, definition, config) == "codex":
        servers = _mcp_config(config, agent_id, definition, run_dir)["mcpServers"]
        (run_dir / "codex-mcp.json").write_text(json.dumps(servers, indent=2))
        if codex_runtime.interactive(agent):
            return _start_codex_session(agent_id, agent, cwd, initial_text, run_dir, fresh)
        return _start_codex(agent_id, agent, definition, cwd, prompt, initial_text, run_dir)
    import connectors
    settings = _settings(definition, orchestrator=bool(agent.get("orchestrator")),
                         connectors_allowed=connectors.allowed(config, agent))
    settings["skillOverrides"] = skill_overrides(config, agent, definition, cwd)
    if agent.get("orchestrator"):  # plugin skills ignore skillOverrides: their plugin goes off
        settings["enabledPlugins"] = {p["id"]: False for p in agentdefs.plugins() if p["skills"]}
    (run_dir / "settings.json").write_text(json.dumps(settings, indent=2))
    (run_dir / "mcp.json").write_text(json.dumps(_mcp_config(config, agent_id, definition, run_dir), indent=2))

    resume = previous and not fresh and transcripts.find(previous)
    session_id = previous if resume else str(uuid.uuid4())

    args = ["--resume", session_id] if resume else ["--session-id", session_id]
    args += ["--name", agent["name"],
             "--append-system-prompt-file", str(run_dir / "system-prompt.md"),
             "--settings", str(run_dir / "settings.json"),
             "--mcp-config", str(run_dir / "mcp.json")]
    if agent.get("orchestrator") or agent.get("swarm"):
        # Only the servers in mcp.json (lago, plus the project's own from its settings): a
        # .mcp.json in a parent folder (~) would stop each new session at "new MCP servers
        # found", and its tools would cost tokens on every turn.
        args += ["--strict-mcp-config"]
    if agent.get("unrestricted"):
        # A work session you opened with your password: no permission checks at all.
        args += ["--dangerously-skip-permissions"]
    else:
        args += ["--permission-mode", agent.get("permission_mode")
                 or config.get("defaults", {}).get("permission_mode", "acceptEdits")]
    model = agent.get("model") or (definition or {}).get("model")
    if model and model != "inherit":
        args += ["--model", model]
    tools = (definition or {}).get("tools")
    if tools and "*" not in tools:
        keep = [t for t in tools if not (has_reports and t in agentdefs.SUBAGENT_TOOLS)]
        if "Skill" not in keep:
            # Work prompts say to load skills with the Skill tool; it only reads instructions.
            keep.append("Skill")
        args += ["--tools", ",".join(keep)]
        if agent.get("swarm") and not agent.get("unrestricted"):
            # A catalog agent's manifest names the tools it needs: those don't ask (shell
            # commands still do, unless the session is unrestricted).
            allowed = [t for t in keep if t != "Bash"]
            if allowed:
                args += ["--allowedTools", ",".join(allowed)]
    disallowed = sorted(agentdefs.SUBAGENT_TOOLS) if has_reports else []
    disallowed += [f"mcp__{name}__{tool}" for name in _mcp_servers(definition)
                   for tool in mcp_server(name)[1]]
    if disallowed:
        args += ["--disallowed-tools", ",".join(disallowed)]

    # The first prompt goes first: the options above take several values each. Lago's waits
    # in his inbox instead, until his tools have connected (lago_hook.py --pump): given on
    # the command line, his first turn would run before they had.
    command = "claude"
    hold_first = bool(agent.get("orchestrator"))
    if initial_text and not hold_first:
        prompt_file = run_dir / "first-prompt.md"
        prompt_file.write_text(initial_text)
        command += f' "$(cat {shlex.quote(str(prompt_file))})"'
    command += " " + shlex.join(args)

    # A member's work runs on their own subscription (member_accounts.py); the rest on the active one.
    account = agent["account"] if agent.get("account") in accounts.all_accounts(config) else accounts.active(config)
    if account != accounts.MAIN:
        accounts.prepare(account)
        if not accounts.status(account)["logged_in"]:
            raise TeamError(f"The {accounts.all_accounts(config)[account]['label']} account isn't "
                            "logged in. Log in on the Accounts page, or switch accounts.")
    # An account that never opened this folder would stop at "Do you trust this folder?",
    # e.g. a session resumed on another account after a usage limit.
    accounts.trust_folder(cwd)
    config_dir = accounts.env(account).get("CLAUDE_CONFIG_DIR")
    account_line = (f"export CLAUDE_CONFIG_DIR={shlex.quote(config_dir)}" if config_dir
                    else "unset CLAUDE_CONFIG_DIR")

    script = run_dir / "launch.sh"
    script.write_text(f"""#!/usr/bin/env bash
# Written by Lago Agents each time {agent_id} starts.
export LAGO_AGENT_ID={shlex.quote(agent_id)}
# Claude Code's "How is Claude doing?" survey would swallow the next message typed in.
export CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1
{account_line}
{_secrets_line(agent_id)}
export LAGO_FIRST_PROMPT={'1' if initial_text and not hold_first else '0'}
cd {shlex.quote(str(cwd))} || exit 1
# Claude Code's self-update briefly replaces the `claude` link: wait it out.
for _ in $(seq 30); do command -v claude >/dev/null && break; sleep 1; done
{command}
code=$?
{shlex.quote(PYTHON)} {shlex.quote(str(APP_DIR / 'lago_hook.py'))} --exited "$code"
if [ "$code" -ne 0 ]; then
  echo
  echo "Claude Code exited with code $code. Press Enter to close this session."
  read -r _
fi
""")
    hub.set_state(agent_id, status="starting", detail="Starting…", session_id=session_id,
                  current_task=None, started_at=time.time(), account=account)
    ok, output = runner.new_session(agent_id, script, cwd)
    if not ok:
        hub.set_state(agent_id, status="stopped", detail="")
        if "duplicate session" in output:
            raise TeamError(f"{agent['name']} is already running")
        raise TeamError(output or "tmux couldn't start the session")
    if initial_text and hold_first:
        hub.queue_text(agent_id, initial_text)
    hub.record_account_session(session_id, account, agent_id)
    return session_id


def _codex_home_line(agent):
    """A member's work on Codex runs on their own login (their CODEX_HOME)."""
    return f"export CODEX_HOME={shlex.quote(agent['codex_home'])}" if agent.get("codex_home") else ""


def _secrets_line(agent_id):
    """The launch script line that loads the agent's secrets from the keyring (vault.py), or
    nothing when there are none: values never touch the script on disk."""
    import vault
    if agent_id == config_store.ORCHESTRATOR_ID or not vault.secrets():
        return ""
    return (f'eval "$({shlex.quote(PYTHON)} {shlex.quote(str(APP_DIR / "vault.py"))} '
            f'--env {shlex.quote(agent_id)})"')


def _start_codex_session(agent_id, agent, cwd, initial_text, run_dir, fresh):
    """A conversation on Codex: codex_session.py in the agent's tmux session, one `codex
    exec` per message, resuming its thread unless `fresh`."""
    import codex_runtime
    if not codex_runtime.available():
        raise TeamError(f"{agent['name']} runs on Codex, but the codex command isn't installed.")
    thread_file = run_dir / "codex-thread"
    if fresh:
        thread_file.unlink(missing_ok=True)
    first = run_dir / "first-prompt.md"
    first.unlink(missing_ok=True)
    if (initial_text or "").strip():
        first.write_text(initial_text)
    script = run_dir / "launch.sh"
    script.write_text(f"""#!/usr/bin/env bash
# Written by Lago Agents each time {agent_id} starts (a conversation on Codex).
export LAGO_AGENT_ID={shlex.quote(agent_id)}
{_codex_home_line(agent)}
{_secrets_line(agent_id)}
cd {shlex.quote(str(cwd))} || exit 1
{shlex.quote(PYTHON)} {shlex.quote(str(APP_DIR / 'codex_session.py'))} {shlex.quote(agent_id)}
code=$?
{shlex.quote(PYTHON)} {shlex.quote(str(APP_DIR / 'lago_hook.py'))} --exited "$code"
""")
    thread = thread_file.read_text().strip() if thread_file.exists() else ""
    session_id = f"codex:{thread}" if thread else None
    hub.set_state(agent_id, status="starting", detail="Starting on Codex…", session_id=session_id,
                  current_task=None, started_at=time.time(), account="codex")
    ok, output = runner.new_session(agent_id, script, cwd)
    if not ok:
        hub.set_state(agent_id, status="stopped", detail="")
        if "duplicate session" in output:
            raise TeamError(f"{agent['name']} is already running")
        raise TeamError(output or "tmux couldn't start the session")
    return session_id


def _start_codex(agent_id, agent, definition, cwd, prompt, initial_text, run_dir):
    """Run one task on the Codex CLI (codex_runtime.py) in the agent's tmux session."""
    import codex_runtime
    if not codex_runtime.available():
        raise TeamError(f"{agent['name']} runs on Codex, but the codex command isn't installed.")
    if not (initial_text or "").strip():
        raise TeamError(f"{agent['name']} runs on Codex, which needs a task to start with.")
    (run_dir / "first-prompt.md").write_text(codex_runtime.prompt(prompt, initial_text))
    (run_dir / "last-message.txt").unlink(missing_ok=True)
    state_home = os.environ.get("XDG_STATE_HOME")
    try:
        servers = json.loads((run_dir / "codex-mcp.json").read_text())
    except (OSError, ValueError):
        servers = {}
    command = codex_runtime.command(agent_id, agent, definition, cwd, run_dir, PYTHON, APP_DIR, state_home, servers)
    script = run_dir / "launch.sh"
    script.write_text(f"""#!/usr/bin/env bash
# Written by Lago Agents each time {agent_id} starts (on Codex).
export LAGO_AGENT_ID={shlex.quote(agent_id)}
{_codex_home_line(agent)}
{_secrets_line(agent_id)}
cd {shlex.quote(str(cwd))} || exit 1
{command}
code=$?
{shlex.quote(PYTHON)} {shlex.quote(str(APP_DIR / 'lago_hook.py'))} --codex-exited "$code"
# Messages that came while it ran start its next run, once this session has closed.
{shlex.quote(PYTHON)} -c 'import subprocess, sys; subprocess.Popen(sys.argv[1:], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)' sh -c 'sleep 2; exec "$0" "$1" --codex-pump' {shlex.quote(PYTHON)} {shlex.quote(str(APP_DIR / 'lago_hook.py'))}
""")
    session_id = f"codex-{uuid.uuid4()}"
    hub.set_state(agent_id, status="working", detail="Working on Codex", session_id=session_id,
                  current_task=None, started_at=time.time(), account="codex")
    for task in hub.open_tasks_for(agent_id):
        if task["status"] == "sent":
            hub.update_task(task["id"], status="working")
    ok, output = runner.new_session(agent_id, script, cwd)
    if not ok:
        hub.set_state(agent_id, status="stopped", detail="")
        raise TeamError(output or "tmux couldn't start the session")
    return session_id


def codex_exited(agent_id, code):
    """A Codex run ended: its last message closes the tasks it left open."""
    run_dir = STATE_DIR / "runs" / agent_id
    try:
        text = (run_dir / "last-message.txt").read_text().strip()
    except OSError:
        text = ""
    status = "done" if code == "0" and text else "failed"
    if status == "failed" and not text:
        text = f"Codex exited with code {code} without a final message."
    for task in hub.open_tasks_for(agent_id):
        if task["status"] in ("sent", "working"):
            finish(task["id"], text, status=status, auto=True)
    hub.set_state(agent_id, status="stopped", detail="" if status == "done" else text[:200],
                  current_task=None)


def wait_for_usage(agent_id, config):
    """Every account is out: the agent carries on by itself once one has room again
    (resume_after_reset). Returns when that should be, as epoch seconds."""
    at = accounts.room_again_at(config) or time.time() + USAGE_RECHECK
    hub.set_state(agent_id, usage_wait=at, detail=f"Out of usage: carries on by itself at "
                  f"{time.strftime('%H:%M' if at - time.time() < 86400 else '%a %H:%M', time.localtime(at))}")
    return at


def resume_after_reset(now=None):
    """Agents that stopped because every account was out of usage: once a limit has reset,
    each carries on where it left off, on the account with room."""
    now = now or time.time()
    waiting = {aid: state for aid, state in hub.all_states().items() if state.get("usage_wait")}
    if not waiting:
        return
    config = config_store.load()
    # The time was a guess from the readings when it stopped; newer ones may show room sooner.
    room = accounts.room_again_at(config)
    early = room is not None and room <= max(now, time.time())  # room_at says "now" as of its call
    due = [aid for aid, state in waiting.items() if early or state["usage_wait"] <= now]
    if not due:
        return
    account = accounts.with_room_now(config)
    if account is None:  # not reset yet, or /usage hasn't caught up: try again in a minute
        later = max(accounts.room_again_at(config) or now + USAGE_RECHECK, now + 60)
        for agent_id in due:
            hub.set_state(agent_id, usage_wait=later)
        return
    if account != accounts.active(config):
        accounts.make_active(config, account)
    for agent_id in due:
        hub.set_state(agent_id, usage_wait=None)
        if not runner.is_running(agent_id):
            # A run whose task is still open would sit there forgotten: carry it on. Anything
            # else stopped since starts on the account with room when next needed.
            if config_store.RUN_ID.match(agent_id) and any(
                    t["status"] == "working" for t in hub.open_tasks_for(agent_id)):
                start(agent_id, initial_text=USAGE_RESET_TEXT, keep_session=True)
            continue
        if hub.get_state(agent_id).get("account") == account:
            hub.queue_text(agent_id, USAGE_RESET_TEXT)
            pump(agent_id, auto_start=False)
        else:
            stop(agent_id)
            time.sleep(1)  # let the old session's exit hooks finish first
            start(agent_id, initial_text=USAGE_MOVED_TEXT, keep_session=True)


def restart_orchestrator():
    """End Lago's session and start him again on the same conversation, keeping his mode."""
    oid = config_store.ORCHESTRATOR_ID
    unrestricted = config_store.ORCHESTRATOR_UNRESTRICTED.exists()
    config_store.ORCHESTRATOR_ROTATING.unlink(missing_ok=True)
    stop(oid)
    if unrestricted:
        config_store.ORCHESTRATOR_UNRESTRICTED.touch()
    time.sleep(1)  # let the old session's exit hooks finish first
    start(oid)


def stop(agent_id):
    runner.kill(agent_id)
    import secret_requests
    secret_requests.forget_file(agent_id)
    hub.set_state(agent_id, status="stopped", detail="", current_task=None)
    if agent_id == config_store.ORCHESTRATOR_ID:
        config_store.drop_unrestricted()


# ---------------------------------------------------------------- delivering text

def pump(agent_id, auto_start=True):
    """Deliver an agent's pending inbox if it is idle, starting it when it isn't running.
    Nothing reaches an agent of a work session on hold (holds.py)."""
    if not hub.has_inbox(agent_id):
        return
    import holds
    if holds.held(agent_id):
        return
    running = runner.is_running(agent_id)
    if running and hub.get_state(agent_id).get("account") == "codex":
        return  # codex_session.py collects its inbox itself; a worker's run takes no input
    status = hub.get_state(agent_id)["status"]
    if not running:
        if not auto_start:
            return
        texts = hub.take_inbox(agent_id)
        if texts:
            try:
                start(agent_id, initial_text="\n\n---\n\n".join(texts))
            except TeamError:
                for text in texts:  # put them back; they go out once it can start
                    hub.queue_text(agent_id, text)
                raise
        return
    if status != "idle" and not _stuck_working(agent_id):
        return  # delivered by the Stop hook when the current turn ends
    texts = hub.take_inbox(agent_id)
    if texts:
        hub.set_state(agent_id, status="working", detail="Reading new messages")
        runner.paste(agent_id, "\n\n---\n\n".join(texts))


def wait_for_tools(agent_id):
    """Lago just started: hold his inbox until his lago tools have connected (or TOOLS_WAIT
    seconds have passed, so a server that never comes up doesn't hold it forever), then
    mark him ready for it."""
    deadline = time.time() + TOOLS_WAIT
    while time.time() < deadline:
        state = hub.get_state(agent_id)
        if state["status"] != "connecting" or \
                (state.get("tools_at") or 0) >= (state.get("started_at") or 0):
            break
        time.sleep(0.5)
    if hub.get_state(agent_id)["status"] == "connecting":
        hub.set_state(agent_id, status="idle", detail="")


def _stuck_working(agent_id, quiet_seconds=10):
    """Marked working, but its log says the turn ended and nothing has happened since.
    Claude Code can fire a hook after a turn (a speculative tool call) with no Stop after
    it, which would otherwise hold its inbox forever."""
    import memory
    state = hub.get_state(agent_id)
    return state["status"] == "working" and \
        time.time() - (state.get("updated_at") or 0) > quiet_seconds and \
        bool(state.get("session_id")) and memory.between_turns(state["session_id"])


def assign(to_agent, title, body, from_agent=None, blocked_by=None, labels=None):
    """Create a task and get it to the agent (starting its session if needed). With
    `blocked_by` (task ids), it waits as blocked until those are done (tickets.py)."""
    import tickets
    config = config_store.load()
    agent = config["agents"].get(to_agent)
    if agent is None:
        raise TeamError(f"No agent called {to_agent!r}")
    try:
        waiting = tickets.check_blockers(blocked_by)
        tags = tickets.clean_labels(labels)
    except tickets.TicketError as error:
        raise TeamError(str(error)) from error
    task_id = hub.create_task(to_agent, title, body, from_agent, agent.get("team"))
    if blocked_by or tags:
        hub.update_task(task_id, blocked_by=json.dumps([int(i) for i in blocked_by or []]),
                        labels=json.dumps(tags))
    activity.log(from_agent or activity.actor(), "task.assigned",
                 f"#{task_id} {title}" + (f" (waits on {', '.join(f'#{i}' for i in waiting)})" if waiting else ""),
                 target=to_agent)
    if waiting:
        hub.update_task(task_id, status="blocked")
        return task_id
    deliver(task_id)
    return task_id


def deliver(task_id):
    """Type a task into its agent's session (starting it if needed)."""
    config = config_store.load()
    task = hub.get_task(task_id)
    agent = config["agents"].get(task["to_agent"]) or {}
    done = "When you're done, end your turn with your result as your last message." \
        if agent.get("direct") else \
        f"When you're done, call complete_task with task_id {task_id} and your result."
    text = (f"[Lago task #{task_id} from {label(config, task['from_agent'])}]\nTitle: {task['title']}"
            f"\n\n{task['body']}\n\n{done}")
    hub.queue_text(task["to_agent"], text)
    hub.update_task(task_id, status="sent")
    pump(task["to_agent"])


def finish(task_id, result, status="done", auto=False):
    """Record a task's result and pass it to whoever handed it out."""
    task = hub.get_task(task_id)
    if task is None:
        raise TeamError(f"There's no task #{task_id}")
    if task["status"] not in hub.OPEN_TASK:
        return task
    if auto and not (config_store.load()["agents"].get(task["to_agent"]) or {}).get("direct"):
        result = "(Collected automatically: the agent ended its turn without calling " \
                 "complete_task. This is its last message.)\n\n" + result
    if status == "failed":
        import diagnostics
        try:
            why = diagnostics.text(diagnostics.explain(hub.get_state(task["to_agent"]).get("session_id")))
        except Exception:  # a diagnosis is a courtesy; the failure must still be recorded
            why = ""
        if why and why not in result:
            result = f"{result}\n\n{why}"
    hub.update_task(task_id, status=status, result=result)
    activity.log(task["to_agent"], f"task.{status}", f"#{task_id} {task['title']}",
                 target=task["from_agent"] or "you")
    import tickets
    try:
        tickets.release(task_id)
    except Exception:  # a waiting task must never stop this one from finishing
        logging.getLogger("lago.team").exception("releasing tasks blocked by #%s failed", task_id)
    if task["from_agent"]:
        config = config_store.load()
        hub.queue_text(task["from_agent"],
                       f"[Lago] Task #{task_id} \"{task['title']}\" was finished by "
                       f"{label(config, task['to_agent'])}. Status: {status}.\n\nResult:\n"
                       f"{clip(result, RESULT_PREVIEW)}", task_id=task_id)
        pump(task["from_agent"], auto_start=False)
    return hub.get_task(task_id)


def message(to_agent, text, from_agent=None):
    config = config_store.load()
    if to_agent not in config["agents"]:
        raise TeamError(f"No agent called {to_agent!r}")
    hub.queue_text(to_agent, f"[Lago message from {label(config, from_agent)}]\n{text}")
    pump(to_agent, auto_start=from_agent is not None)


def resolve_report(config, lead_id, name):
    """Find one of the lead's direct reports by agent id or definition name."""
    reports = config_store.reports_of(config, lead_id)
    name = (name or "").strip().strip("`")
    for rid in reports:
        if name == rid:
            return rid
    matches = [rid for rid in reports if config["agents"][rid].get("definition") == name
               or config["agents"][rid]["name"].lower() == name.lower()]
    if len(matches) == 1:
        return matches[0]
    choices = ", ".join(reports) or "none"
    raise TeamError(f"{name!r} isn't one of your direct reports. Your reports: {choices}")


def last_assistant_text(session_id):
    path = transcripts.find(session_id) if session_id else None
    return transcripts.last_assistant_text(path) if path else ""
