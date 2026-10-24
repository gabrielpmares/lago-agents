#!/usr/bin/env python3
"""Claude Code hook for agents started by Lago Agents.

Every agent session runs this on its lifecycle events (see team._settings) and it
records what the agent is doing in the hub. It must stay quiet and fast: it prints
nothing and never blocks the agent.

    lago_hook.py              read a hook event (JSON) from stdin
    lago_hook.py --exited N   the claude process ended with exit code N
    lago_hook.py --pump       deliver the agent's inbox once it is back at its prompt (and,
                              for Lago, his tools have connected)
    lago_hook.py --failover   the agent hit a usage limit: resume it on another account
    lago_hook.py --permission-timeout   Lago didn't answer a permission prompt: ask the human
    lago_hook.py --measure    measure the conversation's spend again, once its log has caught up
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounts  # noqa: E402
import activity  # noqa: E402
import budgets  # noqa: E402
import config_store  # noqa: E402
import diagnostics  # noqa: E402
import hub  # noqa: E402
import live  # noqa: E402
import notify  # noqa: E402
import runner  # noqa: E402
import team  # noqa: E402
import voice  # noqa: E402
import toolrules  # noqa: E402
import transcripts  # noqa: E402

TASK_MARKER = re.compile(r"\[Lago task #(\d+) from ")
PUMP_DELAY = 1.5
MEASURE_DELAY = 4  # seconds after a turn ends, for its last reply to be in the log
LIMIT_ERRORS = ("rate_limit", "billing_error")
# What a crash means, shown above the Restart button.
EXIT_REASONS = {
    "127": "Couldn't find the claude command (Claude Code was probably updating itself). Restart it.",
    "143": "The session was killed. Restart it to carry on.",
}
PERMISSION_TEXT = """[Lago] Permission request from {name} (`{agent_id}`, {where}). The human \
hasn't been told. It wants to: {asked}

Its screen:
{screen}

Approve it (`app` approve, target `{agent_id}`) when it's part of the task and not critical; \
deny it (`app` deny) when it's wrong or out of scope, and tell the session why. Only if it's \
critical (production, irreversible, money, in the human's name, credentials), `app` ask_human \
(target `{agent_id}`) and put it to them as a decision. The human is asked anyway in \
{seconds} seconds if you haven't answered."""
RESUMED_TEXT = ("[Lago] Your previous Claude account ran out of usage, so this session was "
                "resumed on another account. Continue where you left off.")


def pump_later():
    """Deliver pending messages after this hook returns and the prompt is ready again."""
    _detached("--pump")


