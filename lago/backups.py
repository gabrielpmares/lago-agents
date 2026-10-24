"""Daily copies of hub.db, so a broken or wiped database can be put back.

The background loop calls run_if_due() every hour; it makes a consistent copy with SQLite's
online backup (safe while agents write) once a day and keeps the newest KEEP copies in
backups/. restore() puts one back, after saving the current file beside it.

    python3 backups.py            # make a copy now
    python3 backups.py --list
"""

import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime

import hub
from config_store import STATE_DIR

BACKUP_DIR = STATE_DIR / "backups"
KEEP = 7
EVERY_SECONDS = 24 * 3600


class BackupError(Exception):
    pass


def _db_path():
    return hub.DB_PATH


def backups():
    """Newest first: name, path, size and when."""
    if not BACKUP_DIR.exists():
        return []
    files = sorted(BACKUP_DIR.glob("hub-*.db"), reverse=True)
    return [{"name": f.name, "path": str(f), "size": f.stat().st_size,
             "at": f.stat().st_mtime} for f in files]


def backup(now=None):
    """Copy hub.db now. Returns the new copy's path."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now or time.time()).strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"hub-{stamp}.db"
    tmp = target.with_suffix(".part")
    source = sqlite3.connect(_db_path(), timeout=10)
    try:
        dest = sqlite3.connect(tmp)
        try:
            source.backup(dest)
            ok = dest.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dest.close()
    finally:
        source.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise BackupError(f"The copy failed its integrity check: {ok}")
    os.replace(tmp, target)
    for old in backups()[KEEP:]:
        os.unlink(old["path"])
    return target


def run_if_due(now=None):
    """Make the day's copy if the newest one is older than a day. Returns its path or None."""
    now = now or time.time()
    newest = backups()
    if newest and now - newest[0]["at"] < EVERY_SECONDS:
        return None
    path = backup(now)
    import activity
    activity.log("system", "backup.made", f"Backed up the hub database to {path.name}")
    return path


def restore(name):
    """Put a copy back. The current hub.db is kept as hub-before-restore-<time>.db."""
    source = BACKUP_DIR / os.path.basename(name)
    if not source.exists():
        raise BackupError(f"There's no backup called {name}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    db = _db_path()
    if db.exists():
        shutil.copy2(db, BACKUP_DIR / f"hub-before-restore-{stamp}.keep")
    src = sqlite3.connect(source)
    try:
        dest = sqlite3.connect(db, timeout=10)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    return {"ok": True, "restored": source.name}


if __name__ == "__main__":
    if "--list" in sys.argv:
        for b in backups():
            print(f"{b['name']}  {b['size'] // 1024} KB")
    else:
        print(backup())
