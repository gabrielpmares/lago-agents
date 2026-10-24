"""Reading session transcripts: Claude Code's (~/.claude/projects/*/<session id>.jsonl) and
Codex's (~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread id>.jsonl).

Turns them into a readable timeline: prompts, what the agent said, the tools it
used and whether they worked. Files are read incrementally, so polling is cheap. A Codex
conversation's session id is "codex:<thread id>" (codex_session.py sets it), so everything
that reads transcripts handles both.
"""

import json
import os
import re
import threading
import uuid
from pathlib import Path

import token_usage

PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CODEX_PREFIX = "codex:"
MAX_EVENTS = 400

PASTE_TAG = re.compile(r"</?pasted_content[^>]*>")  # Claude Code wraps pasted text in these

_cache = {}
_lock = threading.Lock()


def find(session_id):
    if not session_id:
        return None
    if session_id.startswith(CODEX_PREFIX):
        thread = session_id[len(CODEX_PREFIX):]
        if not re.fullmatch(r"[0-9a-f-]{8,64}", thread):
            return None
        matches = list(CODEX_SESSIONS.glob(f"*/*/*/rollout-*-{thread}.jsonl"))
        return max(matches, key=lambda p: p.stat().st_mtime) if matches else None
    matches = list(PROJECTS.glob(f"*/{session_id}.jsonl"))
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


LIVE = {"list_projects": "Looking at your projects", "work_sessions": "Checking the work sessions",
        "session_detail": "Checking a work session", "find_agents": "Looking for the right agent",
        "find_skills": "Looking for skills", "mail_inbox": "Checking your mail",
        "mail_read": "Reading an email", "github_board": "Checking the GitHub board",
        "github_repo": "Looking at a GitHub repo", "list_schedules": "Checking your reminders",
        "announce": "Saying something aloud", "remember": "Writing that down"}


def tool_live(name, tool_input):
    """What a tool call is doing, said as it happens: "Searching the web for ..."."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if name == "WebSearch":
        return f"Searching the web for “{_short(tool_input.get('query', ''), 80)}”"
    if name == "WebFetch":
        host = re.sub(r"^https?://(www\.)?", "", str(tool_input.get("url", ""))).split("/")[0]
        return f"Reading {host or 'a web page'}"
    if name == "Read":
        path = str(tool_input.get("file_path", ""))
        if "/attachments/" in path:
            return "Looking at your picture" if re.search(r"\.(png|jpe?g|gif|webp)$", path, re.I) \
                else "Reading the file you sent"
        return f"Reading {Path(path).name or 'a file'}"
    if name in ("Glob", "Grep"):
        return "Looking through files"
    action = name.removeprefix("mcp__lago__")
    if action in LIVE:
        return LIVE[action]
    if action == "run_agent":
        return f"Asking {tool_input.get('agent', 'an agent')}"
    if action == "schedule":
        return "Setting a reminder"
    if action == "cancel_schedule":
        return "Cancelling a reminder"
    if action == "app":
        return "Working in the app"
    return tool_summary(name, tool_input)


def _short(text, limit=300):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def tool_summary(name, tool_input):
    """One line saying what a tool call does, e.g. `Edit src/app.py`."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if name.startswith("mcp__lago__"):
        action = name.removeprefix("mcp__lago__")
        if action == "delegate":
            return f"Delegated to {tool_input.get('agent', '?')}: {tool_input.get('title') or _short(tool_input.get('task', ''), 90)}"
        if action == "complete_task":
            return f"Completed task #{tool_input.get('task_id', '?')} ({tool_input.get('status', 'done')})"
        pipeline = f"pipeline #{tool_input.get('pipeline_id', '?')}"
        if action == "propose_pipeline":
            return f"Proposed pipeline: {tool_input.get('title', '')} ({len(tool_input.get('steps') or [])} steps)"
        if action == "run_step":
            return f"Woke an agent for step {tool_input.get('step', '?')} of {pipeline}"
        if action in ("revise_pipeline", "finish_pipeline", "skip_step"):
            return f"{action.replace('_', ' ').capitalize()}: {pipeline}"
        if action == "app":
            args = tool_input.get("args") or {}
            what = " ".join(str(v) for v in args.values() if not isinstance(v, (dict, list)))
            return _short(f"{str(tool_input.get('action', '?')).replace('_', ' ').capitalize()} {what}", 140).strip()
        if action == "run_agent":
            return f"Asked {tool_input.get('agent', '?')}: {_short(tool_input.get('task', ''), 100)}"
        if action == "create_project":
            return f"Created project {tool_input.get('name', '?')} in {tool_input.get('folder', '?')}"
        if action == "open_work_session":
            area = f" / {tool_input['area']}" if tool_input.get("area") else ""
            where = tool_input.get("project") or "no project"
            return f"Opened a work session in {where}{area}"
        if action in ("message_session", "close_work_session"):
            verb = "Messaged" if action == "message_session" else "Closed"
            return f"{verb} work session #{tool_input.get('session', '?')}"
        if action == "schedule":
            return f"Scheduled: {_short(tool_input.get('text', ''), 140)}"
        if action == "cancel_schedule":
            return f"Cancelled reminder #{tool_input.get('id', '?')}"
        if action == "remember":
            return f"Remembered: {_short(tool_input.get('fact', ''), 140)}"
        if action in ("read_manifest", "find_agents", "project_profile"):
            what = tool_input.get("agent") or tool_input.get("query") or tool_input.get("project") or ""
            return f"{action.replace('_', ' ').capitalize()}{': ' + str(what) if what else ''}"
        return action.replace("_", " ").capitalize()
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description",
                "prompt", "skill"):
        if tool_input.get(key):
            value = str(tool_input[key])
            if key in ("file_path", "path"):
                value = "/".join(Path(value).parts[-3:])
            return f"{name}: {_short(value, 140)}"
    return name


