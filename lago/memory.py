#!/usr/bin/env python3
"""Lago's memory of your conversations, kept small so talking to him stays cheap.

Lago (the assistant you talk to) doesn't carry one conversation forever: every day, or
once a conversation's context grows past NEW_CONVERSATION_TOKENS, he starts a fresh one.
The one he leaves is summarized by a cheap model (Haiku) into:

- `memory/days/YYYY-MM-DD.md`: what you asked, what was decided, which sessions were opened,
  what's still open. One file per day, a section per conversation.
- `memory/about-you.md`: lasting facts about you and how you like to work. Lago adds to it
  with his `remember` tool; you can edit it in the app.
- `memory/sessions.md`: one line per closed work session, what it delivered. While a
  session is open Lago follows it live; once it closes, this line is all he keeps.
- `memory/threads.md`: "Where things stand", one line per piece of work you have going:
  where it stands and its next step. It's rewritten, not appended to, each time a
  conversation is written down or a work session closes, so finished items drop off instead
  of coming back as old to-dos.

Each new conversation starts with about-you, where things stand, today's notes (or the last
day's), the headlines of the days before, the latest closed-session lines and, when it picks
up the same day, the last messages of the previous one (see prompt_section). Lago uses them
to pick up where you left off and to suggest a next step, raising each item once at most.
Full days stay on disk for him to grep when you mention them.

    memory.py --summarize SESSION_ID   summarize one past conversation (run detached)
    memory.py --session-closed N       write down work session #N's one-line outcome
    memory.py --rotate                 Lago is idle and due a fresh conversation: summarize,
                                       restart him and deliver what's waiting for him
    memory.py --threads                rebuild "where things stand" from the recent notes
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounts  # noqa: E402
import config_store  # noqa: E402
import hub  # noqa: E402
import token_usage  # noqa: E402
import transcripts  # noqa: E402

MEMORY_DIR = config_store.ORCHESTRATOR_DIR / "memory"
ABOUT_FILE = MEMORY_DIR / "about-you.md"
DAYS_DIR = MEMORY_DIR / "days"
SESSIONS_FILE = MEMORY_DIR / "sessions.md"  # one line per closed work session
THREADS_FILE = MEMORY_DIR / "threads.md"  # where things stand, rewritten each time
SUMMARIZED_FILE = MEMORY_DIR / "summarized.json"  # session id -> written down up to (epoch)
LOCK_FILE = MEMORY_DIR / ".lock"

# Context size (one API call) after which he starts fresh. His instructions and tools alone
# are about 30k, so this sits well above that: below it he'd restart after every message.
NEW_CONVERSATION_TOKENS = 200_000
DAY_STARTS_HOUR = 4               # work past midnight still counts as the same day
RECENT_DAYS = 3                   # earlier days whose headlines go into every new conversation
DAY_LIMIT = 2500                  # characters of today's (or the last day's) notes
DAY_HEADLINES = 3                 # conversations of each earlier day named in it
CLOSED_SESSIONS = 8               # closed-session lines in every new conversation
LATEST_REPORTS = 6                # of each open session (last REPORT_DAYS), for the note writer
REPORT_DAYS = 3
CLEAR_WAIT_SECONDS = 180         # a new conversation you asked for waits for his reply this long
CARRY_MESSAGES = 10               # last messages carried into a same-day fresh conversation
CARRY_CHARS = 1500                # of each of them
TRANSCRIPT_LIMIT = 60_000         # characters of a conversation the summarizer reads
SUMMARY_MODEL = "sonnet"  # notes good enough to pick work up from; written about once a day
SUMMARY_TIMEOUT = 180

ABOUT_TEMPLATE = """# About you

