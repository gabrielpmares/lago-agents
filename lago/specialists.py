"""Specialists: agents that work and teach like a professional in one role.

You pick a role in the Specialists tab, narrowing a field down to a job (IT & Software >
Backend Engineering > .NET Backend Engineer > .NET cloud engineer on Azure), and Lago builds it:

1. Role map: the specialist-builder reads job postings, certification outlines and
   practitioner sources and maps what the role does and must know, as topics
   (role-map.json). You approve it, dropping or adding topics, before any study starts.
2. Study: one specialist-researcher run per topic writes its knowledge file: playbooks,
   trade-offs, pitfalls and sources (knowledge/<topic>.md), MAX_STUDYING at a time.
3. Assemble: the builder writes the exam (before reading the notes, so it tests the field
   and not the notes), the knowledge index and the specialist's working method.
4. Exam: the exam becomes the agent's test suite (evals.py) and runs on it while it's
   switched off. It goes live only once it passes (passed()). A failed exam sends the weak
   topics back to study once, with the grader's notes, and the exam runs again.
5. Live: you ask it anything in its own conversation, which it keeps between questions (the
   tutor session sp-<id>), with files to read (Markdown, PDFs, text, pictures) kept in its
   folder; a case study gets a written walkthrough you can explain every step from; give it work (a work session it leads), and, with NotebookLM
   connected, its notes become a notebook (notebooklm.py). Every `refresh_days` it checks
   what changed in its field and proposes a refresh for you to approve. Refreshed notes are
   studied into knowledge-next/ and must pass the exam before they replace the old ones.

Nothing is fine-tuned: a specialist is its researched notes, its method and an exam it must
keep passing.

Everything about one specialist is in catalog/specialists/<id>/: specialist.json (its
state), role-map.json, knowledge/, exam.json and prompt.md. The agent itself is
catalog/agents/<id>.md, and its exam is catalog/tests/<id>.json.
"""

import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

import agentdefs
import attachments
import config_store
import evals
import hub
import swarm
import team
from team import TeamError

log = logging.getLogger("lago.specialists")

DIR = agentdefs.SPECIALISTS_DIR
WORK_DIR = config_store.SESSIONS_DIR / "specialists"  # the tutors' scratch folders
SHIPPED = config_store.APP_DIR / "prompts" / "specialists"
BUILDER, RESEARCHER = "specialist-builder", "specialist-researcher"
TUTOR_PREFIX = config_store.TUTOR_PREFIX
MODEL = "claude-opus-5-5"
TOOLS = "Read, Write, Edit, Glob, Grep, Bash, WebSearch, WebFetch"
DEPTHS = {"focused": "between 8 and 12", "thorough": "between 14 and 22"}
MAX_STUDYING = 3
STUDY_ATTEMPTS = 2          # a researcher run that fails is tried once more
EXAM_ATTEMPTS = 2           # a failed exam sends the weak topics back to study once
PASS_MEAN = 0.8
MAX_WEAK_SHARE = 1 / 6      # at most one answer in six below evals.PASS_MARK
DEFAULT_REFRESH_DAYS = 30
CHECK_SECONDS = 30
SUGGEST_MODEL = "haiku"

# Where a specialist is: building (the first five), live, or stopped on a failure.
STATUSES = ("mapping", "map_ready", "studying", "assembling", "examining", "live", "failed")
WORKING = ("mapping", "studying", "assembling", "examining")

_lock = threading.Lock()


