"""Why an agent failed, from its transcript: its last failed tool calls and what it last said.

Used when Claude Code exits with an error, when a turn ends on an API error (StopFailure),
and when a task fails, so the report says what went wrong instead of only that it did.
"""

import transcripts

MAX_ERRORS = 3


def explain(session_id=None, path=None):
    """{"errors": [{"tool", "input", "error"}], "last_said": str, "last_tool": str} or None."""
    path = path or (transcripts.find(session_id) if session_id else None)
    if not path:
        return None
    events = transcripts.read(path)["events"]
    if not events:
        return None
    calls = {e["id"]: e for e in events if e["kind"] == "tool" and e.get("id")}
    errors = []
    for event in reversed(events):
        if event["kind"] == "result" and not event["ok"]:
            call = calls.get(event.get("id")) or {}
            errors.append({"tool": call.get("name", "?"), "input": call.get("text", ""),
                           "error": event["text"]})
            if len(errors) == MAX_ERRORS:
                break
    said = next((e["text"] for e in reversed(events) if e["kind"] == "say"), "")
    tool = next((e["text"] for e in reversed(events) if e["kind"] == "tool"), "")
    return {"errors": list(reversed(errors)), "last_said": said[:600], "last_tool": tool}


def text(diagnosis, limit=900):
    """A few lines for a report, or "" when there's nothing useful."""
    if not diagnosis:
        return ""
    lines = []
    for error in diagnosis["errors"]:
        lines.append(f"- {error['input'] or error['tool']} failed: {error['error'][:240]}")
    if diagnosis["last_tool"] and not diagnosis["errors"]:
        lines.append(f"- Last step: {diagnosis['last_tool']}")
    if diagnosis["last_said"]:
        lines.append(f"- Last said: {diagnosis['last_said'][:240]}")
    out = ("What went wrong:\n" + "\n".join(lines)) if lines else ""
    return out[:limit]
