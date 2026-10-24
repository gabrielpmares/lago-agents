"""Documents a work session produces (plans, specs, ADRs, reports), kept as revisions you can
read, compare and comment on.

A session's documents are the plan files in its folder's `.claude/plans/` and any file its
Conductor shares with `share_document`. sync() keeps a revision each time a document's
content changes (text files up to MAX_BYTES), so you can see how it evolved even if the file
is later rewritten. A comment (optionally on a quoted passage) goes to the session's
Conductor as a message, and stays on the document with the revision it was made on.
"""

import difflib
import hashlib
import time
from pathlib import Path

import activity
import config_store
import hub

MAX_BYTES = 300 * 1024
MAX_REVISIONS = 50


class DocumentError(Exception):
    pass


def _session_folder(workspace):
    import swarm
    return swarm.project_cwd(swarm.session_project(config_store.load(), workspace))


def share(workspace_id, path, title=""):
    """Register a file as one of the session's documents. Returns the document."""
    ws = hub.get_workspace(int(workspace_id))
    if ws is None:
        raise DocumentError(f"There's no work session #{workspace_id}")
    file = Path(path).expanduser()
    if not file.is_absolute():
        file = _session_folder(ws) / file
    file = file.resolve()
    if not file.is_file():
        raise DocumentError(f"There's no file {file}")
    if file.stat().st_size > MAX_BYTES:
        raise DocumentError(f"{file.name} is larger than {MAX_BYTES // 1024} KB")
    with hub.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO documents (workspace, path, title, created_at) VALUES (?, ?, ?, ?)",
                     (ws["id"], str(file), (title or file.stem.replace("-", " "))[:120], time.time()))
        row = conn.execute("SELECT * FROM documents WHERE workspace = ? AND path = ?", (ws["id"], str(file))).fetchone()
    sync_one(dict(row))
    return get(row["id"])


def get(doc_id):
    with hub.connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (int(doc_id),)).fetchone()
    if row is None:
        raise DocumentError(f"There's no document #{doc_id}")
    return dict(row)


def _discover(workspace):
    """Plan files in the session folder's .claude/plans/ changed since the session opened."""
    plans = _session_folder(workspace) / ".claude" / "plans"
    if not plans.is_dir():
        return []
    return [p for p in sorted(plans.glob("*.md")) if p.stat().st_mtime >= workspace["created_at"] - 60]


def sync_one(doc):
    """Keep a new revision if the file changed. Returns the new revision number or None."""
    file = Path(doc["path"])
    try:
        data = file.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_BYTES:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    digest = hashlib.sha256(data).hexdigest()[:16]
    with hub.connect() as conn:
        last = conn.execute("SELECT number, digest FROM document_revisions WHERE document = ? ORDER BY number DESC "
                            "LIMIT 1", (doc["id"],)).fetchone()
        if last and last["digest"] == digest:
            return None
        number = (last["number"] if last else 0) + 1
        conn.execute("INSERT INTO document_revisions (document, number, digest, text, created_at) VALUES (?, ?, ?, ?, ?)",
                     (doc["id"], number, digest, text, time.time()))
        conn.execute("DELETE FROM document_revisions WHERE document = ? AND number <= ?",
                     (doc["id"], number - MAX_REVISIONS))
    return number


def sync(workspace_id):
    """Find the session's plan files and keep revisions of every document. Returns its documents."""
    ws = hub.get_workspace(int(workspace_id))
    if ws is None:
        raise DocumentError(f"There's no work session #{workspace_id}")
    for path in _discover(ws):
        with hub.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO documents (workspace, path, title, created_at) VALUES (?, ?, ?, ?)",
                         (ws["id"], str(path.resolve()), path.stem.replace("-", " ")[:120], time.time()))
    docs = documents(ws["id"])
    for doc in docs:
        sync_one(doc)
    return documents(ws["id"])


def documents(workspace_id):
    with hub.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT d.*, (SELECT MAX(number) FROM document_revisions r WHERE r.document = d.id) AS revisions, "
            "(SELECT MAX(created_at) FROM document_revisions r WHERE r.document = d.id) AS changed_at, "
            "(SELECT COUNT(*) FROM document_comments c WHERE c.document = d.id) AS comments "
            "FROM documents d WHERE d.workspace = ? ORDER BY d.id", (int(workspace_id),))]
    return rows


def revision(doc_id, number=None):
    with hub.connect() as conn:
        row = conn.execute("SELECT * FROM document_revisions WHERE document = ? "
                           + ("AND number = ?" if number else "ORDER BY number DESC LIMIT 1"),
                           (int(doc_id), int(number)) if number else (int(doc_id),)).fetchone()
    if row is None:
        raise DocumentError(f"Document #{doc_id} has no revision {number or ''}".strip())
    return dict(row)


def diff(doc_id, number):
    """The unified diff from the revision before `number` to `number`."""
    new = revision(doc_id, number)
    old = revision(doc_id, number - 1)["text"] if number > 1 else ""
    return "".join(difflib.unified_diff(old.splitlines(True), new["text"].splitlines(True),
                                        f"revision {number - 1}", f"revision {number}"))


def comment(doc_id, text, quote="", by="you"):
    """Comment on the latest revision; the Conductor gets it as a message."""
    import team
    doc = get(doc_id)
    text = (text or "").strip()
    if not text:
        raise DocumentError("Write the comment")
    latest = revision(doc["id"])
    with hub.connect() as conn:
        conn.execute("INSERT INTO document_comments (document, revision, quote, text, by, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (doc["id"], latest["number"], (quote or "").strip()[:1000],
                                                   text[:4000], by, time.time()))
    quoted = f"On \"{quote.strip()[:300]}\": " if quote and quote.strip() else ""
    team.message(config_store.conductor_id(doc["workspace"]),
                 f"The human commented on {doc['title']} ({doc['path']}, revision {latest['number']}):\n"
                 f"{quoted}{text}")
    activity.log(by, "document.commented", f"{doc['title']}: {text[:200]}", target=f"document #{doc['id']}",
                 workspace=doc["workspace"])
    return {"ok": True, "message": "Sent to the Conductor."}


def comments(doc_id):
    with hub.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM document_comments WHERE document = ? ORDER BY id",
                                              (int(doc_id),))]