@contextlib.contextmanager
def _locked():
    """One change at a time, across the app and the scheduler service."""
    DIR.mkdir(parents=True, exist_ok=True)
    with _lock, open(DIR / ".lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


# ---------------------------------------------------------------- fields and roles

def fields():
    """The field > area > role > focus tree you narrow a role down in."""
    return json.loads((SHIPPED / "fields.json").read_text(encoding="utf-8"))


SUGGEST_PROMPT = """Someone is choosing a professional role to build an expert AI agent \
for, by narrowing a field down step by step. Their choice so far: {path}

Already listed at the next level: {listed}

Suggest up to 8 more options for the next level down: real, distinct specializations that \
employers hire for today, each one as narrow as a job title or a focus within a job \
(e.g. ".NET cloud engineer on Azure (Bicep, AKS, Cosmos DB, Service Bus)"). Skip anything \
already listed. Reply with JSON only: {{"options": ["...", "..."]}}"""


def suggest(path):
    """More options under a partial path, from a cheap model; kept so asking twice is free."""
    path = [str(p).strip() for p in path or [] if str(p).strip()]
    if not path:
        raise TeamError("Pick a field first.")
    key = " > ".join(path)
    cache_file = DIR / "suggestions.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    node = fields()
    for part in path:
        node = node.get(part, {}) if isinstance(node, dict) else {}
    listed = list(node) if isinstance(node, dict) else list(node)
    import memory
    reply = memory._ask_model(SUGGEST_PROMPT.format(path=key, listed=", ".join(listed) or "(nothing)"),
                              model=SUGGEST_MODEL)
    options = [" ".join(str(o).split())[:160] for o in reply.get("options", []) if str(o).strip()]
    options = [o for o in options if o not in listed][:8]
    cache[key] = options
    DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return options


# ---------------------------------------------------------------- storage

def _folder(sid):
    return DIR / sid


def _meta_file(sid):
    return _folder(sid) / "specialist.json"


def get(sid):
    try:
        return json.loads(_meta_file(sid).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TeamError(f"There's no specialist {sid!r}.") from error


def _save(meta):
    meta["updated_at"] = time.time()
    path = _meta_file(meta["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def all_specialists():
    found = []
    for path in sorted(DIR.glob("*/specialist.json")) if DIR.is_dir() else []:
        try:
            found.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(found, key=lambda m: -m.get("created_at", 0))


def _read_json(sid, name):
    try:
        return json.loads((_folder(sid) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def role_map(sid):
    return _read_json(sid, "role-map.json")


def _slug(text, limit=40):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def _new_id(path):
    """An id from the narrowest choice, without its bracketed details."""
    base = _slug(re.sub(r"\(.*?\)", "", path[-1])) or "specialist"
    taken = set(agentdefs.catalog()) | {p.name for p in DIR.iterdir()} if DIR.is_dir() else set(agentdefs.catalog())
    candidate, n = base, 2
    while candidate in taken or candidate in (BUILDER, RESEARCHER):
        candidate, n = f"{base[:37]}-{n}", n + 1
    return candidate


# ---------------------------------------------------------------- the builders

def _ensure_builders():
    """The builder and researcher ship with the app; they join your catalog the first time
    they're needed, so you can rate, test and improve them like any agent."""
    for agent_id in (BUILDER, RESEARCHER):
        target = agentdefs.CATALOG_AGENTS / f"{agent_id}.md"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SHIPPED / f"{agent_id}.md", target)
    off = swarm.disabled(config_store.load()) & {BUILDER, RESEARCHER}
    if off:
        raise TeamError(f"{', '.join(sorted(off))} is switched off on the Agents tab: switch it "
                        "on to build specialists.")


def _role_line(meta):
    return " > ".join(meta["path"])


def _run(meta, agent_id, task, inputs, step):
    """Wake the builder or a researcher in the specialist's folder. Returns the task id. The
    run is named after the specialist and the step, so Agent Runs says what it's building."""
    _ensure_builders()
    task_id, _, _ = swarm.run_agent(agent_id, task, folder=str(_folder(meta["id"])), inputs=inputs,
                                    intro="Lago Agents is building a specialist and needs this "
                                          "from you. Work only in the folder below.",
                                    label=f"{meta['title']} · {step}"[:80])
    return task_id


def _task_state(task_id):
    """'open', 'done' or 'failed' (a missing task counts as failed)."""
    task = hub.get_task(task_id) if task_id else None
    if task is None:
        return "failed", ""
    if task["status"] in hub.OPEN_TASK:
        return "open", ""
    return ("done" if task["status"] == "done" else "failed"), task.get("result") or ""


# ---------------------------------------------------------------- 1. the role map

def create(path, notes="", depth="focused"):
    """Start a specialist for a role: the builder maps it. Returns its id."""
    path = [" ".join(str(p).split()) for p in path or [] if str(p).strip()]
    if len(path) < 2:
        raise TeamError("Narrow it down to at least a field and a role.")
    if depth not in DEPTHS:
        raise TeamError(f"Depth is one of {', '.join(DEPTHS)}.")
    with _locked():
        sid = _new_id(path)
        _folder(sid).mkdir(parents=True)
        (_folder(sid) / "knowledge").mkdir()
        now = time.time()
        meta = {"id": sid, "title": re.sub(r"\s*\(.*?\)", "", path[-1]).strip() or path[-1],
                "path": path, "notes": (notes or "").strip(), "depth": depth,
                "status": "mapping", "message": "Mapping what the role does and must know.",
                "created_at": now, "built_at": None, "refreshed_at": None,
                "task_id": None, "topics": {}, "exam": {}, "refresh": {}, "notebook": {}}
        _save(meta)
        try:
            meta["task_id"] = _start_map(meta)
        except TeamError:
            shutil.rmtree(_folder(sid), ignore_errors=True)
            raise
        _save(meta)
    return sid


def _start_map(meta):
    task = (f"Mode: map.\n\nRole: {_role_line(meta)}\n"
            f"Number of topics: {DEPTHS[meta['depth']]}.\n"
            + (f"\nThe human's notes (what they care about most): {meta['notes']}\n" if meta["notes"] else "")
            + "\nWrite role-map.json in the folder below.")
    return _run(meta, BUILDER, task, {"mode": "map", "role": _role_line(meta)}, "role map")


def _check_map(sid):
    data = role_map(sid)
    if not isinstance(data, dict) or not isinstance(data.get("topics"), list):
        raise TeamError("The builder didn't write a readable role-map.json.")
    topics, seen = [], set()
    for topic in data["topics"]:
        if not isinstance(topic, dict) or not str(topic.get("name", "")).strip():
            continue
        tid = _slug(str(topic.get("id") or topic["name"])) or f"topic-{len(topics) + 1}"
        while tid in seen:
            tid += "-2"
        seen.add(tid)
        depth = topic.get("depth") if topic.get("depth") in ("core", "working", "awareness") else "working"
        topics.append({**topic, "id": tid, "depth": depth,
                       "priority": int(topic.get("priority") or 2) if str(topic.get("priority", "")).isdigit() else 2})
    if len(topics) < 4:
        raise TeamError("The role map has fewer than 4 topics: too thin to study.")
    data["topics"] = topics
    (_folder(sid) / "role-map.json").write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                                encoding="utf-8")
    return data


# ---------------------------------------------------------------- 2. study

def approve_map(sid, keep=None, added=None, notes=""):
    """Your go-ahead on the role map: study the topics in `keep` (ids; all when None) and the
    ones you `added` ({name, depth, why}). Nothing is studied before this."""
    with _locked():
        meta = get(sid)
        if meta["status"] != "map_ready":
            raise TeamError(f"{meta['title']} is {meta['status']}, not waiting for its role map.")
        data = role_map(sid)
        topics = [t for t in data["topics"] if keep is None or t["id"] in set(keep)]
        taken = {t["id"] for t in topics}
        for extra in added or []:
            name = " ".join(str((extra or {}).get("name", "")).split())
            if not name:
                continue
            tid = _slug(name) or f"topic-{len(topics) + 1}"
            while tid in taken:
                tid += "-2"
            taken.add(tid)
            depth = extra.get("depth") if extra.get("depth") in ("core", "working", "awareness") else "working"
            topics.append({"id": tid, "name": name, "area": "Added by you", "depth": depth, "priority": 1,
                           "why": " ".join(str(extra.get("why") or "You asked for it.").split()),
                           "subtopics": [], "evidence": []})
        if not topics:
            raise TeamError("Keep at least one topic.")
        data["topics"] = topics
        (_folder(sid) / "role-map.json").write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                                    encoding="utf-8")
        if notes.strip():
            meta["notes"] = (meta["notes"] + "\n" + notes.strip()).strip()
        meta["title"] = " ".join(str(data.get("title") or meta["title"]).split())[:80]
        meta["topics"] = {t["id"]: {"status": "queued", "task_id": None, "attempts": 0, "feedback": ""}
                          for t in topics}
        meta.update(status="studying", message=f"Studying {len(topics)} topics.")
        _tell(f"{meta['title']} is studying {len(topics)} topics: follow it on the Specialists "
              "tab, and each study shows in Agent Runs.")
        _save(meta)
        _start_studies(meta, "knowledge")
        _save(meta)
    return meta


def _topic(sid, tid, refresh_note=""):
    found = next((t for t in (role_map(sid) or {}).get("topics", []) if t["id"] == tid), None)
    return found or {"id": tid, "name": tid.replace("-", " "), "depth": "working",
                     "why": refresh_note or "", "subtopics": [], "evidence": []}


def _start_studies(meta, target):
    """Start queued topics while fewer than MAX_STUDYING are being studied."""
    topics = meta["topics"]
    busy = sum(t["status"] == "studying" for t in topics.values())
    for tid, state in topics.items():
        if busy >= MAX_STUDYING:
            break
        if state["status"] != "queued":
            continue
        topic = _topic(meta["id"], tid, state.get("feedback", ""))
        output = _folder(meta["id"]) / target / f"{tid}.md"
        task = (f"Role: {_role_line(meta)}\n\nStudy this topic and write {output}.\n\n"
                f"Topic:\n```json\n{json.dumps(topic, indent=2, ensure_ascii=False)}\n```")
        if meta["notes"]:
            task += f"\n\nThe human's notes on what matters to them: {meta['notes']}"
        if output.exists():
            task += (f"\n\nThe file already exists from an earlier study: read it, keep what's "
                     f"right and current, and rewrite it.")
        if state.get("feedback"):
            task += f"\n\n## What to fix\n{state['feedback']}"
        try:
            state["task_id"] = _run(meta, RESEARCHER, task, {"role": _role_line(meta),
                                                             "topic": topic["name"], "output": str(output)},
                                     f"study {topic['name']}")
        except TeamError as error:
            state.update(status="failed", feedback=str(error))
            continue
        state.update(status="studying", attempts=state["attempts"] + 1)
        busy += 1


def _advance_studies(meta, target):
    """Collect finished studies and start the next. True once every topic is settled."""
    for tid, state in meta["topics"].items():
        if state["status"] != "studying":
            continue
        result, text = _task_state(state["task_id"])
        if result == "open":
            continue
        written = (_folder(meta["id"]) / target / f"{tid}.md")
        if result == "done" and written.exists() and written.stat().st_size > 400:
            state["status"] = "done"
        elif state["attempts"] < STUDY_ATTEMPTS:
            state["status"] = "queued"
        else:
            state.update(status="failed", feedback=text[:500])
    _start_studies(meta, target)
    done = sum(t["status"] == "done" for t in meta["topics"].values())
    left = sum(t["status"] in ("queued", "studying") for t in meta["topics"].values())
    meta["message"] = f"Studied {done} of {len(meta['topics'])} topics." if left else meta["message"]
    return left == 0


# ---------------------------------------------------------------- 3. assemble

def _start_assemble(meta):
    failed = [tid for tid, t in meta["topics"].items() if t["status"] == "failed"]
    task = (f"Mode: assemble.\n\nRole: {_role_line(meta)}\n"
            "role-map.json and knowledge/ are in the folder below. Write exam.json first, then "
            "knowledge/index.md, then prompt.md.")
    if failed:
        task += f"\n\nThese topics couldn't be studied and have no notes: {', '.join(failed)}."
    meta["task_id"] = _run(meta, BUILDER, task, {"mode": "assemble", "role": _role_line(meta)},
                           "exam and method")
    meta.update(status="assembling", message="Writing its exam and working method.")
    _tell(f"{meta['title']} has studied its topics and is writing its exam and working method.")


def agent_body(meta):
    """The specialist's prompt: who it is, how it teaches and works, then the builder's method."""
    data = role_map(meta["id"]) or {}
    method = (_folder(meta["id"]) / "prompt.md")
    method = method.read_text(encoding="utf-8").strip() if method.exists() else ""
    lines = [f"You are a senior {meta['title']}. {data.get('summary', '')}".strip(), "",
             "## Your knowledge base",
             "Your studied notes are in the knowledge folder named below (\"Your knowledge "
             "base\" in the Lago section). Start with its index.md and read the files for the "
             "topic at hand before you answer or build: they hold the playbooks, limits and "
             "pitfalls you rely on. When they don't cover something, or it may have changed "
             "since they were written (each says when it was last checked), check the official "
             "documentation with WebFetch and say that you did.",
             "", "## Files you're given",
             "A question can come with files (Markdown, PDFs, text, pictures), named as "
             "\"[File: /path]\" lines. Open every one with Read and read all of it before you "
             "answer: a PDF over 10 pages in page ranges, with its tables and figures. Say where "
             "in the file each fact you use is (heading, section or page), and keep apart what "
             "the file says, what your notes say and what you assume.",
             "", "## Teaching",
             "When someone asks you about your field, teach like a good senior colleague:",
             "- Find their level from how they ask, and ask one short question if you can't tell.",
             "- Give the mental model first, then the detail, then a concrete example from real work.",
             "- Say what professionals actually do and why, including the trade-offs and the "
             "mistakes people make.",
             "- Point to the note or official page each important fact comes from.",
             "- When it helps, end with one question they can answer to check they understood, "
             "and the next thing worth learning.",
             "- Say plainly when you're not sure, and never invent APIs, limits or flags.",
             "", "## Doing the work",
             "When you're given work in your field, do it as a senior would deliver it: "
             "complete, correct for the current versions, secure by default, and explained in a "
             "short note for whoever maintains it. Follow your method below. When you lead work "
             "that others implement, write the plan and the acceptance criteria precisely, and "
             "review their results against your quality bar before you accept them."]
    if method:
        lines += ["", f"## How a senior {meta['title']} works", method]
    return "\n".join(lines)


def _write_agent(meta):
    data = role_map(meta["id"]) or {}
    areas = sorted({t.get("area", "") for t in data.get("topics", []) if t.get("area")})
    summary = (f"Senior {meta['title']} who teaches anything in the field and does or leads "
               "the work, from researched notes it was examined on.")
    agentdefs.write_agent(
        meta["id"], meta["title"], summary, prompt=agent_body(meta), domain="specialist",
        inputs=[{"name": "request", "description": "A question about the field, or the work to do "
                                            "(the task itself is enough)."}],
        outputs=[{"name": "answer", "required": True,
                  "description": "The explanation, or the work and where its files are."}],
        use_when=f"Questions about {meta['title']} work, or work in that field: "
                 + ", ".join(t["name"] for t in data.get("topics", [])[:8]) + ".",
        avoid_when="Work outside its field: a generalist agent is cheaper there.",
        tags=["specialist", *[_slug(a, 30) for a in areas[:6]]], tools=TOOLS, model=MODEL,
        knowledge=f"{meta['id']}/knowledge", replace=True)


def _finish_assemble(meta):
    folder = _folder(meta["id"])
    exam = _read_json(meta["id"], "exam.json") or {}
    items = [t for t in exam.get("tests", []) if isinstance(t, dict) and t.get("task") and t.get("criteria")]
    if len(items) < 6:
        raise TeamError("The builder wrote fewer than 6 exam questions.")
    if not (folder / "prompt.md").exists() or not (folder / "knowledge" / "index.md").exists():
        raise TeamError("The builder didn't write prompt.md and knowledge/index.md.")
    _switch(meta["id"], False)  # nobody uses it before it passes
    _write_agent(meta)
    saved = evals.save_tests(meta["id"], [{**t, "kind": "regression", "origin": "exam"} for t in items])
    meta["exam"] = {"attempt": 0, "topics": {s["id"]: t.get("topic", "") for s, t in zip(saved, items)},
                    "run_ids": [], "results": {}}


# ---------------------------------------------------------------- 4. the exam

def _start_exam(meta, variant=None):
    meta["exam"]["attempt"] = meta["exam"].get("attempt", 0) + 1
    meta["exam"]["run_ids"] = evals.run_tests(meta["id"], variant=variant)
    meta["exam"]["variant"] = str(variant) if variant else None
    meta["message"] = f"Taking its exam ({len(meta['exam']['run_ids'])} questions)."


def _exam_results(meta):
    """{test id: {score, notes}} once every run of the current attempt is graded, else None.
    A question that couldn't run (a session limit, a crash) isn't the specialist's fault: it's
    left out rather than scored 0, unless so many failed that the exam says nothing."""
    ids = meta["exam"].get("run_ids") or []
    if not ids:
        return None
    runs = hub.test_runs(f"id IN ({','.join('?' * len(ids))})", tuple(ids))
    if len(runs) < len(ids) or any(r["status"] not in ("graded", "error") for r in runs):
        return None
    graded = [r for r in runs if r["status"] == "graded"]
    if len(graded) < len(runs) * 2 / 3:
        why = next((r.get("notes") for r in runs if r["status"] == "error" and r.get("notes")), "")
        raise TeamError(f"{len(runs) - len(graded)} of {len(runs)} exam questions couldn't run"
                        + (f" ({why[:200]})" if why else "") + ". Retry once that's sorted.")
    return {r["test_id"]: {"score": float(r["score"] or 0), "notes": r.get("notes") or ""} for r in graded}


def _exam_live(meta):
    """{test id: {state, score?, notes?}} for the exam being sat now, as each question
    is answered and graded, so the page shows it moving."""
    refresh = meta.get("refresh") or {}
    sitting = refresh.get("exam") if refresh.get("status") == "examining" else meta.get("exam")
    ids = (sitting or {}).get("run_ids") or []
    if not ids:
        return {}
    live = {}
    for run in hub.test_runs(f"id IN ({','.join('?' * len(ids))})", tuple(ids)):
        live[run["test_id"]] = {"state": run["status"]}
        if run["status"] == "graded":
            live[run["test_id"]].update(score=float(run["score"] or 0), notes=run.get("notes") or "")
        elif run["status"] == "error":
            live[run["test_id"]]["notes"] = run.get("notes") or ""
    return live


def passed(results):
    """(passed, mean, weak test ids): a mean of PASS_MEAN and few answers below the mark."""
    if not results:
        return False, 0.0, []
    mean = sum(r["score"] for r in results.values()) / len(results)
    weak = sorted(tid for tid, r in results.items() if r["score"] < evals.PASS_MARK)
    return mean >= PASS_MEAN and len(weak) <= int(len(results) * MAX_WEAK_SHARE), mean, weak


def _restudy_weak(meta, weak, results, target):
    """Send the topics behind the weak answers back to study, with what the grader saw."""
    tests = {t["id"]: t for t in evals.tests(meta["id"])}
    by_topic = {}
    for tid in weak:
        topic = meta["exam"]["topics"].get(tid) or ""
        if topic not in meta["topics"]:
            continue
        test = tests.get(tid, {})
        by_topic.setdefault(topic, []).append(
            f"- Question: {test.get('task', tid)[:600]}\n  It had to: {test.get('criteria', '')[:600]}\n"
            f"  The grader: {results[tid]['notes']}")
    for topic, lines in by_topic.items():
        meta["topics"][topic].update(status="queued", attempts=0,
                                     feedback="The specialist got these exam questions wrong "
                                              "from your notes:\n" + "\n".join(lines))
    return bool(by_topic)


# ---------------------------------------------------------------- moving along

def _switch(sid, on):
    config = config_store.without_swarm(config_store.load())
    off = set(config["swarm"].get("disabled", []))
    off.discard(sid) if on else off.add(sid)
    config["swarm"]["disabled"] = sorted(off)
    config_store.save(config, source="specialists")


def _tell(text):
    hub.add_event(None, None, "news", text)


def _advance(meta):
    status = meta["status"]
    if status == "mapping":
        result, text = _task_state(meta["task_id"])
        if result == "open":
            return
        if result == "failed":
            raise TeamError(f"Mapping the role failed: {text[:300]}")
        data = _check_map(meta["id"])
        meta.update(status="map_ready", task_id=None, title=" ".join(str(data.get("title") or meta["title"]).split())[:80],
                    message=f"Role map ready: {len(data['topics'])} topics. Check it and approve.")
        _tell(f"The role map for {meta['title']} is ready: {len(data['topics'])} topics. "
              "Check it on the Specialists tab and approve it to start studying.")
    elif status == "studying":
        if _advance_studies(meta, "knowledge"):
            if meta["exam"].get("attempt"):  # back from studying weak topics: sit it again
                meta["status"] = "examining"
                _start_exam(meta)
            else:
                _start_assemble(meta)
    elif status == "assembling":
        result, text = _task_state(meta["task_id"])
        if result == "open":
            return
        if result == "failed":
            raise TeamError(f"Assembling failed: {text[:300]}")
        _finish_assemble(meta)
        meta.update(status="examining", task_id=None)
        _tell(f"{meta['title']} is sitting its exam.")
        _start_exam(meta)
    elif status == "examining":
        results = _exam_results(meta)
        if results is None:
            return
        ok, mean, weak = passed(results)
        meta["exam"]["results"] = results
        meta["exam"]["mean"] = mean
        if ok:
            _switch(meta["id"], True)
            meta.update(status="live", built_at=meta.get("built_at") or time.time(),
                        refreshed_at=time.time(),
                        message=f"Passed its exam with {round(mean * 100)}%.")
            _tell(f"{meta['title']} passed its exam ({round(mean * 100)}%) and is ready: ask it "
                  "anything or give it work on the Specialists tab.")
            _sync_notebook(meta)
        elif meta["exam"]["attempt"] < EXAM_ATTEMPTS and _restudy_weak(meta, weak, results, "knowledge"):
            meta.update(status="studying", message=f"Scored {round(mean * 100)}%: studying "
                        f"{sum(t['status'] == 'queued' for t in meta['topics'].values())} weak topics again.")
            _start_studies(meta, "knowledge")
        else:
            meta.update(status="failed", message=f"Failed its exam with {round(mean * 100)}% "
                        f"({len(weak)} weak answers). Try again to study the weak topics once more.")
            _tell(f"{meta['title']} didn't pass its exam ({round(mean * 100)}%). It stays off; "
                  "you can try again from the Specialists tab.")
    elif status == "live":
        _advance_refresh(meta)


def tick():
    for meta in all_specialists():
        if meta["status"] not in (*WORKING, "live"):
            continue
        with _locked():
            meta = get(meta["id"])
            before = json.dumps(meta, sort_keys=True)
            try:
                _advance(meta)
            except TeamError as error:
                meta.update(status="failed", message=str(error)[:600], task_id=None)
                _tell(f"Building {meta['title']} stopped: {str(error)[:300]}")
            if json.dumps(meta, sort_keys=True) != before:
                _save(meta)


def start_worker():
    def loop():
        while True:
            try:
                tick()
            except Exception:  # one bad specialist must not stop the others
                log.exception("specialists check failed")
            time.sleep(CHECK_SECONDS)
    threading.Thread(target=loop, name="specialists", daemon=True).start()


def retry(sid):
    """Pick up a failed specialist where it stopped."""
    with _locked():
        meta = get(sid)
        if meta["status"] != "failed":
            raise TeamError(f"{meta['title']} hasn't failed.")
        results = meta["exam"].get("results")
        if results:  # failed its exam: the weak topics once more, then one more sitting
            _, _, weak = passed(results)
            if not _restudy_weak(meta, weak, results, "knowledge"):
                meta["topics"] = {tid: {**t, "status": "queued", "attempts": 0} for tid, t in meta["topics"].items()}
            meta["exam"]["attempt"] = EXAM_ATTEMPTS - 1
            meta.update(status="studying", message="Studying its weak topics again.")
            _start_studies(meta, "knowledge")
        elif not meta["topics"]:
            meta.update(status="mapping", message="Mapping the role again.")
            meta["task_id"] = _start_map(meta)
        elif any(t["status"] != "done" for t in meta["topics"].values()) \
                and not (_folder(sid) / "exam.json").exists():
            for state in meta["topics"].values():
                if state["status"] == "failed":
                    state.update(status="queued", attempts=0)
            meta.update(status="studying", message="Studying the topics that failed.")
            _start_studies(meta, "knowledge")
        elif meta["exam"].get("run_ids"):  # its exam couldn't run: sit the same attempt again
            meta["exam"]["attempt"] = max(0, meta["exam"].get("attempt", 1) - 1)
            meta["status"] = "examining"
            _start_exam(meta)
        else:
            _start_assemble(meta)
        _save(meta)
    return meta


def remove(sid):
    """Delete a specialist: its agent, exam, notes and tutor session."""
    with _locked():
        meta = get(sid)
        if meta["status"] in WORKING:
            raise TeamError(f"{meta['title']} is still being built. Wait for it to stop or fail.")
        tutor = tutor_id(sid)
        if tutor in config_store.load()["agents"]:
            try:
                team.stop(tutor)
            except TeamError:
                pass
            with hub.connect() as conn:
                conn.execute("DELETE FROM swarm_agents WHERE agent_id = ?", (tutor,))
        (agentdefs.CATALOG_AGENTS / f"{sid}.md").unlink(missing_ok=True)
        (evals.TESTS_DIR / f"{sid}.json").unlink(missing_ok=True)
        config = config_store.without_swarm(config_store.load())
        config["swarm"]["disabled"] = sorted(set(config["swarm"].get("disabled", [])) - {sid})
        config_store.save(config, source="specialists")
        shutil.rmtree(_folder(sid), ignore_errors=True)
        shutil.rmtree(WORK_DIR / sid, ignore_errors=True)
    return meta["title"]


# ---------------------------------------------------------------- 5. live: ask and work

def tutor_id(sid):
    return f"{TUTOR_PREFIX}{sid}"[:48]


def _live(sid):
    meta = get(sid)
    if meta["status"] != "live":
        raise TeamError(f"{meta['title']} isn't ready yet: it's {meta['status']}.")
    return meta


# What you asked, then this, then how to read the files and work the case.
BRIEF_MARK = "\n\n---\n[For the specialist]\n"
FILE_LINE = re.compile(r"^\[File: (/[^\]\n]+)\]$", re.M)
CASE_METHOD = """This is a case study. The human has to solve it and also understand it well \
enough to explain every step themselves (to an examiner, an interviewer or a client), so \
teach it, don't just answer it.

Write the walkthrough to {doc} (Markdown, in their language), the document they keep and \
study from. If it exists, this is the same case: update it.
1. **The case in brief.** The situation, exactly what is asked, the constraints, the facts \
that decide it (each with where it is in the file), the facts that don't matter and why, \
what's missing, and every assumption you make, marked as one.
2. **What it tests.** Each concept or framework the case turns on: the mental model first, \
then how it applies here, with the note or official page it comes from.
3. **The solution, step by step.** For every step: what you do; why (the fact from the case \
and the principle behind it); the alternatives a professional would weigh and why they lose \
here; the mistake people make at this step; and **Say it like this**: two or three \
sentences they could say to explain the step in their own words.
4. **The answer**, complete: the deliverable the case asks for, as a professional would \
hand it in.
5. **Questions they'll get.** The follow-ups an examiner or reviewer would ask, each with a \
model answer, and "what if" variations: which change in the case changes the answer, and how.
6. **Check yourself.** Five to eight questions, the answers at the end of the document.

Then reply with: the case and its answer in a few lines, the steps as a short list, the \
document's path, and an offer to drill it: they explain a step back in their own words and \
you say what's right, what's missing and what's wrong, then ask the next question. When \
they do explain one back later, grade it honestly like that; don't just praise it."""


def files_folder(sid):
    """The files you gave it: kept until you remove the specialist (unlike what you send
    Lago, which goes after a few days)."""
    return WORK_DIR / sid / "files"


def cases_folder(sid):
    """The case walkthroughs it writes for you."""
    return WORK_DIR / sid / "cases"


def add_files(sid, files=(), paths=()):
    """Save files into its files/ folder: data URLs from the app ({"data", "name"}), or
    files on this computer by path (what you sent Lago, a case you have in your home
    folder). Nothing is saved unless every one can be read. Returns their paths."""
    decoded = []
    try:
        decoded += [attachments.decode(item) for item in files or ()]
        for path in paths or ():
            source = _local_file(path)
            decoded.append(attachments.checked(source.read_bytes(), source.name))
    except attachments.AttachmentError as error:
        raise TeamError(str(error)) from error
    folder = files_folder(sid)
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for _kind, name, data in decoded:
        path = folder / name
        stem, suffix, n = path.stem, path.suffix, 1
        while path.exists() and path.read_bytes() != data:  # the same file sent again is kept once
            n += 1
            path = folder / f"{stem}-{n}{suffix}"
        if not path.exists():
            partial = path.with_name(f".{path.name}.partial")
            partial.write_bytes(data)
            os.replace(partial, path)
        saved.append(str(path))
    return saved


def _local_file(path):
    source = Path(os.path.expanduser(str(path or "").strip().strip("`'\""))).resolve()
    allowed = (Path.home().resolve(), attachments.FOLDER.resolve())
    if not source.is_file() or not any(base in source.parents for base in allowed):
        raise TeamError(f"Couldn't find the file {path} in your home folder.")
    if source.stat().st_size > max(attachments.LIMITS.values()):
        raise TeamError(f"{source.name} is too big to read.")
    return source


def ask(sid, question, from_agent=None, files=(), paths=(), case=False):
    """A question (or a small piece of work) for the specialist's own conversation, which it
    keeps from one question to the next, with any files it should read (`files` from the
    app, `paths` on this computer). With `case`, it works the question as a case study and
    writes the walkthrough to cases/. Returns the task id; the answer is its result."""
    meta = _live(sid)
    question = (question or "").strip()
    saved = add_files(sid, files, paths)
    if not question and case and saved:
        question = "Walk me through this case: solve it and teach me every step."
    if not question:
        raise TeamError("Ask it something.")
    agent_id = tutor_id(sid)
    folder = WORK_DIR / sid
    folder.mkdir(parents=True, exist_ok=True)
    swarm._trust(folder)
    hub.add_swarm_agent(agent_id, None, "", sid, f"{meta['title']} · your specialist", folder=str(folder))
    title = question.splitlines()[0][:100]
    body = question
    if saved or case:
        brief = [f"[File: {path}]" for path in saved]
        if saved:
            brief.append("Open each with Read and read all of it before you answer (a PDF over "
                         "10 pages in page ranges, with its tables and figures), and say where in "
                         "it each fact you use is. They're in your files/ folder, with any you "
                         "were given before, and they stay there.")
        if case:
            name = Path(saved[0]).stem if saved else question
            doc = cases_folder(sid) / f"{_slug(name) or 'case'}.md"
            brief.append(CASE_METHOD.format(doc=doc))
            title = f"Case study: {Path(saved[0]).name if saved else title}"[:100]
        body += BRIEF_MARK + "\n".join(brief)
    return team.assign(agent_id, title, body, from_agent=from_agent)


def _question(body):
    """(what you asked, [file paths], whether it was a case study) from a task body."""
    asked, _, brief = (body or "").partition(BRIEF_MARK)
    return asked, FILE_LINE.findall(brief), "This is a case study." in brief


def conversation(sid, limit=40):
    """Your questions to it and its answers, oldest first."""
    rows = hub.tasks("to_agent = ?", (tutor_id(sid),), limit=limit)
    out = []
    for r in sorted(rows, key=lambda r: r["id"]):
        asked, paths, case = _question(r["body"])
        out.append({"id": r["id"], "question": asked, "answer": r["result"] or "", "status": r["status"],
                    "at": r["created_at"], "from": r["from_agent"], "case": case,
                    "files": [{"name": Path(p).name, "path": p} for p in paths]})
    return out


TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".csv", ".json", ".yaml", ".yml", ".log")


def work_files(sid):
    """Its case walkthroughs, then the files you gave it, newest first, for its Ask tab."""
    out = []
    for kind, folder in (("case", cases_folder(sid)), ("file", files_folder(sid))):
        if folder.is_dir():
            out += [{"name": p.name, "path": str(p), "kind": kind, "text": p.suffix.lower() in TEXT_SUFFIXES}
                    for p in sorted(folder.iterdir(), key=lambda p: -p.stat().st_mtime)
                    if p.is_file() and not p.name.startswith(".")]
    return out


def read_work_file(sid, path):
    """A text file from its cases/ or files/ folder, to show in the app."""
    found = Path(str(path or "")).resolve()
    folders = (cases_folder(sid).resolve(), files_folder(sid).resolve())
    if found.parent not in folders or found.suffix.lower() not in TEXT_SUFFIXES or not found.is_file():
        raise TeamError("That file isn't one of its case files.")
    return found.read_text(encoding="utf-8", errors="replace")


def lead_request(sid, request):
    """The opening of a work session the specialist leads: it plans and sets the bar, the
    others implement, and it reviews what they produce."""
    meta = _live(sid)
    request = (request or "").strip()
    if not request:
        raise TeamError("Say what the work is.")
    return (f"{request}\n\n---\nLed by the specialist `{sid}` ({meta['title']}): the human picked it "
            "for this work, so it leads. Make its step first: it writes the plan, the design "
            "and the acceptance criteria. Give implementation to it where the work is its own "
            "field (infrastructure, configuration, code in its stack), otherwise to "
            f"implementer or the agent that fits, and put `{sid}` among the reviewers of every "
            "step that produces code or infrastructure. Its plan is the reference when steps "
            "disagree.")


# ---------------------------------------------------------------- refresh

def _refresh_days():
    try:
        return float((config_store.load().get("swarm") or {}).get("specialist_refresh_days",
                                                                   DEFAULT_REFRESH_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_DAYS


def check_for_updates(sid):
    """Ask the builder what changed in the field since the notes were written (now, instead
    of on the schedule)."""
    with _locked():
        meta = _live(sid)
        if meta["refresh"].get("status") in ("checking", "studying", "examining"):
            raise TeamError(f"{meta['title']} is already being refreshed.")
        _start_refresh_check(meta)
        _save(meta)
    return meta


def _start_refresh_check(meta):
    since = time.strftime("%Y-%m-%d", time.localtime(meta.get("refreshed_at") or meta["created_at"]))
    task = (f"Mode: refresh.\n\nRole: {_role_line(meta)}\nThe notes were last refreshed on {since}. "
            "role-map.json and knowledge/ are in the folder below. Write refresh.json.")
    (_folder(meta["id"]) / "refresh.json").unlink(missing_ok=True)
    meta["refresh"] = {"status": "checking", "task_id": _run(meta, BUILDER, task,
                                                             {"mode": "refresh", "role": _role_line(meta)},
                                                             "refresh check"),
                       "started_at": time.time()}
    meta["message"] = "Checking what changed in its field."


def decide_refresh(sid, approve):
    """Your answer to a proposed refresh. Yes: the changed topics are studied into
    knowledge-next/, and they replace the notes only if the exam passes on them."""
    with _locked():
        meta = _live(sid)
        refresh = meta["refresh"]
        if refresh.get("status") != "ready":
            raise TeamError(f"{meta['title']} has no refresh waiting.")
        if not approve:
            meta["refresh"] = {}
            meta.update(refreshed_at=time.time(), message="Refresh declined: its notes stay as they are.")
            _save(meta)
            return meta
        nxt = _folder(sid) / "knowledge-next"
        shutil.rmtree(nxt, ignore_errors=True)
        shutil.copytree(_folder(sid) / "knowledge", nxt)
        topics = {}
        for change in refresh["changes"]:
            tid = str(change.get("topic") or "").strip()
            if tid.startswith("new:"):
                name = tid[4:].strip()
                tid = _slug(name)
                if tid:
                    _add_topic(sid, tid, name, change)
            if not tid:
                continue
            note = (f"The field changed since these notes: {change.get('what', '')} "
                    f"Why it matters: {change.get('why', '')} Sources: {', '.join(change.get('sources') or [])}")
            if tid in topics:
                topics[tid]["feedback"] += "\n" + note
            else:
                topics[tid] = {"status": "queued", "task_id": None, "attempts": 0, "feedback": note}
        if not topics:
            raise TeamError("The refresh names no topics to study.")
        meta["refresh"].update(status="studying", topics=topics)
        meta["message"] = f"Refreshing {len(topics)} topics."
        _start_studies({**meta, "topics": topics}, "knowledge-next")  # the same studies as a first build
        _save(meta)
    return meta


def _add_topic(sid, tid, name, change):
    data = role_map(sid)
    if any(t["id"] == tid for t in data["topics"]):
        return
    data["topics"].append({"id": tid, "name": " ".join(str(name).split()) or tid, "area": "Added on refresh",
                           "depth": "working", "priority": 2, "why": change.get("why", ""),
                           "subtopics": [], "evidence": change.get("sources") or []})
    (_folder(sid) / "role-map.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _advance_refresh(meta):
    refresh = meta["refresh"]
    status = refresh.get("status")
    if not status:
        days = _refresh_days()
        last = meta.get("refreshed_at") or meta.get("built_at") or meta["created_at"]
        if days > 0 and time.time() - last > days * 86400:
            _start_refresh_check(meta)
        return
    if status == "checking":
        result, text = _task_state(refresh["task_id"])
        if result == "open":
            return
        data = _read_json(meta["id"], "refresh.json") if result == "done" else None
        changes = [c for c in (data or {}).get("changes", []) if isinstance(c, dict) and c.get("topic")]
        if data is None:
            meta["refresh"] = {}
            meta.update(refreshed_at=time.time(), message=f"Couldn't check for changes: {text[:200]}")
        elif not changes:
            meta["refresh"] = {}
            meta.update(refreshed_at=time.time(), message="Checked its field: nothing material changed.")
        else:
            refresh.update(status="ready", summary=str(data.get("summary", ""))[:600], changes=changes)
            meta["message"] = f"{len(changes)} changes in its field: refresh waiting for your OK."
            _tell(f"{meta['title']}: {len(changes)} things changed in its field. "
                  f"{refresh['summary']} Approve the refresh on the Specialists tab.")
    elif status == "studying":
        shadow = {**meta, "topics": refresh["topics"]}
        if _advance_studies(shadow, "knowledge-next"):
            _index_new_topics(meta)
            variant = _folder(meta["id"]) / "agent-next.md"
            text = (agentdefs.CATALOG_AGENTS / f"{meta['id']}.md").read_text(encoding="utf-8")
            variant.write_text(text.replace(f"knowledge: {meta['id']}/knowledge\n",
                                            f"knowledge: {meta['id']}/knowledge-next\n", 1), encoding="utf-8")
            refresh["status"] = "examining"
            refresh["exam"] = {"run_ids": evals.run_tests(meta["id"], variant=variant)}
            meta["message"] = "Taking its exam on the refreshed notes."
        else:
            meta["message"] = shadow["message"]
    elif status == "examining":
        results = _exam_results({"exam": refresh["exam"]})
        if results is None:
            return
        ok, mean, weak = passed(results)
        folder = _folder(meta["id"])
        if ok:
            shutil.rmtree(folder / "knowledge-prev", ignore_errors=True)
            (folder / "knowledge").rename(folder / "knowledge-prev")
            (folder / "knowledge-next").rename(folder / "knowledge")
            meta["exam"].update(results=results, mean=mean)
            meta.update(refreshed_at=time.time(), message=f"Refreshed; passed its exam with {round(mean * 100)}%.")
            _tell(f"{meta['title']} is refreshed and passed its exam again ({round(mean * 100)}%).")
            _sync_notebook(meta)
        else:
            shutil.rmtree(folder / "knowledge-next", ignore_errors=True)
            meta.update(refreshed_at=time.time(), message=f"The refreshed notes scored {round(mean * 100)}% "
                        "on the exam, so it keeps its old ones.")
            _tell(f"{meta['title']}'s refresh didn't pass the exam ({round(mean * 100)}%): it keeps its old notes.")
        (folder / "agent-next.md").unlink(missing_ok=True)
        meta["refresh"] = {}


def _index_new_topics(meta):
    """Topics added on a refresh get a line in the index the specialist starts from."""
    nxt = _folder(meta["id"]) / "knowledge-next"
    index = nxt / "index.md"
    text = index.read_text(encoding="utf-8") if index.exists() else ""
    names = {t["id"]: t["name"] for t in (role_map(meta["id"]) or {}).get("topics", [])}
    new = [tid for tid, t in meta["refresh"]["topics"].items()
           if t["status"] == "done" and f"({tid}.md)" not in text]
    if new:
        text += "\n\n## Added on refresh\n" + "".join(f"- [{names.get(t, t)}]({t}.md)\n" for t in new)
        index.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- NotebookLM

def _sync_notebook(meta):
    """Send its notes to NotebookLM when it's connected, in the background (uploads take
    minutes and the other specialists must keep moving). Never stops a build."""
    import notebooklm
    if notebooklm.connected():
        threading.Thread(target=_sync_quietly, args=(meta["id"],), name="notebook-sync", daemon=True).start()


def _sync_quietly(sid):
    try:
        sync_notebook(sid)
    except Exception as error:  # noqa: BLE001 - a notebook is a bonus; record why and carry on
        log.warning("NotebookLM sync of %s failed: %s", sid, error)
        with _locked():
            meta = get(sid)
            meta["notebook"] = {**(meta.get("notebook") or {}), "error": str(error)[:300]}
            _save(meta)


def sync_notebook(sid):
    """Create or update the specialist's notebook from its notes. Returns its notebook info."""
    import notebooklm
    meta = _live(sid)
    if not notebooklm.connected():
        raise TeamError("NotebookLM isn't connected yet.")
    try:
        notebook = notebooklm.sync(meta, knowledge_files(sid), role_map(sid) or {})
    except notebooklm.NotebookError as error:
        raise TeamError(str(error)) from error
    with _locked():
        meta = get(sid)
        meta["notebook"] = notebook
        _save(meta)
    return notebook


# ---------------------------------------------------------------- for the app

def knowledge_files(sid):
    folder = _folder(sid) / "knowledge"
    return sorted(folder.glob("*.md")) if folder.is_dir() else []


def read_note(sid, name):
    folder = (_folder(sid) / "knowledge").resolve()
    path = (folder / name).resolve()
    if path.parent != folder or path.suffix != ".md" or not path.is_file():
        raise TeamError(f"There's no note {name!r}.")
    return path.read_text(encoding="utf-8")


def card(meta):
    """What the Specialists tab shows of one."""
    topics = meta.get("topics") or {}
    return {**{k: meta.get(k) for k in ("id", "title", "path", "notes", "depth", "status", "message",
                                         "created_at", "built_at", "refreshed_at", "notebook")},
            "studied": sum(t["status"] == "done" for t in topics.values()),
            "topic_count": len(topics),
            "score": round(meta["exam"]["mean"] * 100) if meta.get("exam", {}).get("mean") is not None else None,
            "refresh": {k: v for k, v in (meta.get("refresh") or {}).items() if k in ("status", "summary", "changes")}}


def details(sid):
    meta = get(sid)
    tests = {t["id"]: t for t in evals.tests(sid)} if (agentdefs.CATALOG_AGENTS / f"{sid}.md").exists() else {}
    results = (meta.get("exam") or {}).get("results") or {}
    live = _exam_live(meta)
    exam = [{"id": tid, "name": t["name"], "topic": meta["exam"].get("topics", {}).get(tid, ""),
             **live.get(tid, {}), **results.get(tid, {})} for tid, t in tests.items()]
    shown = card(meta)
    if live and (meta["status"] == "examining" or (meta.get("refresh") or {}).get("status") == "examining"):
        graded = sum(1 for r in live.values() if r["state"] in ("graded", "error"))
        going = sum(1 for r in live.values() if r["state"] in ("running", "grading"))
        shown["message"] = (f"Taking its exam: {graded} of {len(live)} questions graded, "
                            f"{going} being answered or graded now.")
    return {**shown, "role_map": role_map(sid), "topics": meta.get("topics") or {},
            "notes_files": [p.name for p in knowledge_files(sid)], "exam": exam,
            "conversation": conversation(sid) if meta["status"] == "live" else [],
            "work_files": work_files(sid) if meta["status"] == "live" else []}
