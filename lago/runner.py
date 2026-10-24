"""Running agent sessions in Lago's own tmux server.

Each agent is one `claude` session in a tmux session named `la-<agent id>` on the
`lago-agents` socket, separate from any tmux you use yourself. Sessions keep
running when the app is closed.
"""

import os
import subprocess
import time

from config_store import STATE_DIR

SOCKET = "lago-agents"
TMUX_CONF = STATE_DIR / "tmux.conf"
TMUX_CONF_TEXT = """\
set -g default-terminal "tmux-256color"
set -g escape-time 0
set -g history-limit 50000
set -g mouse on
set -g status off
set -g focus-events on
set -g extended-keys on
setw -g window-size latest
"""


def _env():
    env = dict(os.environ)
    env.pop("TMUX", None)  # agents call us from inside their own tmux session
    return env


def tmux(*args, input_text=None, timeout=10):
    if not TMUX_CONF.exists() or TMUX_CONF.read_text() != TMUX_CONF_TEXT:
        TMUX_CONF.write_text(TMUX_CONF_TEXT)
    try:
        proc = subprocess.run(["tmux", "-L", SOCKET, "-f", str(TMUX_CONF), *args],
                              input=input_text, capture_output=True, text=True,
                              timeout=timeout, env=_env())
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def session_name(agent_id):
    return f"la-{agent_id}"


def running_agents():
    ok, output = tmux("list-sessions", "-F", "#{session_name}")
    if not ok:
        return set()
    return {line[3:] for line in output.splitlines() if line.startswith("la-")}


def is_running(agent_id):
    return tmux("has-session", "-t", f"={session_name(agent_id)}")[0]


def new_session(agent_id, script, cwd):
    return tmux("new-session", "-d", "-s", session_name(agent_id), "-x", "200", "-y", "50",
                "-c", str(cwd), f"bash {script}")


def kill(agent_id):
    return tmux("kill-session", "-t", f"={session_name(agent_id)}")


def paste(agent_id, text):
    """Type a message into the agent's prompt and submit it."""
    if text.startswith("!"):
        text = " " + text  # a leading ! would run it as a shell command in Claude Code, not a message
    target = f"={session_name(agent_id)}:"
    buffer = f"lago-{agent_id}"
    ok, output = tmux("load-buffer", "-b", buffer, "-", input_text=text)
    if not ok:
        return False, output
    ok, output = tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", target)
    if not ok:
        return False, output
    time.sleep(0.5)  # let Claude Code take in the paste before Enter submits it
    return tmux("send-keys", "-t", target, "Enter")


def keys(agent_id, *names):
    return tmux("send-keys", "-t", f"={session_name(agent_id)}:", *names)


def capture(agent_id, lines=40):
    ok, output = tmux("capture-pane", "-p", "-J", "-t", f"={session_name(agent_id)}:",
                      "-S", f"-{lines}")
    import redact
    return redact.text(output) if ok else ""


def attach_argv(agent_id):
    return ["tmux", "-L", SOCKET, "-f", str(TMUX_CONF), "attach-session",
            "-t", f"={session_name(agent_id)}"]
