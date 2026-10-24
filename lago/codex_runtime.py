"""Running agents on OpenAI's Codex CLI (a ChatGPT subscription) instead of Claude Code.

The provider (`runtime.default` in config.json: "claude" or "codex") is what new sessions run
on; a catalog agent can name its own (`runtime: codex` or `runtime: claude`). On Codex:

- Lago, leads and team members hold a conversation: they run as codex_session.py in their
  terminal, one `codex exec` per message, each resuming the same Codex thread (`turn_args`).
- Swarm workers do one task: `codex exec` runs it (`command`), with its prompt and the task on
  stdin, and when it exits its last message (`--output-last-message`) closes any task it left
  open (lago_hook.py --codex-exited).

Every Codex agent gets the `lago` MCP server, so complete_task and the rest work as usual.
`codex exec` never stops to ask: by default it may only write in its folder (`workspace-write`,
with network access for git and the like), and unrestricted sessions run with
`--dangerously-bypass-approvals-and-sandbox`. Messages that arrive while a worker runs wait
for its next run, which starts fresh.
"""

import json
import re
import shlex
import shutil
from pathlib import Path

RUNTIMES = ("claude", "codex")
PROVIDERS = {"claude": "Claude subscription (Claude Code)", "codex": "ChatGPT subscription (Codex)"}
TEST_TIMEOUT = 90


def provider(config):
    wanted = str((config.get("runtime") or {}).get("default") or "claude")
    return wanted if wanted in RUNTIMES else "claude"


def runtime(agent, definition, config=None):
    """What an agent runs on: its own `runtime` (a catalog manifest's, or the agent's in
    config.json), else the provider's."""
    import config_store
    config = config or config_store.load()
    own = str((definition or {}).get("runtime") or agent.get("runtime") or "").strip().lower()
    if own in RUNTIMES:
        return own
    return provider(config)


def interactive(agent):
    """Agents that hold a conversation (Lago, leads, team members) rather than one task."""
    return bool(agent.get("orchestrator") or agent.get("conductor") or not agent.get("swarm"))