def _codex_events(entry):
    """Timeline events from one line of a Codex rollout."""
    at = entry.get("timestamp")
    payload = entry.get("payload") or {}
    kind, ptype = entry.get("type"), payload.get("type")
    if kind == "event_msg" and ptype == "user_message":
        text = (payload.get("message") or "").strip()
        return [{"kind": "prompt", "text": text[:4000], "at": at, "sub": False, "uuid": None}] if text else []
    if kind == "event_msg" and ptype == "agent_message":
        text = (payload.get("message") or "").strip()
        return [{"kind": "say", "text": text[:4000], "at": at, "sub": False, "uuid": None}] if text else []
    if kind == "response_item" and ptype in ("function_call", "custom_tool_call"):
        name = payload.get("name", "")
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except ValueError:
            args = {"input": payload.get("arguments") or payload.get("input")}
        if name in ("exec_command", "shell") and isinstance(args, dict):
            name, args = "Bash", {"command": args.get("cmd") or " ".join(args.get("command") or [])}
        return [{"kind": "tool", "name": name, "text": tool_summary(name, args), "live": tool_live(name, args),
                 "id": payload.get("call_id"), "at": at, "sub": False}]
    if kind == "response_item" and ptype in ("function_call_output", "custom_tool_call_output"):
        output = payload.get("output")
        if isinstance(output, dict):
            output = output.get("content") or json.dumps(output)
        output = str(output or "")
        failed = bool(re.search(r"(Process exited with code|exit code:?)\s*[1-9]", output[:400]))
        return [{"kind": "result", "ok": not failed, "text": _short(output, 400), "id": payload.get("call_id"),
                 "at": at, "sub": False}]
    return []


def events_from(entry):
    if entry.get("type") in ("event_msg", "response_item"):
        return _codex_events(entry)
    kind = entry.get("type")
    if kind not in ("user", "assistant") or entry.get("isMeta"):
        return []
    at = entry.get("timestamp")
    sub = bool(entry.get("isSidechain"))
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    events = []
    for block in content or []:
        btype = block.get("type")
        if btype == "text" and block.get("text", "").strip():
            text = PASTE_TAG.sub("", block["text"]).strip()
            if kind == "user" and text.startswith("<") and "command" in text[:40]:
                continue  # slash-command bookkeeping
            events.append({"kind": "prompt" if kind == "user" else "say",
                           "text": text[:4000], "at": at, "sub": sub, "uuid": entry.get("uuid")})
        elif btype == "tool_use":
            events.append({"kind": "tool", "name": block.get("name", ""),
                           "text": tool_summary(block.get("name", ""), block.get("input")),
                           "live": tool_live(block.get("name", ""), block.get("input")),
                           "id": block.get("id"), "at": at, "sub": sub})
        elif btype == "tool_result":
            result = block.get("content")
            if isinstance(result, list):
                result = " ".join(b.get("text", "") for b in result if isinstance(b, dict))
            events.append({"kind": "result", "ok": not block.get("is_error"),
                           "text": _short(result or "", 400), "id": block.get("tool_use_id"),
                           "at": at, "sub": sub})
    return events


