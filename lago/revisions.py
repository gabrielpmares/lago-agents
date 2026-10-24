"""Every change to config.json, kept so it can be looked at and undone.

config_store.save() calls record() with the config before and after. A save that changes
nothing leaves no revision. restore() saves an old revision's config back, as a revision of
its own (so a restore can be undone too). Only the newest KEEP revisions are kept.
"""

import json
import os
import time

import hub

KEEP = 300


class RevisionError(Exception):
    pass


def changed_paths(before, after, prefix="", depth=2):
    """Dotted paths that differ, down to `depth` levels: "agents.am-qa", "voice"."""
    if depth == 0 or not isinstance(before, dict) or not isinstance(after, dict):
        return [prefix] if before != after else []
    paths = []
    for key in sorted(set(before) | set(after), key=str):
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in before or key not in after:
            paths.append(path)
        else:
            paths += changed_paths(before[key], after[key], path, depth - 1)
    return paths


def _source():
    return os.environ.get("LAGO_AGENT_ID") or "app"


def record(before, after, source=None, restored_from=None):
    """Keep a revision if anything changed. Returns its id, or None."""
    changed = changed_paths(before or {}, after)
    if not changed:
        return None
    with hub.connect() as conn:
        cur = conn.execute(
            "INSERT INTO config_revisions (source, changed, before, after, restored_from, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source or _source(), json.dumps(changed), json.dumps(before or {}), json.dumps(after),
             restored_from, time.time()))
        conn.execute("DELETE FROM config_revisions WHERE id <= ?", (cur.lastrowid - KEEP,))
        revision = cur.lastrowid
    import activity
    activity.log(source or _source(), "config.changed",
                 f"Changed {', '.join(changed[:6])}{' …' if len(changed) > 6 else ''}",
                 target=f"revision {revision}")
    return revision


def recent(limit=50):
    """The newest revisions, without the full configs."""
    with hub.connect() as conn:
        rows = conn.execute("SELECT id, source, changed, restored_from, created_at "
                            "FROM config_revisions ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(row), "changed": json.loads(row["changed"])} for row in rows]


def get(revision_id):
    with hub.connect() as conn:
        row = conn.execute("SELECT * FROM config_revisions WHERE id = ?", (revision_id,)).fetchone()
    if row is None:
        raise RevisionError(f"There's no config revision {revision_id}")
    return {**dict(row), "changed": json.loads(row["changed"]),
            "before": json.loads(row["before"]), "after": json.loads(row["after"])}


def _at(config, path):
    for key in path.split("."):
        if not isinstance(config, dict) or key not in config:
            return None
        config = config[key]
    return config


def diff(revision_id):
    """Each changed path with its value before and after."""
    revision = get(revision_id)
    return [{"path": path, "before": _at(revision["before"], path),
             "after": _at(revision["after"], path)} for path in revision["changed"]]


def restore(revision_id, which="before"):
    """Put config.json back the way it was before (or right after) a revision."""
    import config_store
    if which not in ("before", "after"):
        raise RevisionError("Restore the config from 'before' or 'after' a revision")
    target = get(revision_id)[which]
    if not target:
        raise RevisionError(f"Revision {revision_id} has no config {which} it")
    config_store.save(target, source="restore", restored_from=revision_id)
    return {"ok": True, "restored": revision_id, "which": which}
