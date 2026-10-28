"""Lago's clockwork: due reminders, recurring jobs and routines, grading agents' test runs,
carrying on agents' work once a usage limit resets, watchdogs on agents that went wrong,
Lago's connectors kept in step, the daily database backup, and Lago's daily list of
agents on GitHub.

It runs in one process at a time. The lago-agents-scheduler user service runs it when
you're logged in, app open or not (install.sh sets it up); the app runs it too, but only
takes over while the service isn't holding the lock, so nothing fires twice.

    python3 background.py      # what the service runs
"""

import fcntl
import logging
import threading
import time

import activity
import backups
import connectors
import evals
import ghcatalog
import notify
import routines
import schedules
import specialists
import statuscards
import team
import watchdog
from config_store import STATE_DIR

LOCK_PATH = STATE_DIR / "loops.lock"
RETRY_SECONDS = 60
RESUME_CHECK_SECONDS = 30
HOUSEKEEPING_SECONDS = 3600
CONNECTOR_CHECK_SECONDS = 60
log = logging.getLogger("lago.background")
_started = False


def _take_lock():
    """The open lock file if this process got it, else None. Kept open while we run."""
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def _resume_after_reset():
    while True:
        try:
            team.resume_after_reset()
        except Exception:  # a failed check must not end the loop
            log.exception("resuming agents after a usage reset failed")
        time.sleep(RESUME_CHECK_SECONDS)


def _watchdogs():
    while True:
        try:
            watchdog.check()
        except Exception:  # a failed check must not end the loop
            log.exception("the watchdog check failed")
        time.sleep(watchdog.CHECK_SECONDS)


def _status_cards():
    while True:
        try:
            statuscards.refresh_all()
        except Exception:  # a failed pass must not end the loop
            log.exception("refreshing status cards failed")
        time.sleep(statuscards.CHECK_SECONDS)


def _connectors():
    """Lago's session in step with the Connectors page (connectors.refresh_lago)."""
    while True:
        try:
            connectors.refresh_lago()
        except Exception:  # a failed check must not end the loop
            log.exception("checking Lago's connectors failed")
        time.sleep(CONNECTOR_CHECK_SECONDS)


def _housekeeping():
    """Once an hour: the day's database backup, and dropping old activity."""
    while True:
        for job in (backups.run_if_due, activity.prune):
            try:
                job()
            except Exception:  # one failed job must not stop the others
                log.exception("housekeeping job %s failed", job.__name__)
        time.sleep(HOUSEKEEPING_SECONDS)


def _github_list():
    """Lago's list of agents on GitHub, refreshed once a day (ghcatalog.py)."""
    while True:
        try:
            ghcatalog.refresh_if_due()
        except Exception:  # offline, no gh: try again in an hour
            log.exception("refreshing Lago's GitHub list failed")
        time.sleep(HOUSEKEEPING_SECONDS)


def start_loops():
    """Run the loops here once no other process does (checked every RETRY_SECONDS)."""
    global _started
    if _started:
        return
    _started = True

    def wait_for_lock():
        while True:
            lock = _take_lock()
            if lock:
                log.info("running Lago's schedules and evaluations in this process")
                schedules.start_firing(notify.tell_orchestrator)
                routines.start_firing()
                evals.start_worker()
                specialists.start_worker()
                threading.Thread(target=_resume_after_reset, name="usage-resume", daemon=True).start()
                threading.Thread(target=_housekeeping, name="housekeeping", daemon=True).start()
                threading.Thread(target=_watchdogs, name="watchdogs", daemon=True).start()
                threading.Thread(target=_status_cards, name="status-cards", daemon=True).start()
                threading.Thread(target=_connectors, name="connectors", daemon=True).start()
                threading.Thread(target=_github_list, name="github-list", daemon=True).start()
                globals()["_held"] = lock  # released when the process exits
                return
            time.sleep(RETRY_SECONDS)
    threading.Thread(target=wait_for_lock, name="loops-lock", daemon=True).start()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    start_loops()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