def status():
    """Installed and logged in, as far as Codex's own files say (a real check is `test()`)."""
    if not available():
        return {"installed": False, "logged_in": False, "method": ""}
    import subprocess
    try:
        out = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=20)
        text = (out.stdout + out.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        text = ""
    logged = "Logged in" in text
    return {"installed": True, "logged_in": logged,
            "method": "ChatGPT" if "ChatGPT" in text else ("API key" if "API key" in text else ""),
            "limits": limits()}


def test():
    """One tiny real request, since Codex reports "logged in" even when its token expired.
    Returns {"ok", "message"}."""
    import subprocess
    import tempfile
    if not available():
        return {"ok": False, "message": "Codex isn't installed."}
    with tempfile.TemporaryDirectory(prefix="codex-test-") as folder:
        try:
            out = subprocess.run(["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--json",
                                  "--cd", folder, "Reply with exactly: OK"],
                                 capture_output=True, text=True, timeout=TEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": "Codex didn't answer in time."}
    for line in out.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "turn.completed":
            return {"ok": True, "message": "Codex answered."}
        if event.get("type") in ("turn.failed", "error"):
            message = (event.get("error") or {}).get("message") or event.get("message") or "Codex failed."
            return {"ok": False, "message": message}
    return {"ok": False, "message": (out.stderr or "Codex failed.").strip().splitlines()[-1][:300]}


def login_command():
    return "codex login"


def limits():
    """The ChatGPT plan's usage, from the newest Codex session's records, or None."""
    import transcripts
    files = sorted(transcripts.CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"),
                   key=lambda p: p.stat().st_mtime) if transcripts.CODEX_SESSIONS.exists() else []
    for path in reversed(files[-5:]):
        found = transcripts.codex_numbers(path)["rate_limits"]
        if found:
            return found
    return None


def link_skills(skills_dir, codex_skills=Path.home() / ".codex" / "skills"):
    """Codex reads skills from ~/.codex/skills: link Lago's there, keeping any of yours."""
    linked = []
    codex_skills.mkdir(parents=True, exist_ok=True)
    for skill in sorted(Path(skills_dir).glob("*/SKILL.md")):
        target = codex_skills / skill.parent.name
        if not target.exists() and not target.is_symlink():
            target.symlink_to(skill.parent)
            linked.append(skill.parent.name)
    return linked


def available():
    return shutil.which("codex") is not None


def _toml_string(value):
    return json.dumps(str(value))  # a JSON string is a valid TOML basic string


def command(agent_id, agent, definition, cwd, run_dir, python, app_dir, state_home=None, servers=None):
    """The shell command that runs the task in `run_dir/first-prompt.md` on Codex."""
    env = {"LAGO_AGENT_ID": agent_id}
    if state_home:
        env["XDG_STATE_HOME"] = state_home
    env_toml = "{" + ", ".join(f"{k} = {_toml_string(v)}" for k, v in env.items()) + "}"
    args = ["codex", "exec", "--skip-git-repo-check", "--cd", str(cwd),
            "--output-last-message", str(Path(run_dir) / "last-message.txt"),
            "-c", f"mcp_servers.lago.command={_toml_string(python)}",
            "-c", f"mcp_servers.lago.args=[{_toml_string(Path(app_dir) / 'lago_mcp.py')}]",
            "-c", f"mcp_servers.lago.env={env_toml}"] + server_flags(servers)
    if agent.get("unrestricted"):
        args.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        args += ["--sandbox", "workspace-write"]
    model = agent.get("model") or (definition or {}).get("codex_model")
    if model and not str(model).startswith(("claude", "opus", "sonnet", "haiku", "fable", "inherit")):
        args += ["--model", str(model)]
    return shlex.join(args) + " - < " + shlex.quote(str(Path(run_dir) / "first-prompt.md"))


def _mcp_flags(agent_id, python, app_dir, state_home):
    env = {"LAGO_AGENT_ID": agent_id}
    if state_home:
        env["XDG_STATE_HOME"] = state_home
    env_toml = "{" + ", ".join(f"{k} = {_toml_string(v)}" for k, v in env.items()) + "}"
    return ["-c", f"mcp_servers.lago.command={_toml_string(python)}",
            "-c", f"mcp_servers.lago.args=[{_toml_string(Path(app_dir) / 'lago_mcp.py')}]",
            "-c", f"mcp_servers.lago.env={env_toml}"]


def server_flags(servers):
    """`-c` flags for MCP servers in Claude Code's format (a project's own, a manifest's), so
    agents on Codex get the same ones. Servers Codex can't describe are left out."""
    flags = []
    for name, spec in sorted((servers or {}).items()):
        key = re.sub(r"[^A-Za-z0-9_-]", "_", name)
        if key == "lago" or not isinstance(spec, dict):
            continue
        if spec.get("command"):
            flags += ["-c", f"mcp_servers.{key}.command={_toml_string(spec['command'])}",
                      "-c", f"mcp_servers.{key}.args=[{', '.join(_toml_string(a) for a in spec.get('args') or [])}]"]
            if spec.get("env"):
                env = ", ".join(f"{k} = {_toml_string(v)}" for k, v in spec["env"].items())
                flags += ["-c", f"mcp_servers.{key}.env={{{env}}}"]
        elif spec.get("url"):
            flags += ["-c", f"mcp_servers.{key}.url={_toml_string(spec['url'])}"]
    return flags


def turn_args(agent_id, agent, definition, system_prompt, python, app_dir, state_home=None, thread=None,
              servers=None):
    """`codex exec` for one turn of a conversation (the message comes on stdin): a new thread,
    or `resume` of the agent's thread. The agent's instructions go in as developer
    instructions on every turn."""
    args = ["codex", "exec"] + (["resume", thread] if thread else []) + [
        "--json", "--skip-git-repo-check", "-c", f"developer_instructions={_toml_string(system_prompt)}",
        "-c", 'approval_policy="never"'] + _mcp_flags(agent_id, python, app_dir, state_home) + server_flags(servers)
    if agent.get("orchestrator"):
        # Lago acts through his lago tools only, as on Claude Code (where he has no shell).
        args += ["-c", 'sandbox_mode="read-only"']
    elif agent.get("unrestricted"):
        args.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        args += ["-c", 'sandbox_mode="workspace-write"', "-c", "sandbox_workspace_write.network_access=true"]
    model = agent.get("codex_model") or (definition or {}).get("codex_model")
    if model:
        args += ["-m", str(model)]
    return args + ["-"]


def prompt(system_prompt, initial_text):
    return (f"{system_prompt.strip()}\n\n---\n\n{(initial_text or '').strip()}\n\n"
            "When you're done, give your result as your final message: what you did, where the "
            "outputs are, and anything left open.")
