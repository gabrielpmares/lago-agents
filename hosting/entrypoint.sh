#!/bin/sh
# Starts one customer's Lago in its container: makes their folders on the volume the first
# time, answers Claude Code's first-run questions, then serves Lago until the container stops.
set -eu
mkdir -p "$HOME" "$LAGO_AGENTS_HOME" "$XDG_STATE_HOME" /data/projects
chmod 700 /data/home /data/config /data/state

# Claude Code asks once (theme, then whether to use an API key it finds) before it does
# anything: in a container nobody would be there to answer, so the answers are written here.
python3 - <<'PY'
import json, os
path = os.path.expanduser("~/.claude.json")
try:
    with open(path) as f:
        state = json.load(f)
except (OSError, ValueError):
    state = {}
state["hasCompletedOnboarding"] = True
key = os.environ.get("ANTHROPIC_API_KEY", "")
if key:
    answers = state.setdefault("customApiKeyResponses", {"approved": [], "rejected": []})
    if key[-20:] not in answers.setdefault("approved", []):
        answers["approved"].append(key[-20:])
with open(path, "w") as f:
    json.dump(state, f, indent=2)
os.chmod(path, 0o600)
PY

cd /data/projects
exec python3 /app/lago/app_server.py