def _detached(flag):
    """Run this script with `flag` in its own session, so it outlives the agent's tmux."""
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), flag],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def fail_over(agent_id):
    """The agent's turn ended on a limit error. If /usage confirms its account is out (not a
    short rate limit), move to an account with room and resume the agent there."""
    import fcntl
    with open(hub.STATE_DIR / "failover.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # agents hitting the limit together switch once
        account = hub.get_state(agent_id).get("account") or accounts.MAIN
        report = accounts.usage(account, refresh=True)
        if accounts.worst(report) < accounts.EXHAUSTED:
            return
        config = config_store.load()
        target = accounts.out_of_usage(config, account, report)
        if not target:
            team.wait_for_usage(agent_id, config_store.load())
            return
    if target != account:
        team.stop(agent_id)
        time.sleep(1)
        team.start(agent_id, initial_text=RESUMED_TEXT, keep_session=True)


def on_event(agent_id, event):
    name = event.get("hook_event_name", "")
    if name == "SessionStart":
        why = budgets.blocked(agent_id, event.get("session_id"))
        # Lago gets no messages until his tools have connected (team.wait_for_tools).
        connecting = agent_id == config_store.ORCHESTRATOR_ID
        hub.set_state(agent_id, session_id=event.get("session_id"),
                      status="over_budget" if why else
                      "working" if os.environ.get("LAGO_FIRST_PROMPT") == "1" else
                      "connecting" if connecting else "idle",
                      detail=why or ("Connecting his tools…" if connecting else ""))
        if os.environ.get("LAGO_FIRST_PROMPT") != "1" and not why:
            pump_later()
    elif name == "UserPromptSubmit":
        prompt = event.get("prompt", "")
        why = budgets.blocked(agent_id, event.get("session_id"))
        if why:
            hub.queue_text(agent_id, prompt)  # delivered once you extend the budget
            hub.set_state(agent_id, status="over_budget", detail=why)
            print(json.dumps({"decision": "block", "reason": why}))
            return
        task_ids = [int(n) for n in TASK_MARKER.findall(prompt)]
        for task_id in task_ids:
            task = hub.get_task(task_id)
            if task and task["to_agent"] == agent_id and task["status"] in hub.OPEN_TASK:
                hub.update_task(task_id, status="working")
        fields = {"status": "working", "detail": transcripts._short(prompt, 120),
                  "prompted_at": time.time()}
        if task_ids:
            fields["current_task"] = task_ids[-1]
        hub.set_state(agent_id, **fields)
    elif name == "PreToolUse":
        tool = event.get("tool_name", "")
        focused = config_store.focus() if agent_id == config_store.ORCHESTRATOR_ID else None
        if focused and tool in config_store.FOCUS_BLOCKED:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": f"The human is working with `{focused}` only: hand "
                f"this to it with run_agent. If it can't do this, tell them and ask whether "
                f"to stop using it."}}))
            return
        why = budgets.check(agent_id, event.get("session_id"), event.get("transcript_path"))
        if why:
            # Ends the turn here, whatever the permission mode.
            print(json.dumps({"continue": False, "stopReason": why, "hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": why}}))
            return
        waiting = tool == "mcp__lago__wait_for_tasks"
        hub.set_state(agent_id, status="waiting_on_team" if waiting else "working",
                      detail="Waiting for its team" if waiting
                      else transcripts.tool_summary(tool, event.get("tool_input")),
                      last_tool=json.dumps({"tool": tool, "subject": toolrules.subject(tool, event.get("tool_input"))[:500]}))
        try:  # the Live Map's activity; never in the way of the permission check below
            live.record(agent_id, tool, event.get("tool_input"), toolrules.subject(tool, event.get("tool_input")), cwd=event.get("cwd"))
        except Exception as error:
            log_error(f"live.record: {error!r}")
        rule = toolrules.decide(agent_id, tool, event.get("tool_input"))
        if rule:  # a standing permission you set (toolrules.py)
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": rule["decision"],
                "permissionDecisionReason": f"Standing rule #{rule['id']}: {rule['decision']} {rule['tool']} "
                f"{rule['pattern']}" + (" (ask the human another way, or do without it)" if rule["decision"] == "deny" else "")}}))
    elif name == "PostToolUse":
        if hub.get_state(agent_id)["status"] in ("needs_approval", "waiting_on_team"):
            hub.set_state(agent_id, status="working", lago_review=None)
    elif name == "Notification":
        text = event.get("message", "")
        if "permission" in text.lower() or event.get("notification_type") == "permission_prompt":
            asked = hub.get_state(agent_id).get("detail") or ""  # its last tool call (PreToolUse)
            hub.set_state(agent_id, status="needs_approval", detail=text)
            if not hand_to_lago(agent_id, asked or text):
                tell_human(agent_id, "permission", text)
        elif event.get("notification_type") == "idle_prompt":
            on_idle(agent_id, event)
    elif name == "Stop":
        on_stop(agent_id, event)
    elif name == "StopFailure" and event.get("error") in LIMIT_ERRORS:
        hub.set_state(agent_id, status="idle", detail=f"Hit a usage limit: {event.get('error_details', '')}"[:200])
        _detached("--failover")
    elif name == "StopFailure" and agent_id == config_store.ORCHESTRATOR_ID \
            and flagged(event.get("transcript_path")):
        on_flagged(agent_id)
    elif name == "StopFailure":
        error = f"{event.get('error', 'error')}: {event.get('error_details', '')}".strip(": ")
        activity.log(agent_id, "agent.api_error", error[:500], target=agent_id)
        hub.set_state(agent_id, status="idle", detail=f"Its last turn failed ({error})"[:200])
        _end_direct_run(agent_id, event, f"It stopped with an error and didn't finish: {error}")
    elif name == "SessionEnd":
        if agent_id == config_store.ORCHESTRATOR_ID:
            config_store.drop_unrestricted()
        hub.set_state(agent_id, status="stopped", detail="", current_task=None)


def tell_human(agent_id, kind, text):
    """Report for a work session's Conductor or agent (spoken, and relayed if orchestrated)."""
    agent = config_store.load()["agents"].get(agent_id) or {}
    if agent.get("workspace"):
        notify.report(agent["workspace"], kind, f"{agent['name']} ({agent_id}): {text}")