Lasting facts Lago keeps about you: preferences, people, how you like to work. Edit freely.
"""


def _day(at=None):
    """The date a moment belongs to, counting the small hours as the day before."""
    moment = datetime.fromtimestamp(at or time.time()) - timedelta(hours=DAY_STARTS_HOUR)
    return moment.date().isoformat()


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


# ---------------------------------------------------------------- the conversation

INBOX_JOIN = "\n\n---\n\n"  # how team.pump joins waiting messages
SPOKEN_LINE = re.compile(r"^[^\w🔊]*🔊.*$", re.MULTILINE)


def messages(session_id):
    """The conversation without tool traffic: [{"who": you|lago|note, "text", "at"}]. Notes
    are what Lago Agents typed in for you (reports, the greeting prompt)."""
    path = transcripts.find(session_id)
    if not path:
        return []
    events = []
    with open(path, encoding="utf-8") as file:  # all of it: transcripts.read keeps the tail
        for line in file:
            try:
                events.extend(transcripts.events_from(json.loads(line)))
            except ValueError:
                continue
    out = []
    for event in events:
        if event["sub"] or event["kind"] not in ("prompt", "say"):
            continue
        if event["kind"] == "say":
            out.append({"who": "lago", "text": event["text"], "at": event["at"]})
            continue
        # Messages that waited while he was busy arrive together, joined by INBOX_JOIN:
        # your words can be among reports.
        for text in event["text"].split(INBOX_JOIN):
            text = text.strip()
            if text:
                out.append({"who": "note" if text.startswith("[Lago") else "you",
                            "text": text, "at": event["at"]})
    return out


def context_tokens(session_id):
    """How big the conversation's context was on its last API call."""
    path = transcripts.find(session_id)
    if path and (session_id or "").startswith(transcripts.CODEX_PREFIX):
        return transcripts.codex_numbers(path)["context_tokens"]
    last = None
    for _, usage in token_usage.messages(path) if path else ():
        last = usage
    if not last:
        return 0
    return sum(last.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                          "cache_read_input_tokens", "output_tokens"))


def rotation_due(session_id):
    """Whether Lago should leave this conversation for a fresh one: it began on an earlier
    day, or its context has grown past NEW_CONVERSATION_TOKENS."""
    path = transcripts.find(session_id)
    if not path:
        return False
    first = next((m for m in messages(session_id) if m["at"]), None)
    started = _epoch(first["at"]) if first else path.stat().st_mtime
    if started and _day(started) != _day():
        return True
    return context_tokens(session_id) > NEW_CONVERSATION_TOKENS


# ---------------------------------------------------------------- summarizing

def _summarized():
    """{session id: epoch it's written down up to}. A conversation can carry on after it was
    written down (Lago restarted keeping it), so only what came later is summarized next."""
    try:
        marks = json.loads(SUMMARIZED_FILE.read_text())
        if isinstance(marks, list):  # the old format: ids written down when the file was saved
            stamp = SUMMARIZED_FILE.stat().st_mtime
            marks = {session_id: stamp for session_id in marks}
        return marks
    except (OSError, ValueError):
        return {}


def _not_written_down(session_id):
    """The conversation's messages after the point it was last written down."""
    mark = _summarized().get(session_id)
    items = messages(session_id)
    if mark is None:
        return items
    return [m for m in items if (_epoch(m["at"]) or 0) > mark]


def summarized(session_id):
    """Whether nothing you said in the conversation is left to write down."""
    return not any(m["who"] == "you" for m in _not_written_down(session_id))


def about():
    try:
        return ABOUT_FILE.read_text()
    except OSError:
        return ABOUT_TEMPLATE


def save_about(text):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    text = (text or "").strip()
    if text and not text.startswith("#"):
        text = "# About you\n\n" + text
    ABOUT_FILE.write_text((text or ABOUT_TEMPLATE.strip()) + "\n")


def remember(fact):
    """Add one lasting fact to about-you.md."""
    fact = " ".join((fact or "").split())
    if not fact:
        raise ValueError("Say what to remember")
    text = about().rstrip()
    if f"- {fact}" in text:
        return
    save_about(f"{text}\n- {fact}")


