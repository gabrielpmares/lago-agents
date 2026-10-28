"""NotebookLM for specialists: each one's notes become a notebook you can study from
(audio overviews, quizzes, flashcards), and the specialist can ask it for answers grounded
in its sources.

Google has no public NotebookLM API for personal accounts, so this drives the unofficial
notebooklm-py library (https://github.com/teng-lin/notebooklm-py), pinned below, with your
Google sign-in. It can break whenever Google changes NotebookLM; nothing else in Lago
depends on it working. It's installed in its own virtualenv under ~/lago-work/tools, and
connect() opens a terminal that installs it and runs its browser sign-in. Your login stays in
notebooklm-py's own folder (~/.notebooklm); Lago never reads it. notebooklm_bridge.py does
the talking, inside that virtualenv.
"""

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import config_store

PACKAGE = "notebooklm-py[browser]==0.8.5"
TOOL_DIR = config_store.SESSIONS_DIR / "tools" / "notebooklm"
VENV = TOOL_DIR / ".venv"
PYTHON = VENV / "bin" / "python"
CLI = VENV / "bin" / "notebooklm"
BRIDGE = config_store.APP_DIR / "notebooklm_bridge.py"
STATE_FILE = config_store.STATE_DIR / "notebooklm.json"
SIGN_IN = Path.home() / ".notebooklm" / "profiles" / "default" / "storage_state.json"  # notebooklm-py's
SESSION = "setup-notebooklm"
NOTEBOOK_URL = "https://notebooklm.google.com/notebook/{}"
KINDS = {"audio": "Audio overview", "quiz": "Quiz", "flashcards": "Flashcards",
         "study_guide": "Study guide", "slide_deck": "Slide deck"}
TIMEOUTS = {"check": 60, "ask": 180, "generate": 120, "sync": 1800}


class NotebookError(Exception):
    pass


def _state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(**fields):
    state = {**_state(), **fields}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    return state


def installed():
    return PYTHON.exists() and CLI.exists()


def connected():
    """Signed in the last time it was checked."""
    return installed() and bool(_state().get("connected"))


def status():
    """Installed and signed in. A sign-in newer than the last check is checked now, so
    finishing it in the terminal is enough."""
    state = _state()
    try:
        signed_in_at = SIGN_IN.stat().st_mtime
    except OSError:
        signed_in_at = None
    if installed() and signed_in_at and signed_in_at > (state.get("checked_at") or 0):
        check()
        state = _state()
    return {"installed": installed(), "connected": connected(), "checked_at": state.get("checked_at"),
            "error": state.get("error", ""), "kinds": KINDS}


def connect():
    """Open a terminal that installs notebooklm-py (the first time) and signs you in to
    Google in a browser window. Check it afterwards with check()."""
    steps = []
    if not installed():
        TOOL_DIR.mkdir(parents=True, exist_ok=True)
        steps += [f"{shlex.quote(sys.executable)} -m venv {shlex.quote(str(VENV))}",
                  f"{shlex.quote(str(VENV / 'bin' / 'pip'))} install --quiet {shlex.quote(PACKAGE)}"]
    steps += ["echo", "echo 'Sign in to Google in the browser window that opens (the first time it "
              "downloads a browser, which takes a minute).'", f"{shlex.quote(str(CLI))} login"]
    import desktop
    import runner
    import server
    server.launch(SESSION, "", " && ".join(steps), "connect NotebookLM")
    argv = desktop.terminal_argv(" ".join(runner.attach_argv(SESSION)))
    if not argv:
        return {"ok": True, "message": f"Run `{' '.join(runner.attach_argv(SESSION))}` in a terminal "
                                       "to finish signing in."}
    subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "message": "A terminal opened: sign in to Google there, then press Check."}


def _bridge(op, **request):
    if not installed():
        raise NotebookError("NotebookLM isn't set up yet: connect it on the Specialists tab.")
    try:
        proc = subprocess.run([str(PYTHON), "-I", str(BRIDGE)], input=json.dumps({"op": op, **request}),
                              capture_output=True, text=True, timeout=TIMEOUTS[op])
    except subprocess.TimeoutExpired as error:
        raise NotebookError(f"NotebookLM took too long ({op}).") from error
    try:
        reply = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as error:
        raise NotebookError((proc.stderr or "NotebookLM gave no answer.").strip()[-400:]) from error
    if reply.get("error"):
        if "auth" in reply["error"].lower() or "login" in reply["error"].lower():
            _save_state(connected=False, error=reply["error"], checked_at=time.time())
        raise NotebookError(reply["error"])
    return reply


def check():
    """Whether the saved sign-in still works (a real request to NotebookLM)."""
    try:
        _bridge("check")
    except NotebookError as error:
        _save_state(connected=False, error=str(error), checked_at=time.time())
        return status()
    _save_state(connected=True, error="", checked_at=time.time())
    return status()


def sync(meta, files, role_map):
    """Put a specialist's notes, and the official sources its role map cites, in its notebook."""
    urls = []
    for item in [*role_map.get("sources", []), *role_map.get("certifications", [])]:
        url = (item or {}).get("url") if isinstance(item, dict) else None
        if url and url.startswith("https://") and url not in urls:
            urls.append(url)
    reply = _bridge("sync", notebook_id=(meta.get("notebook") or {}).get("id"),
                    title=f"{meta['title']} · Lago specialist", files=[str(p) for p in files], urls=urls[:20])
    return {"id": reply["notebook_id"], "url": NOTEBOOK_URL.format(reply["notebook_id"]),
            "sources": reply["sources"], "failed": reply.get("failed", []), "synced_at": time.time()}


def ask(notebook_id, question):
    reply = _bridge("ask", notebook_id=notebook_id, question=question)
    cited = f"\n\nSources: {', '.join(reply['sources'])}" if reply.get("sources") else ""
    return reply["answer"] + cited


def generate(notebook_id, kind):
    if kind not in KINDS:
        raise NotebookError(f"NotebookLM makes {', '.join(KINDS)}; not {kind!r}.")
    _bridge("generate", notebook_id=notebook_id, kind=kind)
    return (f"{KINDS[kind]} is being made in NotebookLM: it appears in the notebook's Studio panel "
            "in a few minutes.")