def hand_to_lago(agent_id, asked):
    """A permission prompt in a session Lago runs (one he opened, or an agent he started):
    give it to him to approve, deny or pass on, and ask the human only if he doesn't answer
    in time. False when he can't take it (he isn't running, or you opened the session)."""
    config = config_store.load()
    agent = config["agents"].get(agent_id) or {}
    workspace = hub.get_workspace(agent["workspace"]) if agent.get("workspace") else None
    lagos = bool(config_store.RUN_ID.match(agent_id)) or bool(workspace and workspace.get("orchestrated"))
    if not lagos or not runner.is_running(config_store.ORCHESTRATOR_ID):
        return False
    hub.set_state(agent_id, lago_review=time.time())
    lines = [line.rstrip() for line in runner.capture(agent_id, 25).splitlines()]
    screen = "\n".join(line for i, line in enumerate(lines) if line or (i and lines[i - 1])).strip()
    notify.tell_orchestrator(PERMISSION_TEXT.format(
        name=agent.get("name", agent_id), agent_id=agent_id,
        where=f"work session #{workspace['id']}" if workspace else "a run you started",
        asked=asked, screen=team.clip(screen, 2000) or "(empty)",
        seconds=config_store.LAGO_REVIEW_SECONDS), start=False)
    _detached("--permission-timeout")
    return True


def permission_timeout(agent_id):
    """Lago had his chance: if the prompt still waits on him, the human is asked."""
    time.sleep(config_store.LAGO_REVIEW_SECONDS)
    state = hub.get_state(agent_id)
    since = state.get("lago_review")
    if state["status"] != "needs_approval" or not since \
            or time.time() - since < config_store.LAGO_REVIEW_SECONDS - 1:
        return  # answered, passed on, or a newer prompt has its own timer
    hub.set_state(agent_id, lago_review=None)
    tell_human(agent_id, "permission", f"{state['detail']} (Lago didn't get to it in time.)")


FLAG_TEXT = "safeguards flagged this session"
FLAG_RESTART_GAP = 1800


def flagged(transcript_path):
    """Whether the turn failed because the model's safeguards flagged the conversation (the
    hook's error says only invalid_request; the log's last API error says why). A flagged
    conversation fails every turn after, so resuming it is no use."""
    try:
        with open(transcript_path, encoding="utf-8") as file:
            lines = file.readlines()[-30:]
    except (OSError, TypeError):
        return False
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "assistant" and entry.get("isApiErrorMessage"):
            return FLAG_TEXT in json.dumps(entry.get("message") or {})
    return False


def on_flagged(agent_id):
    """Lago's conversation was flagged: start him on a fresh one from his notes, once. If the
    fresh one is flagged too soon after, leave him stopped and tell you."""
    marker = config_store.ORCHESTRATOR_FLAG_RESTART
    activity.log(agent_id, "agent.api_error", f"invalid_request: {FLAG_TEXT}", target=agent_id)
    try:
        recent = time.time() - marker.stat().st_mtime < FLAG_RESTART_GAP
    except OSError:
        recent = False
    if recent:
        hub.set_state(agent_id, status="idle", detail="Flagged again in a fresh conversation: "
                      "pick another model or start a new conversation yourself")
        hub.add_event(None, None, "news", "Lago's fresh conversation was flagged too. Pick another "
                      "model for him, or start a new conversation once the topic has moved on.")
        return
    marker.touch()
    hub.set_state(agent_id, status="idle", detail="Conversation flagged: starting a fresh one")
    import memory
    memory.rotate_later(clear=True)


def _end_direct_run(agent_id, event, why):
    """A run Lago started whose turn ended without a result (an error, or an interrupt):
    fail its task with why and its last message, so it doesn't sit open for ever."""
    if not (config_store.load()["agents"].get(agent_id) or {}).get("direct"):
        return
    path = event.get("transcript_path") or transcripts.find(event.get("session_id"))
    last = (transcripts.last_assistant_text(path) if path else "") or ""
    for task in hub.open_tasks_for(agent_id):
        if task["status"] == "working":
            team.finish(task["id"], why + (f"\n\nIts last message:\n{last}" if last else ""),
                        status="failed")