def _conversation_text(items, user_name):
    lines = []
    for m in items:
        when = datetime.fromtimestamp(_epoch(m["at"]) or 0).strftime("%H:%M")
        who = {"you": user_name, "lago": "Lago", "note": "Lago Agents (automatic)"}[m["who"]]
        text = SPOKEN_LINE.sub("", m["text"]).strip() if m["who"] == "lago" else m["text"]
        lines.append(f"[{when}] {who}: {text}")
    text = "\n\n".join(lines)
    return text[-TRANSCRIPT_LIMIT:]


SUMMARY_PROMPT = """You keep the memory of Lago, a voice assistant that runs {name}'s software \
work: {name} tells Lago what to work on, and Lago opens "work sessions" (one per task, \
numbered #N, each in a project) and relays their reports and questions.

Below are what Lago knows about {name}, where their work stands, the work sessions open now \
and recently closed, and a conversation from {day}. Write Lago's memory of it the way a good \
chief of staff would after a day with their boss: enough to pick up tomorrow where they left \
off, without asking {name} to explain again. Work sessions keep their own records, so leave \
out pipeline steps, file lists and figures.

Reply with only a JSON object, no code fences:
{{"headline": "<one line, at most 12 words: the gist of the conversation>",
 "summary": "<markdown bullets, at most 8: what {name} asked for and why, decisions with \
their reasons, work sessions opened or closed ('#N project: task'), and where things were \
left: what waits on whom.>",
 "threads": "<the full, updated 'Where things stand' list: markdown bullets, at most 10, \
most recent first, one per piece of work {name} has going or said they want to get to, each \
as '**Project**: what, where it stands, the next step and who it waits on (#N when a session \
has it)'. Start from the current list: update what the conversation and the sessions' latest \
reports moved on (they're newer than the list), add new work, \
and drop items that are finished, cancelled, superseded, or that {name} said to forget. An \
item whose session closed stays only if something is still left to do. Empty if nothing is \
in flight.>",
 "about": "<the full, updated 'About you' markdown: lasting facts only (preferences, people, \
priorities, how they like things done). Keep points still true; drop any that are project \
status, one-off decisions, specific features or events, or what Lago can or can't access \
(apps, accounts, tools: that changes, and Lago's tools say it), and merge near-duplicates. \
At most 25 short bullets: when over, merge or drop the least useful.>"}}

## What Lago knows about {name}
{about}

## Where things stand
{threads}

## Work sessions
{sessions}

## The conversation
{conversation}
"""

THREADS_PROMPT = """You keep the memory of Lago, a voice assistant that runs {name}'s software \
work through "work sessions" (one per task, numbered #N, each in a project). Below is Lago's \
"Where things stand" list, the work sessions open now and recently closed, and {news}

Reply with only the updated list: markdown bullets, at most 10, most recent first, one per \
piece of work {name} has going or said they want to get to, each as '**Project**: what, \
where it stands, the next step and who it waits on (#N when a session has it)'. Open \
sessions' reports are newer than the notes: where they disagree, go by the reports. \
Drop items that are finished, cancelled or superseded; an item whose session closed stays only if \
something is still left to do. Don't record what Lago can or can't access (apps, accounts, \
tools): that changes, and his tools say it. Reply with nothing at all if nothing is in flight.

## Where things stand
{threads}

## Work sessions
{sessions}

## {heading}
{body}
"""


