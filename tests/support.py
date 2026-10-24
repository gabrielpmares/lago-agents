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

# Nothing a test does may start, stop or enable your real services: every systemctl call
# answers as if there were no such service.
_real_run = subprocess.run


def _no_systemctl(args, *rest, **kwargs):
    if isinstance(args, (list, tuple)) and args and Path(str(args[0])).name == "systemctl":
        text = kwargs.get("text") or kwargs.get("universal_newlines")
        empty = "" if text else b""
        return subprocess.CompletedProcess(args, 1, empty, "no services in tests" if text else b"no services in tests")
    return _real_run(args, *rest, **kwargs)


subprocess.run = _no_systemctl

config_store.CONFIG_PATH = TMP / "config.json"

# Nothing a test does may speak, play a sound or touch the real tmux server.
import runner  # noqa: E402
import voice  # noqa: E402

voice.say = lambda *a, **k: False
voice.play_file = lambda *a, **k: None
runner.tmux = lambda *a, **k: (False, "tests never use tmux")
runner.running_agents = lambda: set()
runner.is_running = lambda agent_id: False
import listen  # noqa: E402

listen.set_service = lambda on: None  # never installs or starts the real wake-phrase listener

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
    config_store.CONFIG_PATH.write_text(json.dumps(BASE_CONFIG))
