"""Questions with a form: a Conductor asks with choices (and room for a note), you answer with a
click in the app or on your phone, and the answer goes back to it as a message.

A question report may carry a form (events.form, JSON):
    {"questions": [{"id": "db", "prompt": "Which database?", "choices": ["Postgres", "SQLite"],
                    "multi": false, "recommended": "Postgres"}]}
A question with no choices takes a written answer. answer() checks the answers against the
form, records them (events.answer) and types them into the Conductor's session.
"""

import json
import re
import time

import activity
import config_store
import hub

MAX_QUESTIONS = 5
MAX_CHOICES = 8


class QuestionError(Exception):
    pass


def clean_form(questions):
    """Validate a form from a Conductor. Returns the JSON to store, or None for no form."""
    if not questions:
        return None
    if not isinstance(questions, list) or len(questions) > MAX_QUESTIONS:
        raise QuestionError(f"questions must be a list of at most {MAX_QUESTIONS}")
    out, seen = [], set()
    for n, q in enumerate(questions, 1):
        if not isinstance(q, dict) or not str(q.get("prompt") or "").strip():
            raise QuestionError(f"Question {n} needs a prompt")
        qid = re.sub(r"[^a-z0-9_-]", "", str(q.get("id") or f"q{n}").lower()) or f"q{n}"
        if qid in seen:
            raise QuestionError(f"Question id {qid!r} is used twice")
        seen.add(qid)
        choices = [str(c).strip() for c in q.get("choices") or [] if str(c).strip()]
        if len(choices) > MAX_CHOICES:
            raise QuestionError(f"Question {n}: at most {MAX_CHOICES} choices")
        recommended = str(q.get("recommended") or "").strip()
        if recommended and recommended not in choices:
            recommended = ""
        out.append({"id": qid, "prompt": str(q["prompt"]).strip()[:300], "choices": choices,
                    "multi": bool(q.get("multi")) and len(choices) > 1, "recommended": recommended})
    return json.dumps({"questions": out})


def form_text(form):
    """The form as lines, for Lago's relay and for the spoken report's text."""
    lines = []
    for q in json.loads(form)["questions"]:
        options = " / ".join(f"{c}{' (recommended)' if c == q['recommended'] else ''}" for c in q["choices"])
        lines.append(f"- {q['prompt']}" + (f" [{options}]" if options else " [written answer]")
                     + (" (pick any)" if q["multi"] else ""))
    return "\n".join(lines)


def answer(event_id, answers, note="", by="you"):
    """Answer a question report: {"<question id>": "choice" | ["choice", ...] | "text"}."""
    import team
    rows = hub.events("id = ?", (int(event_id),), limit=1)
    if not rows or rows[0]["kind"] != "question" or not rows[0].get("form"):
        raise QuestionError(f"Report #{event_id} isn't a question with a form")
    event = rows[0]
    if event.get("answered_at"):
        raise QuestionError(f"Question #{event_id} was already answered")
    lines, clean = [], {}
    for q in json.loads(event["form"])["questions"]:
        given = (answers or {}).get(q["id"])
        if q["choices"]:
            picked = given if isinstance(given, list) else [given] if given else []
            picked = [p for p in picked if p in q["choices"]]
            if not picked:
                raise QuestionError(f"Pick an answer for: {q['prompt']}")
            if not q["multi"]:
                picked = picked[:1]
            clean[q["id"]] = picked if q["multi"] else picked[0]
            lines.append(f"- {q['prompt']} → {', '.join(picked)}")
        else:
            text = " ".join(str(given or "").split())
            if not text:
                raise QuestionError(f"Write an answer for: {q['prompt']}")
            clean[q["id"]] = text[:2000]
            lines.append(f"- {q['prompt']} → {text[:2000]}")
    note = (note or "").strip()
    with hub.connect() as conn:
        done = conn.execute("UPDATE events SET answer = ?, answered_at = ?, seen = 1 WHERE id = ? "
                            "AND answered_at IS NULL",
                            (json.dumps({"answers": clean, "note": note, "by": by}), time.time(),
                             event["id"])).rowcount
    if not done:
        raise QuestionError(f"Question #{event_id} was already answered")
    who = "The human" if by == "you" else "Lago, for the human,"
    team.message(config_store.conductor_id(event["workspace"]),
                 f"{who} answered your question (report #{event['id']}):\n" + "\n".join(lines)
                 + (f"\nTheir note: {note}" if note else ""))
    activity.log(by, "question.answered", "; ".join(lines)[:500], target=f"report #{event['id']}",
                 workspace=event["workspace"], project=event["project"])
    return {"ok": True, "message": "Answered."}
