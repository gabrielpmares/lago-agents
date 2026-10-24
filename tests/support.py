"""Shared setup for the tests: a throwaway state folder and config.json, so tests never
touch the real hub.db or config. Import this before any Lago module."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "lago"  # the app's code
TMP = Path(tempfile.mkdtemp(prefix="lago-tests-"))
os.environ["XDG_STATE_HOME"] = str(TMP / "state")
os.environ["LAGO_AGENTS_HOME"] = str(TMP / "data")
os.environ.pop("LAGO_AGENT_ID", None)
sys.path.insert(0, str(APP))

import config_store  # noqa: E402
import subprocess  # noqa: E402

# Nothing a test does may start, stop or enable your real services: every systemctl (or, on
# macOS, launchctl) call answers as if there were no such service.
_real_run = subprocess.run


def _no_systemctl(args, *rest, **kwargs):
    if isinstance(args, (list, tuple)) and args and Path(str(args[0])).name in ("systemctl", "launchctl"):
        text = kwargs.get("text") or kwargs.get("universal_newlines")
        empty = "" if text else b""
        return subprocess.CompletedProcess(args, 1, empty, "no services in tests" if text else b"no services in tests")
    return _real_run(args, *rest, **kwargs)


subprocess.run = _no_systemctl

import desktop  # noqa: E402

# Service definitions a test writes land here, not in ~/.config/systemd or ~/Library/LaunchAgents.
desktop.SYSTEMD_DIR = TMP / "systemd-user"
desktop.LAUNCH_DIR = TMP / "launch-agents"
desktop.LAUNCH_LOGS = TMP / "launch-logs"

config_store.CONFIG_PATH = TMP / "config.json"

# Nothing a test does may speak, play a sound or touch the real tmux server.
import runner  # noqa: E402
import voice  # noqa: E402

voice.say = lambda *a, **k: False
voice.play_file = lambda *a, **k: None
REAL_TMUX = runner.tmux  # for a test that runs tmux on a server of its own (it sets runner.SOCKET first)


_tmux_sockets = set()


def note_tmux():
    """Remember where the test's own tmux server keeps its socket (call once it runs)."""
    ok, path = REAL_TMUX("display-message", "-p", "#{socket_path}")
    if ok and path:
        _tmux_sockets.add(path)


def end_tmux():
    """Stop the test's own tmux server and remove its socket file."""
    note_tmux()
    REAL_TMUX("kill-server")
    for path in _tmux_sockets:
        Path(path).unlink(missing_ok=True)
    _tmux_sockets.clear()
runner.tmux = lambda *a, **k: (False, "tests never use tmux")
runner.running_agents = lambda: set()
runner.is_running = lambda agent_id: False
import listen  # noqa: E402

listen.set_service = lambda on: None  # never installs or starts the real wake-phrase listener

# Nothing a test installs may link into your real ~/.claude: skills and agents link here.
import portability  # noqa: E402
import skill_sources  # noqa: E402

# Nor store anything in your real keyring: secrets go to a dict for the tests' run.
import vault  # noqa: E402


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def store(self, name, project, value):
        self.values[(name, project or "")] = value

    def lookup(self, name, project):
        return self.values.get((name, project or ""))

    def clear(self, name, project):
        self.values.pop((name, project or ""), None)


KEYRING = MemoryKeyring()
vault._backend = KEYRING

CLAUDE_HOME = TMP / "claude-home"
skill_sources.LINK_DIR = portability.SKILLS_LINK_DIR = CLAUDE_HOME / "skills"
portability.AGENTS_LINK_DIR = CLAUDE_HOME / "agents"

BASE_CONFIG = {
    "teams": {"dev": {"name": "Dev", "cwd": str(TMP)}},
    "agents": {"dev-lead": {"name": "Lead", "team": "dev", "definition": "orchestrator",
                            "reports_to": None},
               "dev-qa": {"name": "QA", "team": "dev", "definition": "qa-engineer",
                          "reports_to": "dev-lead"}},
    "projects": {"demo": {"name": "Demo", "cwd": str(TMP / "demo")}},
    "swarm": {},
}


def reset():
    """A fresh hub.db and config.json for each test."""
    import hub
    for suffix in ("", "-wal", "-shm"):
        Path(str(hub.DB_PATH) + suffix).unlink(missing_ok=True)
    hub._migrated = False
    import live
    live._ready = False  # its table goes with the old hub.db
    shutil.rmtree(config_store.STATE_DIR / "agent-drafts", ignore_errors=True)
    shutil.rmtree(CLAUDE_HOME, ignore_errors=True)
    KEYRING.values.clear()
    vault._backend = KEYRING
    shutil.rmtree(config_store.DATA_DIR / "claude-skills", ignore_errors=True)
    shutil.rmtree(config_store.DATA_DIR / "catalog" / "agents", ignore_errors=True)  # no agent left over from another test
    config_store.CONFIG_PATH.write_text(json.dumps(BASE_CONFIG))
