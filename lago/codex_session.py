#!/usr/bin/env python3
"""A conversation on Codex: Lago, a lead or a team member, in its terminal.

Claude Code keeps a session open and reports through hooks; `codex exec` runs one turn and
exits. So this loop is the session: it takes the agent's messages (the first prompt, then
whatever arrives in its inbox), runs one `codex exec` per message (resuming the same Codex
thread after the first), shows what happens in a readable form, and tells Lago Agents what a
hook would: a prompt went in (status working, its tasks started), the turn ended (its last
message, tasks it forgot to complete, Lago's spoken reply, idle). Ctrl+C interrupts a turn.

    codex_session.py <agent id>      # what team.start runs in the agent's tmux session
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_store  # noqa: E402
import hub  # noqa: E402
import transcripts  # noqa: E402

POLL_SECONDS = 1.0
INBOX_JOIN = "\n\n---\n\n"
GREY, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def run_dir(agent_id):
    return config_store.STATE_DIR / "runs" / agent_id


def _say(text, style=""):
    print(f"{style}{text}{RESET if style else ''}", flush=True)


def describe(event):
    """A line for the terminal from one `codex exec --json` event, or None."""
    kind = event.get("type", "")
    item = event.get("item") or {}
    itype = item.get("type") or item.get("item_type") or ""
    if kind == "item.completed" and itype in ("agent_message", "assistant_message"):
        return f"\n{item.get('text', '')}\n"
    if kind == "item.started" and itype == "command_execution":
        return f"{GREY}$ {item.get('command', '')}{RESET}"
    if kind == "item.started" and itype == "mcp_tool_call":
        return f"{GREY}→ {item.get('server', '')} {item.get('tool', '')}{RESET}"
    if kind == "item.completed" and itype == "file_change":
        changes = item.get("changes") or []
        return f"{GREY}edited {', '.join(c.get('path', '') for c in changes)[:200]}{RESET}"
    if kind == "turn.failed" or kind == "error":
        message = (event.get("error") or {}).get("message") or event.get("message") or "failed"
        return f"Codex: {message}"
    return None


def next_prompt(agent_id):
    """The first prompt if one is waiting, else everything in the inbox (blocks until some)."""
    first = run_dir(agent_id) / "first-prompt.md"
    if first.exists():
        text = first.read_text()
        first.unlink()
        if text.strip():
            return text
    while True:
        if _held(agent_id):
            time.sleep(POLL_SECONDS)
            continue
        texts = hub.take_inbox(agent_id)
        if texts:
            return INBOX_JOIN.join(texts)
        time.sleep(POLL_SECONDS)


def _held(agent_id):
    import holds
    return holds.held(agent_id)


def run_turn(agent_id, text, args, cwd):
    """Run one `codex exec`. Returns (thread id or None, last message, error or None)."""
    thread, last, error = None, "", None
    proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        proc.stdin.write(text)
        proc.stdin.close()
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "thread.started":
                thread = event.get("thread_id")
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and (item.get("type") or item.get("item_type")) in (
                    "agent_message", "assistant_message"):
                last = item.get("text", "")
            if event.get("type") in ("turn.failed", "error"):
                error = (event.get("error") or {}).get("message") or event.get("message") or "Codex failed"
            line_out = describe(event)
            if line_out:
                _say(line_out)
        proc.stdout.close()
        proc.wait()
    except KeyboardInterrupt:
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        error = "Interrupted"
        _say("\n(interrupted)", GREY)
    if proc.returncode not in (0, None) and not error:
        error = f"Codex exited with code {proc.returncode}"
    return thread, last, error


def main(agent_id):
    import codex_runtime
    import lago_hook
    config = config_store.load()
    agent = config["agents"].get(agent_id) or {}
    folder = run_dir(agent_id)
    cwd = str(config_store.team_cwd(config, agent))
    system_prompt = (folder / "system-prompt.md").read_text()
    try:
        servers = json.loads((folder / "codex-mcp.json").read_text())
    except (OSError, ValueError):
        servers = {}
    thread_file = folder / "codex-thread"
    thread = thread_file.read_text().strip() if thread_file.exists() else None
    if thread:
        hub.set_state(agent_id, session_id=transcripts.CODEX_PREFIX + thread)
    hub.set_state(agent_id, status="idle", detail="")
    _say(f"{BOLD}{agent.get('name', agent_id)}{RESET} on Codex{' (resuming)' if thread else ''}. "
         "Messages arrive here; Ctrl+C interrupts a turn.", GREY)
    state_home = os.environ.get("XDG_STATE_HOME")
    while True:
        try:
            text = next_prompt(agent_id)
        except KeyboardInterrupt:  # nothing running to interrupt
            continue
        session_id = transcripts.CODEX_PREFIX + thread if thread else None
        lago_hook.on_event(agent_id, {"hook_event_name": "UserPromptSubmit", "prompt": text,
                                      "session_id": session_id})
        if hub.get_state(agent_id)["status"] == "over_budget":
            continue
        _say(f"\n{BOLD}▶ {text.strip().splitlines()[0][:160]}{RESET}")
        args = codex_runtime.turn_args(agent_id, agent, None, system_prompt, sys.executable,
                                       config_store.APP_DIR, state_home, thread, servers)
        new_thread, last, error = run_turn(agent_id, text, args, cwd)
        if new_thread and new_thread != thread:
            thread = new_thread
            thread_file.write_text(thread)
        session_id = transcripts.CODEX_PREFIX + thread if thread else None
        if session_id:
            hub.set_state(agent_id, session_id=session_id)
        if error and not last:
            hub.set_state(agent_id, status="idle", detail=f"Its last turn failed ({error})"[:200])
            import activity
            activity.log(agent_id, "agent.api_error", error[:500], target=agent_id)
            continue
        path = transcripts.find(session_id) if session_id else None
        lago_hook.on_stop(agent_id, {"session_id": session_id, "transcript_path": str(path) if path else None,
                                     "last_assistant_message": last})


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        main(sys.argv[1])
    except KeyboardInterrupt:
        pass
