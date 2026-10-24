"""Your verdicts on what Lago decided for you, so his next decisions follow them.

In Decide mode each decision Lago takes is a "decided" report. You can mark one a good call
or not what you'd have done, with a note. memory.prompt_section() puts the recent verdicts in
each new conversation's prompt ("How your past decisions went"), so the same kind of decision
goes your way next time.
"""

import time

import activity
import hub

LESSONS = 8


class DecisionError(Exception):
    pass


def judge(event_id, verdict, note=""):
    """verdict 1 (good call) or -1 (not what you'd have done)."""
    event = hub.events("id = ?", (int(event_id),), limit=1)
    if not event or event[0]["kind"] != "decided":
        raise DecisionError(f"Report #{event_id} isn't one of Lago's decisions")
    if verdict not in (1, -1):
        raise DecisionError("A verdict is 1 (good call) or -1 (not what you'd have done)")
    note = " ".join((note or "").split())[:500]
    with hub.connect() as conn:
        conn.execute("INSERT INTO decision_verdicts (event_id, verdict, note, at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (event_id) DO UPDATE SET verdict = excluded.verdict, "
                     "note = excluded.note, at = excluded.at", (event[0]["id"], verdict, note, time.time()))
        conn.execute("UPDATE events SET seen = 1 WHERE id = ?", (event[0]["id"],))
    first = event[0]["text"].split("\n")[0][:200]
    activity.log("you", "decision.judged", f"{'Good call' if verdict > 0 else 'Not what I would do'}: "
                 f"{first}" + (f" ({note})" if note else ""), target=f"report #{event[0]['id']}",
                 workspace=event[0]["workspace"], project=event[0]["project"])
    return {"ok": True}


def with_verdicts(events):
    """The events, each decision with its "verdict" and "verdict_note" if you gave one."""
    ids = [e["id"] for e in events if e["kind"] == "decided"]
    if not ids:
        return events
    with hub.connect() as conn:
        found = {r["event_id"]: dict(r) for r in conn.execute(
            f"SELECT * FROM decision_verdicts WHERE event_id IN ({','.join('?' * len(ids))})", ids)}
    for e in events:
        if e["id"] in found:
            e["verdict"], e["verdict_note"] = found[e["id"]]["verdict"], found[e["id"]]["note"]
    return events


def lessons(limit=LESSONS):
    """Recent judged decisions as prompt lines, the ones you disagreed with first."""
    with hub.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT v.verdict, v.note, v.at, e.text, e.project FROM decision_verdicts v "
            "JOIN events e ON e.id = v.event_id ORDER BY v.at DESC LIMIT ?", (limit * 3,))]
    rows.sort(key=lambda r: (r["verdict"] > 0, -r["at"]))
    lines = []
    for r in rows[:limit]:
        decision = r["text"].split("\n")[0][:160]
        mark = "Good call" if r["verdict"] > 0 else "Not what they'd have done"
        lines.append(f"- {mark}: {decision}" + (f" ({r['project']})" if r["project"] else "")
                     + (f". Their note: {r['note']}" if r["note"] else ""))
    return lines