def on_idle(agent_id, event):
    """An agent has sat at its prompt for a minute. A run Lago started that still has its
    task open was interrupted (a finished turn would have closed it): fail the task. A
    Conductor that asked something in its terminal without reporting it: tell the human it's
    waiting, with its last message."""
    agent = config_store.load()["agents"].get(agent_id) or {}
    if agent.get("direct"):
        _end_direct_run(agent_id, event, "It stopped before finishing: its turn was interrupted.")
        return
    if not agent.get("conductor"):
        return
    if not notify.quiet_since(agent["workspace"], hub.get_state(agent_id).get("prompted_at")):
        return
    path = event.get("transcript_path") or transcripts.find(event.get("session_id"))
    last = transcripts.last_assistant_text(path) if path else ""
    notify.report(agent["workspace"], "waiting",
                  "The Conductor finished its turn and is waiting for an answer. Its last "
                  f"message:\n\n{last or '(none)'}")


def on_stop(agent_id, event):
    """The agent finished a turn: collect any task it forgot to complete, then go idle (or
    stay stopped when a budget ran out)."""
    why = budgets.blocked(agent_id, event.get("session_id"))
    if why:  # a budget ended this turn: it isn't finished, so its task isn't either
        hub.set_state(agent_id, status="over_budget", detail=why)
        return
    # A run started directly (by Lago or the evaluations) ends with its result as its last
    # message, whoever handed it out; a team member's task from you stays open for your reply.
    direct = (config_store.load()["agents"].get(agent_id) or {}).get("direct")
    working = [t for t in hub.open_tasks_for(agent_id)
               if t["status"] == "working" and (t["from_agent"] or direct)]
    if working:
        path = event.get("transcript_path") or transcripts.find(event.get("session_id"))
        text = event.get("last_assistant_message") or \
            (transcripts.last_assistant_text(path) if path else "") or "(no final message)"
        for task in working:
            team.finish(task["id"], text, auto=True)
    if agent_id == config_store.ORCHESTRATOR_ID:
        path = event.get("transcript_path") or transcripts.find(event.get("session_id"))
        reply = event.get("last_assistant_message") or \
            (transcripts.last_assistant_text(path) if path else "")
        voice.speak_reply(config_store.load(), reply)
    path = event.get("transcript_path") or transcripts.find(event.get("session_id"))
    _detached("--measure")  # the turn's last reply can reach the log after this hook runs
    if budgets.check(agent_id, event.get("session_id"), path, force=True):
        hub.set_state(agent_id, current_task=None)  # check() marked it out of budget
        return
    hub.set_state(agent_id, status="idle", detail="", current_task=None)
    if hub.has_inbox(agent_id):
        pump_later()


def log_error(text):
    with open(hub.STATE_DIR / "hook-errors.log", "a", encoding="utf-8") as file:
        file.write(f"{os.environ.get('LAGO_AGENT_ID')}: {text}\n")


def main():
    agent_id = os.environ.get("LAGO_AGENT_ID")
    if not agent_id:
        return
    if sys.argv[1:] == ["--failover"]:
        fail_over(agent_id)
        return
    if sys.argv[1:] == ["--permission-timeout"]:
        permission_timeout(agent_id)
        return
    if sys.argv[1:] == ["--measure"]:
        time.sleep(MEASURE_DELAY)
        session_id = hub.get_state(agent_id).get("session_id")
        path = transcripts.find(session_id) if session_id else None
        if path and budgets.check(agent_id, session_id, path, force=True):
            hub.set_state(agent_id, current_task=None)
        return
    if sys.argv[1:] == ["--pump"]:
        team.wait_for_tools(agent_id)
        time.sleep(PUMP_DELAY)
        team.pump(agent_id, auto_start=False)
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--codex-exited":
        team.codex_exited(agent_id, sys.argv[2])
        return
    if sys.argv[1:] == ["--codex-pump"]:
        team.pump(agent_id)
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--exited":
        code = sys.argv[2]
        if agent_id == config_store.ORCHESTRATOR_ID:
            config_store.drop_unrestricted()
        detail = "" if code == "0" else EXIT_REASONS.get(
            code, f"Claude Code exited with code {code}. Its terminal is below.")
        if code != "0":
            why = diagnostics.text(diagnostics.explain(hub.get_state(agent_id).get("session_id")))
            activity.log(agent_id, "agent.crashed", f"{detail}\n{why}".strip(), target=agent_id)
            detail = f"{detail}\n{why}".strip()[:600]
        hub.set_state(agent_id, status="stopped" if code == "0" else "exited", detail=detail,
                      current_task=None)
        return
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    on_event(agent_id, event)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # a broken hook must never get in the agent's way
        log_error(repr(error))
    sys.exit(0)