def _ask_codex(prompt):
    """The same one-shot question on Codex, for setups whose provider is a ChatGPT subscription."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="lago-ask-") as folder:
        answer = Path(folder) / "answer.txt"
        proc = subprocess.run(["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--cd", folder,
                               "-c", 'sandbox_mode="read-only"', "-o", str(answer), "-"],
                              input=prompt, capture_output=True, text=True, timeout=SUMMARY_TIMEOUT)
        if proc.returncode != 0 or not answer.exists():
            raise RuntimeError((proc.stderr or proc.stdout).strip()[-300:] or "codex exec failed")
        return answer.read_text()


def _ask_model(prompt, json_reply=True, model=None):
    config = config_store.load()
    import codex_runtime
    if codex_runtime.provider(config) == "codex":
        text = _ask_codex(prompt).strip()
        if not json_reply:
            return text
        found = re.search(r"\{.*\}", text, re.DOTALL)
        if not found:
            raise RuntimeError("The summary wasn't JSON")
        return json.loads(found.group(0))
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                                        "CLAUDE_CONFIG_DIR", "LAGO_"))}
    environment.update(accounts.env(accounts.active(config)))
    model = model or (config.get("orchestrator") or {}).get("memory_model") or SUMMARY_MODEL
    proc = subprocess.run(
        ["claude", "-p", "--model", model, "--tools", "", "--strict-mcp-config",
         "--no-session-persistence", "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=SUMMARY_TIMEOUT,
        env=environment, cwd=MEMORY_DIR)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:300] or "claude -p failed")
    if not json_reply:
        return proc.stdout.strip()
    found = re.search(r"\{.*\}", proc.stdout, re.DOTALL)
    if not found:
        raise RuntimeError("The summary wasn't JSON")
    return json.loads(found.group(0))


def summarize(session_id):
    """Write a past conversation into its days' files (one summary per day it spans) and
    update about-you. Safe to call twice: only what came after the last write-down is
    summarized, and a conversation with nothing new is skipped."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    DAYS_DIR.mkdir(exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if summarized(session_id):
            return False
        items = _not_written_down(session_id)
        by_day = {}
        for m in items:
            by_day.setdefault(_day(_epoch(m["at"])), []).append(m)
        name = config_store.load().get("user_name") or "the human"
        for day, items in sorted(by_day.items()):
            if any(m["who"] == "you" for m in items):
                _summarize_day(day, items, name)
        marks = _summarized()
        marks[session_id] = max([_epoch(m["at"]) or 0 for m in items] or [time.time()])
        SUMMARIZED_FILE.write_text(json.dumps(marks, indent=0, sort_keys=True))
        return True


def _summarize_day(day, items, name):
    result = _ask_model(SUMMARY_PROMPT.format(name=name, day=day, about=about(),
                                              threads=threads() or "(nothing yet)",
                                              sessions=_sessions_text(),
                                              conversation=_conversation_text(items, name)))
    stamps = [t for t in (_epoch(m["at"]) for m in items) if t]
    span = "–".join(datetime.fromtimestamp(t).strftime("%H:%M")
                    for t in (stamps[0], stamps[-1])) if stamps else ""
    day_file = DAYS_DIR / f"{day}.md"
    head = "" if day_file.exists() else \
        f"# {datetime.fromisoformat(day).strftime('%A %-d %B %Y')}\n"
    with open(day_file, "a", encoding="utf-8") as file:
        file.write(f"{head}\n## {span} · {_markdown(result.get('headline'))}\n\n"
                   f"{_markdown(result.get('summary'))}\n")
    if _markdown(result.get("about")):
        save_about(_markdown(result["about"]))
    if "threads" in result:
        save_threads(_markdown(result["threads"]))


def _markdown(value):
    """The model sometimes answers a list where it was asked for markdown bullets."""
    if isinstance(value, list):
        return "\n".join(item if str(item).lstrip().startswith(("-", "#")) else f"- {item}"
                         for item in map(str, value))
    return str(value or "").strip()


def summarize_later(session_id):
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--summarize", session_id],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def rotate_later(clear=False):
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--rotate"]
                     + (["--clear"] if clear else []),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


LOCAL_COMMAND_TAGS = ("<bash-input>", "<bash-stdout>", "<bash-stderr>", "<local-command-", "<command-name>")


def _local_command(entry):
    """A `!` shell command or a slash command run in his terminal: no turn of his, no Stop."""
    content = (entry.get("message") or {}).get("content") if entry.get("type") == "user" else None
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return isinstance(content, str) and content.lstrip().startswith(LOCAL_COMMAND_TAGS)


def between_turns(session_id):
    """Whether Lago's conversation ended its last turn (its log says so), whatever his status
    record says."""
    path = transcripts.find(session_id)
    if not path:
        return True
    last = None
    with open(path, encoding="utf-8") as file:
        for line in file:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") in ("user", "assistant", "system") and not _local_command(entry):
                last = entry
    return bool(last) and last.get("type") == "system" and \
        last.get("subtype") in ("turn_duration", "stop_hook_summary")