def _shown(state):
    """The cached events with secrets hidden. Only events new since the last call are
    redacted (the UI asks for Lago's every 1.5 s), unless the known secrets changed.
    state["shown"][i] is events[i] hidden, for the first len(shown) events."""
    import redact
    if not redact.enabled():
        state["known"] = None
        return list(state["events"])
    known = redact._known_pattern() or False
    if state.get("known") is not known:
        state["known"], state["shown"] = known, []
    shown = state.setdefault("shown", [])
    shown.extend(redact.deep(state["events"][len(shown):]))
    return list(shown)


def read(path):
    """{"events": [...newest last], "cost": float|None, "tokens": int (output),
    "input_tokens": int (cached or not), "model": str}"""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return {"events": [], "cost": None, "tokens": 0, "input_tokens": 0, "model": ""}
    with _lock:
        state = _cache.get(path)
        if state is None or size < state["offset"]:
            state = {"offset": 0, "events": [], "cost": None, "usage": token_usage.empty()}
            _cache[path] = state
        if size > state["offset"]:
            with open(path, "rb") as file:
                file.seek(state["offset"])
                chunk = file.read(size - state["offset"])
            end = chunk.rfind(b"\n") + 1  # leave a half-written last line for next time
            state["offset"] += end
            for raw in chunk[:end].splitlines():
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if entry.get("type") == "cost-state" and entry.get("totalCostUSD") is not None:
                    state["cost"] = entry["totalCostUSD"]
                token_usage.add_entry(state["usage"], entry)
                state["events"].extend(events_from(entry))
            excess = len(state["events"]) - MAX_EVENTS
            if excess > 0:
                del state["events"][:excess]
                del state.setdefault("shown", [])[:excess]
        usage = state["usage"]
        return {"events": _shown(state), "cost": state["cost"],
                "tokens": usage["output_tokens"], "input_tokens": token_usage.total_input(usage),
                "model": usage["model"], "api_cost": usage["cost"]}


class TranscriptError(Exception):
    pass


def fork_before(session_id, message_uuid):
    """Copy a session's transcript up to (not including) the message `message_uuid`, as a
    new session in the same folder, and return its id; None when nothing came before it.
    The original is left as it was. This is what Claude Code's rewind does: resuming the
    copy carries on from just before that message."""
    path = find(session_id)
    if not path:
        raise TranscriptError("Lago's conversation wasn't found")
    with open(path, encoding="utf-8") as file:
        entries = []
        for raw in file:
            try:
                entries.append(json.loads(raw))
            except ValueError:
                continue  # a half-written last line
    cut = next((i for i, e in enumerate(entries)
                if e.get("type") == "user" and e.get("uuid") == message_uuid), None)
    if cut is None:
        raise TranscriptError("That message isn't in Lago's current conversation")
    head = entries[:cut]
    if not any(e.get("type") in ("user", "assistant") for e in head):
        return None
    new_id = str(uuid.uuid4())
    target = path.with_name(f"{new_id}.jsonl")
    partial = path.with_name(f".{new_id}.jsonl.partial")
    with open(partial, "w", encoding="utf-8") as file:
        for entry in head:
            if "sessionId" in entry:
                entry["sessionId"] = new_id
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(partial, target)
    return new_id


def codex_numbers(path):
    """From a Codex rollout: the context's size on the last call and the plan's usage limits."""
    tokens, limits = 0, None
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return {"context_tokens": 0, "rate_limits": None}
    for line in lines:
        if '"token_count"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        info = payload.get("info") or {}
        last = info.get("last_token_usage") or {}
        if last:
            tokens = last.get("input_tokens", 0) + last.get("output_tokens", 0)
        limits = payload.get("rate_limits") or limits
    return {"context_tokens": tokens, "rate_limits": limits}


def last_assistant_text(path):
    for event in reversed(read(path)["events"]):
        if event["kind"] == "say" and not event["sub"]:
            return event["text"]
    return ""
