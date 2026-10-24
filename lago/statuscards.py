"""Status cards: standing "keep me posted on X" notes, rewritten only when X changes.

A card watches a project (every report, plan and history line about it) and/or a topic (the
records matching every word of `query`, as History's search finds them). The background loop
checks each card every CHECK_SECONDS: when what it watches has changed since its last write
(a fingerprint of those records), a cheap model rewrites it from them, in a few lines. Unchanged,
it costs nothing. A project summary is a card that watches one whole project; each project
gets one the first time you open its summary.
"""

import hashlib
import time

import activity
import hub

CHECK_SECONDS = 15 * 60
MATERIAL_CHARS = 9000
LOOKBACK_DAYS = 14
MODEL = "haiku"

PROMPT = """You keep a standing status card for a busy founder. Card: "{title}".
{focus}
From the records below (newest first), write the card in plain text: at most 6 short lines. Lead
with where things stand, then what changed recently, then what's next or blocked and who it
waits on. No preamble, no headings, no markdown tables; don't invent anything that isn't in
the records. If nothing in them bears on the card, say so in one line.

Records:
{material}
"""


class CardError(Exception):
    pass


def get(card_id):
    with hub.connect() as conn:
        row = conn.execute("SELECT * FROM status_cards WHERE id = ?", (int(card_id),)).fetchone()
    if row is None:
        raise CardError(f"There's no status card #{card_id}")
    return dict(row)


def cards(include_archived=False):
    with hub.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM status_cards" + ("" if include_archived else " WHERE archived = 0")
            + " ORDER BY id")]


def add(title, project="", query="", summary=False):
    title = " ".join(str(title or "").split())
    if not title:
        raise CardError("Give the card a title")
    if not project and not str(query or "").strip():
        raise CardError("A card watches a project, a topic (query), or both")
    now = time.time()
    with hub.connect() as conn:
        cur = conn.execute("INSERT INTO status_cards (title, project, query, summary, text, fingerprint, "
                           "created_at, updated_at) VALUES (?, ?, ?, ?, '', '', ?, ?)",
                           (title[:120], project or "", " ".join(str(query or "").split())[:200],
                            1 if summary else 0, now, now))
        card_id = cur.lastrowid
    activity.log(activity.actor(), "card.added", f"#{card_id} {title}", target=f"card #{card_id}",
                 project=project or None)
    return get(card_id)


def project_summary(project):
    """The project's summary card, made on first use."""
    with hub.connect() as conn:
        row = conn.execute("SELECT id FROM status_cards WHERE project = ? AND summary = 1 AND archived = 0",
                           (project,)).fetchone()
    return get(row["id"]) if row else add(f"Summary: {project}", project=project, summary=True)


def archive(card_id):
    card = get(card_id)
    with hub.connect() as conn:
        conn.execute("UPDATE status_cards SET archived = 1, updated_at = ? WHERE id = ?", (time.time(), card["id"]))
    return {"ok": True}


def _records(card, now):
    """What the card watches, newest first: [(kind, id, when, text)]."""
    import activity as history
    since = now - LOOKBACK_DAYS * 86400
    rows = []
    with hub.connect() as conn:
        if card["project"]:
            for e in conn.execute("SELECT id, kind, text, created_at FROM events WHERE project = ? "
                                  "AND created_at >= ? ORDER BY id DESC LIMIT 60", (card["project"], since)):
                rows.append(("report", e["id"], e["created_at"], f"{e['kind']}: {e['text']}"))
            for p in conn.execute("SELECT id, title, status, summary, updated_at FROM pipelines WHERE "
                                  "project = ? AND updated_at >= ? ORDER BY id DESC LIMIT 20",
                                  (card["project"], since)):
                rows.append(("plan", p["id"], p["updated_at"],
                             f"plan #{p['id']} {p['title']} [{p['status']}] {p['summary'] or ''}"))
            for a in conn.execute("SELECT id, actor, kind, text, created_at FROM activity WHERE project = ? "
                                  "AND created_at >= ? AND kind NOT LIKE 'card.%' ORDER BY id DESC LIMIT 40",
                                  (card["project"], since)):
                rows.append(("history", a["id"], a["created_at"], f"{a['kind']} by {a['actor']}: {a['text']}"))
    if card["query"]:
        for r in history.search(card["query"], limit=40):
            if (r["at"] or 0) >= since and not (r["kind"] == "activity" and r["title"].startswith("card.")):
                rows.append((r["kind"], r["id"], r["at"], f"{r['title']}: {r['snippet']}"))
    seen, unique = set(), []
    for row in sorted(rows, key=lambda r: -(r[2] or 0)):
        if (row[0], row[1]) not in seen:
            seen.add((row[0], row[1]))
            unique.append(row)
    return unique


def fingerprint(records):
    return hashlib.sha256("|".join(f"{k}:{i}:{t}" for k, i, t, _ in records).encode()).hexdigest()[:16]


def refresh(card_id, force=False, ask=None, now=None):
    """Rewrite the card if what it watches changed (or `force`). Returns (card, rewritten)."""
    now = now or time.time()
    card = get(card_id)
    records = _records(card, now)
    fp = fingerprint(records)
    if fp == card["fingerprint"] and not force:
        return card, False
    material, used = [], 0
    for kind, rid, at, text in records:
        line = f"- [{time.strftime('%m-%d %H:%M', time.localtime(at or 0))}] {kind} #{rid}: {' '.join(text.split())[:500]}"
        if used + len(line) > MATERIAL_CHARS:
            break
        material.append(line)
        used += len(line)
    if not material:
        text = "Nothing recorded about this in the last two weeks."
    else:
        focus = (f"It follows the project {card['project']}." if card["project"] else "") + \
            (f" It follows the topic: {card['query']}." if card["query"] else "")
        prompt = PROMPT.format(title=card["title"], focus=focus.strip(), material="\n".join(material))
        if ask is None:
            import memory
            memory.MEMORY_DIR.mkdir(parents=True, exist_ok=True)  # claude -p runs there
            ask = lambda p: memory._ask_model(p, json_reply=False, model=MODEL)  # noqa: E731
        try:
            text = ask(prompt).strip()[:2000]
        except Exception as error:  # keep the old text; try again next time
            with hub.connect() as conn:
                conn.execute("UPDATE status_cards SET error = ?, updated_at = ? WHERE id = ?",
                             (str(error)[:300], now, card["id"]))
            return get(card_id), False
    with hub.connect() as conn:
        conn.execute("UPDATE status_cards SET text = ?, fingerprint = ?, generated_at = ?, error = '', "
                     "updated_at = ? WHERE id = ?", (text, fp, now, now, card["id"]))
    return get(card_id), True


def refresh_all(ask=None):
    """The background loop's pass: rewrite every card whose records changed."""
    done = []
    for card in cards():
        try:
            _, rewritten = refresh(card["id"], ask=ask)
        except CardError:
            continue
        if rewritten:
            done.append(card["id"])
    return done