def rotate(clear=False):
    """Lago is idle and due a fresh conversation: write this one down, restart him (keeping
    his unrestricted mode) and hand him what's waiting in his inbox. With `clear` you asked
    for it: it needn't be due, it waits for him to finish his reply, and the new conversation
    starts from his notes only, without the last messages."""
    import runner
    import team

    oid = config_store.ORCHESTRATOR_ID
    previous = hub.get_state(oid).get("session_id")
    if not previous or config_store.rotating() or not (clear or rotation_due(previous)):
        return
    config_store.ORCHESTRATOR_ROTATING.touch()
    try:
        def busy():
            return hub.get_state(oid)["status"] not in ("idle", "stopped") \
                and not (clear and between_turns(previous))
        deadline = time.time() + CLEAR_WAIT_SECONDS
        while clear and runner.is_running(oid) and time.time() < deadline and busy():
            time.sleep(2)  # he's still answering the message that asked for this
        try:
            summarize(previous)
        except Exception as error:  # a fresh start without a summary beats a stuck Lago
            _log(f"summary of {previous} failed: {error!r}")
        if runner.is_running(oid):
            if busy():
                return  # you started talking meanwhile; try again next time
            unrestricted = config_store.ORCHESTRATOR_UNRESTRICTED.exists()
            team.stop(oid)
            if unrestricted:
                config_store.ORCHESTRATOR_UNRESTRICTED.touch()
            time.sleep(2)  # let the old session's exit hooks finish first
        if clear:
            waiting = hub.take_inbox(oid)
            team.start(oid, fresh=True, carry=False,
                       initial_text="\n\n---\n\n".join(waiting) if waiting else None)
        elif hub.has_inbox(oid):
            team.pump(oid)
        else:
            team.start(oid)
    finally:
        config_store.ORCHESTRATOR_ROTATING.unlink(missing_ok=True)


# ---------------------------------------------------------------- where things stand

def threads():
    """The "Where things stand" bullets, or "" when nothing's written down."""
    try:
        return THREADS_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_threads(text):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    THREADS_FILE.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def _sessions_text():
    """The open work sessions with their latest plan, and the recently closed ones."""
    config = config_store.load()
    lines = ["Open now:"]
    for ws in sorted(hub.workspaces("status = 'open'"), key=lambda w: w["id"]):
        project = config["projects"].get(ws["project"], {}).get("name", ws["project"]) \
            if ws["project"] else config_store.NO_PROJECT
        latest = hub.pipelines("workspace = ?", (ws["id"],), limit=1)
        plan = f' (plan "{latest[0]["title"]}": {latest[0]["status"]})' if latest else ""
        lines.append(f'- #{ws["id"]} {project}: "{ws["title"][:120]}"{plan}')
        # Newer than the notes when only reports came in since. Oldest first.
        for report in reversed(hub.events("workspace = ? AND kind != 'update' AND created_at > ?",
                                          (ws["id"], time.time() - REPORT_DAYS * 86400),
                                          limit=LATEST_REPORTS)):
            when = datetime.fromtimestamp(report["created_at"]).strftime("%d %b %H:%M")
            lines.append(f"  Report ({when}, {report['kind']}): "
                         + " ".join(report["text"].split())[:300])
    if len(lines) == 1:
        lines.append("- none")
    lines += ["", "Closed recently:", *(closed_sessions() or ["- none"])]
    return "\n".join(lines)


def update_threads(news, heading, body):
    """Rewrite where things stand after something happened, by the memory model. `news` ends
    the prompt's opening sentence ("a work session that just closed."), `heading` titles
    `body`, the details."""
    name = config_store.load().get("user_name") or "the human"
    reply = _ask_model(THREADS_PROMPT.format(
        name=name, news=news, heading=heading, body=body,
        threads=threads() or "(nothing yet)", sessions=_sessions_text()), json_reply=False)
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        save_threads("\n".join(line for line in reply.splitlines() if line.lstrip().startswith(("-", "*"))))


def rebuild_threads():
    """Build where things stand from the last few days' notes (the first time, or to reset)."""
    notes = "\n\n".join(d["text"] for d in reversed(days(RECENT_DAYS + 1))) or "(no notes)"
    update_threads("Lago's notes from the last few days.", "Lago's notes", notes[-TRANSCRIPT_LIMIT:])


def _log(text):
    with open(config_store.STATE_DIR / "hook-errors.log", "a", encoding="utf-8") as file:
        file.write(f"memory: {text}\n")


def days(limit=None):
    """[{"day", "path", "headlines"}], newest first."""
    out = []
    for path in sorted(DAYS_DIR.glob("*.md"), reverse=True)[:limit]:
        text = path.read_text(encoding="utf-8")
        headlines = [line.split(" · ", 1)[-1].strip() for line in text.splitlines()
                     if line.startswith("## ")]
        out.append({"day": path.stem, "path": str(path), "headlines": headlines, "text": text})
    return out


# ---------------------------------------------------------------- closed work sessions

SESSION_PROMPT = """A work session (one task, run by a Conductor and its agents) just closed. \
Write the one line an assistant would keep about it.

Reply with only that line, no quotes or markdown: at most 25 words, what was delivered and \
where (a file, PR or deploy), or that it was cancelled or failed and why. Go by the final \
report and the pipelines' status: earlier reports are history, and problems they mention may \
have been fixed since.

Session #{id} in {place}: "{title}"
{pipelines}
## Earlier reports, oldest first
{history}

## Final report
{final}
"""


def session_closed_later(workspace_id):
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--session-closed",
                      str(workspace_id)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def session_closed(workspace_id):
    """Write down a closed work session as one line in sessions.md."""
    import notify
    workspace = hub.get_workspace(int(workspace_id))
    if workspace is None:
        return
    config = config_store.load()
    _, place = notify.where(config, workspace)
    pipelines = hub.pipelines("workspace = ?", (workspace["id"],), limit=3)
    events = hub.events("workspace = ?", (workspace["id"],), limit=5)
    try:
        line = _ask_model(SESSION_PROMPT.format(
            id=workspace["id"], place=place, title=workspace["title"],
            pipelines="\n".join(f"Pipeline #{p['id']} \"{p['title']}\": {p['status']}"
                                for p in pipelines),
            history="\n".join(f"- {e['kind']}: {e['text'][:800]}" for e in events[:0:-1])
            or "(none)",
            final=f"{events[0]['kind']}: {events[0]['text'][:2000]}" if events else "(none)"),
            json_reply=False)
    except Exception as error:
        _log(f"resume of session #{workspace_id} failed: {error!r}")
        line = f"closed after a {events[0]['kind']} report" if events else "closed"
    line = " ".join(line.split()).strip("\"'")[:300]
    title = workspace["title"] if len(workspace["title"]) <= 60 \
        else workspace["title"][:60].rstrip() + "…"
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(SESSIONS_FILE, "a", encoding="utf-8") as file:
        file.write(f"- {_day()} · #{workspace['id']} {place}, \"{title}\": {line}\n")
    if threads():
        try:
            update_threads("a work session that just closed.", "The session that closed",
                           f"#{workspace['id']} {place}, "
                           f"\"{workspace['title']}\": {line}")
        except Exception as error:
            _log(f"threads after session #{workspace_id} closed: {error!r}")


def closed_sessions(limit=CLOSED_SESSIONS):
    try:
        lines = SESSIONS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line for line in lines if line.startswith("- ")][-limit:]


# ---------------------------------------------------------------- what Lago starts with

def _demote(text, levels):
    """Move markdown headings down, so a day file nests under the prompt's own headings."""
    return re.sub(r"^(#+) ", lambda m: "#" * min(6, len(m.group(1)) + levels) + " ", text,
                  flags=re.MULTILINE)


def prompt_section(config, previous_session=None):
    """The memory part of Lago's system prompt for a new conversation."""
    name = config.get("user_name") or "the human"
    lines = ["## Your memory",
             f"Your notes from earlier conversations, so you can pick up where you left off and "
             f"understand short requests without making {name} explain again. Bring an item up "
             f"at most once per conversation, when it fits; never recite them. They're a "
             "snapshot: check live state before repeating what they say about running work, "
             "and go by your tools, not your notes, for what you can reach. Everything older "
             "is in `memory/days/` and `memory/sessions.md` in your folder.",
             "", "### About " + name, re.sub(r"\A# .*\n+", "", about().strip())]
    standing = threads()
    if standing:
        written = datetime.fromtimestamp(THREADS_FILE.stat().st_mtime).strftime("%a %d %b %H:%M")
        lines += ["", f"### Where things stand (as of {written})",
                  "Open sessions' live state (`work_sessions`) is newer when they differ.", standing]
    recent = days(RECENT_DAYS + 1)
    today = recent[0] if recent and recent[0]["day"] == _day() else None
    earlier = [d for d in recent if d is not today][:RECENT_DAYS]
    if earlier and not today:  # a new day: the last one in full, to carry on from
        last = earlier.pop(0)
        text = _demote(re.sub(r"\A# .*\n+", "", last["text"].strip()), 3)
        lines += ["", f"### Last time ({last['day']})", text if len(text) <= DAY_LIMIT else
                  text[-DAY_LIMIT:] + f"\n…(all of it in memory/days/{last['day']}.md)"]
    if earlier:
        lines += ["", "### Earlier days (headlines; details in memory/days/)"]
        for d in reversed(earlier):
            shown = d["headlines"][-DAY_HEADLINES:]
            more = len(d["headlines"]) - len(shown)
            lines.append(f"- {d['day']}: " + "; ".join(shown)
                         + (f" (+{more} more)" if more else ""))
    if config_store.lago_mode(config) == "decide":
        import decisions
        judged = decisions.lessons()
        if judged:
            lines += ["", "### How your past decisions went",
                      f"{name}'s verdicts on decisions you took for them. Decide the same kind of "
                      "question their way; when one you're about to take looks like one they "
                      "disagreed with, bring it to them instead.", *judged]
    finished = closed_sessions()
    if finished:
        lines += ["", "### Work sessions that closed recently", *finished]
    if today:
        text = _demote(today["text"].strip(), 3)
        lines += ["", "### Today so far", text if len(text) <= DAY_LIMIT else
                  text[:DAY_LIMIT] + f"\n…(more in memory/days/{today['day']}.md)"]
    carried = [m for m in messages(previous_session) if m["who"] != "note"][-CARRY_MESSAGES:] \
        if previous_session else []
    if carried and _epoch(carried[-1]["at"]) and _day(_epoch(carried[-1]["at"])) == _day():
        lines += ["", "### Where you left off",
                  "Your previous conversation today got long, so this is a fresh one. Its last "
                  "messages:"]
        for m in carried:
            who = name if m["who"] == "you" else "You (Lago)"
            text = SPOKEN_LINE.sub("", m["text"]).strip()
            lines.append(f"> **{who}:** {text[:CARRY_CHARS]}".replace("\n", "\n> "))
    return "\n".join(lines)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--summarize":
        try:
            summarize(sys.argv[2])
        except Exception as error:
            _log(f"summary of {sys.argv[2]} failed: {error!r}")
    elif len(sys.argv) == 3 and sys.argv[1] == "--session-closed":
        try:
            session_closed(sys.argv[2])
        except Exception as error:
            _log(f"resume of session #{sys.argv[2]} failed: {error!r}")
    elif sys.argv[1:] == ["--threads"]:
        rebuild_threads()
        print(threads() or "(nothing in flight)")
    elif sys.argv[1:2] == ["--rotate"]:
        try:
            rotate(clear="--clear" in sys.argv)
        except Exception as error:
            _log(f"rotation failed: {error!r}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
